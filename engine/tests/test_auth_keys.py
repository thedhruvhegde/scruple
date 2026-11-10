"""API keys: hashing, lookup, revocation, last use, audit, storage guards, CLI, HTTP."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from hypothesis import given
from hypothesis import strategies as st

from scruple.api.app import create_app
from scruple.auth import keys as keys_mod
from scruple.auth.authn import DEV_WARNING, Authenticator
from scruple.auth.keys import (
    EVENT_KEY_CREATED,
    EVENT_KEY_REVOKED,
    ApiKeyService,
    InvalidKeyError,
    ScryptParams,
    UnknownKeyError,
    hash_secret,
    new_token,
    parse_token,
    verify_secret,
)
from scruple.auth.models import Permission
from scruple.cli.main import main
from scruple.service import AgentGate
from scruple.settings import Settings, load_settings
from scruple.sim.library import GOOD_RULE, GOOD_RULE_ID
from scruple.storage import ImmutableHistoryError, SQLiteStorage
from tests.ci_support import PROV
from tests.storage.conftest import RawStorage, pg_store, store  # noqa: F401  (fixtures)

FAST = ScryptParams(n=1024)
T0 = datetime(2026, 3, 1, tzinfo=UTC)


class Clock:
    """A clock tests move by hand."""

    def __init__(self, t: datetime = T0) -> None:
        self.t = t

    def now(self) -> datetime:
        return self.t


class Mono:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def db() -> Iterator[SQLiteStorage]:
    s = SQLiteStorage(":memory:")
    yield s
    s.close()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def svc(db: SQLiteStorage, clock: Clock) -> ApiKeyService:
    return ApiKeyService(db, clock, FAST)


def _count_scrypt(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    real = keys_mod._scrypt

    def counting(secret: str, salt: bytes, n: int, r: int, p: int) -> bytes:
        calls.append(n)
        return real(secret, salt, n, r, p)

    monkeypatch.setattr(keys_mod, "_scrypt", counting)
    return calls


# --- hashing and format ---------------------------------------------------------


def test_hash_round_trip_and_salting() -> None:
    a, b = hash_secret("s" * 43, FAST), hash_secret("s" * 43, FAST)
    assert a != b  # fresh salt each time
    assert verify_secret("s" * 43, a) and verify_secret("s" * 43, b)
    assert not verify_secret("t" + "s" * 42, a)
    assert a.startswith("scrypt$1024$8$1$")


def test_verify_uses_the_stored_parameters() -> None:
    enc = hash_secret("x" * 43, ScryptParams(n=2048, r=4, p=2))
    assert verify_secret("x" * 43, enc)


@pytest.mark.parametrize(
    "encoded",
    ["", "scrypt$1024$8$1$salt", "bcrypt$1024$8$1$c2FsdA$aGFzaA", "scrypt$x$8$1$c2FsdA$aGFzaA"],
)
def test_malformed_hashes_never_verify(encoded: str) -> None:
    assert not verify_secret("x" * 43, encoded)


def test_tampered_hash_does_not_verify() -> None:
    enc = hash_secret("y" * 43, FAST)
    head, digest = enc.rsplit("$", 1)
    flipped = ("A" if digest[0] != "A" else "B") + digest[1:]
    assert not verify_secret("y" * 43, f"{head}${flipped}")


@pytest.mark.parametrize(("n", "r", "p"), [(1000, 8, 1), (1, 8, 1), (1024, 0, 1), (1024, 8, 17)])
def test_bad_scrypt_parameters_are_refused(n: int, r: int, p: int) -> None:
    with pytest.raises(ValueError, match="scrypt"):
        ScryptParams(n=n, r=r, p=p)


def test_new_tokens_parse_and_are_unique() -> None:
    seen = set()
    for _ in range(200):
        key_id, token = new_token()
        parsed = parse_token(token)
        assert parsed is not None and parsed[0] == key_id
        assert len(parsed[1]) == 43
        seen.add(token)
    assert len(seen) == 200


@given(st.text(max_size=80))
def test_arbitrary_text_is_not_a_token(text: str) -> None:
    parsed = parse_token(text)
    if parsed is not None:
        assert text == f"{parsed[0]}_{parsed[1]}" and text.startswith("scr_")


# --- service --------------------------------------------------------------------


def test_create_returns_the_key_once_and_stores_only_a_hash(tmp_path: Path, clock: Clock) -> None:
    path = tmp_path / "keys.db"
    s = SQLiteStorage(str(path))
    svc = ApiKeyService(s, clock, FAST)
    rec, token = svc.create("alice", [Permission.READ], "local-admin", description="ci")
    secret = token.get_secret_value()
    s.close()
    raw = path.read_bytes()
    assert secret.encode() not in raw
    assert parse_token(secret) is not None
    assert secret[len(rec.key_id) + 1 :].encode() not in raw
    assert rec.key_id.encode() in raw  # the id is public
    assert "**" in repr(token) and secret not in repr(rec) and secret not in str(rec)
    assert "secret_hash" not in repr(rec) and "secret_hash" not in rec.public()


def test_authenticate_gives_the_key_principal(svc: ApiKeyService) -> None:
    rec, token = svc.create("alice", [Permission.READ, Permission.PROPOSE], "local-admin")
    p, got = svc.authenticate(token.get_secret_value())
    assert p.subject == "alice" and p.tenant == "default"
    assert p.permissions == {Permission.READ, Permission.PROPOSE}
    assert p.method == "api_key" and p.credential_id == rec.key_id
    assert got.key_id == rec.key_id


def test_a_key_needs_a_scope(svc: ApiKeyService) -> None:
    with pytest.raises(ValueError, match="scope"):
        svc.create("alice", [], "local-admin")


def test_wrong_unknown_and_malformed_keys_fail_alike(
    svc: ApiKeyService, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec, token = svc.create("alice", [Permission.READ], "local-admin")
    calls = _count_scrypt(monkeypatch)
    secret = token.get_secret_value()
    wrong_secret = rec.key_id + "_" + ("A" if secret[-1] != "A" else "B") * 43
    unknown_id = "scr_" + "0" * 16 + secret[len(rec.key_id) :]
    for bad in (wrong_secret, unknown_id, "scr_short", "", "Bearer x", secret + "x"):
        with pytest.raises(InvalidKeyError) as e:
            svc.authenticate(bad)
        assert str(e.value) == "invalid API key"
    # every failure paid exactly one scrypt: no fast path reveals which ids exist
    assert len(calls) == 6


def test_revocation_takes_effect_at_once_even_when_cached(svc: ApiKeyService) -> None:
    rec, token = svc.create("alice", [Permission.READ], "local-admin")
    svc.authenticate(token.get_secret_value())  # now cached
    out = svc.revoke(rec.key_id, "local-admin")
    assert out.revoked_at is not None
    with pytest.raises(InvalidKeyError):
        svc.authenticate(token.get_secret_value())
    assert svc.revoke(rec.key_id, "local-admin").revoked_at == out.revoked_at  # idempotent


def test_revoked_elsewhere_is_seen_through_the_cache(db: SQLiteStorage, clock: Clock) -> None:
    """Another process revokes the key: the cache skips scrypt but not the row read."""
    a = ApiKeyService(db, clock, FAST)
    b = ApiKeyService(db, clock, FAST)
    rec, token = a.create("alice", [Permission.READ], "local-admin")
    a.authenticate(token.get_secret_value())
    b.revoke(rec.key_id, "local-admin")
    with pytest.raises(InvalidKeyError):
        a.authenticate(token.get_secret_value())


def test_expired_keys_fail(svc: ApiKeyService, clock: Clock) -> None:
    _, token = svc.create(
        "alice", [Permission.READ], "local-admin", expires_at=T0 + timedelta(hours=1)
    )
    svc.authenticate(token.get_secret_value())
    clock.t = T0 + timedelta(hours=1)
    with pytest.raises(InvalidKeyError):
        svc.authenticate(token.get_secret_value())


def test_revoke_checks_the_tenant(svc: ApiKeyService) -> None:
    rec, _ = svc.create("alice", [Permission.READ], "local-admin")
    with pytest.raises(UnknownKeyError):
        svc.revoke(rec.key_id, "local-admin", tenant="other-bank")
    with pytest.raises(UnknownKeyError):
        svc.revoke("scr_" + "f" * 16, "local-admin")


def test_cache_skips_scrypt_until_it_expires(
    db: SQLiteStorage, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    mono = Mono()
    svc = ApiKeyService(db, clock, FAST, cache_seconds=10, monotonic=mono)
    _, token = svc.create("alice", [Permission.READ], "local-admin")
    calls = _count_scrypt(monkeypatch)
    for _ in range(5):
        svc.authenticate(token.get_secret_value())
    assert len(calls) == 1
    mono.t = 11
    svc.authenticate(token.get_secret_value())
    assert len(calls) == 2


def test_cache_is_bounded(db: SQLiteStorage, clock: Clock, monkeypatch: pytest.MonkeyPatch) -> None:
    svc = ApiKeyService(db, clock, FAST, cache_size=2)
    tokens = [svc.create(f"u{i}", [Permission.READ], "local-admin")[1] for i in range(3)]
    for t in tokens:
        svc.authenticate(t.get_secret_value())
    calls = _count_scrypt(monkeypatch)
    svc.authenticate(tokens[0].get_secret_value())  # evicted: checked again
    svc.authenticate(tokens[2].get_secret_value())  # still cached
    assert len(calls) == 1


def test_last_used_is_tracked_but_not_on_every_request(svc: ApiKeyService, clock: Clock) -> None:
    rec, token = svc.create("alice", [Permission.READ], "local-admin")
    assert svc.list()[0].last_used_at is None
    svc.authenticate(token.get_secret_value())
    assert svc.list()[0].last_used_at == T0
    clock.t = T0 + timedelta(seconds=30)
    svc.authenticate(token.get_secret_value())
    assert svc.list()[0].last_used_at == T0
    clock.t = T0 + timedelta(seconds=61)
    svc.authenticate(token.get_secret_value())
    assert svc.list()[0].last_used_at == T0 + timedelta(seconds=61)
    assert svc.list()[0].key_id == rec.key_id


def test_key_events_are_audited_and_the_chain_verifies(db: SQLiteStorage, clock: Clock) -> None:
    g = AgentGate(db)
    g.bootstrap_baseline()
    svc = ApiKeyService(db, clock, FAST)
    rec, _ = svc.create("alice", [Permission.READ], "local-admin")
    svc.revoke(rec.key_id, "local-admin")
    events = [(e.event_type, e.actor, e.payload.get("key_id")) for e in g.audit.entries()]
    assert (EVENT_KEY_CREATED, "local-admin", rec.key_id) in events
    assert (EVENT_KEY_REVOKED, "local-admin", rec.key_id) in events
    assert all("secret" not in json.dumps(e.payload) for e in g.audit.entries())
    assert g.verify_integrity().ok


# --- storage guards (every backend) ---------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE api_keys SET subject='mallory'",
        "UPDATE api_keys SET scopes_json='[\"admin\"]'",
        "UPDATE api_keys SET secret_hash='x'",
        "UPDATE api_keys SET tenant_id='other'",
        "UPDATE api_keys SET expires_at=NULL",
        "DELETE FROM api_keys",
    ],
)
def test_key_identity_is_immutable_in_the_database(store: RawStorage, sql: str) -> None:  # noqa: F811
    svc = ApiKeyService(store, Clock(), FAST)
    svc.create("alice", [Permission.READ], "local-admin", expires_at=T0 + timedelta(days=1))
    with pytest.raises(ImmutableHistoryError):
        store.unsafe_execute(sql)


def test_a_revocation_cannot_be_undone(store: RawStorage) -> None:  # noqa: F811
    svc = ApiKeyService(store, Clock(), FAST)
    rec, token = svc.create("alice", [Permission.READ], "local-admin")
    svc.revoke(rec.key_id, "local-admin")
    with pytest.raises(ImmutableHistoryError):
        store.unsafe_execute("UPDATE api_keys SET revoked_at=NULL")
    with pytest.raises(InvalidKeyError):
        svc.authenticate(token.get_secret_value())


def test_keys_round_trip_on_every_backend(store: RawStorage) -> None:  # noqa: F811
    svc = ApiKeyService(store, Clock(), FAST)
    rec, token = svc.create(
        "alice",
        [Permission.PROPOSE, Permission.READ],
        "local-admin",
        description="nightly job",
        expires_at=T0 + timedelta(days=30),
        rate_per_minute=60,
        burst=5,
        max_body_bytes=4096,
    )
    assert store.get_api_key(rec.key_id) == rec
    assert store.list_api_keys("default") == [rec]
    assert store.list_api_keys("other-bank") == []
    assert svc.authenticate(token.get_secret_value())[0].subject == "alice"


# --- HTTP -----------------------------------------------------------------------


@pytest.fixture
def api(db: SQLiteStorage) -> tuple[TestClient, ApiKeyService, AgentGate]:
    g = AgentGate(db)
    g.bootstrap_baseline()
    svc = ApiKeyService(db, params=FAST)
    return TestClient(create_app(g, auth=Authenticator(svc))), svc, g


def _hdr(token: object) -> dict[str, str]:
    return {"X-API-Key": token.get_secret_value()}  # type: ignore[attr-defined]


def test_http_requires_a_credential(api: tuple[TestClient, ApiKeyService, AgentGate]) -> None:
    c, _, _ = api
    r = c.get("/rules")
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    assert c.get("/health").status_code == 200 and c.get("/version").status_code == 200


def test_x_actor_is_not_trusted_by_default(
    api: tuple[TestClient, ApiKeyService, AgentGate],
) -> None:
    c, svc, _ = api
    assert c.get("/rules", headers={"X-Actor": "model-risk@demo.bank"}).status_code == 401
    _, token = svc.create("alice", [Permission.READ], "local-admin")
    r = c.get("/auth/whoami", headers={**_hdr(token), "X-Actor": "model-risk@demo.bank"})
    assert r.json()["subject"] == "alice"
    assert "x-scruple-insecure-dev-auth" not in r.headers


@pytest.mark.parametrize("how", ["x-api-key", "bearer"])
def test_both_header_forms_work(api: tuple[TestClient, ApiKeyService, AgentGate], how: str) -> None:
    c, svc, _ = api
    _, token = svc.create("alice", [Permission.READ], "local-admin")
    secret = token.get_secret_value()
    h = {"X-API-Key": secret} if how == "x-api-key" else {"Authorization": f"Bearer {secret}"}
    assert c.get("/rules", headers=h).status_code == 200


@pytest.mark.parametrize(
    "headers",
    [
        {"X-API-Key": "scr_0123456789abcdef_" + "a" * 43},
        {"X-API-Key": "nope"},
        {"Authorization": "Basic YWxpY2U6cHc="},
        {"Authorization": "Bearer "},
        {"Authorization": "Bearer eyJhbGciOiJub25lIn0.e30."},  # no OIDC configured
    ],
)
def test_bad_credentials_are_401_even_on_public_routes(
    api: tuple[TestClient, ApiKeyService, AgentGate], headers: dict[str, str]
) -> None:
    c, _, _ = api
    for path in ("/rules", "/health"):
        r = c.get(path, headers=headers)
        assert r.status_code == 401, (path, r.text)
        assert isinstance(r.json()["detail"], str)


def test_two_credentials_are_refused(api: tuple[TestClient, ApiKeyService, AgentGate]) -> None:
    c, svc, _ = api
    _, token = svc.create("alice", [Permission.READ], "local-admin")
    secret = token.get_secret_value()
    r = c.get("/rules", headers={"X-API-Key": secret, "Authorization": f"Bearer {secret}"})
    assert r.status_code == 401 and "one credential" in r.json()["detail"]


def test_scopes_limit_routes(api: tuple[TestClient, ApiKeyService, AgentGate]) -> None:
    c, svc, _ = api
    _, reader = svc.create("auditor", [Permission.READ], "local-admin")
    r = c.post(f"/rules/{GOOD_RULE_ID}/rollback", json={"reason": "drill"}, headers=_hdr(reader))
    assert r.status_code == 403 and "approve" in r.json()["detail"]
    assert c.get("/auth/keys", headers=_hdr(reader)).status_code == 403


def test_the_audit_actor_is_the_key_subject(
    api: tuple[TestClient, ApiKeyService, AgentGate],
) -> None:
    c, svc, g = api
    _, agent = svc.create(PROV.agent_id, [Permission.READ, Permission.PROPOSE], "local-admin")
    _, other = svc.create("someone-else", [Permission.READ, Permission.PROPOSE], "local-admin")
    body = {
        "rule_id": GOOD_RULE_ID,
        "spec": GOOD_RULE.model_dump(mode="json"),
        "owner": "ops-team",
        "provenance": PROV.model_dump(mode="json"),
    }
    assert c.post("/rules", json=body, headers=_hdr(other)).status_code == 403
    assert c.post("/rules", json=body, headers=_hdr(agent)).status_code == 201
    _, risk = svc.create("model-risk@demo.bank", [Permission.APPROVE], "local-admin")
    baseline = g.registry.active_rules()[0].rule_id
    r = c.post(f"/rules/{baseline}/rollback", json={"reason": "drill"}, headers=_hdr(risk))
    assert r.status_code == 200, r.text
    assert g.decisions()[-1].decision.actor == "model-risk@demo.bank"
    assert g.audit.entries()[-1].actor == "model-risk@demo.bank"


def test_admin_lists_keys_without_hashes(api: tuple[TestClient, ApiKeyService, AgentGate]) -> None:
    c, svc, _ = api
    rec, admin = svc.create("risk-admin", [Permission.READ, Permission.ADMIN], "local-admin")
    r = c.get("/auth/keys", headers=_hdr(admin))
    assert r.status_code == 200
    assert [k["key_id"] for k in r.json()] == [rec.key_id]
    assert "secret_hash" not in r.text and "scrypt$" not in r.text


def test_keys_never_reach_the_logs(
    api: tuple[TestClient, ApiKeyService, AgentGate], caplog: pytest.LogCaptureFixture
) -> None:
    c, svc, _ = api
    caplog.set_level(logging.DEBUG)
    _, token = svc.create("alice", [Permission.READ], "local-admin")
    secret = token.get_secret_value()
    c.get("/rules", headers={"X-API-Key": secret})
    c.get("/rules", headers={"Authorization": f"Bearer {secret}"})
    c.get("/rules", headers={"X-API-Key": secret[:-1] + "!"})
    text = "\n".join(f"{r.getMessage()} {r.args} {r.__dict__}" for r in caplog.records)
    assert secret not in text and secret[len("scr_") + 17 :] not in text


# --- development mode -----------------------------------------------------------


def test_dev_mode_is_off_by_default() -> None:
    assert Settings().auth_dev_actor_header is False
    on = load_settings(env={"SCRUPLE_AUTH_DEV_ACTOR_HEADER": "true"})
    assert on.auth_dev_actor_header is True


def test_dev_mode_warns_and_flags_every_response(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="scruple.auth")
    auth = Authenticator(dev_actor_header=True)
    assert any(DEV_WARNING in r.getMessage() for r in caplog.records)
    g = AgentGate(SQLiteStorage(":memory:"))
    c = TestClient(create_app(g, auth=auth))
    r = c.get("/auth/whoami", headers={"X-Actor": "dev-user"})
    assert r.status_code == 200 and r.json()["method"] == "dev_header"
    assert r.headers["x-scruple-insecure-dev-auth"] == "true"
    assert c.get("/health").headers["x-scruple-insecure-dev-auth"] == "true"
    assert c.get("/rules", headers={"X-Actor": "bad actor!"}).status_code == 401


def test_app_from_settings_trusts_x_actor_only_when_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AGENT_GATE_DB", raising=False)
    monkeypatch.setenv("SCRUPLE_DATABASE_URL", f"sqlite:///{tmp_path / 'a.db'}")
    assert TestClient(create_app()).get("/rules", headers={"X-Actor": "x"}).status_code == 401
    monkeypatch.setenv("SCRUPLE_AUTH_DEV_ACTOR_HEADER", "true")
    assert TestClient(create_app()).get("/rules", headers={"X-Actor": "x"}).status_code == 200


# --- CLI ------------------------------------------------------------------------


@pytest.fixture
def cli_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("SCRUPLE_AUTH_SCRYPT_N", "1024")
    return str(tmp_path / "cli.db")


def test_cli_create_list_revoke(cli_db: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["keys", "create", "--db", cli_db, "--subject", "alice", "--scope", "read"]) == 0
    out = capsys.readouterr()
    token = out.out.strip()
    assert parse_token(token) is not None and "only time" in out.err
    key_id = token[: len("scr_") + 16]

    assert main(["keys", "list", "--db", cli_db]) == 0
    listing = capsys.readouterr().out
    assert key_id in listing and "active" in listing and token not in listing
    assert "scrypt$" not in listing

    assert main(["keys", "revoke", "--db", cli_db, key_id]) == 0
    assert "revoked" in capsys.readouterr().out
    assert main(["keys", "list", "--db", cli_db, "--json"]) == 0
    (row,) = json.loads(capsys.readouterr().out)
    assert row["key_id"] == key_id and row["revoked_at"] is not None
    assert "secret_hash" not in row


def test_cli_create_json_and_errors(cli_db: str, capsys: pytest.CaptureFixture[str]) -> None:
    args = ["keys", "create", "--db", cli_db, "--subject", "bot", "--scope", "read", "--json"]
    assert main([*args, "--expires-days", "30"]) == 0
    row = json.loads(capsys.readouterr().out)
    assert row["api_key"].startswith(row["key_id"] + "_") and row["expires_at"]
    assert main(["keys", "create", "--db", cli_db, "--subject", "bot"]) == 2
    assert "scope" in capsys.readouterr().err
    assert main(["keys", "revoke", "--db", cli_db, "scr_" + "0" * 16]) == 2
    with pytest.raises(SystemExit):
        main(["keys", "create", "--db", cli_db, "--subject", "bot", "--scope", "root"])
