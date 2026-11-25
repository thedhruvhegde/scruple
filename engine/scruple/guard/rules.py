"""Scruple Guard: a deterministic pre-flight check of one agent payment against the
user's signed mandate.

    authorize(mandate, request, state, registry, key=...) -> Decision

returns `allow`, `deny` or `step_up` (the user must approve this payment themselves)
with the result of every rule, so the decision can be explained and audited. It reads
nothing but its arguments: no clock (the request carries its time), no network, no
randomness. `GuardState` is the spend history the decision depends on; it is rebuilt
from the ledger of authorised payments (`GuardState.from_payments`), never taken
from the agent.

Rules, in the order they run (every rule runs; the verdict is the worst outcome):

| id      | checks                                                          | on failure |
|---------|-----------------------------------------------------------------|------------|
| G-SIG   | the mandate's signature verifies (A-G2)                         | deny       |
| G-BIND  | the request names this mandate and the agent the mandate binds  | deny       |
| G-TIME  | issued_at <= request time < expires_at                          | deny       |
| G-CUR   | the request's currency is the mandate's                         | deny       |
| G-CART  | amount = lines + shipping + tax; every line's category is in    | deny       |
|         | the intent, at most its unit price, and the quantity bought so  |            |
|         | far plus this one at most the intent's                          |            |
| G-TXN   | amount <= the per-payment cap                                   | deny       |
| G-CUM   | gross spend so far + amount <= the cumulative cap (A-G4)        | deny       |
| G-MER   | the merchant is in the registry, in the allow-list if one is    | deny       |
|         | given, and its category code is allowed and not blocked; the    |            |
|         | request's MCC is the registry's                                 |            |
| G-BEN   | the payee account is the registry's account for the merchant    | step_up    |
| G-REC   | a recurring payment is allowed by the recurrence terms          | deny       |
|         | (merchant, amount, interval since the last one, count)          |            |
| G-DUP   | the idempotency key is new (else deny: a replay); no payment    | deny /     |
|         | with the same cart to the same merchant in the duplicate window | step_up    |
|         | (else step_up: maybe a double charge)                           |            |
| G-STEP  | amount <= step_up_above, and the payments to this merchant in   | step_up    |
|         | the last 24 hours plus this one <= step_up_above (a split       |            |
|         | purchase under the threshold is still stepped up)               |            |

A `step_up` decision becomes `allow` only with `Approval` signed by the user over the
request's digest (`finalize`). The prompt the user sees is rendered by the Guard from
the request (`approval_prompt`), never written by the agent: an agent cannot describe
a payment as something else to get it approved.

Thresholds and outcomes (which failures deny and which step up) are ASSUMPTIONS,
ours; docs/guard/GUARD.md lists them.
"""

from __future__ import annotations

import hmac
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Literal

from pydantic import BaseModel, ConfigDict

from scruple.guard.mandate import Mandate, MerchantRecord, PaymentRequest, signature_ok
from scruple.hashing import canonical_json, content_hash

_FROZEN = ConfigDict(frozen=True, extra="forbid")
Verdict = Literal["allow", "step_up", "deny"]
RANK: dict[str, int] = {"allow": 0, "step_up": 1, "deny": 2}
RULE_IDS = (
    "G-SIG",
    "G-BIND",
    "G-TIME",
    "G-CUR",
    "G-CART",
    "G-TXN",
    "G-CUM",
    "G-MER",
    "G-BEN",
    "G-REC",
    "G-DUP",
    "G-STEP",
)
SPLIT_WINDOW = timedelta(hours=24)


class RuleResult(BaseModel):
    model_config = _FROZEN

    rule: str
    outcome: Verdict
    detail: str


class AuthorisedPayment(BaseModel):
    """A payment the Guard let through (allow, or step_up then approved)."""

    model_config = _FROZEN

    request_id: str
    idempotency_key: str
    merchant_id: str
    amount_minor: int
    cart_digest: str
    at: datetime
    recurring: bool = False
    quantities: dict[str, int] = {}  # category -> units


