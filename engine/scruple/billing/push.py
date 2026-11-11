"""Map usage aggregates to provider meter events, idempotently, and record each push.

Per tenant, per configured meter, per UTC day: the day's total (`scruple.usage`) minus
what the ledger says was already pushed for that day is sent as one meter event, and
the push is appended to the ledger (`billing_pushes`) once the provider accepts it.
So:

- running `push` twice sends nothing the second time;
- if more usage lands on a day after it was pushed (a late sync), the next run sends
  only the difference;
- a retry after a failure sends the same identifier (it is derived from tenant,
  meter, day and the new running total), so the provider deduplicates it, and
  `Idempotency-Key` makes the HTTP request itself safe to repeat.

Days are pushed while they are within `lookback_days` (default 30; Stripe accepts
meter event timestamps up to 35 days old). An older day with unpushed usage is
reported as stale, never silently dropped. Each event's timestamp is the end of its
day (or now, for today), so the provider bills it in the period it happened.

`plan` computes what would be sent without sending anything (dry run).
"""

from __future__ import annotations

import hashlib
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from scruple.billing.stripe import BillingError, StripeBillingAPIError, StripeBillingClient
from scruple.storage.sql import TENANT_PATTERN
from scruple.usage.meter import Meter, UsageStore, next_period

if TYPE_CHECKING:
    from scruple.clock import Clock
    from scruple.storage.sql import Connection

PROVIDER = "stripe"
MAX_LOOKBACK_DAYS = 34  # Stripe: timestamps within the past 35 calendar days


