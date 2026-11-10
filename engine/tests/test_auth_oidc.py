"""OIDC bearer tokens against a local fake JWKS endpoint (no network).

Keys are generated in the test process: RSA 2048 (RS256) and EC P-256 (ES256).
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi.testclient import TestClient
from hypothesis import given
from hypothesis import strategies as st

from scruple.api.app import create_app
from scruple.auth.authn import Authenticator, AuthError
from scruple.auth.models import Permission
from scruple.auth.oidc import JWKSCache, OIDCConfig, OIDCVerifier
from scruple.service import AgentGate
from scruple.storage import SQLiteStorage

ISS = "https://idp.example.test/"
AUD = "scruple-api"
JWKS_URL = "https://idp.example.test/.well-known/jwks.json"

RSA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
RSA_OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)
EC_KEY = ec.generate_private_key(ec.SECP256R1())
EC_P384 = ec.generate_private_key(ec.SECP384R1())


def rsa_jwk(key: rsa.RSAPrivateKey, kid: str, **extra: Any) -> dict[str, Any]:
    d: dict[str, Any] = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    return {**d, "kid": kid, "use": "sig", **extra}


def ec_jwk(key: ec.EllipticCurvePrivateKey, kid: str, **extra: Any) -> dict[str, Any]:
    d: dict[str, Any] = jwt.algorithms.ECAlgorithm.to_jwk(key.public_key(), as_dict=True)
    return {**d, "kid": kid, "use": "sig", **extra}


class FakeIdP:
    """A JWKS endpoint served through httpx.MockTransport; counts fetches."""

    def __init__(self, keys: list[dict[str, Any]]) -> None:
        self.keys = keys
        self.fetches = 0
        self.status = 200
        self.raw: bytes | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert str(request.url) == JWKS_URL
        self.fetches += 1
        if self.raw is not None:
            return httpx.Response(self.status, content=self.raw)
        return httpx.Response(self.status, json={"keys": self.keys})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


class Mono:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def claims(**over: Any) -> dict[str, Any]:
    now = int(time.time())
    base: dict[str, Any] = {
        "iss": ISS,
        "aud": AUD,
        "sub": "model-risk@demo.bank",
        "iat": now,
        "nbf": now,
        "exp": now + 300,
        "scope": "read approve",
        "scruple_tenant": "default",
    }
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


def sign(c: dict[str, Any], key: Any = RSA_KEY, alg: str = "RS256", kid: str = "rsa-1") -> str:
    return jwt.encode(c, key, algorithm=alg, headers={"kid": kid})


CFG = OIDCConfig(issuer=ISS, audience=AUD, jwks_url=JWKS_URL, leeway_seconds=30)


def verifier(
    idp: FakeIdP, cfg: OIDCConfig = CFG, mono: Callable[[], float] | None = None
) -> OIDCVerifier:
    cache = JWKSCache(
        cfg.jwks_url,
        idp.client(),
        algorithms=cfg.algorithms,
        cache_seconds=cfg.jwks_cache_seconds,
        min_refresh_seconds=cfg.jwks_min_refresh_seconds,
        monotonic=mono or time.monotonic,
    )
    return OIDCVerifier(cfg, cache)


@pytest.fixture
def idp() -> FakeIdP:
    return FakeIdP([rsa_jwk(RSA_KEY, "rsa-1"), ec_jwk(EC_KEY, "ec-1")])


# --- good tokens ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "alg", "kid"), [(RSA_KEY, "RS256", "rsa-1"), (EC_KEY, "ES256", "ec-1")]
)
def test_valid_tokens_give_a_principal(idp: FakeIdP, key: Any, alg: str, kid: str) -> None:
    p = verifier(idp).verify(sign(claims(), key, alg, kid))
    assert p.subject == "model-risk@demo.bank" and p.tenant == "default"
    assert p.permissions == {Permission.READ, Permission.APPROVE}
    assert p.method == "oidc" and p.credential_id == f"{ISS}#model-risk@demo.bank"


def test_scope_may_be_a_list_and_unknown_scopes_are_ignored(idp: FakeIdP) -> None:
    p = verifier(idp).verify(sign(claims(scope=["read", "openid", "root"])))
    assert p.permissions == {Permission.READ}


def test_audience_may_be_one_of_several(idp: FakeIdP) -> None:
    assert verifier(idp).verify(sign(claims(aud=["other", AUD]))).subject


def test_clock_skew_within_leeway_is_accepted(idp: FakeIdP) -> None:
    now = int(time.time())
    v = verifier(idp)
    assert v.verify(sign(claims(exp=now - 10)))  # expired 10 s ago, leeway 30 s
    assert v.verify(sign(claims(nbf=now + 10, iat=now + 10)))  # issuer clock 10 s ahead


# --- bad tokens -----------------------------------------------------------------


def _forged_none(c: dict[str, Any]) -> str:
    def b64(o: object) -> str:
        return base64.urlsafe_b64encode(json.dumps(o).encode()).decode().rstrip("=")

    return f"{b64({'alg': 'none', 'kid': 'rsa-1'})}.{b64(c)}."


def _hs256_with_public_key(c: dict[str, Any]) -> str:
    """The classic confusion attack: HMAC keyed with the issuer's public key."""
    from cryptography.hazmat.primitives import serialization

    pub = RSA_KEY.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    import hashlib
    import hmac

    def b64(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).decode().rstrip("=")

    head = b64(json.dumps({"alg": "HS256", "kid": "rsa-1", "typ": "JWT"}).encode())
    body = b64(json.dumps(c).encode())
    sig = hmac.new(pub, f"{head}.{body}".encode(), hashlib.sha256).digest()
    return f"{head}.{body}.{b64(sig)}"


