"""Seeded generator of AML alert cases on synthetic transaction graphs.

`generate_aml_case(seed, family, difficulty)` is a pure function of its arguments
(Python's `random.Random` with a string seed). Accounts, names, banks, timestamps and
amounts are made up. Each case has:

- a subject account at our bank with a KYC profile (occupation, declared monthly
  inflow, cash-intensive or not, account opening date);
- 60 days of background activity (salary or client payments in, card spend and bills
  out) that no alert rule looks at;
- one pattern in the 14-day alert window, drawn from the family (`PATTERNS`), with the
  alert rule it trips and the transfers the alert links;
- the reply a request for information would get.

The subject's opening balance is set so that its balance never goes negative: no
transfer spends money the account does not hold.

Difficulty in [0, 1] (all ASSUMPTION AM-7, ours, chosen to make triage harder, not
to match any real distribution): more background noise; typologies that sit closer
to their lookalikes (a wider structuring band, slower pass-through, a few aged
accounts among new ones); lookalikes that look more like typologies (more
near-threshold cash, more new counterparties, faster wires); and requests for
information that go unanswered, or come back with a story and no documents, more
often.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from scruple.hashing import sha256_hex
from scruple.sim.aml.models import (
    CASH,
    CTR_THRESHOLD_MINOR,
    FAMILIES,
    LOOKBACK_DAYS,
    TYPOLOGIES,
    WINDOW_DAYS,
    Account,
    Alert,
    AlertRule,
    AmlCase,
    AmlFamily,
    AmlTruth,
    Channel,
    RfiResponse,
    Transfer,
)

F = AmlFamily
BASE_DATE = date(2026, 2, 2)
OUR_BANK = "SYNTH BANK"
NEAR_LO_MINOR = 700_000  # mirrors scruple.sim.aml.indicators (AM-2)


@dataclass
class _Leg:
    at: datetime
    src: str
    dst: str
    amount_minor: int
    channel: Channel
    branch: str = ""
    memo: str = ""
    linked: bool = False
    typology: bool = False


@dataclass
class Builder:
    """Mutable scratch space for one case; `generate_aml_case` freezes it."""

    rng: random.Random
    raised_on: date
    difficulty: float
    accounts: dict[str, Account] = field(default_factory=dict)
    legs: list[_Leg] = field(default_factory=list)
    _n: int = 0

    @property
    def window_start(self) -> date:
        return self.raised_on - timedelta(days=WINDOW_DAYS)

    @property
    def lookback_start(self) -> date:
        return self.raised_on - timedelta(days=LOOKBACK_DAYS)

    def window_day(self, lo: int = 0, hi: int = WINDOW_DAYS - 1) -> date:
        return self.window_start + timedelta(days=self.rng.randint(lo, hi))

    def at(self, day: date, lo_hour: int = 8, hi_hour: int = 17) -> datetime:
        return datetime.combine(
            day, time(self.rng.randint(lo_hour, hi_hour), self.rng.randrange(60))
        )

    def _id(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}-{self._n:03d}{self.rng.randrange(1000):03d}"

    def internal(
        self,
        kind: str,
        occupation: str,
        opened_days_ago: int,
        expected_minor: int,
        cash_intensive: bool = False,
    ) -> str:
        aid = self._id("A")
        self.accounts[aid] = Account(
            account_id=aid,
            internal=True,
            holder=f"SYNTH {'CO' if kind == 'business' else 'HOLDER'} {aid[2:]}",
            kind=kind,  # type: ignore[arg-type]
            bank=OUR_BANK,
            opened_on=self.raised_on - timedelta(days=opened_days_ago),
            occupation=occupation,
            cash_intensive=cash_intensive,
            expected_monthly_in_minor=expected_minor,
        )
        return aid

    def external(self, holder: str, kind: str = "business", country: str = "US") -> str:
        aid = self._id("X")
        self.accounts[aid] = Account(
            account_id=aid,
            internal=False,
            holder=holder,
            kind=kind,  # type: ignore[arg-type]
            bank=f"SYNTH EXTERNAL BANK {self.rng.randint(1, 9)}",
            country=country,
        )
        return aid

    def person(self, opened_lo: int, opened_hi: int) -> str:
        """A retail customer at our bank (a counterparty)."""
        job = self.rng.choice(("teacher", "nurse", "driver", "student", "engineer", "retiree"))
        days = self.rng.randint(opened_lo, opened_hi)
        return self.internal("individual", job, days, self.rng.randint(150, 900) * 1_000)

    def leg(
        self, at: datetime, src: str, dst: str, amount: int, channel: Channel, **kw: object
    ) -> None:
        self.legs.append(_Leg(at, src, dst, amount, channel, **kw))  # type: ignore[arg-type]

    def dollars(self, lo: int, hi: int, cents: bool = True) -> int:
        """A random amount in minor units between lo and hi dollars."""
        base = self.rng.randint(lo, hi) * 100
        return base + (self.rng.randrange(100) if cents else 0)


@dataclass(frozen=True)
class Pattern:
    """A family's subject profile and window activity."""

    rule: Callable[[Builder], AlertRule]
    subject: Callable[[Builder], str]
    activity: Callable[[Builder, str, AlertRule], None]
    rfi: tuple[str, tuple[str, ...]]  # explanation and documents a genuine customer gives


