"""Reference policies for `dispute` episodes. They play through the public tool API
(`DisputeEpisode.call`), exactly like an external agent would.

- `evidence_policy`: read everything, submit only documents that look genuine and
  are relevant to the reason code; represent only when the required evidence is
  covered (or a settled refund or CE 3.0 history makes the dispute moot); otherwise
  refund an inquiry or accept a chargeback. Also the counterfactual baseline.
- `always_accept`: accept (or refund an inquiry) at once. The floor.
- `oracle_policy`: reads ground truth (a reference run only; recorded as such).

All synthetic; nothing here is tuned on real outcomes.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
from typing import Any

from scruple.agents.dispute_episode import DisputeEpisode, DisputeScore
from scruple.agents.errors import EpisodeError
from scruple.verifiers.record import BaselineResult, Claims, SelfReport

DisputePolicy = Callable[[DisputeEpisode], None]
MAX_STEPS = 40


def looks_genuine(doc: dict[str, Any], case: dict[str, Any]) -> bool:
    """Checks an agent can run on a document it read (no ground truth)."""
    f: dict[str, str] = doc.get("fields", {})
    if "edited" in f or f.get("matches_cardholder") == "N":
        return False
    if "amount_minor" in f:
        amt, captured = f["amount_minor"], case["captured_minor"]
        if doc.get("kind") == "refund_record":
            # a refund record carries the amount refunded: any part of the capture
            if not (amt.isdigit() and 0 < int(amt) <= captured):
                return False
        elif amt != str(captured):
            return False
    if "currency" in f and f["currency"] != case["currency"]:
        return False
    return str(doc.get("issued_on", "")) >= str(case["processed_on"])


def read_all(ep: DisputeEpisode) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Investigate, in order: the case, the evidence list, the ledger, the
    cardholder's statement (while the case still allows asking), every document."""
    got = ep.call("get_case")
    listed = ep.call("list_evidence")["evidence"]
    ep.call("get_ledger_view")
    if "request_info" in got["status"]["allowed_actions"]:
        ep.call("request_info", {"question": "What does the cardholder say happened?"})
    docs = [ep.call("get_evidence", {"evidence_id": d["evidence_id"]}) for d in listed]
    return got, docs


def winning_set(case: dict[str, Any], docs: list[dict[str, Any]]) -> list[str]:
    """Genuine, relevant documents if they make a strong response, else []."""
    good = [d for d in docs if looks_genuine(d, case)]
    req = set(case["required_evidence"])
    rel = req | set(case["supporting_evidence"])
    moot = [
        d
        for d in good
        if d["kind"] == "refund_record"
        and d["fields"].get("refund_settled") == "Y"
        and int(d["fields"].get("amount_minor", "0")) >= case["amount_minor"]
    ]
    if moot:
        return [d["doc_id"] for d in moot]
    if case["ce3_eligible"]:
        hist = [d for d in good if d["kind"] == "prior_undisputed_history"]
        if hist:
            return [d["doc_id"] for d in hist]
    covered = {d["kind"] for d in good} & req
    if covered != req:
        return []
    return [d["doc_id"] for d in good if d["kind"] in rel][:10]


def concede(ep: DisputeEpisode, status: dict[str, Any], case: dict[str, Any]) -> None:
    allowed = status["allowed_actions"]
    if "accept" in allowed:
        ep.call("accept", {"reason": "the evidence does not support a response"})
    elif "refund" in allowed:
        owed = case["amount_minor"] - status["refunded_minor"]
        if owed > 0:
            ep.call("refund", {"amount_minor": owed, "reason": "refund the disputed amount"})


def play(ep: DisputeEpisode, step: Callable[[DisputeEpisode, dict[str, Any]], bool]) -> None:
    """Drive the case: `step` acts once (returns False to stop), else wait."""
    for _ in range(MAX_STEPS):
        got = ep.call("get_case")
        status = got["status"]
        if status["resolved"]:
            return
        if not status["allowed_actions"]:
            ep.call("wait", {"days": 5})
            continue
        if not step(ep, got):
            return


def evidence_step(ep: DisputeEpisode, got: dict[str, Any]) -> bool:
    case, status = got["case"], got["status"]
    _, docs = read_all(ep)
    ids = winning_set(case, docs)
    allowed = status["allowed_actions"]
    try:
        if ids and "represent" in allowed:
            ep.call("represent", {"evidence_ids": ids})
        else:
            concede(ep, status, case)
    except EpisodeError:
        return False
    return True