BAD: dict[str, Callable[[], str]] = {
    "expired beyond leeway": lambda: sign(claims(exp=int(time.time()) - 120)),
    "not yet valid": lambda: sign(claims(nbf=int(time.time()) + 120)),
    "wrong issuer": lambda: sign(claims(iss="https://evil.example.test/")),
    "wrong audience": lambda: sign(claims(aud="someone-else")),
    "no exp": lambda: sign(claims(exp=None)),
    "no iat": lambda: sign(claims(iat=None)),
    "no aud": lambda: sign(claims(aud=None)),
    "no sub": lambda: sign(claims(sub=None)),
    "signed by another key": lambda: sign(claims(), RSA_OTHER),
    "unknown kid": lambda: sign(claims(), kid="nope"),
    "no kid": lambda: jwt.encode(claims(), RSA_KEY, algorithm="RS256"),
    "alg none": lambda: _forged_none(claims()),
    "hs256 with public key": lambda: _hs256_with_public_key(claims()),
    "RS384 not allowed": lambda: sign(claims(), RSA_KEY, "RS384"),
    "RSA key under an EC kid": lambda: sign(claims(), RSA_KEY, "RS256", "ec-1"),
    "EC token under an RSA kid": lambda: sign(claims(), EC_KEY, "ES256", "rsa-1"),
    "garbage": lambda: "not.a.jwt",
    "oversized": lambda: "a" * 9000,
    "tampered payload": lambda: _tamper(sign(claims())),
}


def _tamper(token: str) -> str:
    head, body, sig = token.split(".")
    c = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    c["scope"] = "read approve admin"
    new = base64.urlsafe_b64encode(json.dumps(c).encode()).decode().rstrip("=")
    return f"{head}.{new}.{sig}"


@pytest.mark.parametrize("case", sorted(BAD))
def test_bad_tokens_are_refused(idp: FakeIdP, case: str) -> None:
    with pytest.raises(AuthError) as e:
        verifier(idp).verify(BAD[case]())
    assert e.value.detail in ("invalid bearer token",)


@pytest.mark.parametrize(
    "over",
    [{"sub": "bad subject!"}, {"scruple_tenant": "Not A Tenant"}, {"scruple_tenant": None}],
)
def test_unacceptable_subject_or_tenant(idp: FakeIdP, over: dict[str, Any]) -> None:
    with pytest.raises(AuthError, match="subject or tenant"):
        verifier(idp).verify(sign(claims(**over)))


def test_default_tenant_fills_a_missing_claim(idp: FakeIdP) -> None:
    cfg = CFG.model_copy(update={"default_tenant": "acme-bank"})
    assert verifier(idp, cfg).verify(sign(claims(scruple_tenant=None))).tenant == "acme-bank"


def test_es256_only_config_refuses_rs256(idp: FakeIdP) -> None:
    cfg = CFG.model_copy(update={"algorithms": ("ES256",)})
    with pytest.raises(AuthError):
        verifier(idp, cfg).verify(sign(claims()))
    assert verifier(idp, cfg).verify(sign(claims(), EC_KEY, "ES256", "ec-1"))


def test_jwks_entries_that_do_not_fit_are_skipped() -> None:
    idp = FakeIdP(
        [
            rsa_jwk(RSA_KEY, "enc-key", use="enc"),
            rsa_jwk(RSA_KEY, "hs-key", alg="HS256"),
            ec_jwk(EC_P384, "p384"),  # ES384: not an allowed algorithm
            {"kty": "RSA", "kid": "broken", "n": "!!", "e": "AQAB"},
            {"no": "kid"},
            rsa_jwk(RSA_KEY, "good"),
        ]
    )
    v = verifier(idp)
    assert v.verify(sign(claims(), kid="good"))
    for kid in ("enc-key", "hs-key", "broken"):
        with pytest.raises(AuthError):
            v.verify(sign(claims(), kid=kid))
    with pytest.raises(AuthError):
        v.verify(sign(claims(), EC_P384, "ES384", "p384"))


