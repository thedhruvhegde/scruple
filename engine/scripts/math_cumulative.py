"""Seeded evidence for docs/math/10_cumulative_spend.md (synthetic).

1. Random adversarial request streams against the Guard: gross authorised spend never
   exceeds the cap (Theorem 10.1), and un-stepped-up spend to one merchant in any
   24-hour window never exceeds the step-up threshold (Theorem 10.3); the
   multi-merchant construction attains M * T (Corollary 10.4).
2. Headroom accounting (Proposition 10.2): with refunds credited when claimed, a
   buy-claim-buy loop drives exposure to k * a; with gross accounting it stops at C.
3. The anytime-valid violation-rate certificate (Theorem 10.5) on commerce episodes:
   how many clean episodes certify p <= eps, and how fast a violating agent is caught.

    uv run python scripts/math_cumulative.py
"""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta

from scruple.guard import (
    Approval,
    CartLine,
    GuardState,
    Mandate,
    MerchantRecord,
    PaymentRequest,
    authorize,
    finalize,
    sign_mandate,
)

KEY = "math-cumulative-key-0000"
T0 = datetime(2026, 6, 1, tzinfo=UTC)


def mandate(cap: int, txn: int, step: int, merchants: int) -> Mandate:
    return sign_mandate(
        Mandate(
            mandate_id="md",
            principal_id="u",
            agent_id="a",
            currency="USD",
            issued_at=T0,
            expires_at=T0 + timedelta(days=30),
            per_txn_cap_minor=txn,
            cumulative_cap_minor=cap,
            step_up_above_minor=step,
            allowed_merchants=tuple(f"m{i}" for i in range(merchants)),
        ),
        KEY,
    )


def registry(n: int) -> dict[str, MerchantRecord]:
    return {
        f"m{i}": MerchantRecord(merchant_id=f"m{i}", mcc="5999", payee_account=f"acct-{i}")
        for i in range(n)
    }


def request(
    i: int, merchant: str, amount: int, at: datetime, payee: str | None = None
) -> PaymentRequest:
    return PaymentRequest(
        request_id=f"r{i}",
        mandate_id="md",
        agent_id="a",
        merchant_id=merchant,
        mcc="5999",
        payee_account=payee or f"acct-{merchant[1:]}",
        currency="USD",
        amount_minor=amount,
        lines=(CartLine(sku="x", category="any", quantity=1, unit_minor=amount),),
        idempotency_key=f"k{i}",
        at=at,
    )


def adversarial_streams(reps: int, seed: int) -> None:
    rng = random.Random(seed)
    worst_cap = 0.0
    worst_window = 0.0
    for _ in range(reps):
        n_m = rng.randint(1, 4)
        cap, step = rng.randint(5, 50) * 1_000, rng.randint(2, 20) * 500
        m = mandate(cap, cap, step, n_m)
        reg = registry(n_m)
        st = GuardState(mandate_id="md")
        t = T0
        plain: list[tuple[datetime, str, int]] = []  # allowed without a step-up
        for i in range(rng.randint(5, 60)):
            t += timedelta(minutes=rng.choice((1, 5, 60, 600, 1_500)))
            if t >= m.expires_at:
                break
            r = request(i, f"m{rng.randrange(n_m)}", rng.randint(1, step * 2), t)
            d = authorize(m, r, st, reg, key=KEY)
            appr = Approval.sign(d.request_digest, rng.random() < 0.7, KEY)
            v = finalize(d, appr, key=KEY)
            if v == "allow":
                st = st.after(r)
                if d.verdict == "allow":
                    plain.append((t, r.merchant_id, r.amount_minor))
        worst_cap = max(worst_cap, st.spent_minor / cap)
        for t1, mer, _ in plain:
            s = sum(a for t2, m2, a in plain if m2 == mer and t1 <= t2 <= t1 + timedelta(hours=24))
            worst_window = max(worst_window, s / step)
    print(f"## {reps} random adversarial streams (seed {seed})")
    print(f"max gross spend / cap                                   : {worst_cap:.4f} (bound 1)")
    print(f"max un-stepped-up spend to one merchant in 24h / thresh : {worst_window:.4f} (bound 1)")


def multi_merchant(n_m: int, step: int) -> None:
    m = mandate(10**7, step, step, n_m)
    reg = registry(n_m)
    st = GuardState(mandate_id="md")
    total = 0
    for i in range(n_m):
        r = request(i, f"m{i}", step, T0 + timedelta(minutes=i + 1))
        d = authorize(m, r, st, reg, key=KEY)
        assert d.verdict == "allow"
        st = st.after(r)
        total += step
    print(f"## {n_m} merchants, threshold {step}: un-stepped-up in one hour = {total} = M*T")


def headroom(cap: int, a: int, rounds: int) -> None:
    """Exposure under request-credited headroom (the refused-refund loop) vs gross."""
    exposure_request_credit = 0
    headroom_left = cap
    for _ in range(rounds):
        if a <= headroom_left:
            exposure_request_credit += a  # pay
            headroom_left -= a
            headroom_left += a  # claim a refund: credited at once; the merchant refuses it
    gross_exposure = min(cap, a * rounds) // a * a
    print(
        f"## headroom: cap {cap}, payment {a}, {rounds} rounds: exposure with "
        f"request-credited headroom {exposure_request_credit}, with gross accounting "
        f"{gross_exposure}"
    )


def certificate(eps: float, alpha: float) -> None:
    lam = 1 / (1 - eps)
    n = math.ceil(math.log(1 / alpha) / -math.log(1 - eps))
    print(f"## certificate p <= {eps} at alpha {alpha}: {n} clean episodes with lambda = 1/(1-eps)")
    # a grid mixture over lambda in (0, 1/(1-eps)]: robust to a few violations
    n_grid = 20
    grid = [lam * j / n_grid for j in range(1, n_grid + 1)]

    def wealth(seq: list[int]) -> float:
        return sum(math.prod(1 + g * (eps - v) for v in seq) for g in grid) / n_grid

    for at in ((), (50,), (50, 150, 250)):
        k = 0
        seq: list[int] = []
        while wealth(seq) < 1 / alpha and k < 200_000:
            seq.append(1 if k in at else 0)
            k += 1
        print(f"   mixture (J={n_grid}), violations at episodes {list(at)}: certified after {k}")
    # a violating agent: p = 10 eps; the certificate never fires (Ville)
    rng = random.Random(7)
    seq = [1 if rng.random() < 10 * eps else 0 for _ in range(20_000)]
    best = max(wealth(seq[:i]) for i in range(1, 20_000, 500))
    print(f"   p = {10 * eps}: max mixture wealth over 20,000 episodes {best:.3g} (< {1 / alpha})")
    n_obs = 3_000
    eps_cert = 1 - alpha ** (1 / n_obs)
    print(f"   {n_obs} clean episodes certify p <= {eps_cert:.5f} at alpha {alpha}")


def main() -> int:
    adversarial_streams(3_000, 20261125)
    multi_merchant(4, 10_000)
    headroom(30_000, 10_000, 50)
    certificate(0.01, 0.05)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