def _background(b: Builder, subject: str) -> None:
    """Salary or client payments in, card spend and bills out, over the lookback."""
    acct = b.accounts[subject]
    declared = acct.expected_monthly_in_minor or 300_000
    if acct.kind == "individual":
        payer = b.external("SYNTH EMPLOYER LLC")
        for k in range(LOOKBACK_DAYS // 14 + 1):
            day = b.lookback_start + timedelta(days=2 + 14 * k)
            if day >= b.raised_on:
                break
            amt = max(10_000, declared // 2 + b.rng.randint(-3_000, 3_000))
            b.leg(b.at(day, 6, 7), payer, subject, amt, "ach", memo="SALARY")
    else:
        clients = [b.external(f"SYNTH CLIENT {i}") for i in range(b.rng.randint(2, 3))]
        for k in range(LOOKBACK_DAYS // 7):
            day = b.lookback_start + timedelta(days=1 + 7 * k)
            amt = max(10_000, declared // 4 + b.rng.randint(-20_000, 20_000))
            b.leg(b.at(day), b.rng.choice(clients), subject, amt, "ach", memo="INVOICE")
    merchants = [b.external(f"SYNTH MERCHANT {i}") for i in range(3)]
    n_card = b.rng.randint(4, 8) + round(8 * b.difficulty)
    for _ in range(n_card):
        day = b.lookback_start + timedelta(days=b.rng.randint(1, LOOKBACK_DAYS - 1))
        b.leg(b.at(day, 9, 21), subject, b.rng.choice(merchants), b.dollars(8, 180), "card")
    landlord = b.external("SYNTH PROPERTY MGMT")
    for k in range(2):
        day = b.lookback_start + timedelta(days=5 + 30 * k)
        b.leg(b.at(day), subject, landlord, max(5_000, declared * 3 // 10), "ach", memo="RENT")


# --- benign lookalikes ------------------------------------------------------------------
def _cash_business_subject(b: Builder) -> str:
    job = b.rng.choice(("grocery store", "restaurant", "laundromat", "car wash"))
    return b.internal("business", job, b.rng.randint(700, 4_000), 0, cash_intensive=True)


def _cash_business(b: Builder, subject: str, rule: AlertRule) -> None:
    n_over = b.rng.randint(1, 2)
    n_near = b.rng.randint(0, 1 + round(3 * b.difficulty))
    n_low = b.rng.randint(4, 8)
    amounts = [b.rng.randint(CTR_THRESHOLD_MINOR + 100, 1_600_000) for _ in range(n_over)]
    amounts += [b.rng.randint(NEAR_LO_MINOR, CTR_THRESHOLD_MINOR - 100) for _ in range(n_near)]
    amounts += [b.rng.randint(80_000, NEAR_LO_MINOR - 100) for _ in range(n_low)]
    b.rng.shuffle(amounts)
    for amt in amounts:
        b.leg(
            b.at(b.window_day(), 16, 20),
            CASH,
            subject,
            amt,
            "cash",
            branch="BR-1",
            memo="DAILY TAKINGS",
            linked=True,
        )
    total = sum(amounts)
    b.accounts[subject] = b.accounts[subject].model_copy(
        update={"expected_monthly_in_minor": int(total * 2 * b.rng.uniform(0.9, 1.3))}
    )


def _seller_subject(b: Builder) -> str:
    kind = b.rng.choice(("individual", "business"))
    return b.internal(
        kind, "online reseller", b.rng.randint(400, 3_000), b.dollars(3_000, 12_000, False)
    )


def _seller(b: Builder, subject: str, rule: AlertRule) -> None:
    k = b.rng.randint(6, 14)
    n_new = int(k * 0.3 * b.difficulty)
    total = 0
    last = b.window_start
    for i in range(k):
        buyer = b.person(10, 170) if i < n_new else b.person(365, 4_000)
        for _ in range(b.rng.randint(1, 2)):
            day = b.window_day(0, 9)
            amt = b.dollars(20, 400)
            total += amt
            last = max(last, day)
            b.leg(b.at(day, 7, 23), buyer, subject, amt, "p2p", memo="ORDER", linked=True)
    supplier = b.external("SYNTH WHOLESALE SUPPLY")
    share = b.rng.uniform(0.2, 0.5 + 0.2 * b.difficulty)
    day = last + timedelta(days=b.rng.randint(1, 4))
    b.leg(
        b.at(day), subject, supplier, max(100, int(total * share)), "ach", memo="STOCK", linked=True
    )


def _payroll_subject(b: Builder) -> str:
    job = b.rng.choice(("landscaping company", "dental practice", "auto repair shop"))
    return b.internal("business", job, b.rng.randint(700, 4_000), b.dollars(40_000, 120_000, False))


def _payroll(b: Builder, subject: str, rule: AlertRule) -> None:
    client = b.external("SYNTH ANCHOR CLIENT INC")
    amount = b.dollars(20_000, 80_000)
    day = b.window_day(0, 8)
    t0 = b.at(day, 7, 10)
    b.leg(t0, client, subject, amount, "ach", memo="INVOICE PAID", linked=True)
    k = b.rng.randint(5, 12)
    staff = [b.person(200, 4_000) for _ in range(k)]
    if b.rng.random() < b.difficulty:  # a new hire
        staff[0] = b.person(10, 60)
    share = b.rng.uniform(0.5, 0.8 + 0.15 * b.difficulty)
    weights = [b.rng.uniform(0.7, 1.3) for _ in staff]
    pay_at = t0 + timedelta(hours=b.rng.randint(20, 46))
    for s, w in zip(staff, weights, strict=True):
        amt = max(100, int(amount * share * w / sum(weights)))
        b.leg(pay_at, subject, s, amt, "ach", memo="PAYROLL", linked=True)


def _property_subject(b: Builder) -> str:
    return b.internal(
        "individual",
        b.rng.choice(("engineer", "physician", "retiree")),
        b.rng.randint(700, 5_000),
        b.dollars(5_000, 15_000, False),
    )


def _property(b: Builder, subject: str, rule: AlertRule) -> None:
    brokerage = b.external("SYNTH BROKERAGE", kind="business")
    title = b.external("SYNTH TITLE CO", kind="business")
    amount = b.dollars(150_000, 600_000, False)
    day = b.window_day(0, 8)
    t0 = b.at(day, 9, 12)
    if b.rng.random() < b.difficulty:  # earnest money, before the proceeds arrive
        b.leg(
            t0 - timedelta(days=3),
            subject,
            title,
            amount // 50,
            "wire",
            memo="EARNEST MONEY",
            linked=True,
        )
    b.leg(t0, brokerage, subject, amount, "wire", memo="SALE PROCEEDS", linked=True)
    lag = b.rng.randint(round(24 - 18 * b.difficulty), round(120 - 72 * b.difficulty))
    out = int(amount * b.rng.uniform(0.9, 1.0))
    b.leg(t0 + timedelta(hours=lag), subject, title, out, "wire", memo="CLOSING FUNDS", linked=True)


def _fixed(rule: AlertRule) -> Callable[[Builder], AlertRule]:
    return lambda b: rule


PATTERNS: dict[AmlFamily, Pattern] = {
    F.CASH_BUSINESS: Pattern(
        _fixed(AlertRule.CASH_NEAR_THRESHOLD),
        _cash_business_subject,
        _cash_business,
        (
            "Daily cash takings of the business, banked after close.",
            ("till reports", "sales ledger"),
        ),
    ),
    F.MARKETPLACE_SELLER: Pattern(
        _fixed(AlertRule.FAN_IN),
        _seller_subject,
        _seller,
        ("Buyer payments for online orders; stock bought from a wholesaler.", ("order history",)),
    ),
    F.PAYROLL: Pattern(
        _fixed(AlertRule.FAN_OUT),
        _payroll_subject,
        _payroll,
        (
            "A client paid its invoice; the next day we ran payroll.",
            ("payroll register", "invoice"),
        ),
    ),
    F.PROPERTY_PURCHASE: Pattern(
        _fixed(AlertRule.PASS_THROUGH),
        _property_subject,
        _property,
        (
            "Proceeds of selling investments, wired to the title company to close on a house.",
            ("closing statement", "brokerage statement"),
        ),
    ),
}


def pick_family(rng: random.Random) -> AmlFamily:
    return rng.choice([f for f in FAMILIES if f in PATTERNS])


def _rfi(b: Builder, fam: AmlFamily, pattern: Pattern) -> RfiResponse:
    d = b.difficulty
    if fam not in TYPOLOGIES:
        if b.rng.random() < d / 2:
            return RfiResponse(responded=False)
        text, docs = pattern.rfi
        return RfiResponse(responded=True, explanation=text, documents=docs)
    if b.rng.random() < 0.6 * d:  # a cover story, and nothing to back it
        return RfiResponse(responded=True, explanation=pattern.rfi[0], documents=())
    if b.rng.random() < 0.5:
        return RfiResponse(responded=True, explanation="Money from friends and family.")
    return RfiResponse(responded=False)


def generate_aml_case(
    seed: int,
    family: AmlFamily | str | None = None,
    difficulty: float = 0.5,
    case_id: str | None = None,
) -> AmlCase:
    if not 0.0 <= difficulty <= 1.0:
        raise ValueError("difficulty must be in [0, 1]")
    fam_key = None if family is None else AmlFamily(family).value
    rng = random.Random(f"aml:{seed}:{fam_key}:{difficulty:.6f}")
    fam = AmlFamily(family) if family is not None else pick_family(rng)
    if fam not in PATTERNS:
        raise ValueError(f"no generator for aml family {fam.value}")
    pattern = PATTERNS[fam]
    raised = BASE_DATE + timedelta(days=rng.randrange(300))
    b = Builder(rng, raised, difficulty)
    rule = pattern.rule(b)
    subject = pattern.subject(b)
    _background(b, subject)
    pattern.activity(b, subject, rule)

    legs = sorted(b.legs, key=lambda x: x.at)
    transfers = tuple(
        Transfer(
            txn_id=f"T-{i + 1:05d}",
            at=x.at,
            src=x.src,
            dst=x.dst,
            amount_minor=x.amount_minor,
            channel=x.channel,
            branch=x.branch,
            memo=x.memo,
        )
        for i, x in enumerate(legs)
    )
    running = low = 0
    for t in transfers:
        if subject in (t.src, t.dst):
            running += t.amount_minor if t.dst == subject else -t.amount_minor
            low = min(low, running)
    cushion = b.dollars(50, 300) if fam in TYPOLOGIES else b.dollars(500, 5_000)
    linked = tuple(t.txn_id for t, x in zip(transfers, legs, strict=True) if x.linked)
    typology = tuple(t.txn_id for t, x in zip(transfers, legs, strict=True) if x.typology)
    cid = case_id or "aml-" + sha256_hex(f"case:{seed}:{fam_key}:{difficulty}")[:12]
    suspicious = fam in TYPOLOGIES
    return AmlCase(
        case_id=cid,
        subject_id=subject,
        accounts=tuple(sorted(b.accounts.values(), key=lambda a: a.account_id)),
        transfers=transfers,
        alert=Alert(
            alert_id="AL-" + sha256_hex(cid)[:8].upper(),
            subject_id=subject,
            rule=rule,
            raised_on=raised,
            window_start=b.window_start,
            window_end=raised - timedelta(days=1),
            linked_txn_ids=linked,
        ),
        opening_balance_minor=cushion - low,
        lookback_start=b.lookback_start,
        difficulty=difficulty,
        rfi=_rfi(b, fam, pattern),
        truth=AmlTruth(
            family=fam,
            suspicious=suspicious,
            expected="file_sar" if suspicious else "close",
            typology_txn_ids=typology,
        ),
    )


def generate_aml_cases(
    seed: int, n: int, difficulty: float = 0.5, family: AmlFamily | str | None = None
) -> tuple[AmlCase, ...]:
    return tuple(
        generate_aml_case(seed * 100_003 + i, family, difficulty, case_id=f"aml-{seed}-{i:05d}")
        for i in range(n)
    )


__all__ = [
    "BASE_DATE",
    "PATTERNS",
    "Builder",
    "Pattern",
    "generate_aml_case",
    "generate_aml_cases",
    "pick_family",
]
