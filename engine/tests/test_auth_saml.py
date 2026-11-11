"""SAML sign-on (D07): SP-initiated login, signed-assertion verification by
python3-saml/xmlsec, attribute mapping to roles and tenant, the console session.

The IdP is local: a key and certificate generated per test module and responses
signed with xmlsec (tests/saml_support.py). Every attack below must be refused."""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scruple.auth.deps import iter_routes, protection_of
from scruple.auth.saml import (
    RelayState,
    ReplayCache,
    SAMLConfig,
    SAMLError,
    SAMLServiceProvider,
    safe_next,
)
from scruple.auth.session import COOKIE, SessionSigner, cookie_value, in_scope
from scruple.console.app import create_console_app
from scruple.service import AgentGate
from scruple.storage import SQLiteStorage
from tests.saml_support import (
    ACS_URL,
    IDP_ENTITY,
    IDP_SSO,
    SP_ENTITY,
    KeyPair,
    assertion_xml,
    b64,
    make_keypair,
    relay_of,
    request_id_of,
    response_xml,
    sign,
)

SECRET = "saml-test-secret-0123456789abcdef-xyz"


@pytest.fixture(scope="module")
def idp() -> KeyPair:
    return make_keypair()


@pytest.fixture(scope="module")
def other_idp() -> KeyPair:
    return make_keypair("someone else")


@pytest.fixture
def web(idp: KeyPair, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    cert = tmp_path / "idp.pem"
    cert.write_text(idp.cert_pem)
    env = {
        "SCRUPLE_SAML_SP_ENTITY_ID": SP_ENTITY,
        "SCRUPLE_SAML_ACS_URL": ACS_URL,
        "SCRUPLE_SAML_IDP_ENTITY_ID": IDP_ENTITY,
        "SCRUPLE_SAML_IDP_SSO_URL": IDP_SSO,
        "SCRUPLE_SAML_IDP_CERT_FILE": str(cert),
        "SCRUPLE_SAML_ROLE_MAP": "risk-team=risk_admin,examiners=auditor",
        "SCRUPLE_CONSOLE_SECRET": SECRET,
        "SCRUPLE_AUTH_SCRYPT_N": "1024",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    db = SQLiteStorage(":memory:")
    gate = AgentGate(db.for_tenant("acme"))
    gate.bootstrap_baseline()
    app = create_console_app(gate, secret=SECRET.encode())
    with TestClient(app, base_url="https://scruple.test") as c:
        yield c
    db.close()


def _start(c: TestClient, next_path: str = "/console/runs") -> tuple[str, str]:
    r = c.get(f"/auth/saml/login?next={next_path}", follow_redirects=False)
    assert r.status_code == 303
    loc = r.headers["location"]
    assert loc.startswith(IDP_SSO + "?")
    return request_id_of(loc), relay_of(loc)


def _post(c: TestClient, xml: str, relay: str | None) -> Any:
    data = {"SAMLResponse": b64(xml)}
    if relay is not None:
        data["RelayState"] = relay
    return c.post("/auth/saml/acs", data=data, follow_redirects=False)


def _signed(idp: KeyPair, rid: str, **kw: Any) -> str:
    return response_xml(sign(assertion_xml(in_response_to=rid, **kw), idp), in_response_to=rid)


# --- the happy path ---------------------------------------------------------------------


def test_sp_initiated_login_sets_a_console_session(web: TestClient, idp: KeyPair) -> None:
    rid, relay = _start(web)
    r = _post(web, _signed(idp, rid), relay)
    assert r.status_code == 303, r.text
    assert r.headers["location"] == "/console/runs"
    set_cookie = r.headers["set-cookie"]
    assert f"{COOKIE}=" in set_cookie
    for flag in ("HttpOnly", "Secure", "Path=/console", "SameSite=lax"):
        assert flag.lower() in set_cookie.lower(), flag
    page = web.get("/console/runs")
    assert page.status_code == 200
    assert "acting as alice@bank.example" in page.text


def test_roles_map_to_permissions_like_api_keys(web: TestClient, idp: KeyPair) -> None:
    rid, relay = _start(web)
    assert _post(web, _signed(idp, rid, roles=("examiners",)), relay).status_code == 303
    assert web.get("/console/runs").status_code == 200  # auditor: read
    token = web.cookies.get(COOKIE)
    assert token is not None
    claims = SessionSigner(SECRET.encode()).claims(token)
    assert claims is not None and claims.roles == ("auditor",) and claims.tenant == "acme"


def test_the_session_is_for_the_assertions_tenant_only(web: TestClient, idp: KeyPair) -> None:
    rid, relay = _start(web)
    assert _post(web, _signed(idp, rid, tenant="other-bank"), relay).status_code == 303
    # This console serves tenant acme; a user of another tenant sees nothing.
    assert web.get("/console/runs").status_code == 404


def test_metadata_and_routes_declare_their_protection(web: TestClient) -> None:
    r = web.get("/auth/saml/metadata")
    assert r.status_code == 200 and SP_ENTITY in r.text and ACS_URL in r.text
    kinds = {
        route.path: {p.kind for p in protection_of(route)}
        for route in iter_routes(web.app)  # type: ignore[arg-type]
        if route.path.startswith("/auth/saml")
    }
    assert kinds == {
        "/auth/saml/login": {"public"},
        "/auth/saml/metadata": {"public"},
        "/auth/saml/acs": {"signed"},
        "/auth/saml/logout": {"public"},
    }


def test_logout_clears_the_session(web: TestClient, idp: KeyPair) -> None:
    rid, relay = _start(web)
    assert _post(web, _signed(idp, rid), relay).status_code == 303
    assert web.get("/console/runs").status_code == 200
    assert web.post("/auth/saml/logout").status_code == 200
    assert web.get("/console/runs").status_code == 401


# --- attacks: every one is refused and sets no cookie ---------------------------------------


def _refused(r: Any) -> None:
    assert r.status_code == 401, r.text
    assert COOKIE not in r.headers.get("set-cookie", "")


def test_unsigned_assertion_is_refused(web: TestClient) -> None:
    rid, relay = _start(web)
    xml = response_xml(assertion_xml(in_response_to=rid), in_response_to=rid)
    _refused(_post(web, xml, relay))


def test_assertion_signed_by_another_key_is_refused(web: TestClient, other_idp: KeyPair) -> None:
    rid, relay = _start(web)
    _refused(_post(web, _signed(other_idp, rid), relay))


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("alice@bank.example", "mallory@bank.example"),
        (">risk_admin<", ">approver<"),
        (">acme<", ">other<"),
    ],
)
def test_tampering_after_signing_is_refused(
    web: TestClient, idp: KeyPair, old: str, new: str
) -> None:
    rid, relay = _start(web)
    xml = _signed(idp, rid)
    assert old in xml
    _refused(_post(web, xml.replace(old, new), relay))


