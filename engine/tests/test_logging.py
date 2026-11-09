from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scruple.api.app import create_app
from scruple.logging import (
    JsonFormatter,
    RequestIDMiddleware,
    configure_logging,
    current_request_id,
    request_context,
)
from scruple.service import AgentGate
from scruple.storage import SQLiteStorage


@pytest.fixture
def logs() -> Iterator[io.StringIO]:
    buf = io.StringIO()
    root = logging.getLogger()
    level = root.level
    handler = configure_logging("DEBUG", "json", buf)
    yield buf
    root.removeHandler(handler)
    root.setLevel(level)


def _lines(buf: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in buf.getvalue().splitlines()]


def _probe_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)

    @app.get("/sync")
    def sync_route() -> dict[str, str | None]:
        logging.getLogger("scruple.test").info("inside sync", extra={"k": 1})
        return {"rid": current_request_id()}

    @app.get("/async")
    async def async_route() -> dict[str, str | None]:
        return {"rid": current_request_id()}

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("boom")

    return app


def test_json_lines_carry_request_id_and_extras(logs: io.StringIO) -> None:
    log = logging.getLogger("scruple.test")
    log.info("outside")
    with request_context("req-1"):
        log.warning("hello %s", "world", extra={"rule_id": "r-1", "n": 2})
    a, b = _lines(logs)
    assert "request_id" not in a and a["msg"] == "outside"
    assert b["request_id"] == "req-1" and b["msg"] == "hello world"
    assert b["level"] == "WARNING" and b["logger"] == "scruple.test"
    assert b["rule_id"] == "r-1" and b["n"] == 2
    assert str(b["ts"]).endswith("+00:00")
    assert current_request_id() is None  # context restored


def test_exceptions_are_logged_as_one_json_line(logs: io.StringIO) -> None:
    try:
        raise ValueError("bad")
    except ValueError:
        logging.getLogger("scruple.test").exception("failed")
    (line,) = _lines(logs)
    assert "ValueError: bad" in str(line["exc"])


@pytest.mark.parametrize("route", ["/sync", "/async"])
def test_middleware_sets_header_and_context(logs: io.StringIO, route: str) -> None:
    client = TestClient(_probe_app())
    r = client.get(route)
    rid = r.headers["X-Request-ID"]
    assert len(rid) == 32 and r.json() == {"rid": rid}
    access = [x for x in _lines(logs) if x["logger"] == "scruple.api"]
    assert access[-1]["request_id"] == rid and access[-1]["status"] == 200
    assert access[-1]["path"] == route and access[-1]["method"] == "GET"


def test_well_formed_incoming_id_is_kept(logs: io.StringIO) -> None:
    client = TestClient(_probe_app())
    r = client.get("/sync", headers={"X-Request-ID": "upstream-42.a:b"})
    assert r.headers["X-Request-ID"] == "upstream-42.a:b"
    assert any(x.get("request_id") == "upstream-42.a:b" for x in _lines(logs))


@pytest.mark.parametrize(
    "bad", [b"", b"has space", b"x" * 129, b"inject\xe2\x80\xa8line", b'a"b', b"\xff\xfe"]
)
def test_malformed_incoming_id_is_replaced(bad: bytes) -> None:
    client = TestClient(_probe_app())
    r = client.get("/async", headers={"X-Request-ID": bad})
    got = r.headers["X-Request-ID"]
    assert got.encode() != bad and len(got) == 32


def test_ids_differ_per_request() -> None:
    client = TestClient(_probe_app())
    assert (
        client.get("/async").headers["X-Request-ID"] != client.get("/async").headers["X-Request-ID"]
    )


def test_failed_request_is_logged_with_status_500(logs: io.StringIO) -> None:
    client = TestClient(_probe_app(), raise_server_exceptions=False)
    assert client.get("/boom").status_code == 500
    access = [x for x in _lines(logs) if x["logger"] == "scruple.api"]
    assert access[-1]["status"] == 500


def test_real_app_returns_request_ids() -> None:
    client = TestClient(create_app(AgentGate(SQLiteStorage(":memory:"))))
    for r in (client.get("/health"), client.get("/decisions/dec-99999")):
        assert len(r.headers["X-Request-ID"]) == 32


def test_reconfiguring_replaces_only_our_handler() -> None:
    root = logging.getLogger()
    other = logging.NullHandler()
    root.addHandler(other)
    level = root.level
    try:
        h1 = configure_logging("INFO", "text", io.StringIO())
        h2 = configure_logging("INFO", "json", io.StringIO())
        assert h1 not in root.handlers and h2 in root.handlers and other in root.handlers
        assert isinstance(h2.formatter, JsonFormatter)
    finally:
        root.removeHandler(other)
        root.removeHandler(h2)
        root.setLevel(level)


def test_text_format_includes_request_id() -> None:
    buf = io.StringIO()
    root = logging.getLogger()
    level = root.level
    h = configure_logging("INFO", "text", buf)
    try:
        with request_context("req-9"):
            logging.getLogger("scruple.test").info("plain")
    finally:
        root.removeHandler(h)
        root.setLevel(level)
    assert buf.getvalue().rstrip().endswith("plain request_id=req-9")
