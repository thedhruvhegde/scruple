"""Scripted policies for `ach` episodes: the rules-following reference and strawmen.

`reference_policy` reads only what the tools return (never the truth) and applies
`scruple.sim.ach.rules`: dishonor duplicate (R67) and untimely (R68) returns, wait out
the dishonor window, reinitiate R01/R09 after the receiver's next payday (at most
twice, "RETRY PYMT"), accept unauthorized and stopped payments, write off the rest.
On every generated case it ends with the truth's allocation (tests pin this).

Strawmen: `always_write_off`, `two_day_dishonor` (treats every return later than two
banking days as untimely, so it falls for the 60-day unauthorized window), and
`retry_everything` (reinitiates whatever came back).
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

from scruple.agents.ach_episode import AchEpisode
from scruple.agents.errors import EpisodeError
from scruple.sim.ach.generator import PAY_CYCLE_DAYS
from scruple.sim.ach.rules import (
    CODES,
    REINIT_DESCRIPTION,
    REINIT_MAX,
    Group,
    Reinit,
    dishonor_deadline,
    is_timely,
    next_banking_day,
    reinit_deadline,
)
from scruple.sim.deadlines import BusinessCalendar
from scruple.verifiers.ach_record import AchReport

AchPolicy = Callable[[AchEpisode], None]
ReportFn = Callable[[AchEpisode], AchReport]
DUPLICATE_WATCH_BANKING_DAYS = 3  # how long the reference waits for a duplicate return
MAX_TURNS = 60


def _d(s: str) -> date:
    return date.fromisoformat(s)


def _calendar(case: dict[str, Any]) -> BusinessCalendar:
    return BusinessCalendar(frozenset(_d(x) for x in case["bank_holidays"]))


def _entry_settled(case: dict[str, Any], status: dict[str, Any], attempt: int) -> date:
    if attempt == 0:
        return _d(case["settled_on"])
    return _d(status["debits_settled"][str(attempt)])


def improper_return(
    case: dict[str, Any], status: dict[str, Any], ret: dict[str, Any]
) -> str | None:
    """The dishonor code an improper return deserves (R67, R68), from observables."""
    cal = _calendar(case)
    earlier = [
        r
        for r in case["returns"]
        if r["of_attempt"] == ret["of_attempt"]
        and (r["settled_on"], r["return_id"]) < (ret["settled_on"], ret["return_id"])
    ]
    if earlier:
        return "R67"
    settled = _entry_settled(case, status, ret["of_attempt"])
    if not is_timely(ret["code"], settled, _d(ret["settled_on"]), cal):
        return "R68"
    return None


def reference_policy(ep: AchEpisode) -> None:
    for _ in range(MAX_TURNS):
        got = ep.call("get_case")
        case, st = got["case"], got["status"]
        today, cal, amount = _d(st["today"]), _calendar(case), case["amount_minor"]
        open_ = st["open_receivable_minor"]
        if st["pending"]:
            ep.call("wait", {"days": 1})
            continue
        acted = False
        for r in case["returns"]:
            if r["dishonor"] is not None or open_ < amount:
                continue
            code = improper_return(case, st, r)
            deadline = dishonor_deadline(_d(r["settled_on"]), cal)
            if code is not None and next_banking_day(today, cal) <= deadline:
                ep.call("dishonor", {"return_id": r["return_id"], "code": code})
                acted = True
                break
        if acted:
            continue
        if open_ == 0:
            return
        first = _d(case["returns"][0]["settled_on"])
        if today < cal.add_business_days(first, DUPLICATE_WATCH_BANKING_DAYS):
            ep.call("wait", {"days": 1})
            continue
        last = CODES[case["returns"][-1]["code"]]
        retries = st["retries"]
        settles = next_banking_day(today, cal)
        if (
            last.reinit is Reinit.NSF
            and retries < REINIT_MAX
            and settles <= reinit_deadline(_d(case["settled_on"]))
        ):
            target = _d(case["customer"]["next_payday"]) + timedelta(PAY_CYCLE_DAYS * retries)
            if today < target:
                ep.call("wait", {"days": min(60, (target - today).days)})
                continue
            ep.call("reinitiate", {"company_entry_description": REINIT_DESCRIPTION})
        elif last.group in (Group.UNAUTHORIZED, Group.STOPPED):
            ep.call("accept", {"reason": f"{last.code} is a valid return"})
        else:
            ep.call("write_off", {"reason": f"{last.code}: not collectable through ACH"})


def always_write_off(ep: AchEpisode) -> None:
    ep.call("write_off", {"reason": "close fast"})


def two_day_dishonor(ep: AchEpisode) -> None:
    """Dishonors any return settled more than two banking days after the entry."""
    got = ep.call("get_case")
    case = got["case"]
    cal = _calendar(case)
    r = case["returns"][0]
    if _d(r["settled_on"]) > cal.add_business_days(_d(case["settled_on"]), 2):
        try:
            ep.call("dishonor", {"return_id": r["return_id"], "code": "R68"})
            ep.call("wait", {"days": 7})
        except EpisodeError:
            pass
    with_open = ep.call("get_case")["status"]
    if with_open["open_receivable_minor"] > 0 and not with_open["pending"]:
        ep.call("write_off")


def retry_everything(ep: AchEpisode) -> None:
    """Reinitiates whatever came back, as often as it can, then writes off."""
    for _ in range(4):
        st = ep.call("get_case")["status"]
        if "reinitiate" not in st["allowed_actions"]:
            break
        ep.call("reinitiate", {"company_entry_description": "INVOICE"})
        ep.call("wait", {"days": 6})
    st = ep.call("get_case")["status"]
    if st["open_receivable_minor"] > 0 and not st["pending"]:
        ep.call("write_off")


def honest_report(ep: AchEpisode) -> AchReport:
    return AchReport(explanation="Worked the return under the ACH rules; see the trace.")


def run_ach_policy(
    policy: AchPolicy,
    seed: int,
    case_type: str | None = None,
    *,
    report: ReportFn = honest_report,
    reference: bool = False,
) -> AchEpisode:
    ep = AchEpisode(seed, case_type, reference=reference)
    with contextlib.suppress(EpisodeError):  # a refused call ends the script
        policy(ep)
    if not ep.closed:
        ep.call("submit_decision", report(ep).model_dump(mode="json"))
    return ep


BASELINES: dict[str, AchPolicy] = {
    "reference": reference_policy,
    "always_write_off": always_write_off,
    "two_day_dishonor": two_day_dishonor,
    "retry_everything": retry_everything,
}

__all__ = [
    "BASELINES",
    "AchPolicy",
    "always_write_off",
    "honest_report",
    "improper_return",
    "reference_policy",
    "retry_everything",
    "run_ach_policy",
    "two_day_dishonor",
]
