"""Liveness and readiness probes. Both are public: no credentials, no secrets out.

- `GET /health` (liveness): the process is up and serving. No I/O, so a slow or
  unreachable database never gets a healthy pod restarted.
- `GET /ready` (readiness): 200 only if every check passes, else 503 with the
  failing check named. Checks:
    database     a real read from storage (the last audit entry)
    migrations   postgres only: alembic_version equals the code's single head
                 (SQLite builds its schema in code: "not applicable")
    audit_chain  the hash chain re-verifies from genesis. This re-hashes every
                 entry, so the result is cached for SCRUPLE_READY_AUDIT_TTL_SECONDS
                 (default 30; 0 = every probe). A failure is never cached past the TTL.

Details are short fixed phrases; driver errors are logged, never returned, so the
public probe cannot leak hostnames or SQL.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from scruple.auth import public
from scruple.service import AgentGate
from scruple.settings import Settings

log = logging.getLogger("scruple.api.health")

# Public, and exempt from rate limits (`AuthMiddleware(unlimited=...)`).
PROBE_PATHS = ("/health", "/ready")


class Check(BaseModel):
    ok: bool
    detail: str


class Readiness(BaseModel):
    status: str
    checks: dict[str, Check]


def check_database(gate: AgentGate) -> Check:
    try:
        gate.storage.last_audit()
    except Exception:
        log.exception("readiness: database read failed")
        return Check(ok=False, detail="database unreachable")
    return Check(ok=True, detail="reachable")


def check_migrations(gate: AgentGate) -> Check:
    from scruple.storage.postgres import PostgresStorage

    storage = gate.storage
    if not isinstance(storage, PostgresStorage):
        return Check(ok=True, detail="not applicable (sqlite)")
    from scruple.storage.migrate import head

    want = head()
    try:
        rows = storage._conn.query("SELECT version_num FROM alembic_version")
    except Exception as exc:
        log.warning("readiness: alembic_version unreadable", extra={"error": type(exc).__name__})
        return Check(ok=False, detail="not migrated (no alembic_version)")
    have = sorted(str(r["version_num"]) for r in rows)
    if have != [want]:
        return Check(ok=False, detail=f"at {','.join(have) or 'none'}, head is {want}")
    return Check(ok=True, detail=f"at head {want}")


class AuditChainCheck:
    """`gate.audit.verify()`, cached for `ttl` seconds (thread-safe, one at a time)."""

    def __init__(
        self, gate: AgentGate, ttl: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.gate, self.ttl, self.clock = gate, ttl, clock
        self._lock = threading.Lock()
        self._at: float | None = None
        self._last: Check | None = None

    def __call__(self) -> Check:
        with self._lock:
            now = self.clock()
            if self._last is not None and self._at is not None and now - self._at < self.ttl:
                return self._last
            try:
                result = self.gate.audit.verify()
            except Exception:
                log.exception("readiness: audit chain could not be read")
                check = Check(ok=False, detail="audit chain unreadable")
            else:
                if result.ok:
                    check = Check(ok=True, detail=f"{result.entries} entries verified")
                else:
                    log.error(
                        "readiness: audit chain broken",
                        extra={"first_bad_seq": result.first_bad_seq, "problem": result.problem},
                    )
                    check = Check(ok=False, detail=f"broken at position {result.first_bad_seq}")
            self._at, self._last = now, check
            return check


def health_router(
    gate: AgentGate, settings: Settings, clock: Callable[[], float] = time.monotonic
) -> APIRouter:
    router = APIRouter(tags=["health"])
    audit_chain = AuditChainCheck(gate, settings.ready_audit_ttl_seconds, clock)

    @router.get("/health", dependencies=[public()])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @router.get(
        "/ready",
        response_model=Readiness,
        responses={503: {"model": Readiness}},
        dependencies=[public()],
    )
    def ready() -> JSONResponse:
        checks = {"database": check_database(gate)}
        if checks["database"].ok:
            checks["migrations"] = check_migrations(gate)
            checks["audit_chain"] = audit_chain()
        ok = all(c.ok for c in checks.values())
        body = Readiness(status="ready" if ok else "not_ready", checks=checks)
        return JSONResponse(body.model_dump(), status_code=200 if ok else 503)

    return router
