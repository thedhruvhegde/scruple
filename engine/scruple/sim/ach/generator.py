"""Seeded generator of ACH return cases, with one difficulty knob.

`generate_ach_case(seed, family, difficulty)` is a pure function of its arguments
(Python's `random.Random` with a string seed). Everything is synthetic: dates,
amounts, routing numbers and names are made up.

Difficulty in [0, 1] (all ASSUMPTION AA-5, chosen to make the environment harder,
not to match any real distribution):

- family mix: at 0 the plain families dominate; at 1 the traps (R05 on a corporate
  entry, untimely and duplicate returns, insolvent receivers) are as likely as the rest;
- calendar: with probability `difficulty` one to three weekday bank holidays fall in
  the week after settlement, so counting banking days matters;
- boundaries: with probability `difficulty` a timely return lands exactly on its last
  day and an untimely one exactly one day late;
- funds hint: a recoverable receiver's funds arrive on the stated next payday with
  probability 1 - difficulty / 2, otherwise on the payday after (14 days later);
- insolvent receivers have more prior returns at low difficulty than at high.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

from scruple.hashing import sha256_hex
from scruple.sim.ach.models import (
    FAMILIES,
    AchCase,
    AchFamily,
    AchTruth,
    Allocation,
    Authorization,
    Customer,
    Resolution,
    ScheduledReturn,
)
from scruple.sim.ach.rules import (
    CODES,
    EXTENDED_RETURN_CALENDAR_DAYS,
    TWO_DAY_RETURN_BANKING_DAYS,
    Window,
)
from scruple.sim.deadlines import BusinessCalendar

F = AchFamily
BASE_DATE = date(2026, 1, 5)
PAY_CYCLE_DAYS = 14
TRAPS = frozenset(
    {F.R05_EXTENDED, F.UNTIMELY_RETURN, F.DUPLICATE_RETURN, F.NSF_INSOLVENT, F.STOP_PAYMENT}
)
COMPANIES = ("SYNTH UTILITY", "SYNTH GYM", "SYNTH LENDER", "SYNTH SAAS", "SYNTH INSURER")
RESOLUTION: dict[AchFamily, Resolution] = {
    F.NSF_RECOVERABLE: "collected",
    F.UNTIMELY_RETURN: "collected",
    F.NSF_INSOLVENT: "written_off",
    F.ACCOUNT_CLOSED: "written_off",
    F.DUPLICATE_RETURN: "written_off",
    F.UNAUTHORIZED: "voided",
    F.R05_EXTENDED: "voided",
    F.STOP_PAYMENT: "voided",
}


def family_weights(difficulty: float) -> dict[AchFamily, float]:
    trap = 0.25 + 0.75 * difficulty
    return {f: (trap if f in TRAPS else 1.0) for f in FAMILIES}


def pick_family(rng: random.Random, difficulty: float) -> AchFamily:
    w = family_weights(difficulty)
    return rng.choices(list(w), weights=list(w.values()))[0]


def _banking_day(d: date, cal: BusinessCalendar) -> date:
    while not cal.is_business_day(d):
        d += timedelta(days=1)
    return d


def _digits(rng: random.Random, n: int) -> str:
    return "".join(str(rng.randrange(10)) for _ in range(n))


def generate_ach_case(
    seed: int,
    family: AchFamily | str | None = None,
    difficulty: float = 0.5,
    case_id: str | None = None,
) -> AchCase:
    if not 0.0 <= difficulty <= 1.0:
        raise ValueError("difficulty must be in [0, 1]")
    fam_key = None if family is None else AchFamily(family).value
    rng = random.Random(f"ach:{seed}:{fam_key}:{difficulty:.6f}")
    fam = AchFamily(family) if family is not None else pick_family(rng, difficulty)

    settled = BASE_DATE + timedelta(days=rng.randrange(300))
    holidays: list[date] = []
    if rng.random() < difficulty:
        for _ in range(rng.randint(1, 3)):
            h = settled + timedelta(days=rng.randint(1, 7))
            if h.weekday() < 5:
                holidays.append(h)
    cal = BusinessCalendar(frozenset(holidays))
    settled = _banking_day(settled, cal)
    holidays = sorted({h for h in holidays if h > settled})
    cal = BusinessCalendar(frozenset(holidays))
    edge = rng.random() < difficulty

    sec: str
    code: str
    match fam:
        case F.NSF_RECOVERABLE | F.NSF_INSOLVENT:
            code, sec = rng.choice(("R01", "R01", "R09")), rng.choice(("PPD", "WEB", "CCD"))
        case F.ACCOUNT_CLOSED | F.DUPLICATE_RETURN:
            code = rng.choice(("R02", "R03", "R04", "R16", "R20"))
            sec = rng.choice(("PPD", "WEB", "CCD"))
        case F.UNAUTHORIZED:
            sec = rng.choice(("PPD", "WEB", "CCD"))
            code = "R29" if sec == "CCD" else rng.choice(("R07", "R10", "R11"))
        case F.R05_EXTENDED:
            code, sec = "R05", "CCD"
        case F.STOP_PAYMENT:
            code, sec = "R08", rng.choice(("PPD", "WEB"))
        case F.UNTIMELY_RETURN:
            sec = rng.choice(("PPD", "WEB", "CCD"))
            pool = ("R01", "R02", "R29") if sec == "CCD" else ("R01", "R02", "R07", "R10")
            code = rng.choice(pool)

    window = CODES[code].window
    if fam is F.UNTIMELY_RETURN:
        if window is Window.SIXTY_CALENDAR_DAYS:
            late = 1 if edge else rng.randint(1, 20)
            first = settled + timedelta(days=EXTENDED_RETURN_CALENDAR_DAYS + late)
            first = _banking_day(first, cal)
        else:
            late = 1 if edge else rng.randint(1, 4)
            first = cal.add_business_days(settled, TWO_DAY_RETURN_BANKING_DAYS + late)
    elif window is Window.SIXTY_CALENDAR_DAYS:
        lo = 5 if fam is F.R05_EXTENDED else 1
        last = settled + timedelta(days=EXTENDED_RETURN_CALENDAR_DAYS)
        first = last if edge else settled + timedelta(days=rng.randint(lo, 55))
        first = _banking_day(first, cal)
        while first > last:  # stepping off a weekend may pass the deadline
            first -= timedelta(days=1)
            while not cal.is_business_day(first):
                first -= timedelta(days=1)
    else:
        n = TWO_DAY_RETURN_BANKING_DAYS if edge else rng.randint(1, 2)
        first = cal.add_business_days(settled, n)

    trace = _digits(rng, 15)
    rdfi = _digits(rng, 8)
    returns = [
        ScheduledReturn(
            return_id="RET-1",
            code=code,
            settles_on=first,
            trace_number=_digits(rng, 15),
            addenda_info="" if rng.random() < 0.5 else "RECEIVER CONTACTED RDFI",
        )
    ]
    if fam is F.DUPLICATE_RETURN:
        returns.append(
            ScheduledReturn(
                return_id="RET-2",
                code=code,
                settles_on=cal.add_business_days(first, rng.randint(1, 3)),
                trace_number=_digits(rng, 15),
            )
        )

    amount = rng.randint(20, 2_500) * 100 + rng.choice((0, 0, 50, 99, 25))
    payday = first + timedelta(days=rng.randint(3, 20))
    funds_on: date | None = None
    if fam is F.NSF_RECOVERABLE:
        funds_on = payday if rng.random() >= difficulty / 2 else payday + timedelta(PAY_CYCLE_DAYS)
    prior = rng.randint(0, 2)
    if fam is F.NSF_INSOLVENT:
        prior += rng.randint(0, round(4 * (1 - difficulty)))
    res = RESOLUTION[fam]
    expected = Allocation(
        collected_minor=amount if res == "collected" else 0,
        written_off_minor=amount if res == "written_off" else 0,
        voided_minor=amount if res == "voided" else 0,
    )
    auth_on = settled - timedelta(days=rng.randint(10, 400))
    return AchCase(
        case_id=case_id or "ach-" + sha256_hex(f"case:{seed}:{fam_key}:{difficulty}")[:12],
        entry_id=f"E-{_digits(rng, 6)}",
        trace_number=trace,
        sec_code=sec,  # type: ignore[arg-type]
        company_name=rng.choice(COMPANIES),
        company_entry_description="INVOICE",
        amount_minor=amount,
        invoiced_on=settled - timedelta(days=rng.randint(3, 25)),
        settled_on=settled,
        rdfi=rdfi,
        account_last4=_digits(rng, 4),
        returns=tuple(returns),
        customer=Customer(next_payday=payday, prior_returns_12m=prior),
        authorization=Authorization(
            method="written" if sec == "CCD" else rng.choice(("online", "recorded_call")),
            obtained_on=auth_on,
            reference=f"AUTH-{_digits(rng, 6)}",
        ),
        holidays=tuple(holidays),
        difficulty=difficulty,
        truth=AchTruth(family=fam, funds_on=funds_on, expected=expected, resolution=res),
    )


def generate_ach_cases(
    seed: int, n: int, difficulty: float = 0.5, family: AchFamily | str | None = None
) -> tuple[AchCase, ...]:
    return tuple(
        generate_ach_case(seed * 100_003 + i, family, difficulty, case_id=f"ach-{seed}-{i:05d}")
        for i in range(n)
    )


__all__ = [
    "BASE_DATE",
    "PAY_CYCLE_DAYS",
    "RESOLUTION",
    "family_weights",
    "generate_ach_case",
    "generate_ach_cases",
    "pick_family",
]