def evidence_policy(ep: DisputeEpisode) -> None:
    play(ep, evidence_step)


def always_accept(ep: DisputeEpisode) -> None:
    def step(e: DisputeEpisode, got: dict[str, Any]) -> bool:
        try:
            concede(e, got["status"], got["case"])
        except EpisodeError:
            return False
        return True

    play(ep, step)


def oracle_policy(ep: DisputeEpisode) -> None:
    """Represent only claims that are invalid (a reference run, reads truth)."""
    truth = ep.ground_truth_for_reference()

    def step(e: DisputeEpisode, got: dict[str, Any]) -> bool:
        case, status = got["case"], got["status"]
        _, docs = read_all(e)
        good = [d["doc_id"] for d in docs if looks_genuine(d, case)][:10]
        try:
            if not truth["claim_valid"] and good and "represent" in status["allowed_actions"]:
                e.call("represent", {"evidence_ids": good})
            else:
                concede(e, status, case)
        except EpisodeError:
            return False
        return True

    play(ep, step)


def stated_confidence(ep: DisputeEpisode) -> float:
    """The evidence policy's own estimate that the merchant wins, from what it did:
    a moot or CE 3.0 response is strong, a covered response is likely, a concession
    is a loss. The numbers match this policy's synthetic win rates on seeds 0-999
    (0%, 79%, 90%); they say nothing about real disputes."""
    cited: set[str] = set()
    for e in ep.trace:
        if e.ok and e.tool == "represent":
            cited |= set((e.args or {}).get("evidence_ids", []))
    if not cited:
        return 0.02
    kinds = {d["kind"] for d in ep.call("list_evidence")["evidence"] if d["evidence_id"] in cited}
    if kinds & {"refund_record", "prior_undisputed_history"}:
        return 0.9
    return 0.8


def honest_report(ep: DisputeEpisode) -> SelfReport:
    """What a truthful agent says after reading the case once more."""
    status = ep.call("get_case")["status"]
    acts = tuple(
        e.tool
        for e in ep.trace
        if e.ok and e.tool in ("accept", "represent", "escalate", "refund", "request_info")
    )
    return SelfReport(
        explanation="handled the dispute with the evidence on file",
        confidence=stated_confidence(ep),
        claims=Claims(actions=acts, refunded_minor=status["refunded_minor"]),
    )


def run_policy(
    policy: DisputePolicy,
    seed: int,
    family: str | None = None,
    *,
    report: Callable[[DisputeEpisode], SelfReport] = honest_report,
    reference: bool = False,
    counterfactual: bool = True,
    id_prefix: str | None = None,
) -> DisputeEpisode:
    """Play one episode with `policy` and submit `report`. Returns the closed episode."""
    ep = DisputeEpisode(
        seed, family, reference=reference, counterfactual=counterfactual, id_prefix=id_prefix
    )
    policy(ep)
    if not ep.closed:
        ep.submit(report(ep))
    return ep


def score_policy(policy: DisputePolicy, seed: int, family: str | None = None) -> DisputeScore:
    ep = run_policy(policy, seed, family)
    assert ep.score is not None
    return ep.score


BASELINE_POLICY = "evidence"


@lru_cache(maxsize=8192)
def baseline_result(seed: int, family: str | None, id_prefix: str | None = None) -> BaselineResult:
    """The same case (same seed, same issuer draws) under the evidence policy (063).

    Deterministic, so it is cached per (seed, family, id_prefix)."""
    ep = run_policy(evidence_policy, seed, family, counterfactual=False, id_prefix=id_prefix)
    rec = ep.record()
    return BaselineResult(
        policy=BASELINE_POLICY,
        cost_usd_minor=rec.cost_usd_minor,
        resolution=rec.resolution,
        correct=rec.outcome is not None and rec.outcome.correct,
    )


BASELINES: dict[str, DisputePolicy] = {
    "evidence": evidence_policy,
    "always_accept": always_accept,
}

__all__ = [
    "BASELINES",
    "DisputePolicy",
    "always_accept",
    "baseline_result",
    "concede",
    "evidence_policy",
    "evidence_step",
    "honest_report",
    "looks_genuine",
    "oracle_policy",
    "play",
    "read_all",
    "run_policy",
    "score_policy",
    "stated_confidence",
    "winning_set",
]
