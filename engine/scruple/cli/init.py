"""`scruple init`: set up a new partner install in one command, safely re-runnable.

Steps, in order; each reports `created`, `exists` or `ok`:

1. config: write `scruple.toml` (database URL without any password, tenant, log
   format). An existing file is validated with `scruple.settings` and kept; flags
   that disagree with it are an error, never a silent overwrite.
2. policy: write `scruple-policy.toml`, an approval policy skeleton (named approvers,
   quorum, required role) for the partner to fill in. With `--approver` flags it is
   filled now. An existing file is validated (`ApprovalPolicy`) and kept. Init does
   not put the policy in force: that is an audited operator action.
3. database: PostgreSQL is migrated to the alembic head; SQLite creates its schema
   on open.
4. tenant: the synthetic baseline policy is imported into the tenant (idempotent) and
   a `tenant.initialised` audit entry is appended once.
5. admin key: a `risk_admin` API key for `--admin`, created only when that subject
   has no active risk_admin key in the tenant. The key is shown once (stdout, or
   `--key-out FILE` written with mode 0600); a re-run never shows or creates another.
6. smoke: the audit chain verifies, the API answers `/health`, refuses an anonymous
   `/auth/whoami`, and (when this run created the key) accepts the new key in the
   right tenant with the admin permission. All in process: no port is opened.

Interactive by default: missing values are asked for and the plan is confirmed.
`--yes` takes defaults and flags without asking (CI). Without a terminal and without
`--yes`, init refuses rather than guess. Nothing here is a statement about any real
partner's setup; the baseline policy is synthetic.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from scruple.auth.models import TENANT_PATTERN
from scruple.registry.models import ACTOR_PATTERN

CONFIG_NAME = "scruple.toml"
POLICY_NAME = "scruple-policy.toml"
EVENT_TENANT_INITIALISED = "tenant.initialised"
INIT_ACTOR = "scruple-init"
ADMIN_ROLE = "risk_admin"
APPROVER_SPEC = re.compile(r"^(?P<name>[A-Za-z0-9._@:-]{1,128}?)(?:=(?P<roles>[a-z0-9_,-]*))?$")


class InitError(ValueError):
    """Init cannot proceed; the message says what to change."""


@dataclass
class Step:
    name: str
    status: str  # created | exists | ok | failed | skipped
    detail: str

    def line(self) -> str:
        return f"[{self.status}] {self.name}: {self.detail}"


@dataclass
class InitResult:
    steps: list[Step] = field(default_factory=list)
    api_key: str | None = None
    key_id: str | None = None

    @property
    def ok(self) -> bool:
        return all(s.status != "failed" for s in self.steps)

    def add(self, name: str, status: str, detail: str) -> Step:
        s = Step(name, status, detail)
        self.steps.append(s)
        return s

    def to_json(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "key_id": self.key_id,
            "steps": [{"name": s.name, "status": s.status, "detail": s.detail} for s in self.steps],
        }


@dataclass(frozen=True)
class Plan:
    directory: Path
    database_url: str
    tenant: str
    admin: str
    approvers: tuple[tuple[str, tuple[str, ...]], ...]
    required: int | None
    required_role: str | None
    key_out: Path | None
    smoke: bool

    @property
    def config_path(self) -> Path:
        return self.directory / CONFIG_NAME

    @property
    def policy_path(self) -> Path:
        return self.directory / POLICY_NAME


# --- pure helpers ---------------------------------------------------------------------


def split_password(url: str) -> tuple[str, str | None]:
    """(URL without a password, the password or None). Only URLs carry one."""
    if "://" not in url:
        return url, None
    parts = urlsplit(url)
    if parts.password is None:
        return url, None
    netloc = parts.hostname or ""
    if ":" in netloc:
        netloc = f"[{netloc}]"
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    if parts.username:
        netloc = f"{parts.username}@{netloc}"
    return urlunsplit(parts._replace(netloc=netloc)), parts.password


def parse_approver(spec: str) -> tuple[str, tuple[str, ...]]:
    """`alice@bank.example=model-risk,ops` -> ("alice@bank.example", ("model-risk", "ops"))."""
    m = APPROVER_SPEC.match(spec)
    if m is None or not re.match(ACTOR_PATTERN, m["name"]):
        raise InitError(f"approver {spec!r} must look like name or name=role,role")
    roles = tuple(r for r in (m["roles"] or "").split(",") if r)
    return m["name"], roles


def _toml_str(s: str) -> str:
    # A JSON string is a valid TOML basic string (same escapes, \uXXXX included).
    return json.dumps(s, ensure_ascii=True)


def config_text(database_url: str, tenant: str) -> str:
    return (
        "# scruple settings (scruple.settings). Secrets never go here: set\n"
        "# SCRUPLE_DATABASE_PASSWORD, SCRUPLE_CONSOLE_SECRET and the rest in the environment.\n"
        "# Use it with SCRUPLE_CONFIG=<this file>.\n"
        "[scruple]\n"
        f"database_url = {_toml_str(database_url)}\n"
        f"tenant = {_toml_str(tenant)}\n"
        'log_format = "json"\n'
    )


def policy_text(plan: Plan) -> str:
    head = (
        "# Approval policy for this tenant (scruple.gate.approval.ApprovalPolicy).\n"
        "# Named approvers only; the proposer, owner and agent of a rule can never approve\n"
        "# it. `scruple init` validates this file but does not put it in force.\n"
    )
    if not plan.approvers:
        return head + (
            "# Fill in and uncomment:\n"
            "# [approval]\n"
            "# required = 2\n"
            '# required_role = "model-risk"\n'
            "# [[approval.approvers]]\n"
            '# name = "first.approver@your-bank.example"\n'
            '# roles = ["model-risk"]\n'
        )
    required = plan.required or 1
    lines = [head, "[approval]", f"required = {required}"]
    if plan.required_role:
        lines.append(f"required_role = {_toml_str(plan.required_role)}")
    for name, roles in plan.approvers:
        lines += [
            "",
            "[[approval.approvers]]",
            f"name = {_toml_str(name)}",
            f"roles = [{', '.join(_toml_str(r) for r in roles)}]",
        ]
    return "\n".join(lines) + "\n"


def read_policy(path: Path) -> Any:
    """The `ApprovalPolicy` in a policy file, or None for the unfilled skeleton."""
    return parse_policy(path.read_text(encoding="utf-8"), path)


def parse_policy(text: str, path: Path) -> Any:
    from pydantic import ValidationError

    from scruple.gate.approval import ApprovalPolicy

    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise InitError(f"{path} is not valid TOML: {exc}") from exc
    unknown = set(data) - {"approval"}
    if unknown:
        raise InitError(f"{path}: unknown sections {', '.join(sorted(unknown))}")
    if "approval" not in data:
        return None
    try:
        return ApprovalPolicy.model_validate(data["approval"])
    except ValidationError as exc:
        raise InitError(f"{path}: approval policy is not valid: {exc}") from exc


def _write_new(path: Path, text: str, mode: int = 0o644) -> None:
    """Create `path` (never overwrite: O_EXCL)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


