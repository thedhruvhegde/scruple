"""Stripe billing hooks (D09): usage aggregates to billing meter events, test mode by
default, idempotent, checked against the vendored Stripe OpenAPI subset. Every request
goes to a fake transport; no test makes a network call."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from scruple.billing.push import BillingConfig, BillingLedger, identifier, plan, push
from scruple.billing.stripe import (
    METER_EVENTS,
    STRIPE_VERSION,
    BillingError,
    LiveModeRefusedError,
    MeterEvent,
    StripeBillingClient,
)
from scruple.clock import Clock
from scruple.storage import ImmutableHistoryError, SQLiteStorage
from scruple.usage.meter import Meter, UsageEvent, UsageStore

FIX = Path(__file__).parent / "fixtures" / "stripe_billing"
SPEC = json.loads((FIX / "spec3.billing.json").read_text())
FIXTURES = json.loads((FIX / "fixtures3.billing.json").read_text())
KEY = "sk_test_" + "a1B2c3D4e5F6g7H8"
NOW = datetime(2026, 11, 11, 15, 30, tzinfo=UTC)
CONFIG = BillingConfig(
    customers={"acme": "cus_Acme123", "beta": "cus_Beta456"},
    meters={Meter.GATE_RUN: "scruple_gate_runs", Meter.ROLLOUT: "scruple_rollouts"},
)


class FixedClock(Clock):
    def __init__(self, at: datetime) -> None:
        self.at = at

    def now(self) -> datetime:
        return self.at


class FakeStripe:
    """Accepts meter events like Stripe: dedups by identifier, sums per customer and
    event name, echoes a `billing.meter_event` built from the vendored fixture."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.events: dict[str, dict[str, Any]] = {}
        self.fail_next: list[int] = []
        self.livemode = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.method == "POST" and request.url.path == METER_EVENTS
        assert request.url.host == "api.stripe.com"
        assert request.headers["authorization"] == f"Bearer {KEY}"
        assert request.headers["stripe-version"] == STRIPE_VERSION
        assert request.headers["content-type"] == "application/x-www-form-urlencoded"
        if self.fail_next:
            status = self.fail_next.pop(0)
            err = {"error": {"type": "api_error", "message": "try again"}}
            return httpx.Response(status, json=err)
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        assert request.headers["idempotency-key"] == form["identifier"]
        ident = form["identifier"]
        if ident not in self.events:
            body = dict(FIXTURES["resources"]["billing.meter_event"])
            body.update(
                event_name=form["event_name"],
                identifier=ident,
                livemode=self.livemode,
                timestamp=int(form["timestamp"]),
                payload={
                    k[len("payload[") : -1]: v for k, v in form.items() if k.startswith("payload[")
                },
            )
            self.events[ident] = body
        return httpx.Response(200, json=self.events[ident])

    def totals(self) -> dict[tuple[str, str], int]:
        out: dict[tuple[str, str], int] = {}
        for e in self.events.values():
            k = (e["payload"]["stripe_customer_id"], e["event_name"])
            out[k] = out.get(k, 0) + int(e["payload"]["value"])
        return out

    def client(self) -> StripeBillingClient:
        return StripeBillingClient(KEY, transport=httpx.MockTransport(self.handler))


def _ev(
    key: str, at: datetime, tenant: str = "acme", meter: Meter = Meter.GATE_RUN, q: int = 1
) -> UsageEvent:
    return UsageEvent(tenant=tenant, event_key=key, meter=meter, quantity=q, at=at, source=key)


@pytest.fixture
def db() -> SQLiteStorage:
    return SQLiteStorage(":memory:")


def _stores(db: SQLiteStorage) -> tuple[UsageStore, BillingLedger]:
    return UsageStore(db.connection), BillingLedger(db.connection)


# --- contract with the vendored spec -------------------------------------------------------


def test_request_fields_match_the_spec() -> None:
    op = SPEC["paths"][METER_EVENTS]["post"]
    schema = op["requestBody"]["content"]["application/x-www-form-urlencoded"]["schema"]
    sent = {"event_name", "identifier", "timestamp", "payload"}
    assert sent <= set(schema["properties"])
    assert set(schema["required"]) <= sent
    assert schema["properties"]["identifier"]["maxLength"] == 100
    assert schema["properties"]["event_name"]["maxLength"] == 100
    assert schema["properties"]["payload"]["additionalProperties"]["type"] == "string"


def test_response_model_matches_the_spec_and_fixture() -> None:
    schema = SPEC["components"]["schemas"]["billing.meter_event"]
    assert set(schema["required"]) == set(MeterEvent.model_fields)
    MeterEvent.model_validate(FIXTURES["resources"]["billing.meter_event"])
    assert SPEC["info"]["version"] == STRIPE_VERSION