def test_signature_wrapping_is_refused(web: TestClient, idp: KeyPair) -> None:
    """XSW: keep the genuinely signed assertion, add an unsigned evil one first."""
    rid, relay = _start(web)
    good = sign(assertion_xml(in_response_to=rid), idp)
    evil = assertion_xml(in_response_to=rid, nameid="mallory@bank.example")
    _refused(_post(web, response_xml(evil + good, in_response_to=rid), relay))
    _refused(_post(web, response_xml(good + evil, in_response_to=rid), relay))


def test_comment_in_nameid_does_not_truncate_the_subject(web: TestClient, idp: KeyPair) -> None:
    """CVE-2017-11427 class: text after a comment must not be dropped."""
    rid, relay = _start(web)
    xml = _signed(idp, rid, raw_nameid="admin@bank.example<!-- -->.evil.example")
    assert _post(web, xml, relay).status_code == 303
    claims = SessionSigner(SECRET.encode()).claims(web.cookies[COOKIE])
    assert claims is not None and claims.sub == "admin@bank.example.evil.example"


def test_sha1_signature_is_refused(web: TestClient, idp: KeyPair) -> None:
    rid, relay = _start(web)
    xml = response_xml(sign(assertion_xml(in_response_to=rid), idp, sha1=True), in_response_to=rid)
    _refused(_post(web, xml, relay))


@pytest.mark.parametrize(
    "kw",
    [
        {"audience": "https://another-sp.example"},
        {"issuer": "https://evil-idp.example"},
        {"recipient": "https://evil.example/acs"},
        {"not_on_or_after": datetime.now(UTC) - timedelta(minutes=10)},
    ],
    ids=["audience", "issuer", "recipient", "expired"],
)
def test_wrong_conditions_are_refused(web: TestClient, idp: KeyPair, kw: dict[str, Any]) -> None:
    rid, relay = _start(web)
    _refused(_post(web, _signed(idp, rid, **kw), relay))


