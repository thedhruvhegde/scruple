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
- **A-15** The mapping from each reason code's key-evidence text (S1) to the simulator's
  `EvidenceKind` values, and the split into required and supporting evidence, is ours.
  S1 itself says its evidence field is a synthesis, not official network guidance.
- **A-16** Mastercard does not use Visa's four categories. We map 4837, 4863, 4870 and
  4871 to fraud; 4808 and 4812 to authorization; 4834 to processing error; 4841, 4850,
  4853 and 4855 to consumer dispute. `legacy_family` (bridge to the original
  three-family simulator) and `card_present` are also our labels.
- **A-17** Dispute fees (scruple/sim/fees.py): $15 when a chargeback lands, $25 on
  entering pre-arbitration, and an arbitration case fee charged only when the merchant
  loses ($600 Visa, $575 Mastercard). Public secondary sources (S16) report figures of
  this order, but real amounts depend on the acquirer and change over time. The
  "loser pays, merchant always pays its own fees" model is a simplification.
- **A-18** Merchant profiles are synthetic: risk tiers (60% low, 30% medium, 10% high),
  chargeback rates of 0.1-0.6%, 0.6-1.4% and 1.4-4.0% of settled transactions per tier,
  log-normal monthly volume, and fraud reports at 20-60% of chargebacks. None of this is
  calibrated to any real portfolio or MCC.
- **A-19** Evidence availability: every case has a receipt and an authorization record.
  A merchant facing an invalid claim holds every required kind for the reason code and
  each supporting kind with probability 0.6; facing a valid claim, 0.25 and 0.2.
  Document contents (refs, carriers, IPs from 203.0.113.0/24, a documentation range)
  are synthetic.
- **A-20** Forged and missing evidence (generator options, off by default): only a
  merchant facing a valid claim forges, and only to fill a missing required document;
  only a merchant in the right loses documents. Forgeries come in three synthetic
  styles (edited after sealing, a resealed copy of another order, backdated), each
  detectable by `document_problems`. Real forgeries can be subtler.
- **A-21** Issuer behaviour (scruple/sim/issuer.py): the chance the merchant prevails
  is a base rate per stage (inquiry 0.30, representment 0.15, pre-arbitration 0.10,
  arbitration 0.05) raised by the share of required evidence submitted (weights 0.5,
  0.7, 0.75, 0.9), plus 0.03 per supporting kind (at most two), capped at 0.97. A
  flawed document is caught with probability 0.3, 0.5, 0.7, 0.95 by stage, and a
  caught forgery loses the stage. The issuer never sees ground truth. All numbers are
  invented to give evidence a clear but noisy effect; none comes from network data.
- **A-22** Case families (scruple/sim/families.py): the suggested mix (22% true fraud,
  28% friendly fraud, 18% service failure, 20% buyer's remorse, 6% merchant error, 6%
  cardholder mistake) and the cardholder statements with their odds are invented.
  Friendly fraud always comes with device and IP evidence linking the cardholder; true
  fraud never does. Real friendly-fraud rates are disputed and not modelled.
- **A-23** Refunds racing disputes: only valid claims race, always with a full refund
  settling 5 days before to 10 days after the dispute opens. A refund that settles
  before the chargeback debit leaves the cardholder paid twice until the merchant
  represents with the refund record, which any issuer accepts (the dispute is moot).
  A merchant refund after the debit is refused by the ledger (the processor would
  bounce or the merchant would catch it); in reality such refunds often go through.
- **A-24** Business days are Monday to Friday with no holidays by default. A stage's
  deadline counts from the day the stage was entered, and an action on the due date is
  on time. Real networks count from specific processing dates and use their own
  calendars; processors often set shorter internal deadlines (S10).
- **A-25** Dispute engine timing and roles: the issuer answers a representment or an
  allocation pre-arbitration 10 days after it is filed; a network arbitration ruling
  arrives 30 days after escalation. The merchant (not the issuer) escalates a
  pre-arbitration to arbitration, and the arbitration filing window then runs from the
  issuer's decline. A missed merchant deadline loses the case; a missed inquiry
  deadline becomes a chargeback. Cases are captured and, when they open as
  chargebacks, debited when the engine is built, whatever the clock says.