class GuardState(BaseModel):
    """Spend history under one mandate, oldest first."""

    model_config = _FROZEN

    mandate_id: str
    payments: tuple[AuthorisedPayment, ...] = ()

    @classmethod
    def from_payments(cls, mandate_id: str, payments: Iterable[AuthorisedPayment]) -> GuardState:
        return cls(mandate_id=mandate_id, payments=tuple(sorted(payments, key=lambda p: p.at)))

    @property
    def spent_minor(self) -> int:
        return sum(p.amount_minor for p in self.payments)

    def bought(self, category: str) -> int:
        return sum(p.quantities.get(category, 0) for p in self.payments)

    def after(self, request: PaymentRequest) -> GuardState:
        return self.model_copy(update={"payments": (*self.payments, authorised(request))})

    def digest(self) -> str:
        return content_hash(self.model_dump(mode="json"))


def authorised(request: PaymentRequest) -> AuthorisedPayment:
    qty: dict[str, int] = {}
    for line in request.lines:
        qty[line.category] = qty.get(line.category, 0) + line.quantity
    return AuthorisedPayment(
        request_id=request.request_id,
        idempotency_key=request.idempotency_key,
        merchant_id=request.merchant_id,
        amount_minor=request.amount_minor,
        cart_digest=request.cart_digest(),
        at=request.at,
        recurring=request.recurring,
        quantities=qty,
    )


class Decision(BaseModel):
    model_config = _FROZEN

    verdict: Verdict
    rules: tuple[RuleResult, ...]
    request_id: str
    request_digest: str
    mandate_id: str
    mandate_digest: str
    state_digest: str
    at: datetime

    @property
    def reasons(self) -> tuple[RuleResult, ...]:
        """The rules that did not allow, worst first."""
        bad = [r for r in self.rules if r.outcome != "allow"]
        return tuple(sorted(bad, key=lambda r: -RANK[r.outcome]))

    def digest(self) -> str:
        return content_hash(self.model_dump(mode="json"))


def _r(rule: str, ok: bool, detail: str, fail: Verdict = "deny") -> RuleResult:
    return RuleResult(rule=rule, outcome="allow" if ok else fail, detail=detail)


def _cart(m: Mandate, req: PaymentRequest, st: GuardState) -> RuleResult:
    problems: list[str] = []
    if req.cart_minor() != req.amount_minor:
        problems.append(f"amount {req.amount_minor} but the cart totals {req.cart_minor()}")
    if m.items:
        if not req.lines:
            problems.append("no cart lines against an itemised mandate")
        qty: dict[str, int] = {}
        for line in req.lines:
            qty[line.category] = qty.get(line.category, 0) + line.quantity
            item = m.item(line.category)
            if item is None:
                problems.append(f"{line.sku}: category {line.category} is not in the intent")
            elif line.unit_minor > item.max_unit_minor:
                problems.append(
                    f"{line.sku}: unit {line.unit_minor} above the intent's {item.max_unit_minor}"
                )
        for cat, n in qty.items():
            item = m.item(cat)
            if item is not None and st.bought(cat) + n > item.max_quantity:
                problems.append(
                    f"{cat}: {st.bought(cat)} bought + {n} above the intent's {item.max_quantity}"
                )
    return _r("G-CART", not problems, "; ".join(problems) or "cart matches the intent")


def _merchant(
    m: Mandate, req: PaymentRequest, registry: Mapping[str, MerchantRecord]
) -> RuleResult:
    rec = registry.get(req.merchant_id)
    if rec is None:
        return _r("G-MER", False, f"merchant {req.merchant_id} is not in the registry")
    problems: list[str] = []
    if m.allowed_merchants and req.merchant_id not in m.allowed_merchants:
        problems.append(f"merchant {req.merchant_id} is not in the mandate's allow-list")
    if req.mcc != rec.mcc:
        problems.append(f"request MCC {req.mcc} but the registry has {rec.mcc}")
    if m.allowed_mccs and rec.mcc not in m.allowed_mccs:
        problems.append(f"MCC {rec.mcc} is not allowed")
    if rec.mcc in m.blocked_mccs:
        problems.append(f"MCC {rec.mcc} is blocked")
    return _r("G-MER", not problems, "; ".join(problems) or "merchant allowed")


