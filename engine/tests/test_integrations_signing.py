"""SSRF rules for outbound HTTP, and every HMAC scheme the integrations use.

No test touches the network: requests go to an `httpx.MockTransport` and names
resolve through a fake resolver."""

from __future__ import annotations

import ipaddress
from typing import Any

import httpx
import pytest
from hypothesis import given
from hypothesis import strategies as st

from scruple.integrations.safehttp import SafeHttp, UnsafeURLError, address_problem, check_url
from scruple.integrations.signing import (
    LinkSigner,
    SignatureError,
    sign_webhook,
    signature_header,
    slack_signature,
    verify_slack,
    verify_webhook,
)

PUBLIC = "93.184.215.14"


def resolver(*addrs: str) -> Any:
    return lambda host, port: list(addrs)


# --- addresses ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "addr",
    [
        "127.0.0.1",
        "127.9.9.9",
        "10.0.0.1",
        "172.16.0.1",
        "172.31.255.255",
        "192.168.1.1",
        "169.254.169.254",  # cloud metadata
        "100.64.0.1",  # carrier-grade NAT
        "0.0.0.0",
        "224.0.0.1",
        "240.0.0.1",
        "255.255.255.255",
        "192.0.2.1",  # documentation
        "::1",
        "::",
        "fe80::1",
        "fc00::1",
        "fd12:3456::1",
        "ff02::1",
        "::ffff:127.0.0.1",
        "::ffff:8.8.8.8",  # mapped: refused whatever it maps to
        "2001:db8::1",
        "not-an-ip",
    ],
)
def test_blocked_addresses(addr: str) -> None:
    assert address_problem(addr) is not None


@pytest.mark.parametrize("addr", [PUBLIC, "8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"])
def test_public_addresses(addr: str) -> None:
    assert address_problem(addr) is None


@given(st.integers(0, 2**24 - 1))
def test_all_of_ten_slash_eight_is_blocked(n: int) -> None:
    assert address_problem(str(ipaddress.IPv4Address(0x0A000000 + n))) is not None


@given(st.integers(0, 2**16 - 1))
def test_all_of_link_local_is_blocked(n: int) -> None:
    assert address_problem(str(ipaddress.IPv4Address(0xA9FE0000 + n))) is not None


# --- URLs ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "needle"),
    [
        ("http://hooks.example.com/x", "scheme"),
        ("ftp://hooks.example.com/x", "scheme"),
        ("file:///etc/passwd", "scheme"),
        ("gopher://hooks.example.com/", "scheme"),
        ("https://user:pw@hooks.example.com/", "credentials"),
        ("https://hooks.example.com@evil.example/", "credentials"),
        ("https://hooks.example.com/x#frag", "fragment"),
        ("https://hooks.example.com:22/", "port"),
        ("https://hooks.example.com:80/", "port"),
        ("https://127.0.0.1/", "blocked"),
        ("https://[::1]/", "blocked"),
        ("https://169.254.169.254/latest/meta-data/", "blocked"),
        ("https://bad_host.example/", "valid hostname"),
        ("https:///nohost", "no host"),
        ("https://hooks.example.com:99999/", "malformed"),
        ("https://" + "a" * 2_100 + ".com/", "too long"),
    ],
)
def test_url_rules(url: str, needle: str) -> None:
    with pytest.raises(UnsafeURLError, match=needle):
        check_url(url, resolver=resolver(PUBLIC))


def test_a_name_with_any_private_address_is_refused() -> None:
    with pytest.raises(UnsafeURLError, match="blocked"):
        check_url("https://hooks.example.com/", resolver=resolver(PUBLIC, "10.1.2.3"))
    with pytest.raises(UnsafeURLError, match="no addresses"):
        check_url("https://hooks.example.com/", resolver=resolver())


def test_allow_hosts_and_http_for_development() -> None:
    t = check_url(
        "http://receiver.internal:8080/hook",
        allow_http=True,
        resolver=resolver("10.0.0.5"),
        allow_hosts=("receiver.internal",),
    )
    assert t.pinned_url == "http://10.0.0.5:8080/hook" and t.host == "receiver.internal"
    with pytest.raises(UnsafeURLError):  # the exemption is per host
        check_url("http://other.internal:8080/", allow_http=True, resolver=resolver("10.0.0.5"))


