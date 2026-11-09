"""`scruple ingest`: validation, redaction, tokenisation, encryption, the case store and
`ingest verify`. Real inputs are the vendored Stripe and Adyen fixtures; everything
else is built here and synthetic."""

from __future__ import annotations

import base64
import copy
import csv
import io
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from scruple.backtest.history import load_store
from scruple.calibration import DEFAULT_PROFILE, stripe_disputes
from scruple.cli.main import main
from scruple.ingest.errors import IngestError
from scruple.ingest.keys import (
    KeyConfigError,
    adyen_keys_from_env,
    cipher_from_env,
    parse_key_ring,
    tokenizer_from_env,
)
from scruple.ingest.models import CaseState, ExternalCase
from scruple.ingest.pipeline import IngestOptions, ingest
from scruple.ingest.store import (
    CaseStore,
    RawPanRefusedError,
    StoredCase,
    StoreError,
    assert_no_raw_pan,
)
from scruple.ingest.tabular import ColumnMapping, ImportConfig, StatusTarget
from scruple.ingest.verify import summarise
from scruple.security.crypto import FieldCipher, TamperedError
from scruple.security.pan import PanTokenizer, luhn_check_digit
from scruple.sim.models import Stage
from tests.test_ingest_adyen import CHARGEBACK, TEST_KEY, example, signed

STRIPE = Path(__file__).parent / "fixtures" / "stripe"
FIXTURES: dict[str, Any] = json.loads((STRIPE / "fixtures3.subset.json").read_text())["resources"]
TOKEN_KEY = b"t" * 32
FIELD_KEY = b"f" * 32
TOK = PanTokenizer({"k1": TOKEN_KEY}, "k1")
CIPHER = FieldCipher({"f1": FIELD_KEY}, "f1")
PAN = "4242424242424242"  # the well-known test card number (Luhn-valid)


def pan(prefix: str = "411111", length: int = 16) -> str:
    body = (prefix + "7" * length)[: length - 1]
    return body + luhn_check_digit(body)


def real_dispute(**over: Any) -> dict[str, Any]:
    d: dict[str, Any] = copy.deepcopy(FIXTURES["dispute"])
    d.update(over)
    return d


def cfg(**over: Any) -> ImportConfig:
    base: dict[str, Any] = {
        "columns": ColumnMapping(
            external_id="id",
            payment_ref="charge",
            amount="amount",
            currency="ccy",
            status="state",
            reason="why",
            opened_at="opened",
            merchant_ref="merchant",
        ),
        "status_map": {
            "won": StatusTarget(stage=Stage.RESOLVED, state=CaseState.WON),
            "lost": StatusTarget(stage=Stage.RESOLVED, state=CaseState.LOST),
        },
        "reason_map": {"fraud": "fraud", "other": None},
    }
    base.update(over)
    return ImportConfig(**base)


def csv_text(rows: list[dict[str, str]], extra: tuple[str, ...] = ()) -> str:
    cols = ["id", "charge", "amount", "ccy", "state", "why", "opened", "merchant", *extra]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({c: r.get(c, "") for c in cols})
    return buf.getvalue()


def row(i: int, **over: str) -> dict[str, str]:
    r = {
        "id": f"c-{i:03d}",
        "charge": f"pay-{i:03d}",
        "amount": "12.50",
        "ccy": "USD",
        "state": "won" if i % 2 else "lost",
        "why": "fraud",
        "opened": "2026-01-05",
        "merchant": "shop-1",
    }
    r.update(over)
    return r


def file_bytes(store: CaseStore) -> bytes:
    store.close()
    return Path(store.path).read_bytes()


@pytest.fixture
def store(tmp_path: Path) -> CaseStore:
    return CaseStore(tmp_path / "cases.db")


# --- Stripe ------------------------------------------------------------------------


def test_real_stripe_fixture_is_stored_and_read_back(store: CaseStore) -> None:
    rep = ingest(json.dumps([real_dispute()]), IngestOptions(source="stripe"), store)
    assert (rep.rows_read, rep.rows_ok, rep.rows_failed, rep.cases_new) == (1, 1, 0, 1)
    assert rep.stored and rep.run_id == 1 and not rep.synthetic
    [sc] = store.cases()
    assert sc.case.external_id == FIXTURES["dispute"]["id"]
    assert sc.card is None and sc.encrypted == {}
    assert store.kind() == "real"