def test_wrong_destination_is_refused(web: TestClient, idp: KeyPair) -> None:
    rid, relay = _start(web)
    xml = response_xml(
        sign(assertion_xml(in_response_to=rid), idp),
        in_response_to=rid,
        destination="https://evil.example/acs",
    )
    _refused(_post(web, xml, relay))


def test_unsolicited_and_mismatched_responses_are_refused(web: TestClient, idp: KeyPair) -> None:
    rid, relay = _start(web)
    _refused(_post(web, _signed(idp, rid), None))  # IdP-initiated: no RelayState
    other_rid, _ = _start(web)
    _refused(_post(web, _signed(idp, other_rid), relay))  # answers a different request
    forged = relay[:-4] + ("AAAA" if not relay.endswith("AAAA") else "BBBB")
    _refused(_post(web, _signed(idp, rid), forged))


def test_replayed_response_is_refused(web: TestClient, idp: KeyPair) -> None:
    rid, relay = _start(web)
    xml = _signed(idp, rid)
    assert _post(web, xml, relay).status_code == 303
    web.cookies.clear()
    _refused(_post(web, xml, relay))


@pytest.mark.parametrize(
    "kw",
    [
        {"roles": ()},
        {"roles": ("no-such-role",)},
        {"roles": ("proposer", "approver")},  # separation of duties
        {"tenant": None},  # no default tenant configured
        {"tenant": "Not A Tenant!"},
        {"nameid": "has spaces in it"},
    ],
    ids=[
        "no-roles",
        "unknown-role",
        "propose-and-approve",
        "no-tenant",
        "bad-tenant",
        "bad-subject",
    ],
)
def test_unacceptable_identities_are_refused(
    web: TestClient, idp: KeyPair, kw: dict[str, Any]
) -> None:
    rid, relay = _start(web)
    _refused(_post(web, _signed(idp, rid, **kw), relay))


def test_garbage_is_refused(web: TestClient) -> None:
    _, relay = _start(web)
    _refused(
        web.post("/auth/saml/acs", data={"SAMLResponse": "not base64 !!", "RelayState": relay})
    )
    _refused(web.post("/auth/saml/acs", data={"SAMLResponse": b64("<x/>"), "RelayState": relay}))
    _refused(web.post("/auth/saml/acs", data={"RelayState": relay}))
    assert web.post("/auth/saml/acs", json={"SAMLResponse": "x"}).status_code == 415


def test_xml_entity_expansion_is_refused(web: TestClient) -> None:
    _, relay = _start(web)
    bomb = (
        '<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaaaaaaaa">'
        '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]><r>&b;</r>'
    )
    _refused(web.post("/auth/saml/acs", data={"SAMLResponse": b64(bomb), "RelayState": relay}))


# --- the session cookie ---------------------------------------------------------------------


def test_session_cookie_is_never_accepted_by_the_json_api(idp: KeyPair) -> None:
    from scruple.auth.authn import Authenticator

    signer = SessionSigner(SECRET.encode())
    token = signer.issue("alice@bank.example", "acme", ("risk_admin",), IDP_ENTITY)
    auth = Authenticator(sessions=signer)
    hdr = {"cookie": f"{COOKIE}={token}"}
    assert auth.authenticate(hdr, "/console/runs") is not None
    for path in ("/rules", "/auth/whoami", "/consoleX", "/", None):
        assert auth.authenticate(hdr, path) is None, path


def test_session_tokens_fail_closed() -> None:
    now = [datetime(2026, 11, 11, tzinfo=UTC)]
    signer = SessionSigner(SECRET.encode(), ttl_seconds=600, now=lambda: now[0])
    token = signer.issue("alice@bank.example", "acme", ("approver",), IDP_ENTITY)
    p = signer.principal(token)
    assert (
        p is not None
        and p.method == "saml"
        and {x.value for x in p.permissions} == {"read", "approve"}
    )
    body, mac = token.split(".")
    assert signer.principal(f"{body}.{mac[:-2]}xx") is None
    assert signer.principal(body + "x." + mac) is None
    assert SessionSigner(b"another-secret-0123456789abcdefgh").principal(token) is None
    assert signer.principal("") is None and signer.principal("a.b.c") is None
    now[0] += timedelta(seconds=601)
    assert signer.principal(token) is None
    mixed = SessionSigner(SECRET.encode()).issue("x", "acme", ("proposer", "approver"), IDP_ENTITY)
    assert SessionSigner(SECRET.encode()).principal(mixed) is None
    with pytest.raises(ValueError):
        SessionSigner(b"short")


