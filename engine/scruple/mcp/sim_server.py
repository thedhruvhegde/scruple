"""MCP server exposing the dispute simulator to any MCP client (stdio by default).

An episode is driven entirely through tools: `start_episode(seed, case_type)` returns
an `episode_id` and the observation; the read, info and action tools from
`scruple.agents.tools` take that `episode_id`; `submit_decision` closes the episode
and returns the verifier scorecard. Episodes live in server memory for the life of
the process. Everything is synthetic and deterministic given the seed.

`start_dispute_episode(seed, family)` starts a `dispute` episode on the rich dispute
engine instead (lifecycle stages, deadlines, evidence documents, issuer replies). The
shared read tools work for both kinds; `accept`, `wait`, `upload_evidence`,
`refund(amount_minor=...)` and `submit_dispute` are for dispute episodes.

`start_ach_episode`, `start_recon_episode`, `start_aml_episode`,
`start_payables_episode` and `start_scenario_episode` start the other episode kinds
the API and CLI serve. ACH, recon, AML and payables episodes have their own tool
sets, exposed with an `ach_`, `recon_`, `aml_` or `payables_` prefix (`ach_get_case`,
`recon_match`, `aml_file_sar`, `payables_pay`, ...) and refused on
episodes of another kind; a scenario episode is a dispute episode on a
real CFPB-grounded scenario and plays the dispute tools above.

Run it with `scruple mcp sim` (or `uv run scruple mcp sim`).
"""

from __future__ import annotations

import inspect
import threading
from collections.abc import Callable, Collection
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field
from pydantic_core import to_jsonable_python

from scruple.agents.ach_episode import ACH_TASK, AchEpisode
from scruple.agents.ach_tools import ACH_TOOLS
from scruple.agents.aml_episode import AML_TASK, AmlEpisode
from scruple.agents.aml_tools import AML_TOOLS
from scruple.agents.dispute_episode import DISPUTE_TASK, DisputeEpisode
from scruple.agents.dispute_tools import DISPUTE_TOOL_BY_NAME, DisputeToolSpec
from scruple.agents.env import FAMILIES, ActionKind, EvidenceKind, SimEnvironment
from scruple.agents.episode import Episode, EpisodeError
from scruple.agents.models import TASK
from scruple.agents.payables_episode import PAYABLES_TASK, TOKENS, PayablesEpisode
from scruple.agents.payables_tools import PAYABLES_TOOLS
from scruple.agents.recon_episode import FORMATS, RECON_TASK, ReconEpisode
from scruple.agents.recon_tools import RECON_TOOLS
from scruple.agents.store import new_episode
from scruple.agents.tools import TOOL_BY_NAME
from scruple.sim.ach.models import AchFamily
from scruple.sim.aml.models import AmlFamily
from scruple.sim.recon.models import BreakKind
from scruple.version import package_version

MAX_EPISODES = 10_000

EpisodeId = Annotated[str, Field(description="The id returned by start_episode.")]
Reason = Annotated[str, Field(description="Why, in a sentence.", max_length=1_000)]
Seed = Annotated[int, Field(description="Seed; same seed, same case.")]
WorkflowCaseType = Annotated[
    str | None, Field(description="Optional case type from list_workflow_case_types.")
]


AnyEpisode = Episode | DisputeEpisode | AchEpisode | ReconEpisode | AmlEpisode | PayablesEpisode
# Episode kinds the unprefixed tools serve (a scenario episode is a dispute episode).
LEGACY_KINDS = frozenset({"simple", "dispute"})
WORKFLOW_PREFIX = {"ach": "ach_", "recon": "recon_", "aml": "aml_", "payables": "payables_"}


def episode_kind(ep: Any) -> str:
    return str(getattr(ep, "kind", "simple"))


def wrong_kind_message(episode_id: str, kind: str) -> str:
    prefix = WORKFLOW_PREFIX.get(kind)
    hint = f"; use the {prefix}* tools" if prefix else ""
    return f"episode {episode_id!r} is a {kind} episode{hint}"