def _beneficiary(req: PaymentRequest, registry: Mapping[str, MerchantRecord]) -> RuleResult:
    rec = registry.get(req.merchant_id)
    if rec is None:
        return _r("G-BEN", False, "no registry record to compare the payee with")
    ok = req.payee_account == rec.payee_account
    why = (
        "payee is the account on file"
        if ok
        else f"payee {req.payee_account} is not the account on file for {req.merchant_id}"
    )
    return _r("G-BEN", ok, why, fail="step_up")


def _recurrence(m: Mandate, req: PaymentRequest, st: GuardState) -> RuleResult:
    if not req.recurring:
        return _r("G-REC", True, "not a recurring payment")
    t = m.recurrence
    if t is None:
        return _r("G-REC", False, "the mandate does not allow recurring payments")
    prior = [p for p in st.payments if p.recurring and p.merchant_id == t.merchant_id]
    problems: list[str] = []
    if req.merchant_id != t.merchant_id:
        problems.append(f"recurring payments are allowed to {t.merchant_id} only")
    if req.amount_minor > t.max_amount_minor:
        problems.append(f"amount above the recurring maximum {t.max_amount_minor}")
    if len(prior) >= t.max_occurrences:
        problems.append(f"{len(prior)} recurring payments already (max {t.max_occurrences})")
    if prior and req.at - prior[-1].at < timedelta(days=t.min_interval_days):
        problems.append(f"less than {t.min_interval_days} days since the last one")
    return _r("G-REC", not problems, "; ".join(problems) or "within the recurrence terms")


def _duplicate(m: Mandate, req: PaymentRequest, st: GuardState) -> RuleResult:
    if any(p.idempotency_key == req.idempotency_key for p in st.payments):
        return _r("G-DUP", False, f"idempotency key {req.idempotency_key} already used (replay)")
    window = timedelta(hours=m.duplicate_window_hours)
    digest = req.cart_digest()
    same = [
        p
        for p in st.payments
        if p.cart_digest == digest and timedelta(0) <= req.at - p.at <= window and not p.recurring
    ]
    if same and not req.recurring:
        return _r(
            "G-DUP",
            False,
            f"same cart to {req.merchant_id} already paid ({same[-1].request_id}) "
            f"within {m.duplicate_window_hours}h",
            fail="step_up",
        )
    return _r("G-DUP", True, "no duplicate")


def _step(m: Mandate, req: PaymentRequest, st: GuardState) -> RuleResult:
    lim = m.step_up_above_minor
    if lim is None:
        return _r("G-STEP", True, "no step-up threshold")
    if req.amount_minor > lim:
        return _r("G-STEP", False, f"amount above the step-up threshold {lim}", "step_up")
    recent = sum(
        p.amount_minor
        for p in st.payments
        if p.merchant_id == req.merchant_id and timedelta(0) <= req.at - p.at <= SPLIT_WINDOW
    )
    if recent and recent + req.amount_minor > lim:
        return _r(
            "G-STEP",
            False,
            f"{recent} to {req.merchant_id} in the last 24h plus this payment exceeds "
            f"the step-up threshold {lim} (split purchase)",
            "step_up",
        )
    return _r("G-STEP", True, "below the step-up threshold")


def evaluate_rules(
    mandate: Mandate,
    request: PaymentRequest,
    state: GuardState,
    registry: Mapping[str, MerchantRecord],
    *,
    key: str | bytes,
) -> tuple[RuleResult, ...]:
    m, req, st = mandate, request, state
    return (
        _r(
            "G-SIG",
            signature_ok(m, key),
            "signature verifies" if signature_ok(m, key) else "mandate signature does not verify",
        ),
        _r(
            "G-BIND",
            req.mandate_id == m.mandate_id and req.agent_id == m.agent_id,
            f"request for mandate {req.mandate_id} by {req.agent_id}; mandate "
            f"{m.mandate_id} binds {m.agent_id}",
        ),
        _r(
            "G-TIME",
            m.issued_at <= req.at < m.expires_at,
            f"request at {req.at.isoformat()}, mandate valid {m.issued_at.isoformat()} to "
            f"{m.expires_at.isoformat()}",
        ),
        _r("G-CUR", req.currency == m.currency, f"{req.currency} against {m.currency}"),
        _cart(m, req, st),
        _r(
            "G-TXN",
            req.amount_minor <= m.per_txn_cap_minor,
            f"amount {req.amount_minor}, per-payment cap {m.per_txn_cap_minor}",
        ),
        _r(
            "G-CUM",
            st.spent_minor + req.amount_minor <= m.cumulative_cap_minor,
            f"spent {st.spent_minor} + {req.amount_minor}, cumulative cap {m.cumulative_cap_minor}",
        ),
        _merchant(m, req, registry),
        _beneficiary(req, registry),
        _recurrence(m, req, st),
        _duplicate(m, req, st),
        _step(m, req, st),
    )


