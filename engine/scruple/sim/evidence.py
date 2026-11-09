"""Evidence kinds a merchant can submit in a dispute.

The kinds are scruple's own vocabulary, distilled from the key-evidence text in the
public reason code dataset (docs/SOURCES.md S1). The mapping from that text to these
kinds is ours (docs/ASSUMPTIONS.md A-15).
"""

from __future__ import annotations

from enum import StrEnum


class EvidenceKind(StrEnum):
    RECEIPT = "receipt"
    AUTHORIZATION_RECORD = "authorization_record"
    AVS_CVV_MATCH = "avs_cvv_match"
    DEVICE_IP_MATCH = "device_ip_match"
    ACCOUNT_ACTIVITY = "account_activity"
    PRIOR_UNDISPUTED_HISTORY = "prior_undisputed_history"
    EMV_CHIP_READ = "emv_chip_read"
    TERMINAL_LOG = "terminal_log"
    DELIVERY_PROOF = "delivery_proof"
    PRODUCT_DESCRIPTION = "product_description"
    TERMS_ACCEPTANCE = "terms_acceptance"
    CANCELLATION_RECORD = "cancellation_record"
    COMMUNICATION_LOG = "communication_log"
    REFUND_RECORD = "refund_record"
    SETTLEMENT_RECORD = "settlement_record"
    PRESENTMENT_RECORD = "presentment_record"
    AUTHENTICITY_PROOF = "authenticity_proof"