@given(st.text(max_size=300))
def test_arbitrary_strings_never_authenticate(text: str) -> None:
    v = verifier(FakeIdP([rsa_jwk(RSA_KEY, "rsa-1")]))
    with pytest.raises(AuthError):
        v.verify(text)


# --- JWKS caching and rotation --------------------------------------------------


def test_jwks_is_cached(idp: FakeIdP) -> None:
    v = verifier(idp)
    for _ in range(5):
        v.verify(sign(claims()))
    assert idp.fetches == 1


def test_rotation_new_kid_triggers_one_refetch(idp: FakeIdP) -> None:
    mono = Mono()
    v = verifier(idp, mono=mono)
    v.verify(sign(claims()))
    idp.keys = [rsa_jwk(RSA_KEY, "rsa-1"), rsa_jwk(RSA_OTHER, "rsa-2")]
    mono.t += 31  # past the minimum refresh interval
    assert v.verify(sign(claims(), RSA_OTHER, kid="rsa-2")).subject
    assert idp.fetches == 2


def test_unknown_kids_cannot_hammer_the_issuer(idp: FakeIdP) -> None:
    mono = Mono()
    v = verifier(idp, mono=mono)
    v.verify(sign(claims()))
    for i in range(20):
        with pytest.raises(AuthError):
            v.verify(sign(claims(), kid=f"random-{i}"))
    assert idp.fetches == 1  # all within min_refresh_seconds of the first fetch
    mono.t += 31
    with pytest.raises(AuthError):
        v.verify(sign(claims(), kid="random-x"))
    assert idp.fetches == 2


def test_a_retired_key_stops_verifying_after_refresh(idp: FakeIdP) -> None:
    mono = Mono()
    v = verifier(idp, mono=mono)
    token = sign(claims())
    v.verify(token)
    idp.keys = [rsa_jwk(RSA_OTHER, "rsa-2")]
    mono.t += CFG.jwks_cache_seconds + 1
    with pytest.raises(AuthError):
        v.verify(token)


def test_issuer_outage_keeps_the_cached_keys(idp: FakeIdP) -> None:
    mono = Mono()
    v = verifier(idp, mono=mono)
    v.verify(sign(claims()))
    idp.status = 503
    mono.t += CFG.jwks_cache_seconds + 1
    assert v.verify(sign(claims())).subject  # stale keys still serve
    assert idp.fetches == 2


@pytest.mark.parametrize("raw", [b"not json", b'{"keys": "nope"}', b"[]", b"x" * 300_000])
def test_a_broken_jwks_never_authenticates(raw: bytes) -> None:
    idp = FakeIdP([])
    idp.raw = raw
    with pytest.raises(AuthError):
        verifier(idp).verify(sign(claims()))


# --- over HTTP ------------------------------------------------------------------


def test_bearer_tokens_over_http(idp: FakeIdP) -> None:
    g = AgentGate(SQLiteStorage(":memory:"))
    g.bootstrap_baseline()
    c = TestClient(create_app(g, auth=Authenticator(tokens=verifier(idp))))
    good = {"Authorization": f"Bearer {sign(claims(scope='read'))}"}
    r = c.get("/auth/whoami", headers=good)
    assert r.status_code == 200 and r.json()["method"] == "oidc"
    assert c.post("/rules/x/rollback", json={"reason": "abc"}, headers=good).status_code == 403
    expired = {"Authorization": f"Bearer {BAD['expired beyond leeway']()}"}
    r = c.get("/rules", headers=expired)
    assert r.status_code == 401 and r.json()["detail"] == "invalid bearer token"


# --- settings -------------------------------------------------------------------


def test_oidc_is_off_unless_fully_configured() -> None:
    from scruple.auth.config import oidc_verifier
    from scruple.settings import load_settings

    assert oidc_verifier(load_settings(env={})) is None
    with pytest.raises(ValueError, match="together"):
        oidc_verifier(load_settings(env={"SCRUPLE_OIDC_ISSUER": ISS}))
    v = oidc_verifier(
        load_settings(
            env={
                "SCRUPLE_OIDC_ISSUER": ISS,
                "SCRUPLE_OIDC_AUDIENCE": AUD,
                "SCRUPLE_OIDC_JWKS_URL": JWKS_URL,
                "SCRUPLE_OIDC_ALGORITHMS": "ES256",
                "SCRUPLE_OIDC_LEEWAY_SECONDS": "5",
            }
        )
    )
    assert v is not None
    assert v.config.algorithms == ("ES256",) and v.config.leeway_seconds == 5
    with pytest.raises(ValueError):
        oidc_verifier(
            load_settings(
                env={
                    "SCRUPLE_OIDC_ISSUER": ISS,
                    "SCRUPLE_OIDC_AUDIENCE": AUD,
                    "SCRUPLE_OIDC_JWKS_URL": JWKS_URL,
                    "SCRUPLE_OIDC_ALGORITHMS": "HS256",
                }
            )
        )
