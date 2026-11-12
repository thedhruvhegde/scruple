"""`scruple serve --demo`: the hosted read-only demo, seeded and reset by itself.

What runs:

- At start, every earlier generation under the demo folder is deleted and a new one
  is seeded with `scruple console demo-seed` data (`scruple.console.demo_seed`):
  a SYNTHETIC tenant `demo` in a fresh SQLite database. Nothing is kept across a
  restart, and nothing needs a secret.
- The API and the console (at /console) are served over it with no login: every
  request is the read-only visitor (`scruple.demo.guard`). `DemoHost` answers 403
  to every method but GET and HEAD before authentication or the body; the app-wide
  `demo_guard` answers 403 to GET routes that need more than `read`.
- Rate limits are on, per client address (the anonymous bucket). With
  `demo_client_ip_header` set, the address is that header's value, which the proxy
  in front (Fly: `Fly-Client-IP`) sets and a client cannot. Bodies are capped by
  `max_body_bytes`, and uvicorn caps concurrent connections.
- Once a day at `demo_reset_utc_hour` a new generation is seeded in a background
  thread while the old one keeps serving, then swapped in; the old database is closed
  and deleted a minute later. The seed's dates are relative to the day it runs, so the
  demo's twelve weeks always end today.
- No job worker runs: the seed runs its jobs itself, and the queued job it leaves
  stays queued.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from starlette.types import Receive, Scope, Send

from scruple.auth.limits import Limits
from scruple.auth.middleware import send_json
from scruple.console.demo_seed import DEMO_TENANT, SeedReport, seed_demo
from scruple.demo.guard import READ_METHODS, READ_ONLY, DemoAuthenticator
from scruple.service import AgentGate
from scruple.settings import Settings

log = logging.getLogger("scruple.demo")

GENERATION_PREFIX = "gen-"
RETIRE_AFTER_S = 60.0  # the old generation finishes its in-flight requests first
CONCURRENCY = 64  # uvicorn answers 503 past this many open connections and tasks
SEEDING = "the demo is seeding its synthetic data; try again in a minute"

Seeder = Callable[..., SeedReport]


def demo_settings(settings: Settings) -> Settings:
    """The settings the demo app runs with: the demo tenant, no metrics without a
    token, no development header, rate buckets in memory."""
    return settings.model_copy(
        update={
            "demo": True,
            "tenant": DEMO_TENANT,
            "metrics_public": False,
            "auth_dev_actor_header": False,
            "rate_limit_backend": "memory",
        }
    )


def next_reset(now: datetime, hour: int) -> datetime:
    """The next `hour`:00 UTC strictly after `now`."""
    now = now.astimezone(UTC)
    at = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    return at if at > now else at + timedelta(days=1)


@dataclass
class Generation:
    """One seeded copy of the demo: its folder, database and app."""

    folder: Path
    gate: AgentGate
    app: FastAPI
    seeded_at: datetime

    def retire(self) -> None:
        try:
            self.gate.storage.close()
        finally:
            shutil.rmtree(self.folder, ignore_errors=True)


class DemoHost:
    """ASGI app: refuses writes, then hands the request to the current generation."""

    def __init__(
        self,
        root: Path,
        settings: Settings,
        *,
        seed: Seeder = seed_demo,
        retire_after_s: float = RETIRE_AFTER_S,
    ) -> None:
        self.root = root
        self.settings = demo_settings(settings)
        self.seed = seed
        self.retire_after_s = retire_after_s
        # One set of buckets for every generation: a reset does not refill them.
        self.limits = Limits.from_settings(self.settings)
        self.current: Generation | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        header = self.settings.demo_client_ip_header
        self._ip_header = header.lower().encode("latin-1") if header else None

    # --- generations -------------------------------------------------------------

    def wipe(self) -> None:
        """Delete every generation on disk (a restart re-seeds from nothing)."""
        self.root.mkdir(parents=True, exist_ok=True)
        for old in self.root.glob(GENERATION_PREFIX + "*"):
            shutil.rmtree(old, ignore_errors=True)

    def build(self) -> Generation:
        from scruple.api.app import create_app
        from scruple.console.sources import ConsoleSources

        now = datetime.now(UTC)
        folder = self.root / f"{GENERATION_PREFIX}{now:%Y%m%dT%H%M%S%fZ}"
        folder.mkdir(parents=True)
        db = folder / "demo.db"
        data = folder / "data"
        started = datetime.now(UTC)
        self.seed(str(db), data, DEMO_TENANT, say=log.info)
        opened = AgentGate.open(str(db))
        gate = AgentGate(opened.storage.for_tenant(DEMO_TENANT), opened.clock, opened.config)
        app = create_app(
            gate,
            self.settings,
            auth=DemoAuthenticator(),
            limits=self.limits,
            console_sources=ConsoleSources.build(data=data),
            demo=True,
        )
        took = (datetime.now(UTC) - started).total_seconds()
        log.info("demo seeded in %s (%.1f s)", folder.name, took)
        return Generation(folder, gate, app, now)

    def reset(self) -> Generation:
        """Seed a new generation, swap it in, retire the old one after a grace period.
        If seeding fails the old generation keeps serving and the error propagates."""
        with self._lock:
            new = self.build()
            old, self.current = self.current, new
        if old is not None:
            t = threading.Timer(self.retire_after_s, old.retire)
            t.daemon = True
            t.start()
        return new

    def start(self) -> None:
        """Wipe, seed the first generation (blocking), start the daily reset."""
        self.wipe()
        self.reset()
        threading.Thread(target=self._daily, name="scruple-demo-reset", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _daily(self) -> None:
        hour = self.settings.demo_reset_utc_hour
        while not self._stop.is_set():
            wait = (next_reset(datetime.now(UTC), hour) - datetime.now(UTC)).total_seconds()
            if self._stop.wait(max(wait, 1.0)):
                return
            try:
                self.reset()
            except Exception:
                log.exception("demo reset failed; the previous generation keeps serving")

    # --- ASGI --------------------------------------------------------------------

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await _lifespan(receive, send)
            return
        if scope["type"] != "http":  # no websockets anywhere in the app
            return
        if scope["method"] not in READ_METHODS:
            await send_json(send, 403, READ_ONLY)
            return
        gen = self.current
        if gen is None:
            await send_json(send, 503, SEEDING, [(b"retry-after", b"60")])
            return
        if self._ip_header is not None:
            scope = self._with_client(scope, self._ip_header)
        await gen.app(scope, receive, send)

    @staticmethod
    def _with_client(scope: Scope, header: bytes) -> Scope:
        for name, value in scope.get("headers") or ():
            if name == header:
                address = value.decode("latin-1").split(",")[0].strip()
                if address and len(address) <= 64:
                    return {**scope, "client": (address, 0)}
        return scope


async def _lifespan(receive: Receive, send: Send) -> None:
    """The demo app has no startup work of its own: seeding happens before serving."""
    while True:
        message = await receive()
        if message["type"] == "lifespan.startup":
            await send({"type": "lifespan.startup.complete"})
        elif message["type"] == "lifespan.shutdown":
            await send({"type": "lifespan.shutdown.complete"})
            return


def serve_demo(settings: Settings, host: str, port: int, **uvicorn_options: Any) -> int:
    """Seed, then serve until stopped. `settings.demo_dir` holds the generations
    (default: a new temporary folder)."""
    import uvicorn

    root = (
        Path(settings.demo_dir)
        if settings.demo_dir
        else Path(tempfile.mkdtemp(prefix="scruple-demo-"))
    )
    app = DemoHost(root, settings)
    log.warning("hosted demo: seeding SYNTHETIC data in %s, every write refused", root)
    app.start()
    try:
        uvicorn.run(
            app,
            host=host,
            port=port,
            log_config=None,
            access_log=False,
            limit_concurrency=CONCURRENCY,
            timeout_keep_alive=5,
            **uvicorn_options,
        )
    finally:
        app.stop()
    return 0


__all__ = [
    "CONCURRENCY",
    "DemoHost",
    "Generation",
    "demo_settings",
    "next_reset",
    "serve_demo",
]
