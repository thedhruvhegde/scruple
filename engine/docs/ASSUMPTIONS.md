# Assumptions

Anything here could be wrong. Validate before relying on it, and never present it
as fact to a customer.

## Regulatory (unverified; not legal advice)

- **A-1** SR 26-2 / OCC Bulletin 2026-13 (April 2026) replaced SR 11-7 / OCC
  2011-12. Secondary commentary says generative and agentic AI are out of scope and
  that the agencies plan an RFI on AI and model risk. We have not read or quoted
  the primary texts in code or docs. Treat it as: there is no prescriptive federal
  standard for governing learning agents today.
- **A-2** Banks will still want documented controls for internal risk appetite,
  audit and examiner conversations. This is a market hypothesis, not a regulatory
  requirement.
- **A-3** EU AI Act Annex III high-risk obligations were deferred to 2 Dec 2027.
  Whether dispute handling falls in scope is unknown and needs legal review.

## Product and market

- **A-4** Regulated institutions want agents to learn but cannot approve
  self-modification without review. To be validated in discovery calls
  (docs/MARKET.md section 7).
- **A-5** Dispute and chargeback operations are a good wedge because ground truth
  exists and arrives within weeks. Real outcome latency, label quality and data
  access are unverified.

## Simulator and verifiers

- **A-6** Policy constraints (escalate over $10,000, no representment without timely
  evidence, never accept a duplicate) are illustrative examples, not real card-network
  or regulatory rules.
- **A-7** Fees ($15 representment loss, $250 arbitration loss), the $35 escalation
  cost, the FX table and every ground-truth probability are invented.
- **A-8** A human analyst resolves an escalated case optimally. Real analysts err.
- **A-9** Outcomes resolve immediately. Real dispute outcomes lag by weeks, which
  delays canary evidence and assurance.
- **A-10** Ground-truth labels are correct. Real labels are noisy (e.g. arbitration
  outcomes are not always "truth").
- **A-11** Gate thresholds (minimum 30 affected cases, 95% CI, zero slice tolerance, +2pp
  escalation limit, 10% canary, $5 canary tolerance) are defaults chosen for the demo,
  not calibrated to any institution's risk appetite.
- **A-12** Scenario slices are the right unit for "edge case regression". Real
  edge cases may cut across our slice labels (see gap G-3).

## Card network model (scruple/sim, roadmap 017 onwards)

Sources are listed in docs/SOURCES.md. Everything below is NOT sourced, or is a
simplification of a sourced rule. Data files flag these values with `assumption: true`.

- **A-13** Inquiry (retrieval request) reply window: 10 business days on both networks.
  Not sourced; chosen so the business-day calendar is exercised.
- **A-14** Mastercard: the 120-day issuer filing window is commonly cited but was not
  confirmed here; the 30-day pre-arbitration response and 45-day arbitration filing
  windows are not sourced (public sources conflict: 15, 30, 45, 75 days). Mastercard
  disputes are modelled with the same collaboration-shaped flow as Visa consumer
  disputes (chargeback, second presentment, pre-arbitration, arbitration).