# --- the steps ------------------------------------------------------------------------


def _config_step(plan: Plan, res: InitResult) -> Any:
    from scruple.settings import SettingsError, load_settings

    path = plan.config_path
    stored_url, _ = split_password(plan.database_url)
    if path.exists():
        try:
            s = load_settings(path, env={})
        except SettingsError as exc:
            raise InitError(f"{path} exists but is not valid: {exc}") from exc
        clash = [
            n
            for n, (have, want) in {
                "database_url": (s.database_url, stored_url),
                "tenant": (s.tenant, plan.tenant),
            }.items()
            if have != want
        ]
        if clash:
            raise InitError(
                f"{path} exists with a different {' and '.join(clash)}; "
                "edit or remove it, or pass matching flags"
            )
        res.add("config", "exists", f"{path} (validated, kept)")
        return s
    plan.directory.mkdir(parents=True, exist_ok=True)
    _write_new(path, config_text(stored_url, plan.tenant))
    res.add("config", "created", str(path))
    return load_settings(path, env={})


def _policy_check(plan: Plan) -> Any:
    """Validate the existing policy file, or the one init would write, before any
    step writes anything: a bad quorum is refused with nothing changed."""
    path = plan.policy_path
    if path.exists():
        return read_policy(path)
    return parse_policy(policy_text(plan), path)


def _policy_step(plan: Plan, res: InitResult) -> None:
    path = plan.policy_path
    if path.exists():
        policy = read_policy(path)
        what = "skeleton, not filled in" if policy is None else "validated"
        res.add("policy", "exists", f"{path} ({what}, kept)")
        return
    text = policy_text(plan)
    policy = parse_policy(text, path)
    _write_new(path, text)
    what = (
        "skeleton to fill in" if policy is None else f"{policy.required} of {len(policy.approvers)}"
    )
    res.add("policy", "created", f"{path} ({what})")