def test_cookie_parsing_and_scope() -> None:
    assert cookie_value({"cookie": f"a=1; {COOKIE}=tok; b=2"}) == "tok"
    assert cookie_value({"cookie": "a=1"}) is None and cookie_value({}) is None
    assert in_scope("/console") and in_scope("/console/x") and not in_scope("/consolex")


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("/console/runs?q=a", "/console/runs?q=a"),
        ("https://evil.example/", "/console/"),
        ("//evil.example", "/console/"),
        ("/console//evil.example", "/console/"),
        ("/console\\@evil", "/console/"),
        ("/rules", "/console/"),
        (None, "/console/"),
        ("javascript:alert(1)", "/console/"),
    ],
)
def test_next_never_leaves_the_console(target: str | None, expected: str) -> None:
    assert safe_next(target) == expected


def test_relay_state_expires() -> None:
    t = [1_000.0]
    rs = RelayState(SECRET.encode(), 60, lambda: t[0])
    state = rs.make("_id1", "/console/runs")
    assert rs.read(state) == ("_id1", "/console/runs")
    t[0] += 61
    with pytest.raises(SAMLError):
        rs.read(state)


def test_replay_cache_forgets_only_after_expiry() -> None:
    t = [0.0]
    cache = ReplayCache(lambda: t[0], max_size=2)
    assert cache.first_use("a", 1_000)
    assert not cache.first_use("a", 1_000)
    t[0] = 1_001
    assert cache.first_use("a", 2_000)
    assert cache.first_use("b", 2_000)
    assert not cache.first_use("c", 2_000)  # full of live ids: fail closed


def test_partial_saml_settings_are_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from scruple.auth.config import saml_setup
    from scruple.settings import load_settings

    s = load_settings(env={"SCRUPLE_SAML_SP_ENTITY_ID": SP_ENTITY})
    with pytest.raises(ValueError, match="together"):
        saml_setup(s)
    assert saml_setup(load_settings(env={})) is None


def test_saml_needs_the_console_secret(
    idp: KeyPair, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scruple.auth.config import saml_setup
    from scruple.settings import load_settings

    cert = tmp_path / "c.pem"
    cert.write_text(idp.cert_pem)
    env = {
        "SCRUPLE_SAML_SP_ENTITY_ID": SP_ENTITY,
        "SCRUPLE_SAML_ACS_URL": ACS_URL,
        "SCRUPLE_SAML_IDP_ENTITY_ID": IDP_ENTITY,
        "SCRUPLE_SAML_IDP_SSO_URL": IDP_SSO,
        "SCRUPLE_SAML_IDP_CERT_FILE": str(cert),
    }
    monkeypatch.delenv("SCRUPLE_CONSOLE_SECRET", raising=False)
    with pytest.raises(ValueError, match="SCRUPLE_CONSOLE_SECRET"):
        saml_setup(load_settings(env=env))


def test_identity_mapping_with_subject_attribute_and_default_tenant(idp: KeyPair) -> None:
    cfg = SAMLConfig(
        sp_entity_id=SP_ENTITY,
        acs_url=ACS_URL,
        idp_entity_id=IDP_ENTITY,
        idp_sso_url=IDP_SSO,
        idp_x509_cert=idp.cert_pem,
        subject_attribute="email",
        default_tenant="acme",
        role_map={"grp-risk": "risk_admin"},
    )
    sp = SAMLServiceProvider(cfg, SECRET.encode())
    who = sp.identity(
        "opaque-id", {"email": ["bob@bank.example"], "scruple_roles": ["grp-risk"]}, "_x"
    )
    assert (who.subject, who.tenant, who.roles) == ("bob@bank.example", "acme", ("risk_admin",))
    with pytest.raises(SAMLError):
        sp.identity("opaque-id", {"scruple_roles": ["grp-risk"]}, "_x")  # no email


def test_login_request_carries_no_secret(web: TestClient) -> None:
    r = web.get("/auth/saml/login", follow_redirects=False)
    assert SECRET not in r.headers["location"]
    assert re.match(r"^_[0-9a-f]+|ONELOGIN_[0-9a-f]+$", request_id_of(r.headers["location"]))
