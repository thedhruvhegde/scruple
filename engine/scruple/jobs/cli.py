"""`scruple worker`: run queued jobs from the gate database until stopped."""

from __future__ import annotations

import argparse
import json
import signal
import threading
from pathlib import Path
from typing import Any


def cmd_worker(args: argparse.Namespace) -> int:
    from scruple.jobs.queue import open_queue
    from scruple.jobs.worker import JobContext, Worker
    from scruple.settings import load_settings

    db = args.db or load_settings().database_dsn()
    cfg = Path(args.gate_config).read_text(encoding="utf-8") if args.gate_config else None
    queue = open_queue(db)
    worker = Worker(
        queue,
        JobContext(database=db, case_store=args.case_store, gate_config_json=cfg),
        worker_id=args.worker_id,
        lease_s=args.lease,
        poll_s=args.poll,
        isolation=args.isolation,
    )
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())  # finish the current job, then exit
    try:
        if args.once:
            job = worker.run_once()
            print(
                json.dumps(
                    {
                        "job": None if job is None else job.job_id,
                        "status": None if job is None else job.status.value,
                    }
                )
            )
        else:
            ran = worker.run(max_jobs=args.max_jobs, stop=stop)
            print(json.dumps({"ran": ran}))
    finally:
        queue.close()
    return 0


def add_worker_parser(sub: Any, default_db: str) -> None:
    p = sub.add_parser("worker", help="run queued jobs (gate evaluation, backtest, bench score)")
    p.set_defaults(fn=cmd_worker)
    p.add_argument(
        "--db",
        default=None,
        help="SQLite path or postgresql:// URL "
        f"(default: $SCRUPLE_DATABASE_URL, else {default_db})",
    )
    p.add_argument("--case-store", default=None, help="case store for case_store backtests")
    p.add_argument("--gate-config", default=None, help="GateConfig JSON for gate evaluations")
    p.add_argument("--worker-id", default=None, help="default: host:pid:thread")
    p.add_argument("--lease", type=float, default=30.0, help="lease seconds (default 30)")
    p.add_argument("--poll", type=float, default=1.0, help="idle poll seconds (default 1)")
    p.add_argument("--isolation", choices=["process", "thread"], default="process")
    p.add_argument("--once", action="store_true", help="run at most one job, then exit")
    p.add_argument("--max-jobs", type=int, default=None, help="exit after this many jobs")


__all__ = ["add_worker_parser", "cmd_worker"]
