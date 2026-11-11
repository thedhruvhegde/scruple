"""`scruple init`: a fresh install in one command, idempotent, non-interactive with --yes."""

from __future__ import annotations

import json
import os
import stat
import tomllib
from pathlib import Path

import pytest

from scruple.auth.keys import ApiKeyService, ScryptParams
from scruple.auth.rbac import key_permissions
from scruple.cli.init import (
    EVENT_TENANT_INITIALISED,
    InitError,
    parse_approver,
    read_policy,
    split_password,
)
from scruple.cli.main import main
from scruple.service import AgentGate
from scruple.settings import load_settings
from scruple.storage import open_storage

FAST = {"SCRUPLE_AUTH_SCRYPT_N": "1024"}


@pytest.fixture(autouse=True)
def _fast_scrypt(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in FAST.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("SCRUPLE_CONFIG", raising=False)
    monkeypatch.delenv("SCRUPLE_DATABASE_PASSWORD", raising=False)


def _init(tmp: Path, *extra: str) -> int:
    return main(
        [
            "init",
            "--yes",
            "--dir",
            str(tmp),
            "--tenant",
            "acme",
            "--admin",
            "ra@acme.example",
            *extra,
        ]
    )


def test_fresh_init_creates_everything_and_prints_the_key_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _init(tmp_path) == 0
    cap = capsys.readouterr()
    key = cap.out.strip()
    assert key.startswith("scr_") and len(key.splitlines()) == 1
    for step in ("config", "policy", "database", "tenant", "admin key", "smoke"):
        assert f"] {step}:" in cap.err
    assert "[failed]" not in cap.err
    settings = load_settings(tmp_path / "scruple.toml", env={})
    assert settings.tenant == "acme"
    storage = open_storage(settings.database_url)
    try:
        gate = AgentGate(storage.for_tenant("acme"))
        events = [e.event_type for e in gate.audit.entries()]
        assert events.count(EVENT_TENANT_INITIALISED) == 1
        assert gate.verify_integrity().ok
        svc = ApiKeyService(storage, params=ScryptParams(n=1024), permissions_for=key_permissions)
        principal, _ = svc.authenticate(key)
        assert principal.tenant == "acme" and principal.subject == "ra@acme.example"
        assert "admin" in {p.value for p in principal.permissions}
        # The default tenant is untouched: init works in its own tenant only.
        assert AgentGate(storage.for_tenant("default")).audit.entries() == []
    finally:
        storage.close()


def test_rerun_is_idempotent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _init(tmp_path) == 0
    first = capsys.readouterr()
    config = (tmp_path / "scruple.toml").read_text()
    assert _init(tmp_path) == 0
    second = capsys.readouterr()
    assert second.out == ""  # no new key shown
    assert "[exists] config" in second.err and "[exists] policy" in second.err
    assert "[exists] tenant" in second.err and "[exists] admin key" in second.err
    assert "[ok] smoke" in second.err
    assert (tmp_path / "scruple.toml").read_text() == config
    storage = open_storage(str(tmp_path / "scruple.db"))
    try:
        keys = storage.list_api_keys("acme")
        assert len(keys) == 1
        entries = AgentGate(storage.for_tenant("acme")).audit.entries()
        assert [e.event_type for e in entries].count(EVENT_TENANT_INITIALISED) == 1
    finally:
        storage.close()
    assert first.out.strip() not in second.err


def test_json_mode_and_key_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "secrets" / "admin.key"
    out.parent.mkdir()
    assert _init(tmp_path, "--json", "--key-out", str(out)) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["ok"] is True and "api_key" not in body
    assert body["key_id"].startswith("scr_")
    assert {s["name"] for s in body["steps"]} >= {"config", "tenant", "admin key", "smoke"}
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert out.read_text().startswith(body["key_id"] + "_")


def test_flags_that_disagree_with_an_existing_config_are_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _init(tmp_path) == 0
    capsys.readouterr()
    rc = main(["init", "--yes", "--dir", str(tmp_path), "--tenant", "other", "--admin", "x"])
    assert rc == 2
    assert "different tenant" in capsys.readouterr().err


def test_policy_file_filled_from_flags_validates(tmp_path: Path) -> None:
    rc = _init(
        tmp_path,
        "--approver",
        "alice@acme.example=model-risk",
        "--approver",
        "bob@acme.example=ops",
        "--required",
        "2",
        "--required-role",
        "model-risk",
        "--no-smoke",
    )
    assert rc == 0
    policy = read_policy(tmp_path / "scruple-policy.toml")
    assert policy is not None and policy.required == 2
    assert [a.name for a in policy.approvers] == ["alice@acme.example", "bob@acme.example"]
    assert policy.required_role == "model-risk"


def test_skeleton_policy_loads_as_unfilled(tmp_path: Path) -> None:
    assert _init(tmp_path, "--no-smoke") == 0
    text = (tmp_path / "scruple-policy.toml").read_text()
    assert tomllib.loads(text) == {}
    assert read_policy(tmp_path / "scruple-policy.toml") is None


def test_an_invalid_policy_quorum_is_refused_before_anything_is_written(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = _init(tmp_path, "--approver", "alice@acme.example", "--required", "3")
    assert rc == 2
    assert "approval policy is not valid" in capsys.readouterr().err
    assert not (tmp_path / "scruple.db").exists()


def test_a_broken_existing_policy_file_is_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "scruple-policy.toml").write_text("[approval]\nrequired = 'two'\n")
    assert _init(tmp_path) == 2
    assert "not valid" in capsys.readouterr().err


def test_without_a_terminal_and_without_yes_init_refuses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    assert main(["init", "--dir", str(tmp_path)]) == 2
    assert "--yes" in capsys.readouterr().err
    assert not any(tmp_path.iterdir())


def test_interactive_asks_and_confirms(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scruple.cli import init as init_mod

    monkeypatch.setattr(init_mod, "_interactive", lambda: True)
    answers = iter(["", "beta", "boss@beta.example", "y"])
    asked: list[str] = []

    def ask(prompt: str) -> str:
        asked.append(prompt)
        return next(answers)

    from scruple.cli.main import build_parser

    args = build_parser().parse_args(["init", "--dir", str(tmp_path), "--no-smoke"])
    assert init_mod.cmd_init(args, ask) == 0
    assert any("tenant" in a for a in asked) and asked[-1].startswith("proceed")
    assert load_settings(tmp_path / "scruple.toml", env={}).tenant == "beta"


def test_interactive_no_does_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scruple.cli import init as init_mod
    from scruple.cli.main import build_parser

    monkeypatch.setattr(init_mod, "_interactive", lambda: True)
    answers = iter(["", "", "", "n"])
    args = build_parser().parse_args(["init", "--dir", str(tmp_path)])
    assert init_mod.cmd_init(args, lambda _p: next(answers)) == 1
    assert not any(tmp_path.iterdir())


def test_the_password_in_a_url_is_never_written(tmp_path: Path) -> None:
    url = "postgresql://scruple:s3cr%40t@db.internal:5432/scruple"
    stored, pw = split_password(url)
    assert pw == "s3cr%40t" and stored == "postgresql://scruple@db.internal:5432/scruple"
    assert split_password("plain.db") == ("plain.db", None)
    assert split_password("postgresql://u@[::1]/d") == ("postgresql://u@[::1]/d", None)
    assert split_password("postgresql://u:p@[::1]:6543/d")[0] == "postgresql://u@[::1]:6543/d"


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("alice", ("alice", ())),
        ("alice@bank.example=model-risk", ("alice@bank.example", ("model-risk",))),
        ("a.b=x,y", ("a.b", ("x", "y"))),
    ],
)
def test_parse_approver(spec: str, expected: tuple[str, tuple[str, ...]]) -> None:
    assert parse_approver(spec) == expected


