"""PII redaction: each detector, adversarial formats, records, determinism, ReDoS bounds.
All names, emails and numbers below are synthetic (test PANs are Luhn-valid examples)."""

from __future__ import annotations

import re
import time

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from scruple.ingest.bank.models import StatementEntry
from scruple.ingest.errors import InputTooLargeError
from scruple.security.pan import luhn_check_digit, luhn_valid
from scruple.security.redact import MAX_TEXT_CHARS, redact_record, redact_text

PAN = "4242424242424242"


def red(text: str, **kw: object) -> str:
    return redact_text(text, **kw).text  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("mail ada@example.com now", "mail [EMAIL] now"),
        ("ADA.LOVELACE+disputes@Mail.Example.CO.UK", "[EMAIL]"),
        ("ada [at] example [dot] com", "[EMAIL]"),
        ("ada(at)example(dot)com", "[EMAIL]"),
        ("ada at example dot com", "[EMAIL]"),
        ("ada＠example．com", "[EMAIL]"),  # fullwidth @ and .  # noqa: RUF001
        ("ad​a@exa‍mple.com", "[EMAIL]"),  # zero-width joiners
        (f"card {PAN}.", "card [PAN]."),
        ("card 4242 4242 4242 4242", "card [PAN]"),
        ("card 4242-4242-4242-4242", "card [PAN]"),
        ("card 4242.4242.4242.4242", "card [PAN]"),
        ("card 4242\n4242\n4242\n4242", "card [PAN]"),
        ("card ４２４２" * 1 + "424242424242", "card [PAN]"),  # fullwidth digits  # noqa: RUF001
        ("card 4​242424242424242", "card [PAN]"),
        ("amex 378282246310005", "amex [PAN]"),
        ("acct 123456789012", "acct [NUMBER]"),  # long digit run, not Luhn
        ("call +44 20 7946 0958", "call [PHONE]"),
        ("call (555) 010-4477", "call [PHONE]"),
        ("call 555.010.4477", "call [PHONE]"),
        ("from 192.168.10.1", "from [IP]"),
        ("from 2001:db8::1", "from [IP]"),
        ("lives at 221B Baker Street, London", "lives at [ADDRESS], London"),
        ("1600 Pennsylvania Avenue NW", "[ADDRESS] NW"),
        ("12 Elm Rd, Apt 4B", "[ADDRESS]"),
        ("PO Box 1234", "[ADDRESS]"),
        ("Ann Arbor, MI 48109-1234", "Ann Arbor, [ADDRESS]"),
        ("postcode SW1A 1AA", "postcode [ADDRESS]"),
        ("My name is Ada Lovelace.", "My name is [NAME]."),
        ("cardholder Grace Hopper called", "cardholder [NAME] called"),
        ("Mrs. Katherine Johnson", "Mrs. [NAME]"),
        ("Attn: Alan Turing", "Attn: [NAME]"),
        ("IBAN GB82 WEST 1234 5698 7654 32", "IBAN [IBAN]"),
        ("IBAN GB82WEST12345698765432", "IBAN [IBAN]"),
    ],
)
def test_detectors(text: str, expected: str) -> None:
    assert red(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "order 2026-01-05 total 1170.25 EUR",
        "charge ch_1PgafuB7WZ01zgkW and dispute dp_1Pgc71B7WZ01zgkWMevJiAUx",
        "network reason code 10.4, case_type inquiry",
        "timestamp 1723679999",
        "version 1.2.3.4.5",
        "R01 Insufficient Funds",
    ],
)
def test_no_false_positives_on_ordinary_ops_text(text: str) -> None:
    assert red(text) == text


def test_bad_iban_checksum_is_not_an_iban_but_digits_still_go() -> None:
    r = redact_text("IBAN GB00 WEST 1234 5698 7654 32")
    assert r.counts() == {"NUMBER": 1}


def test_known_names_cover_uncued_mentions_and_variants() -> None:
    text = "Spoke to lovelace, ADA; ada  lovelace confirmed. Ada-Lovelace too."
    out = red(text, known_names=["Ada Lovelace"])
    assert "ada" not in out.lower()
    assert "lovelace" not in out.lower()


def test_uncued_bare_name_is_a_documented_limit() -> None:
    # Pinned on purpose: without a cue or known_names a bare name is not found.
    assert red("Ada Lovelace phoned") == "Ada Lovelace phoned"