def authorize(
    mandate: Mandate,
    request: PaymentRequest,
    state: GuardState,
    registry: Mapping[str, MerchantRecord],
    *,
    key: str | bytes,
) -> Decision:
    if state.mandate_id != mandate.mandate_id:
        raise ValueError("the state belongs to a different mandate")
    rules = evaluate_rules(mandate, request, state, registry, key=key)
    verdict: Verdict = max((r.outcome for r in rules), key=lambda o: RANK[o])
    return Decision(
        verdict=verdict,
        rules=rules,
        request_id=request.request_id,
        request_digest=request.digest(),
        mandate_id=mandate.mandate_id,
        mandate_digest=mandate.digest(),
        state_digest=state.digest(),
        at=request.at,
    )


# --- step-up -----------------------------------------------------------------------------


def approval_prompt(request: PaymentRequest, decision: Decision) -> dict[str, object]:
    """What the user is shown, rendered from the request by the Guard (trusted display)."""
    return {
        "merchant_id": request.merchant_id,
        "payee_account": request.payee_account,
        "amount_minor": request.amount_minor,
        "currency": request.currency,
        "lines": [line.model_dump(mode="json") for line in request.lines],
        "recurring": request.recurring,
        "why": [r.detail for r in decision.reasons],
        "request_digest": decision.request_digest,
    }


class Approval(BaseModel):
    """The user's answer to a step-up, signed over the request digest."""

    model_config = _FROZEN

    request_digest: str
    approved: bool
    signature: str

    @classmethod
    def sign(cls, request_digest: str, approved: bool, key: str | bytes) -> Approval:
        return cls(
            request_digest=request_digest,
            approved=approved,
            signature=_approval_mac(request_digest, approved, key),
        )


def _approval_mac(digest: str, approved: bool, key: str | bytes) -> str:
    k = key.encode() if isinstance(key, str) else key
    body = canonical_json({"request_digest": digest, "approved": approved})
    return hmac.new(k, body.encode(), sha256).hexdigest()


def approval_ok(approval: Approval, decision: Decision, key: str | bytes) -> bool:
    return approval.request_digest == decision.request_digest and hmac.compare_digest(
        approval.signature, _approval_mac(approval.request_digest, approval.approved, key)
    )


def finalize(decision: Decision, approval: Approval | None, *, key: str | bytes) -> Verdict:
    """The verdict once a step-up has been answered: deny stays deny; step_up becomes
    allow only with a valid, approving signature over this request."""
    if decision.verdict != "step_up":
        return decision.verdict
    if approval is None or not approval_ok(approval, decision, key) or not approval.approved:
        return "deny"
    return "allow"


def replay_spend(
    mandate: Mandate,
    requests: Sequence[tuple[PaymentRequest, Approval | None]],
    registry: Mapping[str, MerchantRecord],
    *,
    key: str | bytes,
) -> list[tuple[Decision, Verdict]]:
    """Run the Guard over a sequence of requests from an empty state, updating the
    state with each payment it lets through. A verifier uses this to re-check what
    the environment's Guard said (defence in depth)."""
    st = GuardState(mandate_id=mandate.mandate_id)
    out: list[tuple[Decision, Verdict]] = []
    for req, appr in requests:
        d = authorize(mandate, req, st, registry, key=key)
        v = finalize(d, appr, key=key)
        out.append((d, v))
        if v == "allow":
            st = st.after(req)
    return out


__all__ = [
    "RANK",
    "RULE_IDS",
    "Approval",
    "AuthorisedPayment",
    "Decision",
    "GuardState",
    "RuleResult",
    "Verdict",
    "approval_ok",
    "approval_prompt",
    "authorised",
    "authorize",
    "evaluate_rules",
    "finalize",
    "replay_spend",
]
