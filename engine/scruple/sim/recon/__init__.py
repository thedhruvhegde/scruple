"""Reconciliation environment (roadmap 045-047): a bank statement against the cash book.

All data is synthetic. The statement is written as camt.053 or BAI2 and read back
through `scruple.ingest.bank`. Breaks and assumptions: docs/WORKFLOWS.md.
"""

from scruple.sim.recon.engine import (
    Adjust,
    Escalate,
    MarkTiming,
    Match,
    ReconAction,
    ReconEngine,
    ReconError,
    ReconOutcome,
    ReconResult,
    Unmatch,
)
from scruple.sim.recon.generator import generate_recon_case
from scruple.sim.recon.models import BREAK_KINDS, Break, BreakKind, ReconCase
from scruple.sim.recon.statement import read_statement, write_bai2, write_camt053

__all__ = [
    "BREAK_KINDS",
    "Adjust",
    "Break",
    "BreakKind",
    "Escalate",
    "MarkTiming",
    "Match",
    "ReconAction",
    "ReconCase",
    "ReconEngine",
    "ReconError",
    "ReconOutcome",
    "ReconResult",
    "Unmatch",
    "generate_recon_case",
    "read_statement",
    "write_bai2",
    "write_camt053",
]
