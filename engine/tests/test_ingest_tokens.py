"""Card tokenisation: Luhn, keyed HMAC tokens with BIN and last4, rotation, and
property tests that no raw PAN reaches storage, errors, reprs or logs."""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from scruple.security.pan import (
    CardToken,
    InvalidPanError,
    PanScrubbingFilter,
    PanTokenizer,
    luhn_check_digit,
    luhn_valid,
    normalise_pan,
    scrub_pans,
)

K1 = b"k" * 32
K2 = b"q" * 32
TOK = PanTokenizer({"k1": K1, "k2": K2}, active="k1")


@pytest.mark.parametrize(
    ("digits", "ok"),
    [
        ("4242424242424242", True),
        ("4000056655665556", True),
        ("378282246310005", True),
        ("6011111111111117", True),
        ("79927398713", True),  # the classic Luhn example
        ("4242424242424241", False),
        ("0", True),
        ("", False),
        ("42a2", False),
        ("４２", False),  # non-ASCII digits are not digits here  # noqa: RUF001
    ],
)
def test_luhn(digits: str, ok: bool) -> None:
    assert luhn_valid(digits) is ok


@pytest.mark.parametrize(
    "raw",
    [
        "4242 4242 4242 4242",
        "4242-4242-4242-4242",
        " 4242424242424242 ",
        "4242​424242424242",
        "４２４２424242424242",  # noqa: RUF001
    ],
)
def test_normalise_accepts_formats(raw: str) -> None:
    assert normalise_pan(raw) == "4242424242424242"


@pytest.mark.parametrize(
    "raw",
    [
        "4242424242424241",
        "424242424242",
        "42424242424242424242",
        "4242x424242424242",
        "",
        "4" * 100,
    ],
)
def test_invalid_pans_raise_without_echoing_input(raw: str) -> None:
    with pytest.raises(InvalidPanError) as e:
        TOK.tokenize(raw)
    assert raw.strip() == "" or raw[:12] not in str(e.value)


def test_token_shape_and_determinism() -> None:
    t = TOK.tokenize("4242 4242 4242 4242")
    assert t == TOK.tokenize("4242424242424242")
    assert (t.bin6, t.last4, t.length, t.key_id) == ("424242", "4242", 16, "k1")
    assert t.token.startswith("ctok_")
    assert "4242424242424242" not in t.model_dump_json()
    other = TOK.tokenize("4000056655665556")
    assert other.token != t.token


def test_keys_matter_and_rotation() -> None:
    old = TOK.tokenize("4242424242424242", key_id="k2")
    assert old.token != TOK.tokenize("4242424242424242").token
    assert TOK.matches(old, "4242-4242-4242-4242")
    assert not TOK.matches(old, "4000056655665556")
    assert not TOK.matches(old, "garbage")
    new = TOK.rotate(old, "4242424242424242")
    assert new.key_id == "k1"
    with pytest.raises(InvalidPanError):
        TOK.rotate(old, "4000056655665556")
    retired = PanTokenizer({"k1": K1}, active="k1")
    assert not retired.matches(old, "4242424242424242")
    with pytest.raises(InvalidPanError):
        TOK.tokenize("4242424242424242", key_id="nope")


def test_tokenizer_config_and_repr() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        PanTokenizer({"a": b"short"}, active="a")
    with pytest.raises(ValueError, match="active"):
        PanTokenizer({"a": K1}, active="b")
    with pytest.raises(ValueError, match="key ids"):
        PanTokenizer({"bad id": K1}, active="bad id")
    assert "k" * 32 not in repr(TOK)
    assert "k1" in repr(TOK)


def test_token_model_rejects_raw_pan() -> None:
    with pytest.raises(ValueError):
        CardToken(token="4242424242424242", key_id="k1", bin6="424242", last4="4242", length=16)


def test_log_filter_scrubs_messages_and_args(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("scruple.test.tokens")
    logger.addFilter(PanScrubbingFilter())
    with caplog.at_level(logging.DEBUG, logger="scruple.test.tokens"):
        logger.warning("charge for %s failed", "4242 4242 4242 4242")
        logger.warning("raw 4000056655665556 in message")
        logger.warning("bad format %s %s", "only one")
    assert "4242 4242" not in caplog.text
    assert "4000056655665556" not in caplog.text
    assert caplog.text.count("[PAN]") == 2


def test_scrub_leaves_non_cards() -> None:
    assert scrub_pans("order 123456789012 on 2026-01-05") == "order 123456789012 on 2026-01-05"


# --- properties: no raw PAN reaches storage or logs -----------------------------


@st.composite
def pans(draw: st.DrawFn) -> str:
    prefix = draw(st.sampled_from(["4", "51", "55", "2221", "34", "37", "6011", "35", "62"]))
    length = draw(st.integers(min_value=13, max_value=19))
    body = prefix + "".join(
        draw(st.sampled_from("0123456789")) for _ in range(length - len(prefix) - 1)
    )
    return body + luhn_check_digit(body)


@settings(
    max_examples=200, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(pans(), st.sampled_from(["", " ", "-"]))
def test_pan_never_reaches_storage_or_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, pan: str, sep: str
) -> None:
    assert luhn_valid(pan)
    raw = sep.join(pan[i : i + 4] for i in range(0, len(pan), 4))
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="scruple.security.pan"):
        token = TOK.tokenize(raw)
        logging.getLogger("scruple.security.pan").info("echo %s", raw)
    db = tmp_path / "cards.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE IF NOT EXISTS cards (token TEXT, body TEXT)")
    con.execute("INSERT INTO cards VALUES (?, ?)", (token.token, token.model_dump_json()))
    con.commit()
    con.close()
    blob = db.read_bytes() + json.dumps(token.model_dump()).encode() + repr(token).encode()
    assert pan.encode() not in blob
    assert pan not in caplog.text
    assert raw not in caplog.text
    assert token.last4 == pan[-4:]
    assert token.bin6 == pan[:6]
    db.unlink()