def test_identifiers_fit_the_spec_and_are_stable() -> None:
    a = identifier("acme", Meter.GATE_RUN, NOW.date(), 7)
    assert a == identifier("acme", Meter.GATE_RUN, NOW.date(), 7)
    assert len(a) <= 100
    others = {
        identifier("beta", Meter.GATE_RUN, NOW.date(), 7),
        identifier("acme", Meter.ROLLOUT, NOW.date(), 7),
        identifier("acme", Meter.GATE_RUN, NOW.date() - timedelta(days=1), 7),
        identifier("acme", Meter.GATE_RUN, NOW.date(), 8),
    }
    assert a not in others and len(others) == 4


# --- pushing -----------------------------------------------------------------------------


def test_push_sends_each_day_once(db: SQLiteStorage) -> None:
    usage, ledger = _stores(db)
    usage.record_many(
        [
            _ev("a1", NOW - timedelta(days=2)),
            _ev("a2", NOW - timedelta(days=2)),
            _ev("a3", NOW - timedelta(hours=1)),
            _ev("r1", NOW - timedelta(hours=1), meter=Meter.ROLLOUT),
            _ev("c1", NOW, meter=Meter.GATE_CASES, q=900),  # not a configured meter
            _ev("b1", NOW - timedelta(days=1), tenant="beta", q=3),
            _ev("z1", NOW, tenant="unbilled"),
        ]
    )
    fake = FakeStripe()
    with fake.client() as client:
        report = push(CONFIG, client, usage, ledger, FixedClock(NOW))
        assert report.ok and len(report.sent) == 4
        assert fake.totals() == {
            ("cus_Acme123", "scruple_gate_runs"): 3,
            ("cus_Acme123", "scruple_rollouts"): 1,
            ("cus_Beta456", "scruple_gate_runs"): 3,
        }
        again = push(CONFIG, client, usage, ledger, FixedClock(NOW + timedelta(minutes=5)))
    assert again.sent == [] and len(fake.requests) == 4
    for r in fake.requests:
        ts = int(parse_qs(r.content.decode())["timestamp"][0])
        assert NOW.timestamp() - 35 * 86_400 < ts <= NOW.timestamp() + 300


def test_late_usage_sends_only_the_difference(db: SQLiteStorage) -> None:
    usage, ledger = _stores(db)
    day = NOW - timedelta(days=1)
    usage.record_many([_ev("a1", day), _ev("a2", day)])
    fake = FakeStripe()
    with fake.client() as client:
        push(CONFIG, client, usage, ledger, FixedClock(NOW))
        usage.record(_ev("a3", day + timedelta(minutes=1)))
        report = push(CONFIG, client, usage, ledger, FixedClock(NOW))
    assert [(p.delta, p.cumulative) for p in report.sent] == [(1, 3)]
    assert fake.totals()[("cus_Acme123", "scruple_gate_runs")] == 3
    assert [r["cumulative"] for r in ledger.rows("acme")] == [2, 3]


def test_a_failure_is_retried_with_the_same_identifier(db: SQLiteStorage) -> None:
    usage, ledger = _stores(db)
    usage.record_many([_ev("a1", NOW), _ev("b1", NOW, tenant="beta")])
    fake = FakeStripe()
    fake.fail_next = [500]
    with fake.client() as client:
        first = push(CONFIG, client, usage, ledger, FixedClock(NOW))
        assert len(first.failed) == 1 and len(first.sent) == 1 and not first.ok
        assert "500" in first.failed[0][1]
        failed_id = first.failed[0][0].identifier
        second = push(CONFIG, client, usage, ledger, FixedClock(NOW))
    assert [p.identifier for p in second.sent] == [failed_id]
    assert fake.totals() == {
        ("cus_Acme123", "scruple_gate_runs"): 1,
        ("cus_Beta456", "scruple_gate_runs"): 1,
    }


def test_a_response_lost_after_stripe_accepted_is_deduplicated(db: SQLiteStorage) -> None:
    """Stripe recorded the event but the answer never arrived: the retry carries the
    same identifier and Idempotency-Key, so Stripe counts it once."""
    usage, ledger = _stores(db)
    usage.record(_ev("a1", NOW, q=1))
    fake = FakeStripe()
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        resp = fake.handler(request)
        if calls["n"] == 1:
            raise httpx.ReadTimeout("lost", request=request)
        return resp

    client = StripeBillingClient(KEY, transport=httpx.MockTransport(flaky))
    cfg = BillingConfig(customers={"acme": "cus_Acme123"}, meters={Meter.GATE_RUN: "runs"})
    assert not push(cfg, client, usage, ledger, FixedClock(NOW)).ok
    assert push(cfg, client, usage, ledger, FixedClock(NOW)).ok
    assert fake.totals() == {("cus_Acme123", "runs"): 1}
    ids = [parse_qs(r.content.decode())["identifier"][0] for r in fake.requests]
    assert len(ids) == 2 and ids[0] == ids[1]


