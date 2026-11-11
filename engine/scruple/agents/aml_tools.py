"""Tools for `aml` episodes: triage one transaction-monitoring alert.

Scopes follow `scruple.agents.dispute_tools.DisputeScope`: `read` tools never change
state, `write` (`request_info`) asks the relationship manager and lets two simulated
days pass, `action` tools decide the alert (one decision ends it), `submit` closes
the episode. Every call, refused ones included, is recorded in the hash-chained trace.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from scruple.agents.dispute_tools import (
    DisputeScope,
    DisputeToolSpec,
    NoArgs,
    RequestInfoArgs,
    _Args,
)
from scruple.sim.aml.indicators import Indicator
from scruple.verifiers.aml_record import AmlReport


class AccountIdArgs(_Args):
    account_id: str = Field(min_length=1, max_length=32, description="An account id.")


class TxnIdsArgs(_Args):
    txn_ids: tuple[str, ...] = Field(
        min_length=1, max_length=200, description="Transfer ids, e.g. from get_alert."
    )


class CloseAlertArgs(_Args):
    reason: str = Field(default="", max_length=1_000, description="Why, in a sentence.")


class EscalateArgs(_Args):
    indicators: tuple[Indicator, ...] = Field(
        default=(), max_length=6, description="Red flags that hold on the cited transfers."
    )
    txn_ids: tuple[str, ...] = Field(default=(), max_length=200, description="Transfers cited.")
    reason: str = Field(default="", max_length=1_000)


class FileSarArgs(_Args):
    indicators: tuple[Indicator, ...] = Field(
        min_length=1, max_length=6, description="Red flags that hold on the cited transfers."
    )
    txn_ids: tuple[str, ...] = Field(
        min_length=1, max_length=200, description="The suspicious transfers."
    )
    amount_minor: int = Field(
        ge=0, description="Total amount involved: the sum of the cited transfers, USD cents."
    )
    narrative: str = Field(min_length=1, max_length=4_000, description="What happened and why.")


S = DisputeScope
AML_TOOLS: tuple[DisputeToolSpec, ...] = (
    DisputeToolSpec(
        "get_alert",
        "The alert: the rule that fired, the subject account id, the alert window and the "
        "transfers it links; today's date and the actions allowed now.",
        S.READ,
        NoArgs,
    ),
    DisputeToolSpec(
        "get_account",
        "One account: KYC profile (occupation, declared monthly inflow, cash-intensive, "
        "opening date) for our customers; holder, bank and country for other banks'.",
        S.READ,
        AccountIdArgs,
    ),
    DisputeToolSpec(
        "list_transactions",
        "Every transfer of an account in the 60-day lookback, oldest first.",
        S.READ,
        AccountIdArgs,
    ),
    DisputeToolSpec("get_transactions", "Read transfers by id.", S.READ, TxnIdsArgs),
    DisputeToolSpec(
        "request_info",
        "Ask the relationship manager to get an explanation from the customer. The reply "
        "comes back after two simulated days; at most two requests.",
        S.WRITE,
        RequestInfoArgs,
    ),
    DisputeToolSpec(
        "close_alert", "Close the alert as a false positive.", S.ACTION, CloseAlertArgs
    ),
    DisputeToolSpec(
        "escalate",
        "Hand the alert to a second-level investigator, citing indicators and transfers.",
        S.ACTION,
        EscalateArgs,
    ),
    DisputeToolSpec(
        "file_sar",
        "File a suspicious activity report (synthetic): indicators, transfers, the amount "
        "involved and a narrative.",
        S.ACTION,
        FileSarArgs,
    ),
    DisputeToolSpec(
        "submit_decision",
        "Finish: explain what you did, optionally with structured claims. Returns the scorecard.",
        S.SUBMIT,
        AmlReport,
    ),
)
AML_TOOL_BY_NAME: dict[str, DisputeToolSpec] = {t.name: t for t in AML_TOOLS}
AML_TOOL_NAMES: tuple[str, ...] = tuple(AML_TOOL_BY_NAME)
AML_ACTIONS = ("close_alert", "escalate", "file_sar")


def aml_anthropic_tools() -> list[dict[str, Any]]:
    return [
        {"name": t.name, "description": t.description, "input_schema": t.json_schema()}
        for t in AML_TOOLS
    ]


__all__ = [
    "AML_ACTIONS",
    "AML_TOOLS",
    "AML_TOOL_BY_NAME",
    "AML_TOOL_NAMES",
    "AccountIdArgs",
    "CloseAlertArgs",
    "EscalateArgs",
    "FileSarArgs",
    "TxnIdsArgs",
    "aml_anthropic_tools",
]
