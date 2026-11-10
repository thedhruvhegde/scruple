"""The graded real-situation task: grounded scenarios through the engine and the tools.

Each real scenario (`scruple.realdata.scenarios`) is played four ways:

- `answer_key`: the scenario's own expected step, straight into the engine
  (`scruple.sim.scenario.run_scenario`). It must pass; it checks the scenario, not a policy.
- every reference policy of `scruple.agents.dispute_baselines` (`evidence`,
  `always_accept`, `oracle`) and one naive strawman (`represent_all`: represent with
  every document that looks genuine, else concede), each driving a `ScenarioEpisode`
  through the public dispute tools (`episode.call`), exactly as an external agent.

A play is *right* when the case ends correctly against ground truth
(`CaseOutcome.correct`: the cardholder ends with exactly what is owed), with the
scenario's expected resolution (accepted, won, partial or lost), and with no more
dispute fees than the answer key. All three matter: contesting a valid claim and
conceding at pre-arbitration still ends "accepted" with the cardholder paid, but with
the fees of two more stages.

The issuer is `ScriptedIssuer` (rules by ground truth unless the scenario scripts a
stage), as in `run_scenario`. Ground truth here is the scenario's case family, which
for a real scenario is the complaint narrative taken at face value: an ASSUMPTION
recorded in every file. So these numbers say whether a policy does the right thing
in a real situation *as the narrative tells it*; they are not real dispute outcomes.

LLM agents: `scruple realdata scenarios prompt` writes one prompt per scenario. An
agent plays it through the episode CLI (`scruple episode start --kind scenario
--case-type <scenario id> --seed 0`, then `call` and `submit`), and
`scruple realdata scenarios grade` grades the closed episodes from the store.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from datetime import timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict

from scruple.agents.dispute_baselines import (
    DisputePolicy,
    always_accept,
    concede,
    evidence_policy,
    honest_report,
    looks_genuine,
    oracle_policy,
    play,
    read_all,
)
from scruple.agents.dispute_episode import DISPUTE_TASK, DisputeEpisode, DisputeObservation
from scruple.agents.dispute_tools import DISPUTE_TOOL_NAMES
from scruple.agents.env import DisputeSession
from scruple.agents.episode import make_episode_id
from scruple.agents.errors import EpisodeError
from scruple.realdata.scenarios import RealScenario, find_real_scenario, load_real_scenarios
from scruple.sim.deadlines import ManualClock
from scruple.sim.disputes import DisputeEngine
from scruple.sim.merchants import MerchantProfile, RiskTier
from scruple.sim.scenario import ScriptedIssuer, Step, _action, run_scenario
from scruple.verifiers.trace import Trace

TASK_ID = "real-situations"
SCENARIO_KIND = "scenario"


# --- an episode on a scenario case -----------------------------------------------------
class ScenarioSession(DisputeSession):
    """A `DisputeSession` on one scenario's case, with the scenario's scripted issuer.

    The scenario's `history` steps are played before the agent gets the case."""

    def __init__(self, rs: RealScenario) -> None:
        sc = rs.scenario
        case = sc.build()
        self.seed = 0
        self.family = None
        self.merchant = MerchantProfile(
            merchant_id=case.merchant_id, mcc=case.mcc, tier=RiskTier.LOW, history=()
        )
        self.clock = ManualClock()
        self.clock.set_date(case.opened_on)
        self.engine = DisputeEngine([case], self.clock, issuer=ScriptedIssuer(sc.issuer))
        self.case_id = case.case_id
        self.original_documents = case.documents
        self._uploads = 0
        _, labels, _ = sc.documents()
        for step in sc.history:
            self._play(step, labels)
        self.engine.sync()

    def _play(self, step: Step, labels: dict[str, str]) -> None:
        case = self.engine.case(self.case_id)
        self.clock.set_date(case.opened_on + timedelta(days=step.day))
        self.engine.sync()
        if step.action == "refund" and step.amount_minor is None:
            step = step.model_copy(update={"amount_minor": case.amount_minor})
        docs = self.engine.vault.for_case(self.case_id)
        ids = tuple(
            labels[r] if r in labels else next(d.doc_id for d in docs if d.kind.value == r)
            for r in step.evidence
        )
        action = _action(step, ids)
        if action is not None:
            res = self.engine.act(self.case_id, action)
            if not res.ok:  # pragma: no cover - the answer key checks every history
                raise ValueError(f"history step refused: {res.error} {res.detail}")