def test_days_past_the_lookback_are_reported_stale(db: SQLiteStorage) -> None:
    usage, ledger = _stores(db)
    usage.record_many([_ev("old", NOW - timedelta(days=40)), _ev("new", NOW)])
    report = plan(CONFIG, usage, ledger, NOW)
    assert [p.day for p in report.planned] == [NOW.date()]
    assert [(t, d) for t, _m, d, _v in report.stale] == [
        ("acme", (NOW - timedelta(days=40)).date())
    ]


def test_plan_sends_nothing(db: SQLiteStorage) -> None:
    usage, ledger = _stores(db)
    usage.record(_ev("a", NOW))
    assert len(plan(CONFIG, usage, ledger, NOW).planned) == 1
    assert ledger.rows("acme") == []


def test_unconfigured_tenant_is_an_error(db: SQLiteStorage) -> None:
    usage, ledger = _stores(db)
    with pytest.raises(BillingError, match="no billing customer"):
        plan(CONFIG, usage, ledger, NOW, ["nobody"])


def test_the_ledger_is_append_only(db: SQLiteStorage) -> None:
    usage, ledger = _stores(db)
    usage.record(_ev("a", NOW))
    fake = FakeStripe()
    with fake.client() as client:
        push(CONFIG, client, usage, ledger, FixedClock(NOW))
    with pytest.raises(ImmutableHistoryError):
        db.connection.execute("UPDATE billing_pushes SET delta = 99")
    with pytest.raises(ImmutableHistoryError):
        db.connection.execute("DELETE FROM billing_pushes")


# --- test mode, keys and errors --------------------------------------------------------------


@pytest.mark.parametrize("key", ["sk_live_" + "x" * 16, "rk_live_" + "y" * 16])
def test_live_keys_are_refused_by_default(key: str) -> None:
    with pytest.raises(LiveModeRefusedError):
        StripeBillingClient(key, transport=httpx.MockTransport(lambda r: httpx.Response(500)))


@pytest.mark.parametrize("key", ["", "pk_test_abcdefgh", "sk_test_short", "sk_test_has spaces!!"])
def test_malformed_keys_are_refused(key: str) -> None:
    with pytest.raises(BillingError):
        StripeBillingClient(key)