def test_requests_are_pinned_to_the_checked_address() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(302, headers={"Location": "https://127.0.0.1/"}, content=b"x" * 10)

    http = SafeHttp(
        transport=httpx.MockTransport(handler),
        resolver=resolver("2606:4700:4700::1111"),
        max_response_bytes=4,
    )
    r = http.post("https://hooks.example.com/in?a=1", content=b"{}")
    assert r.status_code == 302 and r.body == b"xxxx"  # never followed, body capped
    (req,) = seen
    assert req.url.host == "2606:4700:4700::1111" and req.url.path == "/in"
    assert req.url.query == b"a=1"
    assert req.headers["host"] == "hooks.example.com"
    assert req.extensions["sni_hostname"] == "hooks.example.com"
    http.close()


def test_rebinding_between_checks_is_caught_on_each_request() -> None:
    answers = [[PUBLIC], ["127.0.0.1"]]
    http = SafeHttp(
        transport=httpx.MockTransport(lambda r: httpx.Response(200)),
        resolver=lambda h, p: answers.pop(0),
    )
    assert http.get("https://hooks.example.com/").status_code == 200
    with pytest.raises(UnsafeURLError):
        http.get("https://hooks.example.com/")


# --- outbound webhook signatures ------------------------------------------------------

# openssl: printf '1700000000.{"a":1}' | openssl dgst -sha256 -hmac 'whsec_test'
VECTOR = "38877139021993b830af32feea6e18a8da83eb2f6e49ee50bd9e4cf4ca4d3789"


def test_webhook_signature_vector() -> None:
    assert sign_webhook("whsec_test", 1_700_000_000, b'{"a":1}') == VECTOR
    assert (
        signature_header(["whsec_test"], 1_700_000_000, b'{"a":1}') == f"t=1700000000,v1={VECTOR}"
    )


def test_webhook_verify_round_trip_and_rotation() -> None:
    body = b'{"id":"evt_1"}'
    header = signature_header(["new", "old"], 1_000, body)
    assert verify_webhook(header, body, ["old"], now=1_100) == 1_000
    assert verify_webhook(header, body, ["new"], now=1_100) == 1_000
    with pytest.raises(SignatureError, match="no signature"):
        verify_webhook(header, body + b" ", ["new"], now=1_100)
    with pytest.raises(SignatureError, match="no signature"):
        verify_webhook(header, body, ["other"], now=1_100)
    with pytest.raises(SignatureError, match="tolerance"):
        verify_webhook(header, body, ["new"], now=1_301)
    with pytest.raises(SignatureError, match="tolerance"):
        verify_webhook(header, body, ["new"], now=699)
    with pytest.raises(ValueError):
        signature_header([], 1, body)


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "t=1",
        "v1=" + "a" * 64,
        "t=1,t=2,v1=" + "a" * 64,
        "t=x,v1=" + "a" * 64,
        "t=1,v1=zz",
        "t=1,v1=" + "A" * 64,
        "garbage",
        "t=1," + ",".join(["v1=" + "a" * 64] * 9),
        "t=1,v1=" + "a" * 5_000,
    ],
)
def test_malformed_webhook_headers(header: str | None) -> None:
    with pytest.raises(SignatureError):
        verify_webhook(header, b"{}", ["s"], now=1)


# --- Slack ------------------------------------------------------------------------------

# The worked example in Slack's "Verifying requests from Slack" documentation.
SLACK_SECRET = "8f742231b10e8888abcd99yyyzzz85a5"
SLACK_TS = "1531420618"
SLACK_BODY = (
    b"token=xyzz0WbapA4vBCDEFasx0q6G&team_id=T1DC2JH3J&team_domain=testteamnow&"
    b"channel_id=G8PSS9T3V&channel_name=foobar&user_id=U2CERLKJA&user_name=roadrunner&"
    b"command=%2Fwebhook-collect&text=&response_url=https%3A%2F%2Fhooks.slack.com%2F"
    b"commands%2FT1DC2JH3J%2F397700885554%2F96rGlfmibIGlgcZRskXaIFfN&"
    b"trigger_id=398738663015.47445629121.803a0bc887a14d10d2c447fce8b6703c"
)
SLACK_SIG = "v0=a2114d57b48eac39b9ad189dd8316235a7b4a8d21a10bd27519666489c69b503"


