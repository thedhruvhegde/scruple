"""What an imported case says about what happened, read the same way everywhere.

Calibration (153) and the backtest (154) both need to know, for one `ExternalCase`,
whether it reached a chargeback decision, whether the merchant contested it, and
whether it is finished. These readings are scruple's modelling choices (ASSUMPTION,
docs/BACKTEST.md "reading an export"), not processor or network rules:

- Inquiry level: the case is still at `Stage.INQUIRY`, or ended `CLOSED` or
  `PREVENTED` (no chargeback was raised). There was no accept-or-contest decision.
- Resolved chargeback: the final state is `WON`, `LOST` or `ACCEPTED`.
- Contested: evidence was submitted; or, unless the merchant accepted, the case reached
  representment or a later stage, or it was won. Stripe has no "accepted" status
  (accepting closes the dispute as `lost`), so a lost Stripe dispute with no submission
  reads as accepted; a missed deadline reads the same way and cannot be told apart in
  an export.
"""

from __future__ import annotations

from scruple.ingest.models import CaseState, ExternalCase
from scruple.sim.models import Stage

RESOLVED_STATES = frozenset({CaseState.WON, CaseState.LOST, CaseState.ACCEPTED})
INQUIRY_STATES = frozenset({CaseState.CLOSED, CaseState.PREVENTED})
CONTEST_STAGES = frozenset({Stage.REPRESENTMENT, Stage.PRE_ARBITRATION, Stage.ARBITRATION})


def is_inquiry_level(c: ExternalCase) -> bool:
    return c.stage is Stage.INQUIRY or c.state in INQUIRY_STATES


def is_resolved_chargeback(c: ExternalCase) -> bool:
    return not is_inquiry_level(c) and c.state in RESOLVED_STATES


def is_open_chargeback(c: ExternalCase) -> bool:
    return not is_inquiry_level(c) and c.state is CaseState.OPEN


def contested(c: ExternalCase) -> bool:
    if c.evidence_submitted:
        return True
    if c.state is CaseState.ACCEPTED:
        return False
    return c.stage in CONTEST_STAGES or c.state is CaseState.WON


def won(c: ExternalCase) -> bool:
    return c.state is CaseState.WON
