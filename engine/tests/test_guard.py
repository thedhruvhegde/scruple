"""Scruple Guard: every rule, the verdict order, step-up approvals, the evidence chain,
the audit trail and the CLI."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from scruple.audit.log import AuditLog
from scruple.cli.main import main
from scruple.clock import SteppingClock
from scruple.guard import (
    Approval,
    CartLine,
    EvidenceChain,
    GuardState,
    IntentItem,
    Mandate,
    MerchantRecord,
    PaymentRequest,
    RecurrenceTerms,
    approval_prompt,
    authorize,
    finalize,
    record_to_audit,
    replay_spend,
    sign_mandate,
    signature_ok,
    verify_chain,
)
from scruple.storage import open_storage

KEY = "test-user-key-0123456789"
T0 = datetime(2026, 6, 1, tzinfo=UTC)
REG = {
    "m1": MerchantRecord(merchant_id="m1", mcc="5943", payee_account="acct-1"),
    "m2": MerchantRecord(merchant_id="m2", mcc="5943", payee_account="acct-2"),
    "m9": MerchantRecord(merchant_id="m9", mcc="7995", payee_account="acct-9"),
}


def mandate(**kw: object) -> Mandate:
    base: dict[str, object] = {
        "mandate_id": "md-1",
        "principal_id": "u1",
        "agent_id": "a1",
        "currency": "USD",
        "issued_at": T0,
        "expires_at": T0 + timedelta(days=7),
        "per_txn_cap_minor": 20_000,
        "cumulative_cap_minor": 30_000,
        "step_up_above_minor": 10_000,
        "allowed_mccs": ("5943",),
        "blocked_mccs": ("7995",),
        "items": (IntentItem(category="ink", max_quantity=4, max_unit_minor=4_000),),
    }
    base.update(kw)
    return sign_mandate(Mandate.model_validate(base), KEY)


_N = iter(range(10**9))


def req(**kw: object) -> PaymentRequest:
    n = next(_N)
    unit = int(kw.pop("unit", 2_000))  # type: ignore[call-overload]
    qty = int(kw.pop("qty", 2))  # type: ignore[call-overload]
    cat = str(kw.pop("category", "ink"))
    lines = (CartLine(sku=f"{cat}-sku", category=cat, quantity=qty, unit_minor=unit),)
    base: dict[str, object] = {
        "request_id": f"rq{n}",
        "mandate_id": "md-1",
        "agent_id": "a1",
        "merchant_id": "m1",
        "mcc": "5943",
        "payee_account": "acct-1",
        "currency": "USD",
        "amount_minor": unit * qty,
        "lines": lines,
        "idempotency_key": f"k{n}",
        "at": T0 + timedelta(hours=1),
    }
    base.update(kw)
    return PaymentRequest.model_validate(base)


def decide(m: Mandate, r: PaymentRequest, st_: GuardState | None = None) -> tuple[str, set[str]]:
    d = authorize(m, r, st_ or GuardState(mandate_id=m.mandate_id), REG, key=KEY)
    return d.verdict, {x.rule for x in d.reasons}


def test_a_plain_payment_is_allowed() -> None:
    assert decide(mandate(), req()) == ("allow", set())


@pytest.mark.parametrize(
    ("change", "rule", "verdict"),
    [
        ({"agent_id": "a2"}, "G-BIND", "deny"),
        ({"mandate_id": "md-2"}, "G-BIND", "deny"),
        ({"at": T0 + timedelta(days=8)}, "G-TIME", "deny"),
        ({"at": T0 - timedelta(hours=1)}, "G-TIME", "deny"),
        ({"currency": "EUR"}, "G-CUR", "deny"),
        ({"amount_minor": 4_100}, "G-CART", "deny"),  # amount is not the cart
        ({"category": "plan"}, "G-CART", "deny"),
        ({"unit": 4_500}, "G-CART", "deny"),
        ({"qty": 5}, "G-CART", "deny"),
        ({"merchant_id": "m7"}, "G-MER", "deny"),
        ({"merchant_id": "m9", "mcc": "7995", "payee_account": "acct-9"}, "G-MER", "deny"),
        ({"mcc": "5999"}, "G-MER", "deny"),
        ({"payee_account": "acct-evil"}, "G-BEN", "step_up"),
        ({"recurring": True}, "G-REC", "deny"),
    ],
)
def test_each_rule(change: dict[str, object], rule: str, verdict: str) -> None:
    got, rules = decide(mandate(), req(**change))
    assert got == verdict
    assert rule in rules


def test_signature_and_tampering() -> None:
    m = mandate()
    assert signature_ok(m, KEY) and not signature_ok(m, "another-key-0000000")
    raised = m.model_copy(update={"cumulative_cap_minor": 10**9})  # edited after signing
    assert decide(raised, req())[1] >= {"G-SIG"}
    assert decide(raised, req())[0] == "deny"
    unsigned = m.model_copy(update={"signature": ""})
    assert decide(unsigned, req())[0] == "deny"


def test_caps_per_payment_and_cumulative() -> None:
    m = mandate(items=(), step_up_above_minor=None)
    assert decide(m, req(unit=10_500, qty=2))[1] == {"G-TXN"}  # 21_000 > 20_000
    st_ = GuardState(mandate_id="md-1")
    first = req(unit=9_000, qty=2)  # 18_000
    st_ = st_.after(first)
    assert decide(m, req(unit=7_000, qty=2), st_)[1] == {"G-CUM"}  # 18k + 14k > 30k
    assert decide(m, req(unit=6_000, qty=2), st_)[0] == "allow"  # 30k exactly


def test_step_up_threshold_and_split_purchase() -> None:
    m = mandate()
    assert decide(m, req(unit=3_000, qty=4)) == ("step_up", {"G-STEP"})  # 12_000 > 10_000
    st_ = GuardState(mandate_id="md-1").after(req(unit=3_000, qty=3))  # 9_000
    verdict, rules = decide(m, req(unit=1_500, qty=1, merchant_id="m1"), st_)
    assert verdict == "step_up" and "G-STEP" in rules  # 9_000 + 1_500 > 10_000: a split
    assert decide(m, req(unit=1_000, qty=1), st_)[0] == "allow"  # exactly the threshold
    # another merchant does not add up with m1
    assert (
        decide(m, req(unit=1_000, qty=1, merchant_id="m2", payee_account="acct-2"), st_)[0]
        == "allow"
    )
    # a day later the window has passed
    later = req(unit=1_500, qty=1, at=T0 + timedelta(days=2))
    assert decide(m, later, st_)[0] == "allow"


def test_duplicates() -> None:
    m = mandate()
    first = req()
    st_ = GuardState(mandate_id="md-1").after(first)
    replay = req(idempotency_key=first.idempotency_key)
    assert decide(m, replay, st_) == ("deny", {"G-DUP"})
    same_cart = req(at=first.at + timedelta(hours=2))
    verdict, rules = decide(m, same_cart, st_)
    assert verdict == "step_up" and "G-DUP" in rules
    assert decide(m, req(at=first.at + timedelta(hours=30)), st_)[0] == "allow"


def test_quantity_counts_across_payments() -> None:
    m = mandate()
    st_ = GuardState(mandate_id="md-1").after(req(qty=3, at=T0 + timedelta(minutes=1)))
    assert "G-CART" in decide(m, req(qty=2, at=T0 + timedelta(days=2)), st_)[1]
    assert decide(m, req(qty=1, at=T0 + timedelta(days=2)), st_)[0] == "allow"


def test_recurrence_terms() -> None:
    terms = RecurrenceTerms(
        merchant_id="m1", max_amount_minor=5_000, min_interval_days=28, max_occurrences=2
    )
    m = mandate(recurrence=terms, expires_at=T0 + timedelta(days=200))
    first = req(recurring=True)
    assert decide(m, first)[0] == "allow"
    st_ = GuardState(mandate_id="md-1").after(first)
    soon = req(recurring=True, at=first.at + timedelta(days=10))
    assert "G-REC" in decide(m, soon, st_)[1]
    ok = req(recurring=True, at=first.at + timedelta(days=30))
    assert decide(m, ok, st_)[0] == "allow"
    st_ = st_.after(ok)
    third = req(recurring=True, at=first.at + timedelta(days=60))
    assert "G-REC" in decide(m, third, st_)[1]  # max 2
    other = req(recurring=True, merchant_id="m2", payee_account="acct-2")
    assert "G-REC" in decide(m, other)[1]
    big = req(recurring=True, unit=3_000, qty=2)
    assert "G-REC" in decide(m, big)[1]


def test_deny_beats_step_up() -> None:
    verdict, rules = decide(mandate(), req(payee_account="acct-evil", currency="EUR"))
    assert verdict == "deny" and {"G-BEN", "G-CUR"} <= rules


def test_mandate_validation() -> None:
    with pytest.raises(ValueError, match="expire"):
        mandate(expires_at=T0)
    with pytest.raises(ValueError, match="cumulative"):
        mandate(per_txn_cap_minor=40_000)
    with pytest.raises(ValueError):
        authorize(mandate(), req(), GuardState(mandate_id="other"), REG, key=KEY)


def test_step_up_needs_a_valid_signed_approval_for_this_request() -> None:
    m = mandate()
    r = req(payee_account="acct-evil")
    d = authorize(m, r, GuardState(mandate_id="md-1"), REG, key=KEY)
    assert d.verdict == "step_up"
    prompt = approval_prompt(r, d)
    assert prompt["payee_account"] == "acct-evil" and prompt["amount_minor"] == r.amount_minor
    assert finalize(d, None, key=KEY) == "deny"
    assert finalize(d, Approval.sign(d.request_digest, True, KEY), key=KEY) == "allow"
    assert finalize(d, Approval.sign(d.request_digest, False, KEY), key=KEY) == "deny"
    assert finalize(d, Approval.sign("0" * 64, True, KEY), key=KEY) == "deny"  # other request
    assert (
        finalize(d, Approval.sign(d.request_digest, True, "not-the-key-000000"), key=KEY) == "deny"
    )
    allowed = authorize(m, req(), GuardState(mandate_id="md-1"), REG, key=KEY)
    assert finalize(allowed, None, key=KEY) == "allow"


def test_evidence_chain_and_tamper_detection() -> None:
    m = mandate()
    chain = EvidenceChain()
    for r in (req(), req(payee_account="acct-evil"), req(currency="EUR")):
        d = authorize(m, r, GuardState(mandate_id="md-1"), REG, key=KEY)
        chain.append(d, d.verdict)
    recs = list(chain.records)
    assert verify_chain(recs) is None and verify_chain(recs, chain.head) is None
    assert verify_chain(recs, "0" * 64) == 0
    edited = recs[1].model_copy(update={"final": "allow"})
    assert verify_chain([recs[0], edited, recs[2]]) == 2
    assert verify_chain([recs[0], recs[2]]) == 2  # dropped
    assert verify_chain([recs[1], recs[0], recs[2]]) == 1  # reordered


def test_evidence_goes_into_the_audit_trail() -> None:
    audit = AuditLog(open_storage(":memory:"), SteppingClock())
    m = mandate()
    chain = EvidenceChain()
    d = authorize(m, req(), GuardState(mandate_id="md-1"), REG, key=KEY)
    entry = record_to_audit(audit, chain.append(d, d.verdict), "a1")
    assert entry.event_type == "guard.decision"
    assert entry.payload["decision"]["verdict"] == "allow"
    assert audit.verify().ok


@settings(max_examples=150, deadline=None)
@given(
    amounts=st.lists(st.integers(min_value=1, max_value=12_000), min_size=1, max_size=25),
    gaps=st.lists(st.integers(min_value=0, max_value=72), min_size=25, max_size=25),
)
def test_cumulative_cap_is_never_exceeded(amounts: list[int], gaps: list[int]) -> None:
    """Theorem 10.1 (docs/math/10_cumulative_spend.md), checked: whatever sequence of
    requests an agent sends, and whatever the user approves, gross authorised spend
    stays within the cap."""
    m = mandate(items=(), step_up_above_minor=5_000, per_txn_cap_minor=12_000)
    t = T0
    reqs = []
    for a, g in zip(amounts, gaps, strict=False):
        t = t + timedelta(hours=g)
        if t >= m.expires_at:
            break
        r = req(unit=a, qty=1, category="any", at=t, lines=())
        reqs.append(r.model_copy(update={"lines": (), "amount_minor": a}))
    approvals = [
        (
            r,
            Approval.sign(
                authorize(m, r, GuardState(mandate_id="md-1"), REG, key=KEY).request_digest,
                True,
                KEY,
            ),
        )
        for r in reqs
    ]
    out = replay_spend(m, approvals, REG, key=KEY)
    spent = sum(
        r.amount_minor for (r, _), (_, v) in zip(approvals, out, strict=True) if v == "allow"
    )
    assert spent <= m.cumulative_cap_minor
    assert all(
        r.amount_minor <= m.per_txn_cap_minor
        for (r, _), (_, v) in zip(approvals, out, strict=True)
        if v == "allow"
    )


def _write(tmp: Path, name: str, obj: object) -> str:
    p = tmp / name
    p.write_text(json.dumps(obj, default=str))
    return str(p)


def test_cli_sign_check_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SCRUPLE_GUARD_KEY", KEY)
    raw = mandate().model_dump(mode="json")
    raw["signature"] = ""
    mpath = _write(tmp_path, "m.json", raw)
    assert main(["guard", "sign", "--mandate", mpath]) == 0
    signed = json.loads(capsys.readouterr().out)
    assert signature_ok(Mandate.model_validate(signed), KEY)
    spath = _write(tmp_path, "signed.json", signed)
    reg = _write(tmp_path, "reg.json", [r.model_dump() for r in REG.values()])
    chain = str(tmp_path / "chain.json")
    ok = _write(tmp_path, "ok.json", req().model_dump(mode="json"))
    bad = _write(tmp_path, "bad.json", req(currency="EUR").model_dump(mode="json"))
    up = _write(tmp_path, "up.json", req(payee_account="acct-evil").model_dump(mode="json"))
    db = str(tmp_path / "audit.db")
    args = ["guard", "check", "--mandate", spath, "--registry", reg, "--chain", chain]
    assert main([*args, "--request", ok, "--audit-db", db]) == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == "allow"
    assert main([*args, "--request", up]) == 3
    assert main([*args, "--request", bad]) == 4
    capsys.readouterr()
    assert main(["guard", "verify", "--chain", chain]) == 0
    assert json.loads(capsys.readouterr().out)["records"] == 3
    recs = json.loads(Path(chain).read_text())
    recs[0]["final"] = "deny"
    Path(chain).write_text(json.dumps(recs))
    assert main(["guard", "verify", "--chain", chain]) == 1
    monkeypatch.delenv("SCRUPLE_GUARD_KEY")
    assert main(["guard", "check", "--mandate", spath, "--registry", reg, "--request", ok]) == 2


def test_cli_demo_and_redteam(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["guard", "demo", "--seed", "3"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["synthetic"] and out["agents"]["careful"]["chain_ok"]
    assert out["agents"]["careful"]["scorecard"]["gate"]
    assert main(["guard", "redteam", "--seeds", "4", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)["rows"]
    assert {r["id"] for r in rows} >= {"XC1", "XC2", "XC9"}