def test_slack_documented_vector() -> None:
    assert slack_signature(SLACK_SECRET, SLACK_TS, SLACK_BODY) == SLACK_SIG
    now = int(SLACK_TS) + 10
    assert verify_slack([SLACK_SECRET], SLACK_TS, SLACK_SIG, SLACK_BODY, now) == int(SLACK_TS)
    assert verify_slack(["rotated", SLACK_SECRET], SLACK_TS, SLACK_SIG, SLACK_BODY, now)


@pytest.mark.parametrize(
    ("ts", "sig", "body", "now", "needle"),
    [
        (SLACK_TS, SLACK_SIG, SLACK_BODY + b"x", 1531420618, "does not match"),
        (SLACK_TS, SLACK_SIG, SLACK_BODY, 1531420618 + 301, "five minutes"),
        (SLACK_TS, SLACK_SIG, SLACK_BODY, 1531420618 - 301, "five minutes"),
        (None, SLACK_SIG, SLACK_BODY, 1531420618, "Timestamp"),
        ("12a", SLACK_SIG, SLACK_BODY, 1531420618, "Timestamp"),
        (SLACK_TS, None, SLACK_BODY, 1531420618, "Signature"),
        (SLACK_TS, "v1=" + SLACK_SIG[3:], SLACK_BODY, 1531420618, "Signature"),
        (SLACK_TS, SLACK_SIG[:-1], SLACK_BODY, 1531420618, "Signature"),
    ],
)
def test_slack_refusals(
    ts: str | None, sig: str | None, body: bytes, now: int, needle: str
) -> None:
    with pytest.raises(SignatureError, match=needle):
        verify_slack([SLACK_SECRET], ts, sig, body, now)


# --- approval links ------------------------------------------------------------------------

K1, K2 = b"\x01" * 32, b"\x02" * 32


def test_links_round_trip_and_rotate() -> None:
    old = LinkSigner({"k1": K1}, "k1")
    token = old.mint({"a": "approve", "n": "abc"})
    assert old.verify(token) == ("k1", {"a": "approve", "n": "abc"})
    rotated = LinkSigner({"k2": K2, "k1": K1}, "k2")
    assert rotated.verify(token)[0] == "k1"  # old links keep working until k1 is removed
    assert rotated.mint({"x": 1}).split(".")[1] == "k2"
    with pytest.raises(SignatureError, match="no longer accepted"):
        LinkSigner({"k2": K2}, "k2").verify(token)
    assert "\\x01" not in repr(old) and "k1" in repr(old)


def _swap(token: str, i: int, value: str) -> str:
    parts = token.split(".")
    parts[i] = value
    return ".".join(parts)


def test_tampered_or_malformed_links_are_refused() -> None:
    s = LinkSigner({"k1": K1}, "k1")
    token = s.mint({"a": "approve"})
    other = s.mint({"a": "reject"})
    bad = [
        _swap(token, 2, other.split(".")[2]),  # claims of another link
        _swap(token, 3, other.split(".")[3]),
        _swap(token, 0, "v2"),
        token + ".x",
        "garbage",
        _swap(token, 2, "!!!"),
        "v1.k1." + "a" * 3_000 + ".x",
    ]
    for t in bad:
        with pytest.raises(SignatureError):
            s.verify(t)
    # a genuine signature over a non-object body
    import base64
    import hashlib
    import hmac

    body = base64.urlsafe_b64encode(b"[1]").decode().rstrip("=")
    mac = hmac.new(K1, f"scruple-approval-link:v1:k1:{body}".encode(), hashlib.sha256).digest()
    forged = f"v1.k1.{body}.{base64.urlsafe_b64encode(mac).decode().rstrip('=')}"
    with pytest.raises(SignatureError, match="malformed"):
        s.verify(forged)


@pytest.mark.parametrize(
    ("keys", "active"),
    [({"k1": b"short"}, "k1"), ({"k1": K1}, "k2"), ({"bad id!": K1}, "bad id!")],
)
def test_link_key_rules(keys: dict[str, bytes], active: str) -> None:
    with pytest.raises(ValueError):
        LinkSigner(keys, active)
