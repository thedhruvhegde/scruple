"""`scruple ingest stripe-pull`: paging, rate limits, resume, test-mode guard.

A fake Stripe serves copies of the vendored real `dispute` and `charge` fixtures
(stripe/openapi, MIT; tests/fixtures/stripe) through `httpx.MockTransport`. Never a
live call."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from scruple.cli.main import main
from scruple.ingest.errors import IngestError, LiveModeRefusedError
from scruple.ingest.pipeline import IngestOptions, ingest
from scruple.ingest.stripe import StripeClient
from scruple.ingest.stripe import pull as pull_mod
from scruple.ingest.stripe.mapping import parse_dispute, payment_facts
from scruple.ingest.stripe.pull import (
    EVIDENCE_MARKER,
    PullCursor,
    client_from_env,
    parse_instant,
    pull_disputes,
    strip_free_text,
)
from scruple.ingest.stripe.wire import Charge, Dispute

HERE = Path(__file__).parent / "fixtures" / "stripe"
FIXTURES: dict[str, Any] = json.loads((HERE / "fixtures3.subset.json").read_text())["resources"]
SPEC = json.loads((HERE / "spec3.subset.json").read_text())
KEY = "sk_test_" + "0" * 24  # dummy, never a real key
T0 = 1_700_000_000


def make_dispute(i: int, created: int, **over: Any) -> dict[str, Any]:
    d: dict[str, Any] = copy.deepcopy(FIXTURES["dispute"])
    d.update(id=f"dp_{i:05d}", created=created, charge=f"ch_{i:05d}")
    d["evidence"] = dict(d["evidence"], customer_name="Jenny Rosen", uncategorized_text="note")
    d.update(over)
    return d


class FakeStripe:
    """Serves GET /v1/disputes like the spec says: newest first, created range,
    starting_after, limit, expand[]=data.charge (inlines the fixture charge)."""

    def __init__(self, disputes: list[dict[str, Any]], faults: list[httpx.Response] | None = None):
        self.disputes = sorted(disputes, key=lambda d: (-d["created"], d["id"]))
        self.faults = list(faults or [])
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.faults:
            return self.faults.pop(0)
        q = request.url.params
        assert request.url.path == "/v1/disputes"
        rows = [
            d
            for d in self.disputes
            if int(q.get("created[gte]", 0)) <= d["created"] < int(q.get("created[lt]", 1 << 62))
        ]
        after = q.get("starting_after")
        if after is not None:
            idx = [d["id"] for d in rows].index(after)
            rows = rows[idx + 1 :]
        limit = int(q["limit"])
        data = copy.deepcopy(rows[:limit])
        if "data.charge" in q.get_list("expand[]"):
            for d in data:
                d["charge"] = dict(copy.deepcopy(FIXTURES["charge"]), id=d["charge"])
        body = {"object": "list", "url": "/v1/disputes", "has_more": len(rows) > limit}
        return httpx.Response(200, json={**body, "data": data})


def history(n: int, step: int = 3600) -> list[dict[str, Any]]:
    return [make_dispute(i, T0 + i * step) for i in range(n)]


def client(fake: FakeStripe, sleeps: list[float] | None = None, **kw: Any) -> StripeClient:
    out = sleeps if sleeps is not None else []
    return StripeClient(
        KEY, transport=httpx.MockTransport(fake), max_retries=5, sleep=out.append, **kw
    )


def ids(path: Path) -> list[str]:
    return [json.loads(line)["id"] for line in path.read_text().splitlines()]


def test_spec_has_the_query_parameters_the_pull_sends() -> None:
    params = {p["name"] for p in SPEC["paths"]["/v1/disputes"]["get"]["parameters"]}
    assert {"created", "expand", "limit", "starting_after"} <= params


def test_pull_pages_the_window_and_expands_the_charge(tmp_path: Path) -> None:
    fake = FakeStripe(history(25))
    out = tmp_path / "export.jsonl"
    since, until = T0 + 2 * 3600, T0 + 20 * 3600
    rep = pull_disputes(client(fake), since, until, out, page_size=7)
    expected = [f"dp_{i:05d}" for i in range(19, 1, -1)]
    assert ids(out) == expected
    assert (rep.disputes, rep.pages, rep.complete, rep.resumed) == (18, 3, True, False)
    first, second, _ = fake.requests
    assert first.url.params["created[gte]"] == str(since)
    assert first.url.params["created[lt]"] == str(until)
    assert first.url.params.get_list("expand[]") == ["data.charge"]
    assert "starting_after" not in first.url.params
    assert second.url.params["starting_after"] == expected[6]
    for line in out.read_text().splitlines():
        d = Dispute.model_validate_json(line)
        assert not isinstance(d.charge, str)  # expanded


def test_free_text_never_reaches_the_export(tmp_path: Path) -> None:
    out = tmp_path / "export.jsonl"
    pull_disputes(client(FakeStripe(history(3))), T0, T0 + 10**6, out)
    text = out.read_text()
    assert "Jenny Rosen" not in text and '"note"' not in text
    ev = json.loads(text.splitlines()[0])["evidence"]
    assert ev["customer_name"] == EVIDENCE_MARKER
    assert all(v in (None, EVIDENCE_MARKER) for v in ev.values())
    charge = json.loads(text.splitlines()[0])["charge"]
    assert "billing_details" not in charge and "receipt_email" not in charge
    assert set(charge) <= set(Charge.model_fields)


def _shipped(charge: dict[str, Any]) -> dict[str, Any]:
    ship = {
        "address": {"line1": "1 Main St", "postal_code": "94111", "country": None},
        "carrier": "UPS",
        "tracking_number": "1Z999",
        "name": "Jenny Rosen",
        "phone": "+15555550100",
    }
    return dict(charge, shipping=ship)


def test_pruned_charge_keeps_every_payment_fact() -> None:
    """The facts rules read are identical before and after pruning (including the
    shipping presence flags), while names, phones and addresses are gone."""
    raw = make_dispute(1, T0, charge=_shipped(copy.deepcopy(FIXTURES["charge"])))
    stripped = strip_free_text(raw)
    assert payment_facts(parse_dispute(stripped)) == payment_facts(parse_dispute(raw))
    text = json.dumps(stripped)
    for secret in ("Jenny Rosen", "1 Main St", "94111", "1Z999", "+15555550100"):
        assert secret not in text


@given(
    st.dictionaries(st.sampled_from(["a", "b", "c"]), st.one_of(st.none(), st.text(), st.just({})))
)
@settings(max_examples=50)
def test_strip_free_text_keeps_only_presence(evidence: dict[str, Any]) -> None:
    out = strip_free_text({"id": "dp_1", "evidence": evidence})["evidence"]
    assert set(out) == set(evidence)
    for k, v in evidence.items():
        assert out[k] == (EVIDENCE_MARKER if v not in (None, "", {}, []) else None)


def test_export_ingests_with_the_existing_pipeline(tmp_path: Path) -> None:
    out = tmp_path / "export.jsonl"
    pull_disputes(client(FakeStripe(history(12))), T0, T0 + 10**6, out, page_size=5)
    report = ingest(out.read_bytes(), IngestOptions(source="stripe"), None)
    assert (report.rows_read, report.rows_failed, report.cases) == (12, 0, 12)


def test_rate_limits_back_off_and_honour_retry_after(tmp_path: Path) -> None:
    faults = [
        httpx.Response(429, json={"error": {"type": "rate_limit_error", "message": "slow"}}),
        httpx.Response(429, headers={"Retry-After": "7"}, json={"error": {}}),
        httpx.Response(503, content=b"busy"),
    ]
    sleeps: list[float] = []
    out = tmp_path / "export.jsonl"
    rep = pull_disputes(client(FakeStripe(history(4), faults), sleeps), T0, T0 + 10**6, out)
    assert sleeps == [1.0, 7.0, 4.0]
    assert rep.retries == 3 and rep.disputes == 4


def test_retries_are_bounded_and_should_retry_false_is_final(tmp_path: Path) -> None:
    many = [httpx.Response(429, json={"error": {"type": "rate_limit_error"}})] * 10
    with pytest.raises(IngestError, match="429"):
        pull_disputes(client(FakeStripe(history(2), many)), T0, T0 + 10, tmp_path / "e.jsonl")
    sleeps: list[float] = []
    final = [httpx.Response(500, headers={"Stripe-Should-Retry": "false"}, json={"error": {}})]
    with pytest.raises(IngestError, match="500"):
        pull_disputes(
            client(FakeStripe(history(2), final), sleeps), T0, T0 + 10, tmp_path / "f.jsonl"
        )
    assert sleeps == []


def test_transport_errors_are_retried(tmp_path: Path) -> None:
    fake = FakeStripe(history(2))
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("reset")
        return fake(request)

    c = StripeClient(KEY, transport=httpx.MockTransport(flaky), max_retries=2, sleep=lambda _: None)
    assert pull_disputes(c, T0, T0 + 10**6, tmp_path / "e.jsonl").disputes == 2


def test_resume_continues_after_the_cursor(tmp_path: Path) -> None:
    disputes = history(23)
    out = tmp_path / "export.jsonl"
    one = pull_disputes(client(FakeStripe(disputes)), T0, T0 + 10**6, out, page_size=5, max_pages=2)
    assert (one.complete, one.disputes) == (False, 10)
    fake = FakeStripe(disputes)
    two = pull_disputes(client(fake), T0, T0 + 10**6, out, page_size=5)
    assert two.resumed and two.complete and two.fetched_this_run == 13
    assert fake.requests[0].url.params["starting_after"] == ids(out)[9]
    full = tmp_path / "full.jsonl"
    pull_disputes(client(FakeStripe(disputes)), T0, T0 + 10**6, full, page_size=5)
    assert out.read_bytes() == full.read_bytes()
    assert (
        two.export_sha256
        == PullCursor.model_validate_json(
            (tmp_path / "export.jsonl.cursor.json").read_text()
        ).export_sha256
    )


def test_a_finished_pull_reruns_as_a_no_op(tmp_path: Path) -> None:
    out = tmp_path / "export.jsonl"
    pull_disputes(client(FakeStripe(history(6))), T0, T0 + 10**6, out)
    before = out.read_bytes()
    fake = FakeStripe(history(6))
    rep = pull_disputes(client(fake), T0, T0 + 10**6, out)
    assert fake.requests == [] and rep.fetched_this_run == 0 and rep.complete
    assert out.read_bytes() == before


def test_bytes_after_the_last_cursor_are_dropped_on_resume(tmp_path: Path) -> None:
    """A crash between appending a page and writing the cursor leaves extra lines;
    the resume cuts them so no dispute appears twice."""
    disputes = history(9)
    out = tmp_path / "export.jsonl"
    pull_disputes(client(FakeStripe(disputes)), T0, T0 + 10**6, out, page_size=4, max_pages=1)
    with out.open("ab") as f:
        f.write(out.read_bytes())  # the same page again, cursor not updated
    pull_disputes(client(FakeStripe(disputes)), T0, T0 + 10**6, out, page_size=4)
    assert ids(out) == [f"dp_{i:05d}" for i in range(8, -1, -1)]


def test_cursor_for_another_window_or_edited_export_is_refused(tmp_path: Path) -> None:
    out = tmp_path / "export.jsonl"
    pull_disputes(client(FakeStripe(history(9))), T0, T0 + 10**6, out, page_size=4, max_pages=1)
    with pytest.raises(IngestError, match="another window"):
        pull_disputes(client(FakeStripe(history(9))), T0 + 1, T0 + 10**6, out)
    out.write_bytes(b"x" + out.read_bytes()[1:])
    with pytest.raises(IngestError, match="does not match"):
        pull_disputes(client(FakeStripe(history(9))), T0, T0 + 10**6, out)
    rep = pull_disputes(client(FakeStripe(history(9))), T0, T0 + 10**6, out, restart=True)
    assert rep.disputes == 9 and not rep.resumed


def test_live_keys_and_live_objects_are_refused(tmp_path: Path) -> None:
    with pytest.raises(LiveModeRefusedError):
        client_from_env({"STRIPE_API_KEY": "sk_live_" + "1" * 24})
    with pytest.raises(IngestError, match="STRIPE_API_KEY"):
        client_from_env({})
    live = FakeStripe([make_dispute(1, T0, livemode=True)])
    with pytest.raises(LiveModeRefusedError):
        pull_disputes(client(live), T0, T0 + 10, tmp_path / "e.jsonl")
    ok = pull_disputes(client(live), T0, T0 + 10, tmp_path / "l.jsonl", allow_live=True)
    assert ok.disputes == 1


@pytest.mark.parametrize(
    ("text", "unix"),
    [
        ("1700000000", 1_700_000_000),
        ("2026-01-01", 1_767_225_600),
        ("2026-01-01T00:00:00", 1_767_225_600),
        ("2026-01-01T01:00:00+01:00", 1_767_225_600),
    ],
)
def test_parse_instant(text: str, unix: int) -> None:
    assert parse_instant(text) == unix


def test_parse_instant_and_window_errors(tmp_path: Path) -> None:
    with pytest.raises(IngestError):
        parse_instant("last tuesday")
    with pytest.raises(IngestError, match="after"):
        pull_disputes(client(FakeStripe([])), T0, T0, tmp_path / "e.jsonl")


def test_cli_pull_then_ingest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    monkeypatch.setattr(pull_mod, "DEFAULT_TRANSPORT", httpx.MockTransport(FakeStripe(history(8))))
    monkeypatch.setenv("STRIPE_API_KEY", KEY)
    out, store = tmp_path / "export.jsonl", tmp_path / "cases.db"
    argv = ["ingest", "stripe-pull", "--since", str(T0), "--until", "2100-01-01"]
    rc = main([*argv, "--out", str(out), "--max-pages", "1", "--page-size", "3"])
    assert rc == 3  # incomplete: rerun to resume
    rc = main(
        [*argv, "--out", str(out), "--store", str(store), "--report", str(tmp_path / "r.json")]
    )
    assert rc == 0
    text = capsys.readouterr().out
    assert "8 disputes" in text and KEY not in text
    rep = json.loads((tmp_path / "r.json").read_text())
    assert (rep["rows_read"], rep["cases"]) == (8, 8)
    monkeypatch.setenv("STRIPE_API_KEY", "sk_live_" + "1" * 24)
    assert main([*argv, "--out", str(tmp_path / "x.jsonl")]) == 2
