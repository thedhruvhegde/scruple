from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scruple.settings import Settings, SettingsError, load_settings

ROOT = Path(__file__).resolve().parent.parent


def _toml(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "scruple.toml"
    p.write_text(text)
    return p


def test_defaults() -> None:
    s = load_settings(env={})
    assert s == Settings()
    assert s.database_url == "agent-gate.db" and s.log_format == "json" and s.api_port == 8000


def test_precedence_defaults_file_env_override(tmp_path: Path) -> None:
    f = _toml(tmp_path, 'log_level = "DEBUG"\napi_port = 9000\napi_host = "0.0.0.0"\n')
    env = {"SCRUPLE_API_PORT": "9100", "SCRUPLE_LOG_FORMAT": "text"}
    s = load_settings(f, env=env, api_host="10.0.0.1")
    assert s.log_level == "DEBUG"  # file
    assert s.api_port == 9100  # env beats file, and is coerced to int
    assert s.log_format == "text"  # env
    assert s.api_host == "10.0.0.1"  # override beats file


def test_scruple_table_and_config_env(tmp_path: Path) -> None:
    f = _toml(tmp_path, '[scruple]\ndatabase_url = "postgresql://app@db/scruple"\n')
    s = load_settings(env={"SCRUPLE_CONFIG": str(f)})
    assert s.database_url == "postgresql://app@db/scruple"


def test_unrelated_env_vars_are_ignored() -> None:
    env = {"SCRUPLE_BUILD_COMMIT": "abc", "SCRUPLE_TEST_POSTGRES_URL": "x", "HOME": "/"}
    assert load_settings(env=env) == Settings()


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("nonsense_key = 1\n", "nonsense_key"),
        ('log_level = "LOUD"\n', "log_level"),
        ("api_port = 0\n", "api_port"),
        ("api_port = [", "not valid TOML"),
        ('database_password = "hunter2"\n', "must come from the environment"),
        ('database_url = "postgresql://app:hunter2@db/x"\n', "contains a password"),
    ],
)
def test_bad_files_are_refused(tmp_path: Path, text: str, match: str) -> None:
    with pytest.raises(SettingsError, match=match):
        load_settings(_toml(tmp_path, text), env={})


def test_missing_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match="not found"):
        load_settings(tmp_path / "nope.toml", env={})


def test_bad_env_value_is_an_error() -> None:
    with pytest.raises(SettingsError, match="api_port"):
        load_settings(env={"SCRUPLE_API_PORT": "eighty"})


def test_password_from_env_is_secret_and_merged() -> None:
    s = load_settings(
        env={
            "SCRUPLE_DATABASE_URL": "postgresql://app@db.internal:5433/scruple?sslmode=require",
            "SCRUPLE_DATABASE_PASSWORD": "p@ss:w/rd",
        }
    )
    assert "p@ss" not in repr(s) and "p@ss" not in s.model_dump_json()
    assert s.database_dsn() == (
        "postgresql://app:p%40ss%3Aw%2Frd@db.internal:5433/scruple?sslmode=require"
    )


@pytest.mark.parametrize(
    "url",
    ["agent-gate.db", ":memory:", "postgresql://app:already@db/x", "sqlite:///x.db"],
)
def test_dsn_left_alone_when_nothing_to_merge(url: str) -> None:
    s = Settings.model_validate({"database_url": url, "database_password": "pw"})
    assert s.database_dsn() == url


def test_settings_are_frozen() -> None:
    s = Settings()
    with pytest.raises(ValueError, match="frozen"):
        s.api_port = 1  # type: ignore[misc]


def test_api_reads_the_database_url_from_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scruple.api.app import create_app

    db = tmp_path / "from-settings.db"
    monkeypatch.delenv("AGENT_GATE_DB", raising=False)
    monkeypatch.setenv("SCRUPLE_DATABASE_URL", f"sqlite:///{db}")
    create_app()
    assert db.exists()


def test_no_committed_file_holds_a_password() -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "*.toml", "*.ini", "*.yml", "*.yaml", "*.env"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split()
    for name in tracked:
        text = (ROOT / name).read_text(errors="ignore").lower()
        assert "database_password =" not in text, name