- **A-26** Split shipments and partial disputes: a not-received order ships in 2 or 3
  parcels with random amounts; on a valid claim each parcel arrives with probability
  0.5 (at least one is lost) and half the time the cardholder disputes the whole order
  anyway. The merchant wins back exactly the disputed parcels it proves delivered with
  a genuine delivery document, and only if the issuer's evidence decision goes its way.
  Partial disputes cover 20-80% of the capture. Real networks have specific partial
  representment rules that are not modelled.
- **A-27** FX (scruple/sim/fx.py): synthetic base rates (USD per unit: EUR 1.08,
  GBP 1.27, JPY 0.0067) moved by a deterministic daily drift of at most 3%. Not market
  rates. Every posting converts at the rate of its own day, so the merchant bears the
  FX difference between a chargeback debit and its reversal; real schemes convert at
  specific settlement dates and can apply markups, which are not modelled.
- **A-28** CE 3.0 (scruple/sim/ce3.py): implemented from secondary descriptions of
  Visa's rule (S2-S5), not the rule text. The 120/365-day bounds are treated as
  exclusive ("more than 120, fewer than 365", S4). When a case meets the criteria and
  the merchant submits its order history, the simulated issuer always lets the merchant
  prevail (liability moves to the issuer). Not modelled: the reported October 2026
  cross-merchant change (S6), fraud-report exclusions, the public-IP requirement and
  the data exchange itself. Generated histories (2-4 prior orders, 80% inside the age
  window, 10% disputed) are synthetic.
- **A-29** Monitoring programmes (scruple/sim/monitoring.py): thresholds come from
  secondary sources (S13, S14, S15) and are applied at merchant level only, with US
  timing for VAMP. Not sourced, so assumed: the Mastercard ratio uses the previous
  month's settled transactions; ECM/HECM fines after the third month stay at the
  month-3 amount; the VAMP fee applies per chargeback in an excessive month. VAMP
  early-warning levels, acquirer-level thresholds, enumeration ratios, regional
  differences and the HECM issuer recovery assessment are not modelled. Sources
  disagree on fines; the S14 schedule was picked.
- **A-30** Difficulty knob (scruple/sim/difficulty.py): the schedule that maps one
  number in [0, 1] to evidence availability, forgery and missing-evidence rates and
  tempered family weights (`w ** (2 d)`) is ours. "Ambiguity" means the evidence a
  merchant holds says less about who is right; "rarity" means rare case families get
  rarer. Neither is calibrated to real dispute data. `d = 0.5` equals `FULL_MIX`.
- **A-31** Held-out planning (scruple/sim/heldout.py): the defaults ($100 per-case cost
  cap, $5 regression to detect, $20 per-case sd, alpha 0.05, power 0.8) are the
  illustrative values from docs/math, and "case type" defaults to case family. A real
  plan needs the deployment's own caps, tolerances and type definitions.
- **A-32** Generator base mix (scruple/sim/disputes.py `CaseMix` defaults): 60% Visa /
  40% Mastercard, 20% of disputes open as an inquiry, 50% valid claims when no family
  weights are given, amounts `max($5, lognormal(4.4, 0.9) dollars)`, transactions
  processed within 60 days of the start date and disputed 3 to 90 days later. None of
  this is calibrated; see docs/results/SIM_FIDELITY.md for how far one public dataset
  is from it.
- **A-33** Fidelity mapping (scruple/sim/fidelity.py): CFPB "Identity theft / Fraud /
  Embezzlement" maps to fraud reason codes and "Billing disputes" to every other
  category; "Closed with monetary relief" maps to the cardholder ending with money back
  under a fixed reference merchant policy. These mappings, the policy and the
  calibrated profile fitted through them are ours; complaints are not network
  disputes, and the calibrated profile's zero true-fraud share is an artefact of the
  fit (see the results file), not an estimate.