def test_findings_and_counts() -> None:
    r = redact_text(f"a@b.co {PAN} b@c.co")
    assert r.counts() == {"EMAIL": 2, "PAN": 1}
    assert [f.kind for f in r.findings] == ["EMAIL", "PAN", "EMAIL"]


def test_record_redaction() -> None:
    rec = {
        "customer_name": "Ada Lovelace",
        "Customer-Email-Address": "ada@example.com",
        "billing_address": {"line1": "1 Main St", "city": "Springfield", "postal_code": "12345"},
        "card": {"number": PAN, "last4": "4242", "exp_month": 12},
        "notes": [f"paid with {PAN}", "call +1 555 010 4477", None, True, 3.5],
        "amount": 4242424242424242,
        "count": 3,
        "shopperIP": "10.0.0.1",
        "name": "",
    }
    out = redact_record(rec, known_names=["Ada Lovelace"])
    assert out["customer_name"] == "[NAME]"
    assert out["Customer-Email-Address"] == "[EMAIL]"
    assert out["billing_address"] == "[ADDRESS]"
    assert out["card"] == {"number": "[NUMBER]", "last4": "4242", "exp_month": 12}
    assert out["notes"] == ["paid with [PAN]", "call [PHONE]", None, True, 3.5]
    assert out["amount"] == "[PAN]"
    assert out["count"] == 3
    assert out["shopperIP"] == "[IP]"
    assert out["name"] == ""
    assert "Ada" not in str(out)


def test_record_depth_and_size_limits() -> None:
    deep: object = "x"
    for _ in range(40):
        deep = {"a": deep}
    with pytest.raises(InputTooLargeError):
        redact_record(deep)
    with pytest.raises(InputTooLargeError):
        redact_text("a" * (MAX_TEXT_CHARS + 1))


def test_bank_entries_are_redacted_on_construction() -> None:
    e = StatementEntry(
        amount_minor=1, direction=None, description=f"refund ada@example.com card {PAN}"
    )
    assert e.description == "refund [EMAIL] card [PAN]"


@pytest.mark.parametrize(
    "attack",
    [
        "a." * 30_000,
        "1-" * 30_000,
        "Mr " * 20_000,
        "a at " * 20_000,
        "1 " * 50_000,
        "@." * 50_000,
        "Aa " * 50_000,
        "1a " * 50_000,
        "0" * 100_000,
        ":" * 100_000,
    ],
)
def test_hostile_text_runs_in_bounded_time(attack: str) -> None:
    t0 = time.perf_counter()
    redact_text(attack)
    assert time.perf_counter() - t0 < 5.0


# --- properties -----------------------------------------------------------------


@st.composite
def pans(draw: st.DrawFn) -> str:
    prefix = draw(st.sampled_from(["4", "51", "55", "2221", "34", "37", "6011", "35"]))
    length = draw(st.integers(min_value=13, max_value=19))
    body = prefix + "".join(
        draw(st.sampled_from("0123456789")) for _ in range(length - len(prefix) - 1)
    )
    return body + luhn_check_digit(body)


SEPARATORS = st.sampled_from(["", " ", "-", ".", "\n", "​", "⁠"])
NOISE = st.text(alphabet="abcdefghij XYZ,;:!?()[]'\"", max_size=30)


def _format(pan: str, sep: str, group: int) -> str:
    return sep.join(pan[i : i + group] for i in range(0, len(pan), group))


@settings(max_examples=300, deadline=None)
@given(pans(), SEPARATORS, st.sampled_from([4, 3, 5, 19]), NOISE, NOISE, st.booleans())
def test_no_generated_pan_survives(
    pan: str, sep: str, group: int, pre: str, post: str, wide: bool
) -> None:
    assert luhn_valid(pan)
    shown = _format(pan, sep, group)
    if wide:
        shown = "".join(chr(ord(c) + 0xFEE0) if c.isdigit() else c for c in shown)
    out = red(f"{pre} {shown} {post}")
    assert pan not in re.sub(r"[^0-9]", "", out)
    assert pan[-8:] not in re.sub(r"[^0-9]", "", out)


@settings(max_examples=200, deadline=None)
@given(st.emails())
def test_generated_emails_are_redacted(email: str) -> None:
    out = red(f"contact {email} today")
    assert email not in out
    assert "[EMAIL]" in out


@settings(max_examples=300, deadline=None)
@given(st.text(max_size=200))
def test_deterministic_and_idempotent(text: str) -> None:
    once = red(text)
    assert red(text) == once
    assert red(once) == once