def test_row_level_errors_keep_the_good_records(store: CaseStore) -> None:
    docs = [real_dispute(), real_dispute(id="dp_x", status="exploded"), 7, real_dispute(id="dp_y")]
    rep = ingest(json.dumps(docs), IngestOptions(source="stripe"), store)
    assert rep.rows_read == 4 and rep.rows_ok == 2 and rep.rows_failed == 2
    assert [e.row for e in rep.errors] == [2, 3]
    assert "exploded" in rep.errors[0].message
    assert len(store.cases()) == 2
    assert [r[1] for r in store.row_errors()] == [2, 3]


def test_strict_mode_stores_nothing_when_a_record_fails(store: CaseStore) -> None:
    docs = [real_dispute(), real_dispute(id="dp_x", reason="no_such_reason")]
    rep = ingest(json.dumps(docs), IngestOptions(source="stripe", strict=True), store)
    assert not rep.stored and rep.rows_failed == 1
    assert store.cases() == [] and store.runs() == []


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    rep = ingest(json.dumps([real_dispute()]), IngestOptions(source="stripe"), None)
    assert not rep.stored and rep.run_id is None and rep.cases == 1


def test_reimport_is_idempotent_and_new_content_is_a_new_version(store: CaseStore) -> None:
    opts = IngestOptions(source="stripe")
    ingest(json.dumps([real_dispute(status="needs_response")]), opts, store)
    again = ingest(json.dumps([real_dispute(status="needs_response")]), opts, store)
    assert (again.cases_new, again.cases_unchanged, again.cases_updated) == (0, 1, 0)
    later = ingest(json.dumps([real_dispute(status="won")]), opts, store)
    assert later.cases_updated == 1
    [sc] = store.cases()
    assert sc.case.state is CaseState.WON
    assert store.versions() == {sc.case.case_id: 2}


def test_synthetic_history_round_trips_into_a_backtest_history(tmp_path: Path) -> None:
    objs = stripe_disputes(DEFAULT_PROFILE, 120, seed=3)
    st_ = CaseStore(tmp_path / "s.db")
    rep = ingest(json.dumps(objs), IngestOptions(source="stripe", label="synth"), st_)
    st_.close()
    assert rep.synthetic and rep.cases == 120 and rep.movements > 0
    hist = load_store(str(tmp_path / "s.db"))
    assert hist.synthetic and hist.format == "store" and len(hist.cases) == 120
    assert hist.label.startswith("SYNTHETIC")
    assert len(hist.movements) == rep.movements


def test_store_refuses_to_mix_real_and_synthetic(store: CaseStore) -> None:
    ingest(
        json.dumps(stripe_disputes(DEFAULT_PROFILE, 5, 0)), IngestOptions(source="stripe"), store
    )
    with pytest.raises(StoreError, match="synthetic"):
        ingest(json.dumps([real_dispute()]), IngestOptions(source="stripe"), store)


def test_stripe_encrypt_path_keeps_only_an_envelope(store: CaseStore) -> None:
    d = real_dispute()
    d["evidence"]["customer_email_address"] = "jenny.rosen@example.com"
    opts = IngestOptions(source="stripe", encrypt_paths=("evidence.customer_email_address",))
    rep = ingest(json.dumps([d]), opts, store, cipher=CIPHER)
    assert rep.fields_encrypted == 1
    [sc] = store.cases()
    env = sc.encrypted["evidence.customer_email_address"]
    assert CIPHER.decrypt_str(env, sc.context("evidence.customer_email_address")) == (
        "jenny.rosen@example.com"
    )
    with pytest.raises(TamperedError):
        CIPHER.decrypt(env, "case:stripe:other:evidence.customer_email_address")
    assert b"jenny.rosen" not in file_bytes(store)


def test_encryption_without_a_key_refuses_the_record(store: CaseStore) -> None:
    d = real_dispute()
    d["evidence"]["customer_name"] = "Jenny Rosen"
    opts = IngestOptions(source="stripe", encrypt_paths=("evidence.customer_name",))
    rep = ingest(json.dumps([d]), opts, store)
    assert rep.rows_failed == 1 and "SCRUPLE_FIELD_KEYS" in rep.errors[0].message
    assert store.cases() == []


def test_pan_in_unkept_stripe_field_is_counted_never_stored(store: CaseStore) -> None:
    d = real_dispute(metadata={"note": f"card {PAN}"})
    rep = ingest(json.dumps([d]), IngestOptions(source="stripe"), store)
    assert rep.pans_in_input_not_stored == {"metadata.note": 1}
    assert PAN.encode() not in file_bytes(store)