def test_key_never_in_repr() -> None:
    c = StripeBillingClient(KEY, transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    assert KEY not in repr(c)


def test_a_live_answer_to_a_test_key_is_refused(db: SQLiteStorage) -> None:
    usage, ledger = _stores(db)
    usage.record(_ev("a", NOW))
    fake = FakeStripe()
    fake.livemode = True
    with fake.client() as client:
        report = push(CONFIG, client, usage, ledger, FixedClock(NOW))
    assert not report.ok and "live mode" in report.failed[0][1]
    assert ledger.rows("acme") == []


def test_mode_and_key_must_agree(db: SQLiteStorage) -> None:
    usage, ledger = _stores(db)
    live_cfg = CONFIG.model_copy(update={"mode": "live"})
    with FakeStripe().client() as client, pytest.raises(BillingError, match="mode is live"):
        push(live_cfg, client, usage, ledger, FixedClock(NOW))
    live = StripeBillingClient(
        "sk_live_" + "z" * 16,
        transport=httpx.MockTransport(lambda r: httpx.Response(500)),
        allow_live=True,
    )
    with pytest.raises(BillingError, match="mode is test"):
        push(CONFIG, live, usage, ledger, FixedClock(NOW))


@pytest.mark.parametrize(
    ("respond", "match"),
    [
        (
            lambda r: httpx.Response(
                400, json={"error": {"type": "invalid_request_error", "message": "No such meter"}}
            ),
            "invalid_request_error",
        ),
        (lambda r: httpx.Response(200, content=b"not json"), "not JSON"),
        (lambda r: httpx.Response(200, json={"object": "billing.meter_event"}), "bad meter event"),
        (lambda r: httpx.Response(200, content=b"x" * 70_000), "over"),
        (lambda r: httpx.Response(502, content=b"<html>bad gateway</html>"), "502"),
    ],
)
def test_bad_answers_are_errors(
    respond: Callable[[httpx.Request], httpx.Response], match: str
) -> None:
    c = StripeBillingClient(KEY, transport=httpx.MockTransport(respond))
    with pytest.raises(BillingError, match=match):
        c.create_meter_event(
            event_name="runs", customer="cus_A1", value=1, identifier="scr-x", timestamp=1
        )


@pytest.mark.parametrize(
    "kw",
    [
        {"event_name": "bad name!"},
        {"customer": "acme"},
        {"value": 0},
        {"identifier": "x" * 101},
        {"customer_key": "Bad-Key"},
    ],
)
def test_inputs_are_checked_before_sending(kw: dict[str, Any]) -> None:
    sent: list[httpx.Request] = []

    def record(r: httpx.Request) -> httpx.Response:
        sent.append(r)
        return httpx.Response(500)

    c = StripeBillingClient(KEY, transport=httpx.MockTransport(record))
    args: dict[str, Any] = {
        "event_name": "runs",
        "customer": "cus_A1",
        "value": 1,
        "identifier": "scr-x",
        "timestamp": 1,
        **kw,
    }
    with pytest.raises(BillingError):
        c.create_meter_event(**args)
    assert sent == []


# --- configuration and the command line -------------------------------------------------------


def _config_file(tmp: Path, extra: str = "") -> Path:
    p = tmp / "billing.toml"
    p.write_text(
        "[billing]\n"
        'customers = { default = "cus_Default1" }\n'
        'meters = { gate_run = "scruple_gate_runs", gate_cases = "scruple_gate_cases" }\n' + extra
    )
    return p


def test_config_loads_and_refuses_secrets(tmp_path: Path) -> None:
    cfg = BillingConfig.load(_config_file(tmp_path))
    assert cfg.mode == "test" and cfg.meters[Meter.GATE_CASES] == "scruple_gate_cases"
    with pytest.raises(BillingError, match="environment"):
        BillingConfig.load(_config_file(tmp_path, 'api_key = "sk_test_x"\n'))
    with pytest.raises(BillingError):
        BillingConfig.load(_config_file(tmp_path, "lookback_days = 40\n"))
    bad = tmp_path / "bad.toml"
    bad.write_text('[billing]\ncustomers = { acme = "acme" }\nmeters = { gate_run = "x" }\n')
    with pytest.raises(BillingError, match="cus_"):
        BillingConfig.load(bad)
    bad.write_text('[billing]\ncustomers = {}\nmeters = { no_such_meter = "x" }\n')
    with pytest.raises(BillingError):
        BillingConfig.load(bad)


def test_cli_plan_and_push(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from scruple.billing.cli import cmd_push
    from scruple.cli.main import build_parser, main
    from scruple.service import AgentGate
    from scruple.sim.library import BAD_RULE, BAD_RULE_ID
    from tests.ci_support import PROV

    db = str(tmp_path / "g.db")
    g = AgentGate.open(db)
    g.bootstrap_baseline()
    g.propose(BAD_RULE_ID, BAD_RULE, "ops-team", PROV)
    g.evaluate(BAD_RULE_ID, 1)  # one shadow run, blocked
    g.storage.close()
    cfg = _config_file(tmp_path)
    assert main(["billing", "plan", "--config", str(cfg), "--db", db, "--json"]) == 0
    planned = json.loads(capsys.readouterr().out)["planned"]
    assert {p["meter"] for p in planned} == {"gate_run", "gate_cases"}
    monkeypatch.delenv("SCRUPLE_STRIPE_BILLING_KEY", raising=False)
    assert main(["billing", "push", "--config", str(cfg), "--db", db]) == 2
    assert "SCRUPLE_STRIPE_BILLING_KEY" in capsys.readouterr().err
    monkeypatch.setenv("SCRUPLE_STRIPE_BILLING_KEY", KEY)
    fake = FakeStripe()
    args = build_parser().parse_args(["billing", "push", "--config", str(cfg), "--db", db])
    assert cmd_push(args, transport=httpx.MockTransport(fake.handler)) == 0
    out = capsys.readouterr().out
    assert out.count("sent default") == 2
    assert fake.totals()[("cus_Default1", "scruple_gate_runs")] == 1
    assert cmd_push(args, transport=httpx.MockTransport(fake.handler)) == 0
    assert "nothing to send" in capsys.readouterr().out
    assert len(fake.requests) == 2
    monkeypatch.setenv("SCRUPLE_STRIPE_BILLING_KEY", "sk_live_" + "q" * 16)
    assert main(["billing", "push", "--config", str(cfg), "--db", db, "--allow-live"]) == 2
    assert "live Stripe keys are refused" in capsys.readouterr().err
