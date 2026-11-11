"""Per-tenant data folders: what the dashboard reads that lives in files, not the gate.

`scruple console --data DIR` names a root; each tenant's files sit in `DIR/<tenant>/`
and a caller only ever reads its own tenant's folder (the tenant id comes from
authentication and matches `TENANT_PATTERN`, so it cannot climb out of the root):

    DIR/<tenant>/tenant.json            {"synthetic": true, "label": "..."} (optional)
    DIR/<tenant>/cases.db               the tenant's case store (`scruple ingest --store`)
    DIR/<tenant>/backtests/**.json      backtest reports (`scruple backtest run`)
    DIR/<tenant>/pilot/manifest.json    a pilot run (`scruple pilot run --out`)
    DIR/<tenant>/evals/<agent>/<run>/   eval runs (`scruple eval run --out`); the folder
                                        name says which agent the run's model serves
    DIR/<tenant>/anchors/*.json         audit anchors (`scruple audit anchor --out`)

A case store is one tenant's file, as the ingest routes require
(`SCRUPLE_CASE_STORES`), so one folder per tenant keeps every source separate by
construction. Every reader is tolerant: a missing or unreadable file reads as absent,
never as an error page.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from scruple.audit.anchor import GitAnchor, TimestampAnchor
from scruple.console.sources import ReportEntry, report_entries
from scruple.eval.records import EvalRecord, Manifest, load_manifest, load_records
from scruple.storage.sql import TENANT_PATTERN

_TENANT = re.compile(TENANT_PATTERN)
_AGENT = re.compile(r"^[A-Za-z0-9._@:-]{1,128}$")
MAX_EVAL_RUNS = 50
MAX_ANCHORS = 200


class TenantMarker(BaseModel):
    """`tenant.json`: whether the folder holds synthetic data (the demo seed writes it)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    synthetic: bool = False
    label: str = Field(default="", max_length=200)


@dataclass(frozen=True)
class EvalRun:
    agent: str
    name: str
    manifest: Manifest
    records: tuple[EvalRecord, ...]


Anchor = TimestampAnchor | GitAnchor


@dataclass(frozen=True)
class TenantFiles:
    tenant: str
    root: Path

    def marker(self) -> TenantMarker | None:
        p = self.root / "tenant.json"
        try:
            return TenantMarker.model_validate_json(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def case_store(self) -> Path | None:
        p = self.root / "cases.db"
        return p if p.is_file() else None

    def reports(self) -> dict[str, ReportEntry]:
        out = report_entries(self.root / "backtests")
        pilot = self.root / "pilot" / "03-backtest" / "report.json"
        if pilot.is_file() and "pilot" not in out:
            out["pilot"] = ReportEntry("pilot", pilot)
        return out

    def pilot_manifest(self) -> dict[str, Any] | None:
        p = self.root / "pilot" / "manifest.json"
        try:
            body = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return body if isinstance(body, dict) else None

    def eval_runs(self) -> list[EvalRun]:
        base = self.root / "evals"
        if not base.is_dir():
            return []
        out: list[EvalRun] = []
        for agent_dir in sorted(p for p in base.iterdir() if p.is_dir()):
            if not _AGENT.match(agent_dir.name):
                continue
            for run in sorted(p for p in agent_dir.iterdir() if p.is_dir()):
                try:
                    m = load_manifest(run)
                    recs = load_records(run) if m is not None else []
                except (OSError, ValueError, ValidationError):
                    continue
                if m is not None:
                    out.append(EvalRun(agent_dir.name, run.name, m, tuple(recs)))
                if len(out) >= MAX_EVAL_RUNS:
                    return out
        return out

    def anchors(self) -> list[Anchor]:
        base = self.root / "anchors"
        if not base.is_dir():
            return []
        out: list[Anchor] = []
        for p in sorted(base.glob("*.json"))[:MAX_ANCHORS]:
            try:
                body = json.loads(p.read_text(encoding="utf-8"))
                kind = body.get("kind") if isinstance(body, dict) else None
                a: Anchor = (
                    GitAnchor.model_validate(body)
                    if kind == "git"
                    else TimestampAnchor.model_validate(body)
                )
            except (OSError, ValueError, ValidationError):
                continue
            if a.statement.tenant == self.tenant:  # another tenant's anchor is not ours
                out.append(a)
        return out


def tenant_files(root: Path | None, tenant: str) -> TenantFiles | None:
    """The tenant's folder under `root`, or None (no root, bad id, or no folder)."""
    if root is None or not _TENANT.match(tenant):
        return None
    base = root.resolve()
    p = (base / tenant).resolve()
    if p.parent != base or not p.is_dir():
        return None
    return TenantFiles(tenant, p)


__all__ = ["Anchor", "EvalRun", "TenantFiles", "TenantMarker", "tenant_files"]
