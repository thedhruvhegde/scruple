"""Pull disputes from the Stripe API into an export file, page by page, resumably.

`scruple ingest stripe-pull --since D --until D --out export.jsonl [--store cases.db]`

- Pages `GET /v1/disputes` with `created[gte]=since`, `created[lt]=until` and
  `expand[]=data.charge` (the charge carries the payment facts rules read), 100 per
  page, newest first, following `has_more` / `starting_after` (vendored spec).
- The key is read from the environment (`STRIPE_API_KEY`) only. Test-mode keys only;
  a live key, or a live-mode object, is refused unless `allow_live`.
- Free text and personal data are dropped before anything touches disk: every
  `evidence` value is replaced by a presence marker (the mapper reads only which
  evidence fields are present, docs/BACKTEST.md), and the expanded charge is cut to
  the fields the `Charge` wire model declares, with the shipping address and tracking
  number reduced to presence markers too (billing details, emails, receipt numbers
  and the like are never written). The rest of the dispute is kept as Stripe returned
  it, so `scruple ingest stripe` (redaction, card tokenisation, field encryption)
  reads the file like any export.
- Resumable: after each page is appended (and flushed to disk) the cursor file
  records the window, the last dispute id, the export's byte length and its sha256.
  A rerun with the same window continues after that id; bytes written after the
  last cursor (a crash between append and cursor write) are cut off first, so no
  dispute is written twice. A finished pull reruns as a no-op. A cursor for another
  window, or an export that no longer matches its cursor, is refused (`restart`).
- Rate limits: the client retries 429, 5xx and transport errors with exponential
  backoff (`StripeClient`, `max_retries=5`).
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal, get_args

import httpx
from pydantic import BaseModel, ConfigDict, Field

from scruple.ingest.errors import IngestError, LiveModeRefusedError
from scruple.ingest.stripe.client import DEFAULT_BASE_URL, StripeClient
from scruple.ingest.stripe.mapping import _present
from scruple.ingest.stripe.wire import Charge

API_KEY_ENV = "STRIPE_API_KEY"
EVIDENCE_MARKER = "[present; content not kept]"
EXPAND = ("data.charge",)
# Tests swap in an httpx.MockTransport; nothing in the suite reaches the network.
DEFAULT_TRANSPORT: httpx.BaseTransport | None = None

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class PullCursor(BaseModel):
    model_config = _FROZEN

    version: Literal[1] = 1
    since: int
    until: int
    expand: tuple[str, ...]
    starting_after: str | None = None
    pages: int = Field(default=0, ge=0)
    disputes: int = Field(default=0, ge=0)
    export_bytes: int = Field(default=0, ge=0)
    export_sha256: str = hashlib.sha256(b"").hexdigest()
    complete: bool = False


class PullReport(BaseModel):
    model_config = _FROZEN

    since: int
    until: int
    out: str
    pages: int
    disputes: int
    fetched_this_run: int
    pages_this_run: int
    retries: int
    resumed: bool
    complete: bool
    export_sha256: str

    def summary(self) -> str:
        state = "complete" if self.complete else "incomplete (rerun to resume)"
        how = "resumed" if self.resumed else "new"
        return (
            f"stripe pull {how}: {self.disputes} disputes in {self.pages} pages "
            f"({self.fetched_this_run} fetched now, {self.retries} retries), {state}; "
            f"export {self.out} sha256 {self.export_sha256[:12]}"
        )


def parse_instant(text: str) -> int:
    """Unix seconds from an integer, an ISO date (midnight UTC) or an ISO datetime
    (UTC if no offset is given)."""
    t = text.strip()
    if t.isdigit():
        return int(t)
    try:
        if len(t) == 10:
            d = date.fromisoformat(t)
            return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp())
        dt = datetime.fromisoformat(t)
    except ValueError:
        raise IngestError(f"not a date, datetime or unix time: {text!r}") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp())


def _models(annotation: Any) -> list[type[BaseModel]]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    return [m for a in get_args(annotation) for m in _models(a)]


def prune(obj: Any, model: type[BaseModel]) -> Any:
    """`obj` with only the keys `model` declares, recursively (lists and objects)."""
    if isinstance(obj, list):
        return [prune(x, model) for x in obj]
    if not isinstance(obj, Mapping):
        return obj
    out: dict[str, Any] = {}
    for name, field in model.model_fields.items():
        if name not in obj:
            continue
        sub = _models(field.annotation)
        out[name] = prune(obj[name], sub[0]) if sub else obj[name]
    return out


def _mark(v: Any) -> str | None:
    return EVIDENCE_MARKER if _present(v) else None


def strip_free_text(dispute: Mapping[str, Any]) -> dict[str, Any]:
    """The dispute with each evidence value replaced by a presence marker and an
    expanded charge cut to the fields scruple reads (address and tracking number as
    presence markers)."""
    out = dict(dispute)
    ev = dispute.get("evidence")
    if isinstance(ev, Mapping):
        out["evidence"] = {k: _mark(v) for k, v in ev.items()}
    charge = dispute.get("charge")
    if isinstance(charge, Mapping):
        c = prune(charge, Charge)
        ship = c.get("shipping")
        if isinstance(ship, dict):
            if isinstance(ship.get("address"), dict):
                ship["address"] = {k: _mark(v) for k, v in ship["address"].items()}
            if "tracking_number" in ship:
                ship["tracking_number"] = _mark(ship["tracking_number"])
        out["charge"] = c
    return out


def client_from_env(
    env: Mapping[str, str] | None = None,
    *,
    allow_live: bool = False,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] | None = None,
    base_url: str = DEFAULT_BASE_URL,
) -> StripeClient:
    key = (os.environ if env is None else env).get(API_KEY_ENV, "").strip()
    if not key:
        raise IngestError(f"set {API_KEY_ENV} to a Stripe test-mode key (sk_test_...)")
    kw: dict[str, Any] = {} if sleep is None else {"sleep": sleep}
    return StripeClient(
        key,
        transport=transport if transport is not None else DEFAULT_TRANSPORT,
        allow_live=allow_live,
        max_retries=5,
        base_url=base_url,
        **kw,
    )


def _sha(path: Path, n: int) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        h.update(f.read(n))
    return h.hexdigest()


def _write_cursor(path: Path, cur: PullCursor) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(cur.model_dump_json(indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _start(out: Path, cursor_path: Path, fresh: PullCursor, restart: bool) -> PullCursor:
    if restart or not cursor_path.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"")
        _write_cursor(cursor_path, fresh)
        return fresh
    cur = PullCursor.model_validate_json(cursor_path.read_text(encoding="utf-8"))
    if (cur.since, cur.until, cur.expand) != (fresh.since, fresh.until, fresh.expand):
        raise IngestError("the cursor is for another window; use --restart to start over")
    if not out.exists() or out.stat().st_size < cur.export_bytes:
        raise IngestError("the export is shorter than its cursor; use --restart")
    if _sha(out, cur.export_bytes) != cur.export_sha256:
        raise IngestError("the export does not match its cursor; use --restart")
    with out.open("r+b") as f:  # drop a page appended after the last cursor write
        f.truncate(cur.export_bytes)
    return cur


def pull_disputes(
    client: StripeClient,
    since: int,
    until: int,
    out: Path,
    cursor_path: Path | None = None,
    *,
    page_size: int = 100,
    max_pages: int | None = None,
    allow_live: bool = False,
    restart: bool = False,
) -> PullReport:
    """Append every dispute created in [since, until) to `out` (JSON Lines).

    `max_pages` bounds this run only (the next run resumes); None means no bound."""
    if until <= since:
        raise IngestError("--until must be after --since")
    cursor_path = cursor_path or out.with_name(out.name + ".cursor.json")
    fresh = PullCursor(since=since, until=until, expand=EXPAND)
    resumed = cursor_path.exists() and not restart
    cur = _start(out, cursor_path, fresh, restart)
    hasher = hashlib.sha256(out.read_bytes()[: cur.export_bytes])
    retries0 = client.retries_used
    fetched = pages = 0
    while not cur.complete and (max_pages is None or pages < max_pages):
        page, raw = client.list_disputes_raw(
            limit=page_size,
            starting_after=cur.starting_after,
            created_gte=since,
            created_lt=until,
            expand=EXPAND,
        )
        if not allow_live and any(d.livemode for d in page.data):
            raise LiveModeRefusedError("live-mode disputes are refused; use a test-mode key")
        lines = b"".join(
            json.dumps(strip_free_text(d), sort_keys=True, separators=(",", ":")).encode() + b"\n"
            for d in raw
        )
        with out.open("ab") as f:
            f.write(lines)
            f.flush()
            os.fsync(f.fileno())
        pages += 1
        fetched += len(raw)
        hasher.update(lines)
        size = cur.export_bytes + len(lines)
        cur = cur.model_copy(
            update={
                "starting_after": page.data[-1].id if page.data else cur.starting_after,
                "pages": cur.pages + 1,
                "disputes": cur.disputes + len(raw),
                "export_bytes": size,
                "export_sha256": hasher.hexdigest(),
                "complete": not page.has_more or not page.data,
            }
        )
        _write_cursor(cursor_path, cur)
    return PullReport(
        since=since,
        until=until,
        out=str(out),
        pages=cur.pages,
        disputes=cur.disputes,
        fetched_this_run=fetched,
        pages_this_run=pages,
        retries=client.retries_used - retries0,
        resumed=resumed,
        complete=cur.complete,
        export_sha256=cur.export_sha256,
    )


__all__ = [
    "API_KEY_ENV",
    "EVIDENCE_MARKER",
    "EXPAND",
    "PullCursor",
    "PullReport",
    "client_from_env",
    "parse_instant",
    "prune",
    "pull_disputes",
    "strip_free_text",
]