class BillingConfig(BaseModel):
    """`[billing]` in a TOML file. No secrets: the API key comes from the environment."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: Literal["stripe"] = "stripe"
    mode: Literal["test", "live"] = "test"
    customers: dict[str, str] = Field(description="tenant id -> Stripe customer id (cus_...)")
    meters: dict[Meter, str] = Field(
        min_length=1, description="scruple meter -> Stripe meter event_name"
    )
    customer_key: str = Field(default="stripe_customer_id", pattern=r"^[a-z0-9_]{1,40}$")
    value_key: str = Field(default="value", pattern=r"^[a-z0-9_]{1,40}$")
    lookback_days: int = Field(default=30, ge=1, le=MAX_LOOKBACK_DAYS)

    def model_post_init(self, _ctx: object) -> None:
        import re

        for tenant, cus in self.customers.items():
            if not re.match(TENANT_PATTERN, tenant):
                raise ValueError(f"bad tenant id {tenant!r}")
            if not re.match(r"^cus_[A-Za-z0-9]{1,250}$", cus):
                raise ValueError(f"tenant {tenant}: customer must be cus_...")
        for name in self.meters.values():
            if not re.match(r"^[A-Za-z0-9_.:-]{1,100}$", name):
                raise ValueError(f"bad Stripe event name {name!r}")

    @classmethod
    def load(cls, path: str | Path) -> BillingConfig:
        try:
            data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise BillingError(f"{path} is not valid TOML: {exc}") from exc
        section = data.get("billing")
        if not isinstance(section, dict) or set(data) != {"billing"}:
            raise BillingError(f"{path}: expected one [billing] table")
        if any(k in section for k in ("api_key", "secret", "key")):
            raise BillingError(f"{path}: the API key comes from the environment, not a file")
        try:
            return cls.model_validate(section)
        except (ValidationError, ValueError) as exc:
            raise BillingError(f"{path}: {exc}") from exc


@dataclass(frozen=True)
class PlannedPush:
    tenant: str
    meter: Meter
    day: date
    delta: int
    cumulative: int
    event_name: str
    customer: str
    identifier: str
    timestamp: int


@dataclass
class PushReport:
    planned: list[PlannedPush] = field(default_factory=list)
    sent: list[PlannedPush] = field(default_factory=list)
    failed: list[tuple[PlannedPush, str]] = field(default_factory=list)
    stale: list[tuple[str, Meter, date, int]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed


def identifier(tenant: str, meter: Meter, day: date, cumulative: int) -> str:
    raw = f"scruple|{tenant}|{meter.value}|{day.isoformat()}|{cumulative}".encode()
    return "scr-" + hashlib.sha256(raw).hexdigest()[:40]


class BillingLedger:
    """Append-only pushes over a backend `Connection` (the gate database)."""

    def __init__(self, conn: Connection) -> None:
        self._c = conn

    def pushed(self, tenant: str, meter: Meter, day: date, provider: str = PROVIDER) -> int:
        rows = self._c.query(
            "SELECT MAX(cumulative) AS c FROM billing_pushes WHERE tenant_id=? AND provider=? "
            "AND meter=? AND period_start=?",
            (tenant, provider, meter.value, day.isoformat()),
        )
        v = rows[0]["c"] if rows else None
        return int(v) if v is not None else 0

    def record(
        self, p: PlannedPush, livemode: bool, at: datetime, provider: str = PROVIDER
    ) -> None:
        with self._c.transaction():
            self._c.execute(
                "INSERT INTO billing_pushes (tenant_id, provider, identifier, meter, "
                "period_start, delta, cumulative, event_name, customer, livemode, pushed_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT (tenant_id, provider, identifier) DO NOTHING",
                (
                    p.tenant,
                    provider,
                    p.identifier,
                    p.meter.value,
                    p.day.isoformat(),
                    p.delta,
                    p.cumulative,
                    p.event_name,
                    p.customer,
                    int(livemode),
                    at.astimezone(UTC).isoformat(),
                ),
            )

    def rows(self, tenant: str) -> list[dict[str, object]]:
        return [
            dict(r)
            for r in self._c.query(
                "SELECT identifier, meter, period_start, delta, cumulative, event_name, customer, "
                "livemode, pushed_at FROM billing_pushes WHERE tenant_id=? ORDER BY id",
                (tenant,),
            )
        ]


def plan(
    config: BillingConfig,
    usage: UsageStore,
    ledger: BillingLedger,
    now: datetime,
    tenants: Iterable[str] | None = None,
) -> PushReport:
    """What `push` would send now, per tenant (default: every configured tenant)."""
    report = PushReport()
    today = now.astimezone(UTC).date()
    oldest = today - timedelta(days=config.lookback_days)
    now_ts = int(now.timestamp())
    for tenant in sorted(tenants if tenants is not None else config.customers):
        customer = config.customers.get(tenant)
        if customer is None:
            raise BillingError(f"tenant {tenant} has no billing customer configured")
        for agg in usage.aggregate(tenant, "day"):
            event_name = config.meters.get(agg.meter)
            if event_name is None:
                continue
            delta = agg.quantity - ledger.pushed(tenant, agg.meter, agg.period_start)
            if delta <= 0:
                continue
            if agg.period_start < oldest:
                report.stale.append((tenant, agg.meter, agg.period_start, delta))
                continue
            end = next_period(agg.period_start, "day")
            end_ts = int(datetime(end.year, end.month, end.day, tzinfo=UTC).timestamp()) - 1
            report.planned.append(
                PlannedPush(
                    tenant=tenant,
                    meter=agg.meter,
                    day=agg.period_start,
                    delta=delta,
                    cumulative=agg.quantity,
                    event_name=event_name,
                    customer=customer,
                    identifier=identifier(tenant, agg.meter, agg.period_start, agg.quantity),
                    timestamp=min(end_ts, now_ts),
                )
            )
    return report


def push(
    config: BillingConfig,
    client: StripeBillingClient,
    usage: UsageStore,
    ledger: BillingLedger,
    clock: Clock,
    tenants: Iterable[str] | None = None,
) -> PushReport:
    """Send the plan. A failure is reported and leaves no ledger row, so the next run
    retries it with the same identifier; the other pushes still go."""
    if config.mode == "test" and client.livemode:
        raise BillingError("config mode is test but the key is a live key")
    if config.mode == "live" and not client.livemode:
        raise BillingError("config mode is live but the key is a test key")
    report = plan(config, usage, ledger, clock.now(), tenants)
    for p in report.planned:
        try:
            event = client.create_meter_event(
                event_name=p.event_name,
                customer=p.customer,
                value=p.delta,
                identifier=p.identifier,
                timestamp=p.timestamp,
                customer_key=config.customer_key,
                value_key=config.value_key,
            )
        except StripeBillingAPIError as exc:
            report.failed.append((p, str(exc)))
            continue
        except BillingError as exc:
            report.failed.append((p, str(exc)))
            continue
        ledger.record(p, event.livemode, clock.now())
        report.sent.append(p)
    return report


def ledger_for(storage: object) -> BillingLedger | None:
    from scruple.storage.sql import SQLStorage

    return BillingLedger(storage.connection) if isinstance(storage, SQLStorage) else None


__all__ = [
    "PROVIDER",
    "BillingConfig",
    "BillingLedger",
    "PlannedPush",
    "PushReport",
    "identifier",
    "ledger_for",
    "plan",
    "push",
]