def _database_step(plan: Plan, res: InitResult, dsn: str) -> Any:
    from scruple.storage import POSTGRES_PREFIXES, open_storage

    if dsn.startswith(POSTGRES_PREFIXES):
        from scruple.storage import migrate

        migrate.upgrade(dsn)
        res.add("database", "ok", f"postgres migrated to {migrate.head()}")
    storage = open_storage(dsn)
    if not dsn.startswith(POSTGRES_PREFIXES):
        res.add("database", "ok", f"sqlite schema ready at {dsn}")
    return storage


def _tenant_step(plan: Plan, res: InitResult, storage: Any) -> Any:
    from scruple.service import AgentGate

    gate = AgentGate(storage.for_tenant(plan.tenant))
    created = gate.bootstrap_baseline()
    seen = any(e.event_type == EVENT_TENANT_INITIALISED for e in gate.audit.entries())
    if not seen:
        gate.audit.append(
            EVENT_TENANT_INITIALISED, INIT_ACTOR, {"tenant": plan.tenant, "admin": plan.admin}
        )
    status = "exists" if seen and not created else "created"
    res.add("tenant", status, f"{plan.tenant} ({len(created)} baseline rules imported)")
    return gate


def _key_step(plan: Plan, res: InitResult, settings: Any, storage: Any) -> Any:
    from scruple.auth.config import key_service
    from scruple.clock import SystemClock

    svc = key_service(settings, storage)
    now = SystemClock().now()
    for k in svc.list(plan.tenant):
        if k.subject == plan.admin and ADMIN_ROLE in k.roles and k.active(now):
            res.add("admin key", "exists", f"{k.key_id} for {k.subject} (not shown again)")
            return svc
    if plan.key_out is not None and plan.key_out.exists():
        raise InitError(f"{plan.key_out} exists; refusing to overwrite a key file")
    rec, token = svc.create(
        plan.admin,
        (),
        INIT_ACTOR,
        tenant=plan.tenant,
        roles=(ADMIN_ROLE,),
        description="created by scruple init",
    )
    res.key_id = rec.key_id
    res.api_key = token.get_secret_value()
    where = "stdout, once"
    if plan.key_out is not None:
        _write_new(plan.key_out, res.api_key + "\n", 0o600)
        where = f"{plan.key_out} (mode 0600)"
    res.add("admin key", "created", f"{rec.key_id} for {rec.subject}, role {ADMIN_ROLE} ({where})")
    return svc


def _smoke_step(plan: Plan, res: InitResult, gate: Any, settings: Any, svc: Any) -> None:
    from fastapi.testclient import TestClient

    from scruple.api.app import create_app
    from scruple.auth.authn import Authenticator

    report = gate.verify_integrity()
    problems = [] if report.ok else list(report.problems) or ["audit chain does not verify"]
    with TestClient(create_app(gate, settings, auth=Authenticator(svc))) as c:
        if c.get("/health").status_code != 200:
            problems.append("/health did not answer 200")
        if c.get("/auth/whoami").status_code != 401:
            problems.append("/auth/whoami answered an anonymous caller")
        if res.api_key is not None:
            r = c.get("/auth/whoami", headers={"X-API-Key": res.api_key})
            body = r.json() if r.status_code == 200 else {}
            if body.get("tenant") != plan.tenant or "admin" not in body.get("permissions", []):
                problems.append(f"the new key was not accepted as {plan.tenant} admin")
    if problems:
        res.add("smoke", "failed", "; ".join(problems))
    else:
        checked = "chain, /health, auth refusal" + (", new key" if res.api_key else "")
        res.add("smoke", "ok", f"{checked} ({report.chain.entries} audit entries)")


def run_init(plan: Plan) -> InitResult:
    """Run every step. Raises `InitError` when a step cannot proceed safely."""
    from scruple.settings import Settings

    if not re.match(TENANT_PATTERN, plan.tenant):
        raise InitError(f"tenant must match {TENANT_PATTERN}")
    if not re.match(ACTOR_PATTERN, plan.admin):
        raise InitError(f"admin subject must match {ACTOR_PATTERN}")
    _policy_check(plan)
    res = InitResult()
    settings: Settings = _config_step(plan, res)
    _policy_step(plan, res)
    _, password = split_password(plan.database_url)
    # The password, if any, comes from the flag for this run or from the environment.
    env_pw = os.environ.get("SCRUPLE_DATABASE_PASSWORD")
    pw = password or env_pw
    if pw is not None:
        from pydantic import SecretStr

        settings = settings.model_copy(update={"database_password": SecretStr(pw)})
    storage = _database_step(plan, res, settings.database_dsn())
    try:
        gate = _tenant_step(plan, res, storage)
        svc = _key_step(plan, res, settings, storage)
        if plan.smoke:
            _smoke_step(plan, res, gate, settings, svc)
        else:
            res.add("smoke", "skipped", "--no-smoke")
    finally:
        storage.close()
    return res