def test_encrypted_values_have_card_numbers_scrubbed_first(store: CaseStore) -> None:
    d = real_dispute()
    d["evidence"]["uncategorized_text"] = f"customer read out {PAN} on the phone"
    opts = IngestOptions(source="stripe", encrypt_paths=("evidence.uncategorized_text",))
    ingest(json.dumps([d]), opts, store, cipher=CIPHER)
    [sc] = store.cases()
    plain = CIPHER.decrypt_str(
        sc.encrypted["evidence.uncategorized_text"], sc.context("evidence.uncategorized_text")
    )
    assert PAN not in plain and "[PAN]" in plain


# --- Adyen -------------------------------------------------------------------------


def test_adyen_spec_example_is_verified_folded_and_stored(store: CaseStore) -> None:
    env = signed(example(CHARGEBACK))
    rep = ingest(json.dumps(env), IngestOptions(source="adyen"), store, adyen_hmac_keys=[TEST_KEY])
    assert rep.rows_failed == 0 and rep.cases == 1
    assert store.cases()[0].case.source == "adyen"


def test_adyen_bad_signature_is_a_row_error(store: CaseStore) -> None:
    env = signed(example(CHARGEBACK))
    rep = ingest(json.dumps(env), IngestOptions(source="adyen"), store, adyen_hmac_keys=["22" * 32])
    assert rep.rows_failed == 1 and "hmacSignature" in rep.errors[0].message
    assert store.cases() == []


def test_adyen_without_a_key_is_refused() -> None:
    with pytest.raises(IngestError, match="SCRUPLE_ADYEN_HMAC_KEYS"):
        ingest(json.dumps(signed(example(CHARGEBACK))), IngestOptions(source="adyen"), None)


# --- CSV and JSONL: card numbers and sensitive columns ---------------------------


def test_pan_column_is_tokenised_and_never_stored(store: CaseStore) -> None:
    text = csv_text([row(1, card=PAN), row(2, card="4111 1111 1111 1111")], extra=("card",))
    opts = IngestOptions(source="csv", import_config=cfg(), pan_columns=("card",))
    rep = ingest(text, opts, store, tokenizer=TOK)
    assert rep.pans_tokenised == 2 and rep.rows_failed == 0
    cards = [sc.card for sc in store.cases()]
    assert all(c is not None and c.key_id == "k1" for c in cards)
    assert cards[0] is not None and cards[0].last4 == "4242" and TOK.matches(cards[0], PAN)
    raw = file_bytes(store)
    assert PAN.encode() not in raw and b"4111111111111111" not in raw


def test_pan_column_without_a_key_stores_nothing(store: CaseStore) -> None:
    text = csv_text([row(1, card=PAN)], extra=("card",))
    opts = IngestOptions(source="csv", import_config=cfg(), pan_columns=("card",))
    rep = ingest(text, opts, store)
    assert rep.rows_failed == 1 and "SCRUPLE_TOKEN_KEYS" in rep.errors[0].message
    assert store.cases() == []


def test_bad_card_number_is_a_row_error(store: CaseStore) -> None:
    text = csv_text([row(1, card="4242424242424241")], extra=("card",))
    opts = IngestOptions(source="csv", import_config=cfg(), pan_columns=("card",))
    rep = ingest(text, opts, store, tokenizer=TOK)
    assert rep.errors[0].column == "card" and "Luhn" in rep.errors[0].message
    assert "4242424242424241" not in rep.model_dump_json()


def test_pan_in_an_id_column_rejects_the_row(store: CaseStore) -> None:
    rep = ingest(
        csv_text([row(1, id=PAN), row(2)]), IngestOptions(source="csv", import_config=cfg()), store
    )
    assert rep.rows_failed == 1 and "pan" in rep.errors[0].message
    assert [sc.case.external_id for sc in store.cases()] == ["c-002"]
    assert PAN.encode() not in file_bytes(store)


def test_pii_in_text_fields_is_redacted(store: CaseStore) -> None:
    rows = [row(1, merchant=f"shop {PAN}"), row(2, merchant="ops: jenny@example.com")]
    rep = ingest(csv_text(rows), IngestOptions(source="csv", import_config=cfg()), store)
    assert rep.redactions == {"EMAIL": 1, "PAN": 1}
    refs = sorted(str(sc.case.merchant_ref) for sc in store.cases())
    assert refs == ["ops: [EMAIL]", "shop [PAN]"]


