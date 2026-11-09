"""MCP server exposing the dispute simulator to any MCP client (stdio by default).

An episode is driven entirely through tools: `start_episode(seed, case_type)` returns
an `episode_id` and the observation; the read, info and action tools from
`scruple.agents.tools` take that `episode_id`; `submit_decision` closes the episode
and returns the verifier scorecard. Episodes live in server memory for the life of
the process. Everything is synthetic and deterministic given the seed.

Run it with `scruple mcp sim` (or `uv run scruple mcp sim`).
"""

from __future__ import annotations

import threading
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from scruple.agents.env import ActionKind, SimEnvironment
from scruple.agents.episode import Episode, EpisodeError
from scruple.agents.models import TASK
from scruple.agents.tools import TOOL_BY_NAME

MAX_EPISODES = 10_000

EpisodeId = Annotated[str, Field(description="The id returned by start_episode.")]
Reason = Annotated[str, Field(description="Why, in a sentence.", max_length=1_000)]


class EpisodeTable:
    """In-memory episodes for one server process.

    The MCP SDK runs synchronous tools in worker threads, so every call takes a lock:
    episodes see their calls one at a time, in arrival order.
    """

    def __init__(self) -> None:
        self.episodes: dict[str, Episode] = {}
        self._starts: dict[tuple[int, str | None], int] = {}
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

    def call(self, episode_id: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            return self._call(episode_id, tool, args)

    def _call(self, episode_id: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        ep = self.episodes.get(episode_id)
        if ep is None:
            raise ToolError(f"unknown episode {episode_id!r}; call start_episode first")
        try:
            return ep.call(tool, args)
        except EpisodeError as exc:
            raise ToolError(str(exc)) from exc


def _desc(name: str) -> str:
    return TOOL_BY_NAME[name].description


def build_sim_server(table: EpisodeTable | None = None) -> MCPServer:
    t = table or EpisodeTable()
    server = MCPServer(
        "scruple-sim",
        instructions=TASK
        + " Start with start_episode, pass the episode_id to every other tool, and finish "
        "with submit_decision.",
        version="0.1.0",
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
    def refund(episode_id: EpisodeId, reason: Reason = "") -> dict[str, Any]:
        return t.call(episode_id, "refund", {"reason": reason})

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

    return server


def run_stdio() -> None:  # pragma: no cover - blocks on stdin
    build_sim_server().run("stdio")