@pytest.mark.parametrize("spec", ["", "bad name", "a=Role!", "x" * 200])
def test_parse_approver_refuses(spec: str) -> None:
    with pytest.raises(InitError):
        parse_approver(spec)


def test_an_existing_key_file_is_never_overwritten(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "admin.key"
    out.write_text("keep me")
    assert _init(tmp_path, "--key-out", str(out), "--no-smoke") == 2
    assert out.read_text() == "keep me"
    assert "refusing to overwrite" in capsys.readouterr().err


@pytest.mark.postgres
def test_init_migrates_postgres(
    postgres_url: str, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    import uuid

    import psycopg

    from tests.storage.test_migrations import _with_db

    name = f"i_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(postgres_url, autocommit=True) as c:
        c.execute(f"CREATE DATABASE {name}")
    try:
        url = _with_db(postgres_url, name)
        assert _init(tmp_path, "--db", url) == 0
        err = capsys.readouterr().err
        assert "postgres migrated to" in err and "[ok] smoke" in err
        assert _init(tmp_path, "--db", url) == 0
        assert "[exists] admin key" in capsys.readouterr().err
    finally:
        with psycopg.connect(postgres_url, autocommit=True) as c:
            c.execute(f"DROP DATABASE {name} WITH (FORCE)")


def test_umask_does_not_widen_the_key_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    old = os.umask(0)
    try:
        out = tmp_path / "k"
        assert _init(tmp_path, "--key-out", str(out), "--no-smoke") == 0
    finally:
        os.umask(old)
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
