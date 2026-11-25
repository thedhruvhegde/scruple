"""Scruple Guard: pre-flight authorisation of agent-initiated payments against the
user's signed mandate, with hash-chained evidence (docs/guard/GUARD.md)."""

from __future__ import annotations

from scruple.guard.evidence import EvidenceChain, GuardEvidence, record_to_audit, verify_chain
from scruple.guard.mandate import (
    CartLine,
    IntentItem,
    Mandate,
    MerchantRecord,
    PaymentRequest,
    RecurrenceTerms,
    sign_mandate,
    signature_ok,
)
from scruple.guard.rules import (
    RULE_IDS,
    Approval,
    AuthorisedPayment,
    Decision,
    GuardState,
    RuleResult,
    approval_prompt,
    authorize,
    finalize,
    replay_spend,
)

__all__ = [
    "RULE_IDS",
    "Approval",
    "AuthorisedPayment",
    "CartLine",
    "Decision",
    "EvidenceChain",
    "GuardEvidence",
    "GuardState",
    "IntentItem",
    "Mandate",
    "MerchantRecord",
    "PaymentRequest",
    "RecurrenceTerms",
    "RuleResult",
    "approval_prompt",
    "authorize",
    "finalize",
    "record_to_audit",
    "replay_spend",
    "sign_mandate",
    "signature_ok",
    "verify_chain",
]
