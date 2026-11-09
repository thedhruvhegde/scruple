"""Deterministic label mappings from CFPB fields to task labels.

Every mapping is a plain table so it can be read, tested and cited. Nothing here is
learned or inferred from narratives.

Outcome (company response to consumer -> relief):
- relief (1): "Closed with monetary relief", "Closed with non-monetary relief",
  "Closed with relief" (the last value appears in older CFPB data).
- no relief (0): "Closed with explanation".
- dropped: everything else ("Closed", "Closed without relief", "Untimely response",
  "In progress", empty). "Closed" carries no explanation-or-relief signal, and the
  others are not final outcomes.

Regime (product and issue -> error-resolution regime), cfpb2015 only:
- reg_z, 12 CFR 1026.13 (billing error resolution; 1026.12(b) covers liability for
  unauthorized credit card use): product "Credit card" with issue "Billing disputes",
  "Identity theft / Fraud / Embezzlement" or "Transaction issue".
- reg_e, 12 CFR 1005.11 (error resolution for electronic fund transfers; 1005.6
  covers liability for unauthorized transfers): product "Bank account or service"
  with issue "Using a debit or ATM card" or "Making/receiving payments, sending
  money", and product "Prepaid card" with issue "Unauthorized transactions/trans.
  issues" or "Fraud or scam". Prepaid accounts follow 1005.11 as modified by
  1005.18(e), which is not in data/real/regs.
- excluded: every other product/issue (account opening, fees, rewards and so on are
  not error-resolution questions), and the "Money transfers" product: remittance
  transfers have their own error procedure (12 CFR 1005.33, not in data/real/regs)
  and many consumer money transfers are not electronic fund transfers under
  subpart A, so a product-based label would be wrong too often.
- cfpb2025 is not used: it is one deposit product, so every row would be reg_e.

The regime label is a property of the product the consumer picked, not a legal
determination of what applies to the facts in the narrative. A credit card
complaint that is really about a debit card is labelled reg_z. ASSUMPTION: the
selected issues are mostly about a transaction error or an unauthorized
transaction; we did not hand-check the narratives.
"""

from __future__ import annotations

from typing import Literal

Regime = Literal["reg_e", "reg_z"]
REGIMES: tuple[Regime, ...] = ("reg_e", "reg_z")
REGIME_SECTION: dict[Regime, str] = {"reg_e": "1005.11", "reg_z": "1026.13"}
REGIME_PART: dict[Regime, str] = {"reg_e": "1005", "reg_z": "1026"}

RELIEF_RESPONSES: frozenset[str] = frozenset(
    {"Closed with monetary relief", "Closed with non-monetary relief", "Closed with relief"}
)
NO_RELIEF_RESPONSES: frozenset[str] = frozenset({"Closed with explanation"})

REGIME_ISSUES: dict[tuple[str, str], Regime] = {
    ("Credit card", "Billing disputes"): "reg_z",
    ("Credit card", "Identity theft / Fraud / Embezzlement"): "reg_z",
    ("Credit card", "Transaction issue"): "reg_z",
    ("Bank account or service", "Using a debit or ATM card"): "reg_e",
    ("Bank account or service", "Making/receiving payments, sending money"): "reg_e",
    ("Prepaid card", "Unauthorized transactions/trans. issues"): "reg_e",
    ("Prepaid card", "Fraud or scam"): "reg_e",
}


def relief_label(company_response: str) -> int | None:
    """1 relief, 0 explanation only, None dropped."""
    if company_response in RELIEF_RESPONSES:
        return 1
    if company_response in NO_RELIEF_RESPONSES:
        return 0
    return None


def regime_label(product: str, issue: str) -> Regime | None:
    return REGIME_ISSUES.get((product, issue))