def scenario_task(rs: RealScenario) -> str:
    """What the agent is told: the dispute task plus the cardholder's side, as the
    CFPB complaint states it (facts only; never the family, assumptions or answer)."""
    g = rs.grounding
    facts = "; ".join(g.facts)
    return (
        f"{DISPUTE_TASK} This case is grounded in public CFPB complaint {g.complaint} "
        f"({g.product}; {g.issue}). What the cardholder says happened: {facts}. "
        "Amounts are real where the complaint states them; the network, dates and the "
        "merchant's documents are simulated."
    )


class ScenarioEpisode(DisputeEpisode):
    """A `dispute` episode on a real scenario. `family` is the scenario id; the seed
    only enters the episode id. No counterfactual baseline (it is seed-based)."""

    kind = "dispute"

    def __init__(
        self,
        seed: int = 0,
        family: str | None = None,
        *,
        nonce: int = 0,
        reference: bool = False,
        counterfactual: bool = False,
        id_prefix: str | None = None,
    ) -> None:
        if family is None:
            raise ValueError("a scenario episode needs a scenario id (--case-type)")
        rs = find_real_scenario(family)
        super().__init__(seed, None, nonce=nonce, reference=reference, counterfactual=False)
        self.family = family
        self.real = rs
        self.episode_id = make_episode_id(seed, f"{SCENARIO_KIND}:{family}", nonce)
        self.session = ScenarioSession(rs)
        self.trace = Trace()
        self._transitions = 0
        self._entries = set()
        self._docs = set()
        self._listed = set()
        self._incidents = 0
        self._state = None
        self._sync_trace()

    def observe(self) -> DisputeObservation:
        return DisputeObservation(
            episode_id=self.episode_id,
            case=self.session.case_view(),
            status=self.session.status(),
            tools=DISPUTE_TOOL_NAMES,
            task=scenario_task(self.real),
        )


# --- policies ------------------------------------------------------------------------------
def represent_all(ep: DisputeEpisode) -> None:
    """Strawman: represent with every genuine-looking document whenever allowed."""

    def step(e: DisputeEpisode, got: dict[str, Any]) -> bool:
        case, status = got["case"], got["status"]
        _, docs = read_all(e)
        ids = [d["doc_id"] for d in docs if looks_genuine(d, case)][:10]
        try:
            if ids and "represent" in status["allowed_actions"]:
                e.call("represent", {"evidence_ids": ids})
            else:
                concede(e, status, case)
        except EpisodeError:
            return False
        return True

    play(ep, step)


POLICIES: dict[str, DisputePolicy] = {
    "evidence": evidence_policy,
    "always_accept": always_accept,
    "oracle": oracle_policy,
    "represent_all": represent_all,
}
REFERENCE_RUNS = frozenset({"oracle"})  # reads ground truth (declared)
ANSWER_KEY = "answer_key"


