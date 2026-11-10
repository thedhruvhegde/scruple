"""Load measurements (roadmap 169), shared by tests/test_load.py and its CLI.

`python -m tests.load` prints the numbers as JSON. Everything is synthetic and runs
on this machine only; nothing is sent anywhere.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]


def episodes_per_minute(n: int = 2_000, seed: int = 0) -> dict[str, float]:
    """Rollouts through the public SDK (`scruple.agents.sdk.evaluate`): each one loads
    a case, calls tools, acts, submits and is scored by the verifiers."""
    from scruple.agents.baselines import rule_based
    from scruple.agents.sdk import evaluate

    t = time.perf_counter()
    card = evaluate(rule_based, n=n, seed=seed)
    dt = time.perf_counter() - t
    assert card.n == n
    return {"episodes": float(n), "seconds": dt, "per_minute": n / dt * 60}


def cli_episodes_per_minute(n: int = 300) -> dict[str, float]:
    """The same through the episode CLI (`scruple episode start/call/submit`), run in
    process, with the on-disk episode store (each command replays the call log)."""
    from scruple.cli.main import main

    with tempfile.TemporaryDirectory() as store:
        t = time.perf_counter()
        for i in range(n):
            buf = StringIO()
            with redirect_stdout(buf):
                assert main(["episode", "start", "--seed", str(i), "--store", store]) == 0
            eid = json.loads(buf.getvalue())["episode_id"]
            with redirect_stdout(StringIO()):
                assert main(["episode", "call", eid, "get_case", "--store", store]) == 0
                assert main(["episode", "call", eid, "list_evidence", "--store", store]) == 0
                assert main(["episode", "call", eid, "accept_liability", "--store", store]) == 0
                decision = '{"explanation": "load test"}'
                assert main(["episode", "submit", eid, decision, "--store", store]) == 0
        dt = time.perf_counter() - t
    return {"episodes": float(n), "seconds": dt, "per_minute": n / dt * 60}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextmanager
def serving(db: Path) -> Iterator[str]:
    """A real `scruple serve` process (uvicorn) on a free port."""
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "scruple.cli.main", "serve", "--db", str(db), "--port", str(port)],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "SCRUPLE_LOG_LEVEL": "WARNING"},
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                if httpx.get(f"{base}/health", timeout=1).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if proc.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("server did not start")
            time.sleep(0.1)
        yield base
    finally:
        proc.terminate()
        proc.wait(timeout=30)


def _hammer(
    base: str, request: Callable[[httpx.Client], httpx.Response], seconds: float, clients: int
) -> dict[str, float]:
    done = [0] * clients
    errors = [0] * clients
    stop = time.monotonic() + seconds

    def worker(i: int) -> None:
        with httpx.Client(base_url=base, timeout=10) as c:
            while time.monotonic() < stop:
                r = request(c)
                if r.status_code >= 500:
                    errors[i] += 1
                done[i] += 1

    t = time.perf_counter()
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(clients)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    dt = time.perf_counter() - t
    return {
        "requests": float(sum(done)),
        "errors": float(sum(errors)),
        "per_second": sum(done) / dt,
    }


def http_requests_per_second(seconds: float = 4.0, clients: int = 4) -> dict[str, Any]:
    """Requests per second against one uvicorn worker, per endpoint."""
    from scruple.auth.keys import ApiKeyService
    from scruple.auth.models import Permission
    from scruple.clock import SteppingClock
    from scruple.service import AgentGate
    from scruple.sim.library import GOOD_RULE, GOOD_RULE_ID
    from scruple.storage import SQLiteStorage
    from tests.ci_support import PROV

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "load.db"
        g = AgentGate(SQLiteStorage(str(db)), SteppingClock())
        g.bootstrap_baseline()
        g.propose(GOOD_RULE_ID, GOOD_RULE, "ops-team", PROV)
        # Real API keys at the production scrypt cost; the server's verification
        # cache is what keeps steady traffic cheap, so it is part of the measurement.
        keys = ApiKeyService(g.storage)
        read_key = keys.create("load-reader", [Permission.READ], "local")[1]
        agent_key = keys.create(PROV.agent_id, [Permission.PROPOSE], "local")[1]
        reader = {"X-API-Key": read_key.get_secret_value()}
        agent = {"X-API-Key": agent_key.get_secret_value()}
        g.storage.close()
        out: dict[str, Any] = {"clients": clients, "seconds": seconds}
        with serving(db) as base:
            out["GET /health"] = _hammer(base, lambda c: c.get("/health"), seconds, clients)
            out["GET /rules"] = _hammer(
                base, lambda c: c.get("/rules", headers=reader), seconds, clients
            )
            out["GET /audit/verify"] = _hammer(
                base, lambda c: c.get("/audit/verify", headers=reader), seconds, clients
            )
            n = [0]

            def register(c: httpx.Client) -> httpx.Response:
                n[0] += 1  # distinct behaviour each time (amount threshold)
                body = {
                    "rule_id": f"load-{n[0] % 1_000_000}",
                    "spec": {
                        "description": "load test rule",
                        "conditions": [
                            {"field": "amount_usd_minor", "op": "gt", "value": 1000 + n[0]}
                        ],
                        "action": "contest",
                        "priority": 10,
                    },
                    "owner": "ops-team",
                    "provenance": {"agent_id": PROV.agent_id},
                }
                return c.post("/rules", json=body, headers=agent)

            out["POST /rules"] = _hammer(base, register, seconds, 1)
    return out


if __name__ == "__main__":
    print(
        json.dumps(
            {
                "sdk": episodes_per_minute(),
                "cli": cli_episodes_per_minute(),
                "http": http_requests_per_second(),
            },
            indent=1,
        )
    )