# --- command line ------------------------------------------------------------------------

Ask = Callable[[str], str]


def _interactive() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _ask(ask: Ask, label: str, default: str) -> str:
    got = ask(f"{label} [{default}]: ").strip()
    return got or default


def plan_from_args(args: argparse.Namespace, ask: Ask | None = None) -> Plan:
    directory = Path(args.dir).resolve()
    db = args.db
    tenant = args.tenant
    admin = args.admin
    if ask is not None:
        db = db or _ask(
            ask, "database (SQLite path or postgresql:// URL)", str(directory / "scruple.db")
        )
        tenant = tenant or _ask(ask, "tenant id", "default")
        admin = admin or _ask(ask, "admin subject (who holds the first key)", "risk-admin")
    approvers = tuple(parse_approver(a) for a in args.approver or ())
    return Plan(
        directory=directory,
        database_url=db or str(directory / "scruple.db"),
        tenant=tenant or "default",
        admin=admin or "risk-admin",
        approvers=approvers,
        required=args.required,
        required_role=args.required_role,
        key_out=Path(args.key_out).resolve() if args.key_out else None,
        smoke=not args.no_smoke,
    )


def describe(plan: Plan) -> str:
    db, pw = split_password(plan.database_url)
    return "\n".join(
        (
            f"  directory  {plan.directory}",
            f"  config     {plan.config_path}",
            f"  policy     {plan.policy_path}",
            f"  database   {db}{' (password from the flag, not stored)' if pw else ''}",
            f"  tenant     {plan.tenant}",
            f"  admin key  {plan.admin} ({ADMIN_ROLE})"
            + (f" -> {plan.key_out}" if plan.key_out else " -> stdout"),
        )
    )


def cmd_init(args: argparse.Namespace, ask: Ask = input) -> int:
    if not args.yes and not _interactive():
        print(
            "error: no terminal to ask on; pass --yes to run with flags and defaults",
            file=sys.stderr,
        )
        return 2
    plan = plan_from_args(args, None if args.yes else ask)
    if not args.yes:
        print("scruple init will set up:\n" + describe(plan), file=sys.stderr)
        if ask("proceed? [y/N]: ").strip().lower() not in ("y", "yes"):
            print("nothing done", file=sys.stderr)
            return 1
    try:
        res = run_init(plan)
    except InitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        out = res.to_json()
        if res.api_key is not None and plan.key_out is None:
            out["api_key"] = res.api_key
        print(json.dumps(out, sort_keys=True))
    else:
        for s in res.steps:
            print(s.line(), file=sys.stderr)
        if res.api_key is not None and plan.key_out is None:
            print(res.api_key)
            print(
                "This is the only time the key is shown; store it in a secret manager.",
                file=sys.stderr,
            )
        print(f"next: SCRUPLE_CONFIG={plan.config_path} scruple serve", file=sys.stderr)
    return 0 if res.ok else 1


def add_init_parser(sub: Any) -> None:
    p = sub.add_parser("init", help="set up a new partner install (idempotent; --yes for CI)")
    p.set_defaults(fn=cmd_init)
    p.add_argument("--dir", default=".", help="where scruple.toml and scruple-policy.toml go")
    p.add_argument("--db", default=None, help="SQLite path or postgresql:// URL")
    p.add_argument("--tenant", default=None, help="tenant id (default: default)")
    p.add_argument("--admin", default=None, help="subject of the first admin key")
    p.add_argument(
        "--approver",
        action="append",
        help="named approver for the policy file, name or name=role,role (repeatable)",
    )
    p.add_argument("--required", type=int, default=None, help="approvals required (policy file)")
    p.add_argument("--required-role", default=None, help="a role one approval must carry")
    p.add_argument("--key-out", default=None, help="write the new key to this file (0600)")
    p.add_argument("--no-smoke", action="store_true", help="skip the in-process smoke check")
    p.add_argument("--yes", action="store_true", help="do not ask; use flags and defaults")
    p.add_argument("--json", action="store_true", help="print the result as JSON")


__all__ = [
    "CONFIG_NAME",
    "EVENT_TENANT_INITIALISED",
    "POLICY_NAME",
    "InitError",
    "InitResult",
    "Plan",
    "add_init_parser",
    "cmd_init",
    "parse_approver",
    "run_init",
    "split_password",
]