def test_pan_in_an_unmapped_column_is_counted_not_stored(store: CaseStore) -> None:
    text = csv_text([row(1, notes=f"cc {PAN}")], extra=("notes",))
    opts = IngestOptions(source="csv", import_config=cfg(allow_extra_columns=True))
    rep = ingest(text, opts, store)
    assert rep.pans_in_input_not_stored == {"notes": 1} and rep.rows_ok == 1
    assert PAN.encode() not in file_bytes(store)


def test_encrypt_column(store: CaseStore) -> None:
    text = csv_text([row(1, email="jenny@example.com")], extra=("email",))
    opts = IngestOptions(source="csv", import_config=cfg(), encrypt_columns=("email",))
    rep = ingest(text, opts, store, cipher=CIPHER)
    assert rep.fields_encrypted == 1
    [sc] = store.cases()
    assert CIPHER.decrypt_str(sc.encrypted["email"], sc.context("email")) == "jenny@example.com"
    assert b"jenny@" not in file_bytes(store)


def test_jsonl_source(store: CaseStore) -> None:
    lines = "\n".join(json.dumps(row(i)) for i in range(1, 4))
    rep = ingest(lines, IngestOptions(source="jsonl", import_config=cfg()), store)
    assert rep.cases == 3 and rep.rows_read == 3


