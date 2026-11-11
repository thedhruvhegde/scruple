"""Scripted policies for `aml` episodes: the reference analyst and strawmen.

`reference_policy` reads only what the tools return (never the truth): the alert, the
subject's profile, every linked transfer and the profile of every counterparty, then
applies `scruple.sim.aml.indicators.assess`. It files a SAR (citing the indicators
that hold and the linked transfers, with their total) on a typology and closes a
lookalike. On every generated case it decides as the truth does (tests pin this).

Strawmen: `close_everything` and `escalate_everything` (both read first, so only the
decision differs), `trust_the_customer` (closes when a request for information comes
back with documents, files otherwise) and `decide_unread` (closes without reading).
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from datetime import date
from typing import Any

from scruple.agents.aml_episode import AmlEpisode
from scruple.agents.errors import EpisodeError
from scruple.sim.aml.indicators import assess, counterparties
from scruple.sim.aml.models import Account, AlertRule, Transfer
from scruple.verifiers.aml_record import AmlClaims, AmlReport

AmlPolicy = Callable[[AmlEpisode], None]
ReportFn = Callable[[AmlEpisode], AmlReport]


def _account(view: dict[str, Any]) -> Account:
    return Account.model_validate({k: v for k, v in view.items() if k != "synthetic"})


def review(ep: AmlEpisode) -> tuple[dict[str, Any], Account, dict[str, Account], list[Transfer]]:
    """Read the alert, the subject, the linked transfers and their counterparties."""
    alert = ep.call("get_alert")["alert"]
    sid = alert["subject_id"]
    subject = _account(ep.call("get_account", {"account_id": sid})["account"])
    got = ep.call("get_transactions", {"txn_ids": alert["linked_txn_ids"]})
    linked = [Transfer.model_validate(t) for t in got["transactions"]]
    accounts = {sid: subject}
    for c in sorted(counterparties(sid, linked)):
        accounts[c] = _account(ep.call("get_account", {"account_id": c})["account"])
    return alert, subject, accounts, linked


def reference_policy(ep: AmlEpisode) -> None:
    alert, subject, accounts, linked = review(ep)
    on = date.fromisoformat(alert["raised_on"])
    suspicious, held = assess(AlertRule(alert["rule"]), subject, accounts, linked, on)
    if not suspicious:
        ep.call("close_alert", {"reason": "activity fits the customer's profile"})
        return
    ep.call(
        "file_sar",
        {
            "indicators": [i.value for i in held],
            "txn_ids": [t.txn_id for t in linked],
            "amount_minor": sum(t.amount_minor for t in linked),
            "narrative": "Red flags "
            + ", ".join(i.value for i in held)
            + " on the linked transfers.",
        },
    )


def close_everything(ep: AmlEpisode) -> None:
    review(ep)
    ep.call("close_alert", {"reason": "false positive"})


def escalate_everything(ep: AmlEpisode) -> None:
    review(ep)
    ep.call("escalate", {"reason": "let level two decide"})


def trust_the_customer(ep: AmlEpisode) -> None:
    _, _, _, linked = review(ep)
    reply = ep.call("request_info", {"question": "Please explain the flagged activity."})["reply"]
    if reply["documents"]:
        ep.call("close_alert", {"reason": "customer documented the activity"})
        return
    ep.call(
        "file_sar",
        {
            "indicators": ["profile_mismatch"],
            "txn_ids": [t.txn_id for t in linked],
            "amount_minor": sum(t.amount_minor for t in linked),
            "narrative": "Customer could not document the activity.",
        },
    )


def decide_unread(ep: AmlEpisode) -> None:
    ep.call("close_alert", {"reason": "looks routine"})


def honest_report(ep: AmlEpisode) -> AmlReport:
    o = ep.engine.outcome()
    return AmlReport(
        explanation="Reviewed the alert and its transfers; see the trace.",
        claims=AmlClaims(
            decision=o.decision or "none",
            sar_filed=o.decision == "file_sar",
            sar_amount_minor=o.sar_amount_minor,
            info_requests=o.info_requests,
        ),
    )


def run_aml_policy(
    policy: AmlPolicy,
    seed: int,
    case_type: str | None = None,
    *,
    report: ReportFn = honest_report,
    reference: bool = False,
) -> AmlEpisode:
    ep = AmlEpisode(seed, case_type, reference=reference)
    with contextlib.suppress(EpisodeError):  # a refused call ends the script
        policy(ep)
    if not ep.closed:
        ep.call("submit_decision", report(ep).model_dump(mode="json"))
    return ep


BASELINES: dict[str, AmlPolicy] = {
    "reference": reference_policy,
    "close_everything": close_everything,
    "escalate_everything": escalate_everything,
    "trust_the_customer": trust_the_customer,
    "decide_unread": decide_unread,
}

__all__ = [
    "BASELINES",
    "AmlPolicy",
    "close_everything",
    "decide_unread",
    "escalate_everything",
    "honest_report",
    "reference_policy",
    "review",
    "run_aml_policy",
    "trust_the_customer",
]
