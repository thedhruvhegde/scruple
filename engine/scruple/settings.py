"""Typed settings from defaults, an optional TOML file and SCRUPLE_* env vars.

Precedence, lowest first: field defaults, the TOML file, environment variables,
explicit overrides passed to `load_settings`. The file is `config_file` if given,
else `$SCRUPLE_CONFIG`, else none. It may hold the keys at top level or under a
`[scruple]` table. Unknown keys in the file are an error; unrelated SCRUPLE_* env
vars (build metadata, test settings) are ignored.

Secrets never come from files: `database_password` is accepted only from the
environment (or an override), and a password inside a `database_url` read from a
file is refused. Combine them with `Settings.database_dsn()`.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

ENV_PREFIX = "SCRUPLE_"
CONFIG_ENV = "SCRUPLE_CONFIG"
SECRET_FIELDS = frozenset({"database_password", "heldout_salt", "metrics_token"})


class SettingsError(ValueError):
    pass


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    database_url: str = Field(
        default="agent-gate.db",
        min_length=1,
        description="SQLite path, sqlite:///path, :memory: or postgresql://user@host/db",
    )
    database_password: SecretStr | None = Field(
        default=None, description="env only; merged into a postgres URL without a password"
    )
    heldout_salt: SecretStr | None = Field(
        default=None, description="env only; seals the held-out seed namespace (scruple.sim.splits)"
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "text"] = "json"
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65535)
    tenant: str = Field(
        default="default",
        pattern=r"^[a-z0-9][a-z0-9-]{1,62}$",
        description="the tenant the command line acts in (as the local admin)",
    )
    # --- authentication (scruple.auth) ---
    auth_dev_actor_header: bool = Field(
        default=False,
        description="DEVELOPMENT ONLY: trust the X-Actor header as the caller, with every "
        "permission. Off by default; logs a warning and flags every response when on.",
    )
    auth_scrypt_n: int = Field(
        default=2**14, ge=2**10, le=2**20, description="scrypt cost for new API keys"
    )
    auth_key_cache_seconds: float = Field(
        default=300.0, ge=0, le=86_400, description="how long a checked key skips scrypt"
    )
    # OIDC bearer tokens: on when issuer, audience and jwks_url are all set.
    oidc_issuer: str | None = Field(default=None, description="expected `iss` claim")
    oidc_audience: str | None = Field(default=None, description="expected `aud` claim")
    oidc_jwks_url: str | None = Field(default=None, description="the issuer's JWKS URL")
    oidc_algorithms: str = Field(default="RS256,ES256", description="comma separated")
    oidc_leeway_seconds: int = Field(default=60, ge=0, le=600, description="clock skew")
    oidc_tenant_claim: str = "scruple_tenant"
    oidc_default_tenant: str | None = None
    oidc_roles_claim: str = "scruple_roles"
    oidc_subject_claim: str = "sub"
    # --- request limits (scruple.auth.limits), per API key unless the key says otherwise ---
    rate_limit_per_minute: int = Field(default=600, ge=1, le=1_000_000)
    rate_limit_burst: int = Field(default=100, ge=1, le=1_000_000)
    rate_limit_anonymous_per_minute: int = Field(default=120, ge=1, le=1_000_000)
    rate_limit_anonymous_burst: int = Field(default=30, ge=1, le=1_000_000)
    rate_limit_auth_failures_per_minute: int = Field(default=30, ge=1, le=1_000_000)
    rate_limit_auth_failures_burst: int = Field(default=10, ge=1, le=1_000_000)
    max_body_bytes: int = Field(default=2 * 1024 * 1024, ge=1024, le=64 * 1024 * 1024)
    # Production server (gunicorn + uvicorn workers, deploy/docker/gunicorn.conf.py).
    web_workers: int = Field(default=1, ge=1, le=64)
    web_timeout: int = Field(default=60, ge=1, le=3600)
    web_graceful_timeout: int = Field(default=30, ge=0, le=3600)
    web_keepalive: int = Field(default=5, ge=0, le=3600)
    web_max_requests: int = Field(default=0, ge=0)
    # Observability (scruple.observability). The token is a secret: env only.
    metrics_token: SecretStr | None = Field(
        default=None, description="env only; bearer token required for GET /metrics"
    )
    metrics_public: bool = Field(
        default=False, description="serve /metrics without a token (private networks only)"
    )
    otel_enabled: bool = False
    otel_service_name: str = Field(default="scruple-engine", min_length=1, max_length=128)

    def database_dsn(self) -> str:
        """`database_url` with `database_password` merged in (postgres URLs only)."""
        url = self.database_url
        if self.database_password is None or not url.startswith(("postgresql://", "postgres://")):
            return url
        parts = urlsplit(url)
        if parts.password is not None:
            return url
        user = quote(parts.username or "", safe="")
        password = quote(self.database_password.get_secret_value(), safe="")
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        port = f":{parts.port}" if parts.port else ""
        return urlunsplit(parts._replace(netloc=f"{user}:{password}@{host}{port}"))


def _env_values(env: Mapping[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in Settings.model_fields:
        key = ENV_PREFIX + name.upper()
        if key in env:
            out[name] = env[key]
    return out


def _file_values(path: Path) -> dict[str, Any]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SettingsError(f"config file not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise SettingsError(f"config file {path} is not valid TOML: {exc}") from exc
    if set(data) == {"scruple"} and isinstance(data["scruple"], dict):
        data = data["scruple"]
    secrets = SECRET_FIELDS & set(data)
    if secrets:
        raise SettingsError(
            f"{path}: {', '.join(sorted(secrets))} must come from the environment, not a file"
        )
    url = data.get("database_url")
    if isinstance(url, str) and "://" in url and urlsplit(url).password is not None:
        raise SettingsError(
            f"{path}: database_url contains a password; use SCRUPLE_DATABASE_PASSWORD"
        )
    return data


def load_settings(
    config_file: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    **overrides: Any,
) -> Settings:
    e = os.environ if env is None else env
    values: dict[str, Any] = {}
    path = config_file if config_file is not None else e.get(CONFIG_ENV)
    if path:
        values.update(_file_values(Path(path)))
    values.update(_env_values(e))
    values.update(overrides)
    try:
        return Settings.model_validate(values)
    except ValidationError as exc:
        raise SettingsError(str(exc)) from exc
