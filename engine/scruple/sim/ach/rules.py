"""ACH return rules used by the simulator: codes, return windows, dishonor, reinitiation.

Everything here is a simplification of the Nacha Operating Rules for one originator
(the agent's employer) collecting consumer and corporate debits. Source ids (W1-W8)
are listed in docs/WORKFLOWS.md; anything without one is an ASSUMPTION (AA-n, also
in docs/WORKFLOWS.md). The primary rule text (the Nacha Operating Rules book) was not
opened: W2 to W7 were read through public Nacha rule pages and secondary guides via
search summaries. Not legal or compliance advice.

- Titles of return codes come from moov-io/ach (W1, Apache 2.0) through
  `scruple.ingest.nacha_codes.RETURN_REASONS`.
- Return windows (W2, W3): most returns must reach the originating bank by the
  second banking day after the settlement date of the original entry; unauthorized
  debits to consumer accounts (R05, R07, R10, R11) may be returned until the 60th
  calendar day after settlement. R29 (corporate, not authorized) is a two-day return.
- Unauthorized Entry Fee (W4): 4.50 USD per return coded R05, R07, R10, R11, R29 or
  R51, paid by the ODFI to the RDFI. Sources disagree on whether R11 is included; we
  include it. AA-3: the ODFI passes the fee through to the originator on the day the
  return settles.
- Dishonored returns (W5): the ODFI may dishonor an improper return (we model R67
  duplicate and R68 untimely) within five banking days after the settlement date of
  the return. Contested dishonored returns (W5): the RDFI may contest within two
  banking days after the dishonor settles (R72 untimely dishonor, R73 timely original
  return, R75 return not a duplicate).
- Reinitiation (W6, W7): an entry returned R01 or R09 may be reinitiated at most two
  times after the original return, within 180 days after the settlement date of the
  original entry, with the company entry description exactly "RETRY PYMT". AA-4: any
  other return code may not be reinitiated here (the rules allow it once the cause is
  corrected or a new authorization obtained, which the simulator does not model).

Day granularity (AA-1): deadlines that the rules state as "opening of business" are
modelled as "settles on or before that day".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

from scruple.ingest.nacha_codes import RETURN_REASONS
from scruple.sim.deadlines import BusinessCalendar


class Window(StrEnum):
    TWO_BANKING_DAYS = "two_banking_days"
    SIXTY_CALENDAR_DAYS = "sixty_calendar_days"


class Reinit(StrEnum):
    NSF = "nsf"  # R01/R09: up to two retries within 180 days (W6, W7)
    NEVER = "never"  # not reinitiable in this simulator (AA-4)


class Group(StrEnum):
    FUNDS = "funds"  # money may arrive later
    ACCOUNT = "account"  # the account cannot be debited (closed, frozen, invalid)
    UNAUTHORIZED = "unauthorized"  # the receiver disputes the authorization
    STOPPED = "stopped"  # the receiver stopped the payment


@dataclass(frozen=True)
class ReturnCode:
    code: str
    window: Window
    group: Group
    reinit: Reinit
    unauthorized_fee: bool
    source: str  # source ids in docs/WORKFLOWS.md, or an AA-n assumption

    @property
    def title(self) -> str:
        return RETURN_REASONS[self.code]


_SRC2 = "W1, W3"
_C = ReturnCode
CODES: dict[str, ReturnCode] = {
    c.code: c
    for c in (
        _C("R01", Window.TWO_BANKING_DAYS, Group.FUNDS, Reinit.NSF, False, "W1, W3, W6, W7"),
        _C("R02", Window.TWO_BANKING_DAYS, Group.ACCOUNT, Reinit.NEVER, False, _SRC2 + ", AA-4"),
        _C("R03", Window.TWO_BANKING_DAYS, Group.ACCOUNT, Reinit.NEVER, False, _SRC2 + ", AA-4"),
        _C("R04", Window.TWO_BANKING_DAYS, Group.ACCOUNT, Reinit.NEVER, False, _SRC2 + ", AA-4"),
        _C("R05", Window.SIXTY_CALENDAR_DAYS, Group.UNAUTHORIZED, Reinit.NEVER, True, "W1-W4"),
        _C("R07", Window.SIXTY_CALENDAR_DAYS, Group.UNAUTHORIZED, Reinit.NEVER, True, "W1-W4"),
        _C("R08", Window.TWO_BANKING_DAYS, Group.STOPPED, Reinit.NEVER, False, _SRC2 + ", AA-4"),
        _C("R09", Window.TWO_BANKING_DAYS, Group.FUNDS, Reinit.NSF, False, "W1, W3, W6, W7"),
        _C("R10", Window.SIXTY_CALENDAR_DAYS, Group.UNAUTHORIZED, Reinit.NEVER, True, "W1-W4"),
        _C("R11", Window.SIXTY_CALENDAR_DAYS, Group.UNAUTHORIZED, Reinit.NEVER, True, "W1-W4"),
        _C("R16", Window.TWO_BANKING_DAYS, Group.ACCOUNT, Reinit.NEVER, False, _SRC2 + ", AA-4"),
        _C("R20", Window.TWO_BANKING_DAYS, Group.ACCOUNT, Reinit.NEVER, False, _SRC2 + ", AA-4"),
        _C("R29", Window.TWO_BANKING_DAYS, Group.UNAUTHORIZED, Reinit.NEVER, True, "W1-W4"),
    )
}

# Dishonored (ODFI) and contested dishonored (RDFI) return codes that are modelled (W5).
DISHONOR_CODES: dict[str, str] = {"R67": RETURN_REASONS["R67"], "R68": RETURN_REASONS["R68"]}
CONTEST_CODES: dict[str, str] = {c: RETURN_REASONS[c] for c in ("R72", "R73", "R75")}

TWO_DAY_RETURN_BANKING_DAYS = 2  # W2, W3
EXTENDED_RETURN_CALENDAR_DAYS = 60  # W2, W3
DISHONOR_BANKING_DAYS = 5  # W5
CONTEST_BANKING_DAYS = 2  # W5
REINIT_MAX = 2  # W6, W7
REINIT_CALENDAR_DAYS = 180  # W6, W7
REINIT_DESCRIPTION = "RETRY PYMT"  # W6, W7 (the company entry description)
UNAUTHORIZED_ENTRY_FEE_MINOR = 450  # USD cents (W4)
# AA-2: entries the agent submits (a retry, a dishonor) settle on the next banking day;
# the RDFI sends a return or a contest at the end of its window.
SETTLE_BANKING_DAYS = 1

CONSUMER_SEC = frozenset({"PPD", "WEB"})  # consumer debits (W1 SEC codes)
CORPORATE_SEC = frozenset({"CCD"})


def rules_table() -> str:
    """The return-code table in docs/WORKFLOWS.md, rendered from `CODES`."""
    rows = [
        "| code | title | return window | group | reinitiation | unauthorized fee | source |",
        "|---|---|---|---|---|---|---|",
    ]
    for c in CODES.values():
        fee = "yes" if c.unauthorized_fee else "no"
        rows.append(
            f"| {c.code} | {c.title} | {c.window.value} | {c.group.value} | "
            f"{c.reinit.value} | {fee} | {c.source} |"
        )
    return "\n".join(rows)


def return_deadline(code: str, settled_on: date, calendar: BusinessCalendar) -> date:
    """Last day a return of an entry settled on `settled_on` may settle (AA-1)."""
    rc = CODES[code]
    if rc.window is Window.SIXTY_CALENDAR_DAYS:
        return settled_on + timedelta(days=EXTENDED_RETURN_CALENDAR_DAYS)
    return calendar.add_business_days(settled_on, TWO_DAY_RETURN_BANKING_DAYS)


def is_timely(code: str, entry_settled: date, return_settled: date, cal: BusinessCalendar) -> bool:
    return return_settled <= return_deadline(code, entry_settled, cal)


def dishonor_deadline(return_settled: date, calendar: BusinessCalendar) -> date:
    return calendar.add_business_days(return_settled, DISHONOR_BANKING_DAYS)


def reinit_deadline(original_settled: date) -> date:
    return original_settled + timedelta(days=REINIT_CALENDAR_DAYS)


def next_banking_day(d: date, calendar: BusinessCalendar) -> date:
    return calendar.add_business_days(d, SETTLE_BANKING_DAYS)


__all__ = [
    "CODES",
    "CONSUMER_SEC",
    "CONTEST_BANKING_DAYS",
    "CONTEST_CODES",
    "CORPORATE_SEC",
    "DISHONOR_BANKING_DAYS",
    "DISHONOR_CODES",
    "EXTENDED_RETURN_CALENDAR_DAYS",
    "REINIT_CALENDAR_DAYS",
    "REINIT_DESCRIPTION",
    "REINIT_MAX",
    "SETTLE_BANKING_DAYS",
    "TWO_DAY_RETURN_BANKING_DAYS",
    "UNAUTHORIZED_ENTRY_FEE_MINOR",
    "Group",
    "Reinit",
    "ReturnCode",
    "Window",
    "dishonor_deadline",
    "is_timely",
    "next_banking_day",
    "reinit_deadline",
    "return_deadline",
    "rules_table",
]
