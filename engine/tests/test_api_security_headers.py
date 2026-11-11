"""Security headers on the JSON API (`scruple.api.security_headers`): the ZAP
baseline flags their absence. The console's stricter page policy is kept."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from scruple.api.app import create_app
from scruple.api.security_headers import JSON_CSP, SecurityHeadersMiddleware, hsts_value
from scruple.auth.authn import Authenticator
from scruple.clock import SteppingClock
from scruple.console.security import CSP as CONSOLE_CSP
from scruple.gate import GateConfig
from scruple.service import AgentGate
from scruple.settings import Settings
from scruple.storage import SQLiteStorage

READER = {"X-Actor": "auditor"}


def _client(settings: Settings | None = None, *, base_url: str = "http://testserver") -> TestClient:
    g = AgentGate(SQLiteStorage(":memory:"), SteppingClock(), GateConfig(n_cases=200))
    g.bootstrap_baseline()
    app = create_app(g, settings or Settings(), auth=Authenticator(dev_actor_header=True))
    return TestClient(app, headers=READER, base_url=base_url)


@pytest.fixture(scope="module")
def client() -> TestClient:
    return _client()


@pytest.mark.parametrize(
    ("method", "path", "status"),
    [
        ("GET", "/rules", 200),
        ("GET", "/version", 200),
        ("GET", "/health", 200),
        ("GET", "/auth/whoami", 200),
        ("GET", "/rules/no-such-rule/1", 404),
        ("POST", "/rules", 422),
    ],
)
def test_json_responses_carry_the_headers(
    client: TestClient, method: str, path: str, status: int
) -> None:
    r = client.request(method, path, json={} if method == "POST" else None)
    assert r.status_code == status, r.text
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["content-security-policy"] == JSON_CSP
    assert r.headers["cache-control"] == "no-store"
    assert "strict-transport-security" not in r.headers  # plain HTTP, setting off


def test_unauthenticated_errors_carry_them_too(client: TestClient) -> None:
    r = client.get("/rules", headers={"X-Actor": ""})
    assert r.status_code == 401
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-security-policy"] == JSON_CSP


@pytest.mark.parametrize("path", ["/auth/whoami", "/auth/keys"])
def test_auth_routes_are_never_cached(client: TestClient, path: str) -> None:
    assert client.get(path).headers["cache-control"] == "no-store"


def test_console_keeps_its_own_page_policy(client: TestClient) -> None:
    r = client.get("/console/")
    assert r.status_code == 200
    assert r.headers["content-security-policy"] == CONSOLE_CSP
    assert r.headers["referrer-policy"] == "same-origin"
    assert r.headers["x-content-type-options"] == "nosniff"


def test_console_assets_keep_their_cache_policy(client: TestClient) -> None:
    page = client.get("/console/").text
    css = page.split('rel="stylesheet" href="', 1)[1].split('"', 1)[0]
    r = client.get(css)
    assert r.status_code == 200
    assert r.headers["cache-control"].startswith("public")


def test_hsts_when_tls_terminates_in_front() -> None:
    c = _client(Settings(tls_terminated=True, hsts_max_age_seconds=86_400))
    assert c.get("/rules").headers["strict-transport-security"] == hsts_value(86_400)
    assert c.get("/health").headers["strict-transport-security"].startswith("max-age=86400")


def test_hsts_when_the_request_is_already_https() -> None:
    c = _client(base_url="https://testserver")
    assert c.get("/rules").headers["strict-transport-security"] == hsts_value(31_536_000)


def test_tls_setting_reads_from_the_environment() -> None:
    from scruple.settings import load_settings

    s = load_settings(env={"SCRUPLE_TLS_TERMINATED": "true", "SCRUPLE_HSTS_MAX_AGE_SECONDS": "60"})
    assert s.tls_terminated is True and s.hsts_max_age_seconds == 60


def test_non_http_scopes_pass_through() -> None:
    seen: list[str] = []

    async def inner(scope: object, receive: object, send: object) -> None:
        seen.append("called")

    async def noop() -> None:  # pragma: no cover - never awaited
        return None

    mw = SecurityHeadersMiddleware(inner)
    asyncio.run(mw({"type": "lifespan"}, noop, noop))  # type: ignore[arg-type]
    assert seen == ["called"]