class EpisodeTable:
    """In-memory episodes for one server process.

    The MCP SDK runs synchronous tools in worker threads, so every call takes a lock:
    episodes see their calls one at a time, in arrival order.
    """

    def __init__(self) -> None:
        self.episodes: dict[str, AnyEpisode] = {}
        self._starts: dict[tuple[int, str | None], int] = {}
        self._dispute_starts: dict[tuple[int, str | None], int] = {}
        self._kind_starts: dict[tuple[str, int, str | None], int] = {}
        self._lock = threading.Lock()

    def start(self, seed: int, case_type: str | None) -> Episode:
        with self._lock:
            return self._start(seed, case_type)

    def _start(self, seed: int, case_type: str | None) -> Episode:
        if len(self.episodes) >= MAX_EPISODES:
            raise ToolError(f"episode limit reached ({MAX_EPISODES}); restart the server")
        key = (seed, case_type)
        nonce = self._starts.get(key, 0)
        try:
            ep = Episode(seed, case_type, nonce=nonce)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        self._starts[key] = nonce + 1
        self.episodes[ep.episode_id] = ep
        return ep

    def start_dispute(self, seed: int, family: str | None) -> DisputeEpisode:
        with self._lock:
            if len(self.episodes) >= MAX_EPISODES:
                raise ToolError(f"episode limit reached ({MAX_EPISODES}); restart the server")
            key = (seed, family)
            nonce = self._dispute_starts.get(key, 0)
            try:
                ep = DisputeEpisode(seed, family, nonce=nonce)
            except ValueError as exc:
                raise ToolError(str(exc)) from exc
            self._dispute_starts[key] = nonce + 1
            self.episodes[ep.episode_id] = ep
            return ep

    def start_kind(self, kind: str, seed: int, case_type: str | None) -> AnyEpisode:
        """Start an `ach`, `recon` or `scenario` episode (`case_type` is the scenario
        id for `scenario`). Same (kind, seed, case type) again: same case, new id."""
        with self._lock:
            if len(self.episodes) >= MAX_EPISODES:
                raise ToolError(f"episode limit reached ({MAX_EPISODES}); restart the server")
            key = (kind, seed, case_type)
            nonce = self._kind_starts.get(key, 0)
            try:
                ep = new_episode(kind, seed, case_type, nonce)
            except (ValueError, KeyError) as exc:
                raise ToolError(str(exc).strip("'\"")) from exc
            self._kind_starts[key] = nonce + 1
            self.episodes[ep.episode_id] = ep
            return ep

    def call(
        self,
        episode_id: str,
        tool: str,
        args: dict[str, Any],
        kinds: Collection[str] = LEGACY_KINDS,
    ) -> dict[str, Any]:
        """Run `tool` on the episode. `kinds` are the episode kinds the calling MCP tool
        serves; another kind is refused, so `accept` never reaches an ach episode."""
        with self._lock:
            return self._call(episode_id, tool, args, kinds)

    def _call(
        self, episode_id: str, tool: str, args: dict[str, Any], kinds: Collection[str]
    ) -> dict[str, Any]:
        ep = self.episodes.get(episode_id)
        if ep is None:
            raise ToolError(f"unknown episode {episode_id!r}; call start_episode first")
        kind = episode_kind(ep)
        if kind not in kinds:
            raise ToolError(wrong_kind_message(episode_id, kind))
        try:
            return ep.call(tool, args)
        except EpisodeError as exc:
            raise ToolError(str(exc)) from exc


def _desc(name: str) -> str:
    return TOOL_BY_NAME[name].description


