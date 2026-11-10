"""Seeded generator of reconciliation cases with injected breaks.

`generate_recon_case(seed, difficulty, fmt, kinds)` is a pure function of its
arguments. Clean transactions appear once in the cash book and once on the
statement, with the same reference (a bank may book them up to two days later).
Breaks are then injected; each records the treatment a correct reconciler applies
(`Break.expected`, plus `alternatives` where two treatments are equally right, such
as which of two duplicate lines to keep).

Difficulty in [0, 1] (ASSUMPTION AR-6, chosen to make the task harder, not to match
any real data): more clean lines and more breaks; larger booking-date shifts; with
probability `difficulty` an unreferenced receipt has the same amount as an open
invoice (matching by amount alone books it to the wrong customer).

Synthetic parameters: processor fees are 1.5% to 3% of the gross (AR-4); FX uses the
simulator's synthetic rates (`scruple.sim.fx`, docs/ASSUMPTIONS.md A-27); timing
items are booked in the last two days of the period (AR-1); a receipt booked six or
more days before period end that never reached the bank is a break to escalate (AR-2).
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta

from scruple.hashing import sha256_hex
from scruple.sim.fx import FxTable
from scruple.sim.recon.models import (
    BREAK_KINDS,
    Adjustment,
    BankLine,
    Break,
    BreakKind,
    Invoice,
    LedgerLine,
    LineKind,
    ReconCase,
    ReconTruth,
    Treatment,
)
from scruple.sim.recon.statement import write_bai2, write_camt053

K = BreakKind
BASE = date(2026, 1, 5)
PERIOD_DAYS = 28
TIMING_DAYS = 2  # AR-1
MISSING_BANK_MIN_DAYS = 6  # AR-2
FOREIGN = ("GBP", "EUR", "JPY")
FEE = "expense:bank_fees"
FX = "fx:gain_loss"


@dataclass
class _L:  # draft ledger line
    key: int
    booked_on: date
    amount: int
    reference: str
    kind: LineKind
    offset: str
    description: str
    invoice_id: str | None = None
    ocy: str | None = None
    oamt: int | None = None


@dataclass
class _B:  # draft bank line
    key: int
    on: date
    amount: int
    reference: str
    type_code: str
    description: str


@dataclass
class _Draft:
    rng: random.Random
    start: date
    end: date
    ccy: str
    fx: FxTable
    ledger: list[_L] = field(default_factory=list)
    bank: list[_B] = field(default_factory=list)
    invoices: list[Invoice] = field(default_factory=list)
    breaks: list[tuple[BreakKind, list[Treatment]]] = field(default_factory=list)
    clean: list[tuple[int, int]] = field(default_factory=list)
    refs: set[str] = field(default_factory=set)
    n: int = 0

    def key(self) -> int:
        self.n += 1
        return self.n

    def ref(self, prefix: str) -> str:
        while True:
            tail = self.rng.choice("ABCDEFGHJKMNPQRSTUVWXYZ")
            r = f"{prefix}-{self.rng.randint(10_000, 99_999)}{tail}"
            if r not in self.refs:
                self.refs.add(r)
                return r

    def day(self, lo: int = 0, hi_from_end: int = 3) -> date:
        span = (self.end - self.start).days - hi_from_end
        return self.start + timedelta(days=self.rng.randint(lo, max(lo, span)))

    def amount(self, lo: int = 50, hi: int = 20_000) -> int:
        return self.rng.randint(lo, hi) * 100 + self.rng.choice((0, 0, 25, 50, 99, 1))

    def ledger_line(
        self,
        on: date,
        amount: int,
        kind: LineKind,
        reference: str | None = None,
        invoice_id: str | None = None,
        ocy: str | None = None,
        oamt: int | None = None,
    ) -> _L:
        ref = reference or self.ref(_PREFIX[kind])
        inv = invoice_id
        if kind == "customer_receipt" and inv is None:
            inv = f"INV-{self.rng.randint(10_000, 99_999)}"
        offset = {
            "customer_receipt": f"ar:{inv}",
            "processor_settlement": "clearing:card_processor",
            "vendor_payment": f"ap:V-{self.rng.randint(100, 999)}",
            "payroll": "clearing:payroll",
        }[kind]
        text = _DESC[kind] + (f" {inv}" if inv else "")
        line = _L(self.key(), on, amount, ref, kind, offset, text, inv, ocy, oamt)
        self.ledger.append(line)
        return line

    def bank_line(self, on: date, amount: int, reference: str, type_code: str, text: str) -> _B:
        b = _B(self.key(), min(on, self.end), amount, reference, type_code, text)
        self.bank.append(b)
        return b

    def shift(self, on: date, difficulty: float) -> date:
        max_shift = 0 if self.rng.random() > 0.3 + 0.7 * difficulty else 2
        return min(self.end, on + timedelta(days=self.rng.randint(0, max_shift)))


_PREFIX: dict[str, str] = {
    "customer_receipt": "RCV",
    "processor_settlement": "STL",
    "vendor_payment": "PAY",
    "payroll": "PRL",
}
_DESC: dict[str, str] = {
    "customer_receipt": "RECEIPT",
    "processor_settlement": "CARD SETTLEMENT BATCH",
    "vendor_payment": "SUPPLIER PAYMENT",
    "payroll": "PAYROLL",
}


def _type_code(amount: int) -> str:
    return "RCPT" if amount >= 0 else "PYMT"


def _clean(d: _Draft, difficulty: float) -> None:
    kind: LineKind = d.rng.choice(
        (
            "customer_receipt",
            "customer_receipt",
            "processor_settlement",
            "vendor_payment",
            "payroll",
        )
    )
    sign = 1 if kind in ("customer_receipt", "processor_settlement") else -1
    on = d.day()
    lg = d.ledger_line(on, sign * d.amount(), kind)
    b = d.bank_line(
        d.shift(on, difficulty), lg.amount, lg.reference, _type_code(lg.amount), lg.description
    )
    d.clean.append((b.key, lg.key))


def _s(*b: _B) -> tuple[str, ...]:
    """Placeholder statement ids, renamed once lines are sorted."""
    return tuple(f"@b{x.key}" for x in b)


def _l(*lines: _L) -> tuple[str, ...]:
    return tuple(f"@l{x.key}" for x in lines)


def _inject(d: _Draft, kind: BreakKind, difficulty: float) -> None:
    rng = d.rng
    match kind:
        case K.TIMING:
            on = d.end - timedelta(days=rng.randint(0, TIMING_DAYS - 1))
            kind_: LineKind = rng.choice(("customer_receipt", "vendor_payment"))
            sign = 1 if kind_ == "customer_receipt" else -1
            lg = d.ledger_line(on, sign * d.amount(), kind_)
            d.breaks.append((kind, [Treatment(timing=_l(lg))]))
        case K.DUPLICATE_LEDGER:
            on = d.day()
            l1 = d.ledger_line(on, d.amount(), "customer_receipt")
            l2 = d.ledger_line(
                on + timedelta(days=rng.randint(0, 1)),
                l1.amount,
                "customer_receipt",
                reference=l1.reference,
                invoice_id=l1.invoice_id,
            )
            b = d.bank_line(
                d.shift(on, difficulty), l1.amount, l1.reference, "RCPT", l1.description
            )
            alts = [
                Treatment(
                    match=(_s(b), _l(keep)),
                    adjustments=(
                        Adjustment(
                            kind="duplicate_reversal",
                            amount_minor=-drop.amount,
                            offset_account=drop.offset,
                            statement_line_ids=_s(b),
                            ledger_line_ids=_l(drop),
                        ),
                    ),
                )
                for keep, drop in ((l1, l2), (l2, l1))
            ]
            d.breaks.append((kind, alts))
        case K.DUPLICATE_BANK:
            on = d.day()
            lg = d.ledger_line(on, -d.amount(), "vendor_payment")
            b1 = d.bank_line(on, lg.amount, lg.reference, "PYMT", lg.description)
            b2 = d.bank_line(
                on + timedelta(days=rng.randint(0, 1)),
                lg.amount,
                lg.reference,
                "PYMT",
                lg.description,
            )
            alts = [
                Treatment(match=(_s(keep), _l(lg)), escalate_statement=_s(drop))
                for keep, drop in ((b1, b2), (b2, b1))
            ]
            d.breaks.append((kind, alts))
        case K.FX:
            ocy = rng.choice([c for c in FOREIGN if c != d.ccy])
            oamt = (
                rng.randint(10_000, 2_000_000) if ocy == "JPY" else rng.randint(100, 15_000) * 100
            )
            on = d.day()
            for _ in range(10):
                when = min(d.end, on + timedelta(days=rng.randint(1, 3)))
                booked = d.fx.convert(oamt, ocy, d.ccy, on)
                settled = d.fx.convert(oamt, ocy, d.ccy, when)
                if booked != settled:
                    break
                on = d.day()
            lg = d.ledger_line(on, booked, "customer_receipt", ocy=ocy, oamt=oamt)
            b = d.bank_line(when, settled, lg.reference, "RCPT", lg.description)
            adj = Adjustment(
                kind="fx",
                amount_minor=settled - booked,
                offset_account=FX,
                statement_line_ids=_s(b),
            )
            d.breaks.append((kind, [Treatment(match=(_s(b), _l(lg)), adjustments=(adj,))]))
        case K.FEE_NET:
            on = d.day()
            lg = d.ledger_line(on, d.amount(1_000, 40_000), "processor_settlement")
            fee = round(lg.amount * rng.uniform(0.015, 0.03))  # AR-4
            b = d.bank_line(
                d.shift(on, difficulty), lg.amount - fee, lg.reference, "RCPT", lg.description
            )
            adj = Adjustment(
                kind="fee", amount_minor=-fee, offset_account=FEE, statement_line_ids=_s(b)
            )
            d.breaks.append((kind, [Treatment(match=(_s(b), _l(lg)), adjustments=(adj,))]))
        case K.FEE_LINE:
            fee = rng.randint(5, 75) * 100 + rng.choice((0, 50))
            b = d.bank_line(d.day(), -fee, d.ref("FEE"), "FEE", "ACCOUNT SERVICE CHARGE")
            adj = Adjustment(
                kind="fee", amount_minor=-fee, offset_account=FEE, statement_line_ids=_s(b)
            )
            d.breaks.append((kind, [Treatment(adjustments=(adj,))]))
        case K.MISSING_IDENTIFIED:
            inv = Invoice(
                invoice_id=d.ref("INV"),
                amount_minor=d.amount(),
                currency=d.ccy,
                customer=f"SYNTH CUSTOMER {rng.randint(10, 99)}",
            )
            d.invoices.append(inv)
            b = d.bank_line(
                d.day(), inv.amount_minor, inv.invoice_id, "RCPT", f"RECEIPT {inv.invoice_id}"
            )
            adj = Adjustment(
                kind="missing",
                amount_minor=inv.amount_minor,
                offset_account=f"ar:{inv.invoice_id}",
                statement_line_ids=_s(b),
            )
            d.breaks.append((kind, [Treatment(adjustments=(adj,))]))
        case K.MISSING_UNIDENTIFIED:
            amount = d.amount()
            if rng.random() < difficulty:  # same amount as an open invoice: a trap
                decoy = Invoice(
                    invoice_id=d.ref("INV"),
                    amount_minor=amount,
                    currency=d.ccy,
                    customer=f"SYNTH CUSTOMER {rng.randint(10, 99)}",
                )
                d.invoices.append(decoy)
            b = d.bank_line(d.day(), amount, d.ref("E2E"), "RCPT", "INCOMING TRANSFER")
            d.breaks.append((kind, [Treatment(escalate_statement=_s(b))]))
        case K.MISSING_BANK:
            on = d.day(0, MISSING_BANK_MIN_DAYS)
            lg = d.ledger_line(on, d.amount(), "customer_receipt")
            d.breaks.append((kind, [Treatment(escalate_ledger=_l(lg))]))
        case K.PARTIAL_SPLIT:
            on = d.day()
            lg = d.ledger_line(on, d.amount(200, 30_000), "processor_settlement")
            p1 = round(lg.amount * rng.uniform(0.2, 0.45))
            b1 = d.bank_line(on, p1, lg.reference, "RCPT", lg.description)
            b2 = d.bank_line(
                on + timedelta(days=1), lg.amount - p1, lg.reference, "RCPT", lg.description
            )
            d.breaks.append((kind, [Treatment(match=(_s(b1, b2), _l(lg)))]))
        case K.PARTIAL_SHORT:
            on = d.day()
            lg = d.ledger_line(on, d.amount(200, 20_000), "customer_receipt")
            paid = round(lg.amount * rng.uniform(0.4, 0.9))
            b = d.bank_line(d.shift(on, difficulty), paid, lg.reference, "RCPT", lg.description)
            adj = Adjustment(
                kind="partial",
                amount_minor=paid - lg.amount,
                offset_account=lg.offset,
                statement_line_ids=_s(b),
                ledger_line_ids=_l(lg),
            )
            d.breaks.append((kind, [Treatment(match=(_s(b), _l(lg)), adjustments=(adj,))]))


def _rename(t: Treatment, ids: dict[str, str]) -> Treatment:
    def r(xs: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(ids[x] for x in xs))

    return Treatment(
        match=None if t.match is None else (r(t.match[0]), r(t.match[1])),
        adjustments=tuple(
            a.model_copy(
                update={
                    "statement_line_ids": r(a.statement_line_ids),
                    "ledger_line_ids": r(a.ledger_line_ids),
                }
            )
            for a in t.adjustments
        ),
        timing=r(t.timing),
        escalate_statement=r(t.escalate_statement),
        escalate_ledger=r(t.escalate_ledger),
    )


def _lines_of(t: Treatment) -> tuple[set[str], set[str]]:
    s = set(t.escalate_statement) | {x for a in t.adjustments for x in a.statement_line_ids}
    lg = (
        set(t.timing)
        | set(t.escalate_ledger)
        | {x for a in t.adjustments for x in a.ledger_line_ids}
    )
    if t.match is not None:
        s |= set(t.match[0])
        lg |= set(t.match[1])
    return s, lg


def generate_recon_case(
    seed: int,
    difficulty: float = 0.5,
    fmt: str | None = None,
    kinds: tuple[BreakKind | str, ...] | None = None,
    case_id: str | None = None,
) -> ReconCase:
    if not 0.0 <= difficulty <= 1.0:
        raise ValueError("difficulty must be in [0, 1]")
    forced = None if kinds is None else tuple(BreakKind(k) for k in kinds)
    key = f"recon:{seed}:{difficulty:.6f}:{fmt}:{forced}"
    rng = random.Random(key)
    start = BASE + timedelta(days=7 * rng.randrange(40))
    ccy = rng.choice(("USD", "USD", "EUR"))
    d = _Draft(rng, start, start + timedelta(days=PERIOD_DAYS - 1), ccy, FxTable(seed))
    n_clean = 6 + round(10 * difficulty) + rng.randint(0, 4)
    n_breaks = 1 + round(5 * difficulty) + rng.randint(0, 2)
    chosen = (
        list(forced) if forced is not None else [rng.choice(BREAK_KINDS) for _ in range(n_breaks)]
    )
    for _ in range(n_clean):
        _clean(d, difficulty)
    for k in chosen:
        _inject(d, k, difficulty)
    for _ in range(rng.randint(0, 3)):  # distractors: open invoices nobody paid
        d.invoices.append(
            Invoice(
                invoice_id=d.ref("INV"),
                amount_minor=d.amount(),
                currency=ccy,
                customer=f"SYNTH CUSTOMER {rng.randint(10, 99)}",
            )
        )

    bank = sorted(d.bank, key=lambda b: (b.on, b.key))
    ledger = sorted(d.ledger, key=lambda x: (x.booked_on, x.key))
    ids = {f"@b{b.key}": f"S-{i:03d}" for i, b in enumerate(bank, start=1)}
    ids |= {f"@l{x.key}": f"B-{i:03d}" for i, x in enumerate(ledger, start=1)}
    bank_lines = tuple(
        BankLine(
            line_id=ids[f"@b{b.key}"],
            booking_date=b.on,
            amount_minor=b.amount,
            reference=b.reference,
            bank_ref=f"BK{rng.randint(10**7, 10**8 - 1)}",
            type_code=b.type_code,
            description=b.description,
        )
        for b in bank
    )
    ledger_lines = tuple(
        LedgerLine(
            line_id=ids[f"@l{x.key}"],
            booked_on=x.booked_on,
            amount_minor=x.amount,
            reference=x.reference,
            kind=x.kind,
            offset_account=x.offset,
            description=x.description,
            invoice_id=x.invoice_id,
            original_currency=x.ocy,
            original_amount_minor=x.oamt,
        )
        for x in ledger
    )
    breaks = []
    for i, (k, alts) in enumerate(d.breaks, start=1):
        named = [_rename(t, ids) for t in alts]
        s, lg = _lines_of(named[0])
        breaks.append(
            Break(
                break_id=f"BRK-{i}",
                kind=k,
                statement_line_ids=tuple(sorted(s)),
                ledger_line_ids=tuple(sorted(lg)),
                expected=named[0],
                alternatives=tuple(named[1:]),
            )
        )
    clean = tuple(sorted((ids[f"@b{b}"], ids[f"@l{x}"]) for b, x in d.clean))
    statement_format = fmt or rng.choice(("camt053", "bai2"))
    opening = rng.randint(50_000, 500_000) * 100
    digits = f"{rng.randint(10**7, 10**8 - 1)}"
    if statement_format == "camt053":
        account = f"ACCT-{digits}"
        sid = f"STMT-{sha256_hex(key)[:10].upper()}"
        text = write_camt053(sid, account, ccy, (d.start, d.end), opening, bank_lines)
    elif statement_format == "bai2":
        account = digits
        text = write_bai2("SYNTHBANK", account, ccy, d.end, opening, bank_lines)
    else:
        raise ValueError(f"unknown statement format {statement_format!r}")
    return ReconCase(
        case_id=case_id or "rec-" + sha256_hex(key)[:12],
        account_id=account,
        currency=ccy,  # type: ignore[arg-type]
        period_start=d.start,
        period_end=d.end,
        opening_balance_minor=opening,
        statement_format=statement_format,  # type: ignore[arg-type]
        statement_file=text,
        bank_lines=bank_lines,
        ledger_lines=ledger_lines,
        invoices=tuple(sorted(d.invoices, key=lambda i: i.invoice_id)),
        difficulty=difficulty,
        truth=ReconTruth(breaks=tuple(breaks), clean=clean),
    )


__all__ = [
    "FEE",
    "FX",
    "MISSING_BANK_MIN_DAYS",
    "PERIOD_DAYS",
    "TIMING_DAYS",
    "generate_recon_case",
]
