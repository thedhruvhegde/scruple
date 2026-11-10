"""Gunicorn settings for the production image, taken from scruple `Settings`.

Every value comes from `load_settings()` (SCRUPLE_* environment variables or the
SCRUPLE_CONFIG TOML file), so the container, compose and the Helm chart configure
the server the same way as the rest of the engine:

    SCRUPLE_API_HOST / SCRUPLE_API_PORT   bind address (image default 0.0.0.0:8000)
    SCRUPLE_WEB_WORKERS                   uvicorn worker processes (default 1)
    SCRUPLE_WEB_TIMEOUT                   seconds before a silent worker is killed
    SCRUPLE_WEB_GRACEFUL_TIMEOUT          seconds a worker gets to finish on SIGTERM
    SCRUPLE_WEB_KEEPALIVE                 keep-alive seconds behind a load balancer
    SCRUPLE_WEB_MAX_REQUESTS              recycle a worker after N requests (0 = never)

The app is built per worker (`preload_app = False`): a database connection must
never be shared across fork. The synthetic baseline is imported once in the master
before forking, so workers never race to import it.

Shadow recorders (POST /shadow/...) keep their running state in the worker's
memory. With more than one worker (or replica) a client's batches can land on
different workers, so keep SCRUPLE_WEB_WORKERS=1 if you use the shadow endpoints.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from scruple.logging import configure_logging
from scruple.settings import load_settings

_settings = load_settings()

_host = _settings.api_host
if ":" in _host and not _host.startswith("["):
    _host = f"[{_host}]"

bind = [f"{_host}:{_settings.api_port}"]
wsgi_app = "scruple.api.app:create_app()"
worker_class = "uvicorn_worker.UvicornWorker"
workers = _settings.web_workers
timeout = _settings.web_timeout
graceful_timeout = _settings.web_graceful_timeout
keepalive = _settings.web_keepalive
max_requests = _settings.web_max_requests
max_requests_jitter = _settings.web_max_requests // 10
preload_app = False
# One JSON line per request comes from RequestIDMiddleware; no gunicorn access log.
accesslog = None
errorlog = "-"
loglevel = _settings.log_level.lower()
proc_name = "scruple"
# The gunicornc control socket would live under $HOME, which is read-only in the
# image; the orchestrator (signals, rollouts) manages the process instead.
control_socket_disable = True


MULTIPROC_ENV = "PROMETHEUS_MULTIPROC_DIR"


def on_starting(server: Any) -> None:
    """Master, before forking: JSON logging, an empty metrics directory (samples of
    a previous run would be summed into this one), and the baseline imported once."""
    from scruple.service import AgentGate

    configure_logging(_settings.log_level, _settings.log_format)
    if multiproc := os.environ.get(MULTIPROC_ENV):
        path = Path(multiproc)
        path.mkdir(parents=True, exist_ok=True)
        for stale in path.glob("*.db"):
            stale.unlink()
    gate = AgentGate.open(_settings.database_dsn())
    try:
        gate.bootstrap_baseline()
    finally:
        gate.storage.close()


def post_worker_init(worker: Any) -> None:
    configure_logging(_settings.log_level, _settings.log_format)


def child_exit(server: Any, worker: Any) -> None:
    """Drop a dead worker's live gauges (in-flight requests) from the aggregate."""
    if os.environ.get(MULTIPROC_ENV):
        from prometheus_client import multiprocess

        multiprocess.mark_process_dead(worker.pid)  # type: ignore[no-untyped-call]