def _workflow_tool(
    t: EpisodeTable, kind: str, spec: DisputeToolSpec
) -> Callable[..., dict[str, Any]]:
    """An MCP tool function for one ach/recon tool, with one typed parameter per field
    of the tool's argument model (plus `episode_id`), so clients see a real schema.
    The episode validates the arguments again with the full model (constraints and
    all) and counts a refusal as a tool error, as on every other surface."""

    fields = spec.args.model_fields
    defaults = {
        n: to_jsonable_python(f.get_default()) for n, f in fields.items() if not f.is_required()
    }

    def fn(episode_id: str, **kwargs: Any) -> dict[str, Any]:
        # Arguments left at their default are dropped, so the episode logs the same
        # call as from the API or CLI and the trace head matches across surfaces.
        args = {
            n: v
            for n, v in to_jsonable_python(kwargs).items()
            if n not in defaults or v != defaults[n]
        }
        return t.call(episode_id, spec.name, args, kinds={kind})

    params = [
        inspect.Parameter(
            "episode_id",
            inspect.Parameter.KEYWORD_ONLY,
            annotation=Annotated[str, Field(description=f"The id from start_{kind}_episode.")],
        )
    ]
    for name, f in fields.items():
        params.append(
            inspect.Parameter(
                name,
                inspect.Parameter.KEYWORD_ONLY,
                default=inspect.Parameter.empty if f.is_required() else f.get_default(),
                annotation=Annotated[f.annotation, Field(description=f.description)],
            )
        )
    fn.__signature__ = inspect.Signature(params, return_annotation=dict[str, Any])  # type: ignore[attr-defined]
    fn.__name__ = WORKFLOW_PREFIX[kind] + spec.name
    return fn