class Play(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scenario_id: str
    complaint: str
    player: str
    expected_action: str
    expected_resolution: str
    first_action: str | None
    resolution: str
    correct: bool
    right: bool
    action_matches: bool
    cardholder_net_minor: int
    liability_minor: int
    fees_usd_minor: int
    tool_calls: int = 0


def _concede_class(action: str | None) -> str | None:
    return "concede" if action in ("accept", "refund") else action


_KEY_FEES: dict[tuple[str, str], int] = {}


def answer_key_fees(rs: RealScenario) -> int:
    """Dispute fees on the answer key's path (the cost a right play must not exceed)."""
    key = (rs.path, rs.id)
    if key not in _KEY_FEES:
        _KEY_FEES[key] = play_answer_key(rs).fees_usd_minor
    return _KEY_FEES[key]


def play_policy(rs: RealScenario, name: str, policy: DisputePolicy) -> Play:
    ep = ScenarioEpisode(0, rs.id, reference=name in REFERENCE_RUNS)
    policy(ep)
    if not ep.closed:
        ep.submit(honest_report(ep))
    return grade_episode(rs, ep, name)


def first_action(ep: DisputeEpisode) -> str | None:
    for e in ep.trace:
        if e.ok and e.tool in ("accept", "represent", "escalate", "refund"):
            return e.tool
    return None


def grade_episode(rs: RealScenario, ep: DisputeEpisode, player: str) -> Play:
    """Grade a closed episode on a real scenario."""
    if not ep.closed:
        raise EpisodeError("the episode is still open: submit a decision first")
    out = ep.record().outcome
    assert out is not None
    want = rs.scenario.expected
    assert want.resolution is not None
    act = first_action(ep)
    return Play(
        scenario_id=rs.id,
        complaint=rs.grounding.complaint,
        player=player,
        expected_action=want.action,
        expected_resolution=want.resolution.value,
        first_action=act,
        resolution=out.resolution.value,
        correct=out.correct,
        right=out.correct
        and out.resolution is want.resolution
        and out.fees_usd_minor <= answer_key_fees(rs),
        action_matches=_concede_class(act) == _concede_class(want.action),
        cardholder_net_minor=out.cardholder_net_minor,
        liability_minor=out.liability_minor,
        fees_usd_minor=out.fees_usd_minor,
        tool_calls=ep.tool_calls,
    )


def play_answer_key(rs: RealScenario) -> Play:
    run = run_scenario(rs.scenario)
    out = run.outcome
    assert out is not None
    want = rs.scenario.expected
    assert want.resolution is not None
    return Play(
        scenario_id=rs.id,
        complaint=rs.grounding.complaint,
        player=ANSWER_KEY,
        expected_action=want.action,
        expected_resolution=want.resolution.value,
        first_action=want.action,
        resolution=out.resolution.value,
        correct=out.correct,
        right=run.passed,
        action_matches=True,
        cardholder_net_minor=out.cardholder_net_minor,
        liability_minor=out.liability_minor,
        fees_usd_minor=out.fees_usd_minor,
    )


def run_all(
    scenarios: Sequence[RealScenario] | None = None,
    policies: dict[str, DisputePolicy] | None = None,
) -> list[Play]:
    """Every scenario under the answer key and every policy. Deterministic."""
    scs = list(scenarios) if scenarios is not None else load_real_scenarios()
    pols = POLICIES if policies is None else policies
    plays: list[Play] = []
    for rs in scs:
        plays.append(play_answer_key(rs))
        plays += [play_policy(rs, name, pol) for name, pol in pols.items()]
    return plays


def _wilson(k: int, n: int, z: float = 1.959963984540054) -> tuple[float, float]:
    from scruple.realdata.timeliness import wilson

    return wilson(k, n, z)


def summarise(plays: Iterable[Play], scenarios: Sequence[RealScenario]) -> dict[str, Any]:
    """Per-player rates (with Wilson 95% intervals), per-slice rates and the matrix."""
    rows = list(plays)
    by_id = {s.id: s for s in scenarios}
    players = list(dict.fromkeys(p.player for p in rows))
    out: dict[str, Any] = {"task": TASK_ID, "n_scenarios": len(by_id), "players": {}}
    for name in players:
        mine = [p for p in rows if p.player == name]
        k = sum(p.right for p in mine)
        lo, hi = _wilson(k, len(mine))
        slices: dict[str, dict[str, int]] = {}
        for p in mine:
            sc = by_id[p.scenario_id]
            for key in (
                f"expected:{sc.scenario.expected.action}",
                f"family:{sc.scenario.case.family.value}",
                f"regime:{sc.grounding.regime}",
                f"source:{sc.grounding.complaint.split('-')[0]}",
            ):
                s = slices.setdefault(key, {"n": 0, "right": 0})
                s["n"] += 1
                s["right"] += int(p.right)
        out["players"][name] = {
            "n": len(mine),
            "right": k,
            "right_rate": round(k / len(mine), 4) if mine else None,
            "right_ci95": [round(lo, 4), round(hi, 4)],
            "correct_outcome": sum(p.correct for p in mine),
            "action_matches": sum(p.action_matches for p in mine),
            "fees_usd_minor": sum(p.fees_usd_minor for p in mine),
            "reference_run": name in REFERENCE_RUNS,
            "slices": dict(sorted(slices.items())),
        }
    out["matrix"] = {
        sid: {p.player: p.right for p in rows if p.scenario_id == sid} for sid in by_id
    }
    out["labels"] = {
        "data": "real CFPB complaint narratives (public domain) for the cardholder's side; "
        "network, dates and merchant documents simulated",
        "truth": "the narrative taken at face value (ASSUMPTION, per scenario file)",
        "issuer": "ScriptedIssuer (rules by ground truth unless scripted)",
    }
    return out


# --- prompts and grading for LLM agents ---------------------------------------------------
SYSTEM_PROMPT = (
    "You are a dispute agent for a merchant. You work one card dispute through a "
    "command-line episode. Each command prints one JSON document. Investigate before "
    "acting, act only through the tools, and finish with submit_decision."
)


def render_prompt(rs: RealScenario, store: str = ".scruple/episodes") -> str:
    g = rs.grounding
    lines = [
        f"Real situation {rs.id} (CFPB complaint {g.complaint}, {g.product}; {g.issue}).",
        "What the cardholder says happened (paraphrased from the public complaint):",
        *[f"- {f}" for f in g.facts],
        "",
        "Start the episode and read its observation:",
        f"  scruple episode start --kind scenario --case-type {rs.id} --seed 0 --store {store}",
        "Then call tools until the case is handled:",
        f"  scruple episode call <episode_id> <tool> '<json args>' --store {store}",
        f"  tools: {', '.join(DISPUTE_TOOL_NAMES)}",
        "Finish with:",
        f'  scruple episode submit <episode_id> \'{{"explanation": "..."}}\' --store {store}',
        "Write the episode id you used next to this prompt's id in your answers file "
        '(one JSON line: {"id": "<prompt id>", "episode_id": "<episode id>"}).',
        "You are graded from the engine's ledger: did the cardholder end with exactly "
        "what is owed, through the right resolution, at the lowest cost.",
    ]
    return "\n".join(lines)


def prompt_rows(
    scenarios: Sequence[RealScenario] | None = None, store: str = ".scruple/episodes"
) -> list[dict[str, Any]]:
    scs = list(scenarios) if scenarios is not None else load_real_scenarios()
    return [
        {
            "id": rs.id,
            "task": TASK_ID,
            "complaint": rs.grounding.complaint,
            "system": SYSTEM_PROMPT,
            "prompt": render_prompt(rs, store),
            "kind": SCENARIO_KIND,
            "case_type": rs.id,
            "seed": 0,
        }
        for rs in scs
    ]


def grade_answers(
    answers: dict[str, str],
    prompted: Sequence[str],
    load_episode: Callable[[str], DisputeEpisode],
    player: str = "agent",
) -> dict[str, Any]:
    """Grade episode ids per prompted scenario. Missing, unknown, open, or
    mismatched episodes count as not right."""
    scs = {s.id: s for s in load_real_scenarios()}
    plays: list[Play] = []
    failures: dict[str, str] = {}
    for sid in prompted:
        rs = scs[sid]
        eid = answers.get(sid)
        reason = None
        if eid is None:
            reason = "no answer"
        else:
            try:
                ep = load_episode(eid)
            except (KeyError, EpisodeError, ValueError) as exc:
                reason = f"episode not loadable: {exc}"
            else:
                if not isinstance(ep, ScenarioEpisode) or ep.family != sid:
                    reason = "episode is not this scenario"
                elif not ep.closed:
                    reason = "episode still open"
                else:
                    plays.append(grade_episode(rs, ep, player))
        if reason is not None:
            failures[sid] = reason
    used = [scs[s] for s in prompted]
    card = summarise(plays, used)
    k = sum(p.right for p in plays)
    lo, hi = _wilson(k, len(prompted))
    card["agent"] = {
        "player": player,
        "prompted": len(prompted),
        "graded": len(plays),
        "right": k,
        "right_rate": round(k / len(prompted), 4) if prompted else None,
        "right_ci95": [round(lo, 4), round(hi, 4)],
        "failures": failures,
    }
    card["plays"] = [p.model_dump() for p in plays]
    return card


__all__ = [
    "ANSWER_KEY",
    "POLICIES",
    "Play",
    "ScenarioEpisode",
    "ScenarioSession",
    "grade_answers",
    "grade_episode",
    "play_answer_key",
    "play_policy",
    "prompt_rows",
    "represent_all",
    "run_all",
    "summarise",
]
