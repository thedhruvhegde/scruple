"""Agentic-commerce simulator: a shopping agent with a wallet, merchants that may
misbehave, Scruple Guard in front of every payment, and a double-entry ledger."""

from __future__ import annotations

from scruple.sim.commerce.engine import CommerceEngine, CommerceOutcome
from scruple.sim.commerce.generator import generate_commerce_case
from scruple.sim.commerce.models import CommerceCase, RefundReason, Scenario

__all__ = [
    "CommerceCase",
    "CommerceEngine",
    "CommerceOutcome",
    "RefundReason",
    "Scenario",
    "generate_commerce_case",
]