def build_sim_server(table: EpisodeTable | None = None) -> MCPServer:
    t = table or EpisodeTable()
    server = MCPServer(
        "scruple-sim",
        instructions=TASK
        + " Start with start_episode, pass the episode_id to every other tool, and finish "
        "with submit_decision.",
        version=package_version(),
    )

    @server.tool(description="List the synthetic case types the simulator can generate.")
    def list_case_types() -> dict[str, Any]:
        return {"case_types": list(SimEnvironment().case_types()), "synthetic": True}

    @server.tool(
        description="Start a new episode: one simulated dispute. Returns the episode_id and "
        "the observation (whitelisted case features only)."
    )
    def start_episode(
        seed: Annotated[int, Field(description="Seed; same seed, same case.")],
        case_type: Annotated[
            str | None, Field(description="Optional case type from list_case_types.")
        ] = None,
    ) -> dict[str, Any]:
        ep = t.start(seed, case_type)
        return {"episode_id": ep.episode_id, "observation": ep.observe().model_dump(mode="json")}

    @server.tool(description=_desc("get_case"))
    def get_case(episode_id: EpisodeId) -> dict[str, Any]:
        return t.call(episode_id, "get_case", {})

    @server.tool(description=_desc("list_evidence"))
    def list_evidence(episode_id: EpisodeId) -> dict[str, Any]:
        return t.call(episode_id, "list_evidence", {})

    @server.tool(description=_desc("get_evidence"))
    def get_evidence(
        episode_id: EpisodeId,
        evidence_id: Annotated[str, Field(description="An id from list_evidence, e.g. ev-01.")],
    ) -> dict[str, Any]:
        return t.call(episode_id, "get_evidence", {"evidence_id": evidence_id})

    @server.tool(description=_desc("get_ledger_view"))
    def get_ledger_view(episode_id: EpisodeId) -> dict[str, Any]:
        return t.call(episode_id, "get_ledger_view", {})

    @server.tool(description=_desc("request_info"))
    def request_info(
        episode_id: EpisodeId,
        question: Annotated[str, Field(description="What you need to know.")],
    ) -> dict[str, Any]:
        return t.call(episode_id, "request_info", {"question": question})

    @server.tool(description=_desc("accept_liability"))
    def accept_liability(episode_id: EpisodeId, reason: Reason = "") -> dict[str, Any]:
        return t.call(episode_id, "accept_liability", {"reason": reason})

    @server.tool(description=_desc("refund"))
    def refund(
        episode_id: EpisodeId,
        reason: Reason = "",
        amount_minor: Annotated[
            int | None, Field(description="Dispute episodes: amount to refund, minor units.")
        ] = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {"reason": reason}
        if amount_minor is not None:
            args["amount_minor"] = amount_minor
        return t.call(episode_id, "refund", args)

    @server.tool(description=_desc("represent"))
    def represent(
        episode_id: EpisodeId,
        evidence_ids: Annotated[list[str], Field(description="Evidence ids to submit.")],
        narrative: Annotated[str, Field(description="The merchant's rebuttal.")] = "",
    ) -> dict[str, Any]:
        return t.call(
            episode_id, "represent", {"evidence_ids": evidence_ids, "narrative": narrative}
        )

    @server.tool(description=_desc("escalate"))
    def escalate(episode_id: EpisodeId, reason: Reason = "") -> dict[str, Any]:
        return t.call(episode_id, "escalate", {"reason": reason})

    @server.tool(description=_desc("submit_decision"))
    def submit_decision(
        episode_id: EpisodeId,
        explanation: Annotated[str, Field(description="What you did and why.")],
        action: Annotated[
            ActionKind | None,
            Field(description="The action, if you have not called an action tool."),
        ] = None,
        evidence_ids: Annotated[
            list[str] | None, Field(description="Evidence for represent, if acting here.")
        ] = None,
        claimed_cost_usd_minor: Annotated[
            int | None, Field(description="What you believe this cost, in cents.")
        ] = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {"explanation": explanation}
        if action is not None:
            args["action"] = action.value
        if evidence_ids:
            args["evidence_ids"] = evidence_ids
        if claimed_cost_usd_minor is not None:
            args["claimed_cost_usd_minor"] = claimed_cost_usd_minor
        return t.call(episode_id, "submit_decision", args)

    # --- dispute episodes (additive) --------------------------------------------------
    @server.tool(description="List the synthetic case families for dispute episodes.")
    def list_case_families() -> dict[str, Any]:
        return {"families": list(FAMILIES), "synthetic": True}

    @server.tool(
        description="Start a dispute episode on the rich dispute engine (lifecycle, "
        "deadlines, evidence documents, issuer replies). " + DISPUTE_TASK
    )
    def start_dispute_episode(
        seed: Annotated[int, Field(description="Seed; same seed, same case.")],
        family: Annotated[
            str | None, Field(description="Optional family from list_case_families.")
        ] = None,
    ) -> dict[str, Any]:
        ep = t.start_dispute(seed, family)
        return {"episode_id": ep.episode_id, "observation": ep.observe().model_dump(mode="json")}

    @server.tool(description=DISPUTE_TOOL_BY_NAME["accept"].description)
    def accept(episode_id: EpisodeId, reason: Reason = "") -> dict[str, Any]:
        return t.call(episode_id, "accept", {"reason": reason})

    @server.tool(description=DISPUTE_TOOL_BY_NAME["wait"].description)
    def wait(
        episode_id: EpisodeId,
        days: Annotated[int, Field(description="Simulated days to let pass (1-120).")],
    ) -> dict[str, Any]:
        return t.call(episode_id, "wait", {"days": days})

    @server.tool(description=DISPUTE_TOOL_BY_NAME["upload_evidence"].description)
    def upload_evidence(
        episode_id: EpisodeId,
        kind: Annotated[EvidenceKind, Field(description="Evidence kind.")],
        issued_on: Annotated[str, Field(description="Date on the document, YYYY-MM-DD.")],
        fields: Annotated[dict[str, str] | None, Field(description="Document fields.")] = None,
    ) -> dict[str, Any]:
        return t.call(
            episode_id,
            "upload_evidence",
            {"kind": kind.value, "issued_on": issued_on, "fields": fields or {}},
        )

    @server.tool(description=DISPUTE_TOOL_BY_NAME["submit_decision"].description)
    def submit_dispute(
        episode_id: EpisodeId,
        explanation: Annotated[str, Field(description="What you did and why.")],
        confidence: Annotated[
            float | None, Field(description="Your probability that the merchant wins.")
        ] = None,
        claims: Annotated[
            dict[str, Any] | None,
            Field(description="Structured claims: outcome, refunded_minor, actions, ..."),
        ] = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {"explanation": explanation}
        if confidence is not None:
            args["confidence"] = confidence
        if claims:
            args["claims"] = claims
        return t.call(episode_id, "submit_decision", args)

    # --- ach, recon and real-scenario episodes (additive) ------------------------------
    @server.tool(
        description="List the case types for ach and recon episodes and the real "
        "scenario ids for scenario episodes."
    )
    def list_workflow_case_types() -> dict[str, Any]:
        from scruple.realdata.scenarios import load_real_scenarios

        return {
            "ach": {
                "families": [f.value for f in AchFamily],
                "format": "family, family@difficulty or @difficulty (difficulty in [0, 1])",
            },
            "recon": {
                "break_kinds": [k.value for k in BreakKind],
                "formats": list(FORMATS),
                "format": "kinds and a file format joined by +, then optionally @difficulty",
            },
            "scenario": {"scenario_ids": [rs.id for rs in load_real_scenarios()]},
            "aml": {
                "families": [f.value for f in AmlFamily],
                "format": "family, family@difficulty or @difficulty (difficulty in [0, 1])",
                "data": "synthetic transaction graphs",
            },
            "payables": {
                "tokens": list(TOKENS),
                "format": "invoice kinds joined by +, then optionally @difficulty",
                "data": "synthetic invoices, POs, receipts and vendors",
            },
            "data": "ach and recon cases are synthetic; scenario cases are built from "
            "public CFPB complaints (docs/data/CFPB.md)",
        }

    @server.tool(
        description="Start an ach episode: one returned ACH debit to work through. " + ACH_TASK
    )
    def start_ach_episode(seed: Seed, case_type: WorkflowCaseType = None) -> dict[str, Any]:
        ep = t.start_kind("ach", seed, case_type)
        return {"episode_id": ep.episode_id, "observation": ep.observe().model_dump(mode="json")}

    @server.tool(
        description="Start a recon episode: reconcile one bank statement against the cash "
        "book. " + RECON_TASK
    )
    def start_recon_episode(seed: Seed, case_type: WorkflowCaseType = None) -> dict[str, Any]:
        ep = t.start_kind("recon", seed, case_type)
        return {"episode_id": ep.episode_id, "observation": ep.observe().model_dump(mode="json")}

    @server.tool(
        description="Start a scenario episode: a dispute episode on a real scenario "
        "grounded in public CFPB complaints. Play it with the dispute tools (accept, wait, "
        "upload_evidence, refund, submit_dispute and the shared read tools)."
    )
    def start_scenario_episode(
        scenario_id: Annotated[str, Field(description="An id from list_workflow_case_types.")],
        seed: Annotated[int, Field(description="Enters the episode id only.")] = 0,
    ) -> dict[str, Any]:
        ep = t.start_kind("scenario", seed, scenario_id)
        return {"episode_id": ep.episode_id, "observation": ep.observe().model_dump(mode="json")}

    @server.tool(
        description="Start an aml episode: triage one transaction-monitoring alert on a "
        "synthetic transaction graph. " + AML_TASK
    )
    def start_aml_episode(seed: Seed, case_type: WorkflowCaseType = None) -> dict[str, Any]:
        ep = t.start_kind("aml", seed, case_type)
        return {"episode_id": ep.episode_id, "observation": ep.observe().model_dump(mode="json")}

    @server.tool(
        description="Start a payables episode: work one pay run of vendor invoices. "
        + PAYABLES_TASK
    )
    def start_payables_episode(seed: Seed, case_type: WorkflowCaseType = None) -> dict[str, Any]:
        ep = t.start_kind("payables", seed, case_type)
        return {"episode_id": ep.episode_id, "observation": ep.observe().model_dump(mode="json")}

    workflows = (
        ("ach", ACH_TOOLS),
        ("recon", RECON_TOOLS),
        ("aml", AML_TOOLS),
        ("payables", PAYABLES_TOOLS),
    )
    for kind, specs in workflows:
        for spec in specs:
            fn = _workflow_tool(t, kind, spec)
            server.add_tool(fn, name=fn.__name__, description=spec.description)

    return server


def run_stdio() -> None:  # pragma: no cover - blocks on stdin
    build_sim_server().run("stdio")