@pytest.mark.parametrize(
    "kwargs",
    [
        {"source": "csv"},
        {"source": "stripe", "pan_columns": ("card",)},
        {"source": "csv", "import_config": cfg(), "encrypt_paths": ("a.b",)},
        {"source": "csv", "import_config": cfg(), "pan_columns": ("amount",)},
        {"source": "csv", "import_config": cfg(), "pan_columns": ("a",), "encrypt_columns": ("a",)},
        {"source": "stripe", "encrypt_paths": ("a..b",)},
    ],
)
def test_bad_options_are_refused(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        IngestOptions(**kwargs)


# --- the store's own guards --------------------------------------------------------


def _case(**over: Any) -> ExternalCase:
    base: dict[str, Any] = {
        "source": "csv",
        "external_id": "c-1",
        "payment_ref": "p-1",
        "stage": Stage.RESOLVED,
        "state": CaseState.LOST,
        "reason_code": None,
        "source_reason": "other",
        "amount_minor": 100,
        "currency": "USD",
        "opened_at": "2026-01-01T00:00:00+00:00",
    }
    base.update(over)
    return ExternalCase(**base)


@pytest.mark.parametrize(
    "over",
    [
        {"merchant_ref": PAN},
        {"merchant_ref": "4242-4242-4242-4242"},
        {"network_reason_code": "4242 4242 4242 4242"},
        {"external_id": f"x{PAN}"},
        {"amount_minor": int(PAN)},
    ],
)
def test_store_refuses_raw_pans_whatever_the_caller_did(
    store: CaseStore, over: dict[str, Any]
) -> None:
    sc = StoredCase(case=_case(**over))
    with pytest.raises(RawPanRefusedError):
        assert_no_raw_pan(sc)
    with pytest.raises(RawPanRefusedError):
        store.write_run(
            source="csv",
            input_sha256="0" * 64,
            label="t",
            cases=[sc],
            movements=[],
            errors=[],
            report={},
        )
    assert store.runs() == [] and store.cases() == []


def test_store_refuses_fake_envelopes() -> None:
    with pytest.raises(RawPanRefusedError):
        assert_no_raw_pan(StoredCase(case=_case(), encrypted={"x": "plain text"}))


def test_store_is_append_only(store: CaseStore) -> None:
    ingest(json.dumps([real_dispute()]), IngestOptions(source="stripe"), store)
    store.close()
    db = sqlite3.connect(store.path)
    for sql in (
        "UPDATE cases SET body_json = '{}'",
        "DELETE FROM cases",
        "DELETE FROM ingest_runs",
    ):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            db.execute(sql)
    db.close()


def test_store_rejects_other_sqlite_files(tmp_path: Path) -> None:
    other = tmp_path / "other.db"
    db = sqlite3.connect(other)
    db.execute("CREATE TABLE t (x)")
    db.commit()
    db.close()
    with pytest.raises(StoreError, match="not a scruple case store"):
        CaseStore(other)
    with pytest.raises(StoreError, match="no case store"):
        CaseStore(tmp_path / "missing.db", create=False)
    (tmp_path / "junk.db").write_bytes(b"not sqlite at all, really not" * 10)
    with pytest.raises(StoreError):
        CaseStore(tmp_path / "junk.db")


# --- keys ----------------------------------------------------------------------------


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def test_keys_from_env() -> None:
    env = {
        "SCRUPLE_TOKEN_KEYS": f"new:{_b64(b'n' * 32)}, old:{_b64(b'o' * 40)}",
        "SCRUPLE_FIELD_KEYS": f"f1:{_b64(FIELD_KEY)}",
        "SCRUPLE_ADYEN_HMAC_KEYS": "aa, bb",
    }
    tok = tokenizer_from_env(env)
    assert tok is not None and tok.tokenize(PAN).key_id == "new"
    cipher = cipher_from_env(env)
    assert cipher is not None and cipher.active_key_id == "f1"
    assert adyen_keys_from_env(env) == ("aa", "bb")
    assert tokenizer_from_env({}) is None and cipher_from_env({}) is None


@pytest.mark.parametrize(
    ("raw", "env"),
    [
        ("nokid", "SCRUPLE_TOKEN_KEYS"),
        ("a:@@@", "SCRUPLE_TOKEN_KEYS"),
        (f"a:{_b64(b'x' * 32)},a:{_b64(b'y' * 32)}", "SCRUPLE_TOKEN_KEYS"),
        (f"a:{_b64(b'short')}", "SCRUPLE_TOKEN_KEYS"),
        (f"a:{_b64(b'x' * 31)}", "SCRUPLE_FIELD_KEYS"),
        (",", "SCRUPLE_FIELD_KEYS"),
    ],
)
def test_bad_keys_are_refused_without_echoing_them(raw: str, env: str) -> None:
    load = tokenizer_from_env if env == "SCRUPLE_TOKEN_KEYS" else cipher_from_env
    with pytest.raises(KeyConfigError) as ei:
        load({env: raw})
    secret = raw.partition(":")[2].split(",")[0]
    assert not secret or secret not in str(ei.value)


def test_parse_key_ring_first_is_active() -> None:
    keys, active = parse_key_ring(f"b:{_b64(b'1' * 32)},a:{_b64(b'2' * 32)}", "X")
    assert active == "b" and sorted(keys) == ["a", "b"]


# --- verify ----------------------------------------------------------------------------


def test_verify_summary_suppresses_small_cells(store: CaseStore) -> None:
    rows = [row(i) for i in range(1, 26)] + [row(30, why="other")]
    ingest(csv_text(rows), IngestOptions(source="csv", import_config=cfg()), store)
    s = summarise(store, k=10)
    assert s.ok and s.cases == 26 and s.raw_pans_found == 0
    assert s.by_reason.cells == {"fraud": 25} or s.by_reason.suppressed >= 10
    assert all(v == 0 or v >= 10 for v in s.by_reason.cells.values())
    assert "other" not in s.by_reason.cells
    assert all(v >= 10 for v in s.by_state.cells.values())


def test_verify_detects_a_planted_pan(tmp_path: Path) -> None:
    st_ = CaseStore(tmp_path / "c.db")
    ingest(json.dumps([real_dispute()]), IngestOptions(source="stripe"), st_)
    st_._db.execute("INSERT INTO store_meta(key, value) VALUES ('planted', ?)", (PAN,))
    s = summarise(st_)
    assert s.raw_pans_found == 1 and not s.ok


def test_verify_checks_envelopes_with_the_key(store: CaseStore) -> None:
    text = csv_text([row(1, email="a@example.com")], extra=("email",))
    opts = IngestOptions(source="csv", import_config=cfg(), encrypt_columns=("email",))
    ingest(text, opts, store, cipher=CIPHER)
    assert summarise(store, cipher=CIPHER).envelopes_checked == 1
    other = FieldCipher({"f1": b"z" * 32}, "f1")
    assert summarise(store, cipher=other).envelopes_failed == 1


# --- properties ----------------------------------------------------------------------

_cell = st.one_of(
    st.text(
        alphabet=st.characters(codec="utf-8", exclude_characters="\x00"),
        max_size=12,
    ),
    st.builds(
        lambda p: f"note {p}", st.sampled_from([PAN, pan(), pan("5555", 16), pan("3782", 15)])
    ),
)


@given(
    rows=st.lists(
        st.fixed_dictionaries(
            {
                "amount": st.sampled_from(["12.50", "1", "-3", "x", "0.001"]),
                "state": st.sampled_from(["won", "lost", "???"]),
                "why": st.sampled_from(["fraud", "other", "nope"]),
                "merchant": _cell,
                "notes": _cell,
                "card": st.sampled_from([PAN, pan(), "", "123"]),
            }
        ),
        min_size=1,
        max_size=25,
    )
)
def test_property_every_row_accounted_for_and_no_pan_stored(
    rows: list[dict[str, str]], tmp_path_factory: pytest.TempPathFactory
) -> None:
    path = tmp_path_factory.mktemp("prop") / "c.db"
    st_ = CaseStore(path)
    text = csv_text([row(i, **r) for i, r in enumerate(rows, 1)], extra=("notes", "card"))
    opts = IngestOptions(
        source="csv", import_config=cfg(allow_extra_columns=True), pan_columns=("card",)
    )
    rep = ingest(text, opts, st_, tokenizer=TOK)
    assert rep.rows_read == len(rows) == rep.rows_ok + rep.rows_failed
    assert rep.cases == rep.rows_ok == len(st_.cases())
    assert summarise(st_).raw_pans_found == 0
    raw = file_bytes(st_)
    for r in rows:
        for v in (r["card"], r["notes"], r["merchant"]):
            for p in (PAN, pan(), pan("5555", 16), pan("3782", 15)):
                if p in v:
                    assert p.encode() not in raw


@given(data=st.binary(max_size=400))
def test_property_garbage_raises_only_ingest_errors(data: bytes) -> None:
    for source in ("stripe", "adyen"):
        try:
            rep = ingest(data, IngestOptions(source=source, verify_adyen=False), None)
        except IngestError:
            continue
        assert rep.rows_read == rep.rows_ok + rep.rows_failed


# --- CLI ------------------------------------------------------------------------------


def test_cli_ingest_and_verify(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = tmp_path / "disputes.json"
    src.write_text(json.dumps([real_dispute(), real_dispute(id="dp_bad", status="?")]))
    db = tmp_path / "cases.db"
    rc = main(
        [
            "ingest",
            "stripe",
            "--in",
            str(src),
            "--store",
            str(db),
            "--report",
            str(tmp_path / "r.json"),
        ]
    )
    out = capsys.readouterr().out
    assert rc == 1 and "1 ok, 1 failed" in out and "row 2" in out
    assert json.loads((tmp_path / "r.json").read_text())["rows_failed"] == 1
    assert main(["ingest", "verify", "--store", str(db)]) == 0
    assert "STORE OK" in capsys.readouterr().out
    assert main(["ingest", "verify", "--store", str(db), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["cases"] == 1


def test_cli_csv_with_pan_column_uses_env_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    src = tmp_path / "cases.csv"
    src.write_text(csv_text([row(1, card=PAN)], extra=("card",)))
    conf = tmp_path / "import.json"
    conf.write_text(cfg().model_dump_json())
    db = tmp_path / "c.db"
    args = [
        "ingest",
        "csv",
        "--in",
        str(src),
        "--store",
        str(db),
        "--config",
        str(conf),
        "--pan-column",
        "card",
    ]
    assert main(args) == 1  # no key: the row fails, nothing is stored
    capsys.readouterr()
    monkeypatch.setenv("SCRUPLE_TOKEN_KEYS", f"k1:{_b64(TOKEN_KEY)}")
    assert main(args) == 0
    assert "1 card numbers tokenised" in capsys.readouterr().out
    assert PAN.encode() not in db.read_bytes()


def test_cli_errors_exit_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = tmp_path / "x.json"
    src.write_text("{not json")
    assert main(["ingest", "stripe", "--in", str(src), "--store", str(tmp_path / "c.db")]) == 2
    (tmp_path / "plain.db").write_bytes(b"")
    db = sqlite3.connect(tmp_path / "plain.db")
    db.execute("CREATE TABLE t (x)")
    db.commit()
    db.close()
    assert main(["ingest", "verify", "--store", str(tmp_path / "plain.db")]) == 2
    assert main(["ingest", "verify", "--store", str(tmp_path / "missing.db")]) == 2
    assert "error:" in capsys.readouterr().err
