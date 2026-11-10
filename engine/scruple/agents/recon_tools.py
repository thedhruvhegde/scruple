"""Tools for `recon` episodes: reconcile one bank statement against the cash book.

Scopes as for disputes: `read` never changes state, `action` asks the engine to
record a match, an adjusting entry, a timing item or an escalation (refused when
malformed), `submit` closes the episode. There is no clock: a reconciliation is
worked on a closed period.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from scruple.agents.dispute_tools import DisputeScope, DisputeToolSpec, NoArgs, _Args
from scruple.verifiers.recon_record import ReconReport

_Ids = tuple[str, ...]


class MatchArgs(_Args):
    statement_line_ids: _Ids = Field(min_length=1, max_length=20, description="S-ids.")
    ledger_line_ids: _Ids = Field(min_length=1, max_length=20, description="B-ids.")


class UnmatchArgs(_Args):
    match_id: str = Field(min_length=1, max_length=16, description="An M-id from a match.")


class AdjustArgs(_Args):
    kind: Literal["fee", "fx", "missing", "partial", "duplicate_reversal"] = Field(
        description="fee: bank charges; fx: rate difference; missing: book a statement "
        "line the cash book lacks; partial: reinstate an unpaid remainder as receivable; "
        "duplicate_reversal: reverse a book line booked twice."
    )
    amount_minor: int = Field(description="Signed effect on book cash, minor units.")
    statement_line_ids: _Ids = Field(
        default=(), max_length=20, description="Statement lines that justify the entry."
    )
    ledger_line_ids: _Ids = Field(
        default=(), max_length=20, description="Book lines the entry corrects."
    )
    invoice_id: str | None = Field(
        default=None, max_length=32, description="Open invoice the cash belongs to."
    )
    note: str = Field(default="", max_length=500)


class TimingArgs(_Args):
    ledger_line_ids: _Ids = Field(min_length=1, max_length=20)
    note: str = Field(default="", max_length=500)


class EscalateArgs(_Args):
    statement_line_ids: _Ids = Field(default=(), max_length=20)
    ledger_line_ids: _Ids = Field(default=(), max_length=20)
    reason: str = Field(default="", max_length=1_000)


S = DisputeScope
RECON_TOOLS: tuple[DisputeToolSpec, ...] = (
    DisputeToolSpec(
        "get_task", "The account, period, book opening balance and progress.", S.READ, NoArgs
    ),
    DisputeToolSpec(
        "get_statement",
        "The bank statement as parsed from the bank's file: lines (S-ids) and balances.",
        S.READ,
        NoArgs,
    ),
    DisputeToolSpec(
        "get_statement_file", "The bank's statement file as delivered.", S.READ, NoArgs
    ),
    DisputeToolSpec("get_ledger", "The cash book for the period (B-ids).", S.READ, NoArgs),
    DisputeToolSpec("get_invoices", "Open customer invoices.", S.READ, NoArgs),
    DisputeToolSpec(
        "get_ledger_view", "The journal with adjusting entries and balances.", S.READ, NoArgs
    ),
    DisputeToolSpec(
        "match", "Match statement lines to book lines (one or many each).", S.ACTION, MatchArgs
    ),
    DisputeToolSpec("unmatch", "Undo a match.", S.ACTION, UnmatchArgs),
    DisputeToolSpec(
        "adjust",
        "Post an adjusting entry to book cash, citing the statement lines that justify it.",
        S.ACTION,
        AdjustArgs,
    ),
    DisputeToolSpec(
        "mark_timing", "Leave book lines outstanding for the next statement.", S.ACTION, TimingArgs
    ),
    DisputeToolSpec(
        "escalate", "Hand lines to a human (bank error, unknown payer).", S.ACTION, EscalateArgs
    ),
    DisputeToolSpec(
        "submit_decision",
        "Finish: explain the reconciliation, optionally with structured claims. Returns "
        "the scorecard.",
        S.SUBMIT,
        ReconReport,
    ),
)
RECON_TOOL_BY_NAME: dict[str, DisputeToolSpec] = {t.name: t for t in RECON_TOOLS}
RECON_TOOL_NAMES: tuple[str, ...] = tuple(RECON_TOOL_BY_NAME)


def recon_anthropic_tools() -> list[dict[str, Any]]:
    return [
        {"name": t.name, "description": t.description, "input_schema": t.json_schema()}
        for t in RECON_TOOLS
    ]


__all__ = [
    "RECON_TOOLS",
    "RECON_TOOL_BY_NAME",
    "RECON_TOOL_NAMES",
    "AdjustArgs",
    "EscalateArgs",
    "MatchArgs",
    "TimingArgs",
    "UnmatchArgs",
    "recon_anthropic_tools",
]
