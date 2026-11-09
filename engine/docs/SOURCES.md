# Sources

Public sources behind every real-world card-network rule in `scruple/sim/`. Each rule in
the simulator's data files carries a `source` id from this table, or is flagged
`assumption: true` and listed in docs/ASSUMPTIONS.md (A-13 onwards).

How these were gathered (2026-11-09): the build container could only reach github.com
and raw.githubusercontent.com directly. S1 was cloned and read in full. Every other
source was read through web-search result summaries; the primary documents (Visa Core
Rules, the Mastercard Chargeback Guide) were not opened. Treat S2 to S16 as
"publicly reported", not as the rule text, and re-check before relying on any figure.
Several sources disagree; where they do, the simulator either picks one and says so,
or marks the value as an assumption.

| Id | Title | Publisher | URL | Accessed | Supports |
|---|---|---|---|---|---|
| S1 | Chargeback Reason Codes (open dataset), commit `8ded368`, licence CC BY 4.0 | small-business-software-maker (maintained with chargebackkit.app) | https://github.com/small-business-software-maker/chargeback-reason-codes | 2026-11-09 | Visa and Mastercard reason codes and titles; the key evidence text per code (quoted verbatim in `data/reason_codes.json`); standard merchant response windows (Visa 30 days, Mastercard 45 days); Visa category numbering (10 fraud, 11 authorization, 12 processing errors, 13 consumer disputes). The dataset itself says its evidence and winnability fields are a synthesis, not official guidance. |
| S2 | Compelling Evidence 3.0 Merchant Readiness (March 2023) | Visa | https://usa.review.visa.com/content/dam/VCOM/regional/na/us/support-legal/documents/compelling-evidence-3.0-merchant-readiness-mar2023.pdf (host as returned by search; the same path is expected under usa.visa.com) | 2026-11-09 (search summary) | CE 3.0 matching: at least two of four data elements (user/account id, IP address, shipping address, device id/fingerprint) match, and one of the two is the IP address or the device id. |
| S3 | CE3.0 Overview; Compelling Evidence 3.0 301 Training: Qualified Transaction Matching | Verifi (a Visa company) | https://verifi.review.visa.com/compelling-evidence-ce30.html (host as returned by search) | 2026-11-09 (search summary) | CE 3.0 applies to dispute condition 10.4 (card-absent fraud); two prior undisputed transactions processed 120 to 365 days before the disputed one. |
| S4 | Visa Compelling Evidence 3.0 | Chargebacks911 | https://chargebacks911.com/visa-compelling-evidence-3-0/ | 2026-11-09 (search summary) | Same 120 to 365 day window and two prior transactions (secondary confirmation of S3). |
| S5 | Visa Compelling Evidence 3.0 explainer | Kount | https://kount.com/node/211 | 2026-11-09 (search summary) | Device id at least 15 characters; IP must be the cardholder's public address; account ids in clear text. |
| S6 | Visa Compelling Evidence 3.0 explained; Visa rule update notes | Chargeflow; Rapidcents | https://www.chargeflow.io/blog/visa-compelling-evidence-3-0-explained | 2026-11-09 (search summary) | Reported change for disputes processed from 24 Oct 2026 (prior transactions across merchants may qualify). Not modelled; see A-22. |
| S7 | Visa Chargeback Time Limits | Chargebacks911; ClearSale | https://chargebacks911.com/?p=36452 | 2026-11-09 (search summary) | Visa issuer filing limit: generally 120 calendar days from the transaction processing date (or from the expected delivery date), outer cap 540 days. |
| S8 | Visa Claims Resolution: efficient dispute processing for merchants (VBS, 14 Apr 2016) | Visa | https://bd.visa.com/dam/VCOM/download/merchants/visa-claims-resolution-efficient-dispute-processing-for-merchants-VBS-14.APR.16.pdf | 2026-11-09 (search summary) | Allocation and collaboration workflows; 30-day pre-arbitration and pre-arbitration response windows; 10-day arbitration window. A 2016 document: may be out of date. |
| S9 | Visa Dispute Workflows | Qualpay | https://help.qualpay.com/help/visa-dispute-workflows | 2026-11-09 (search summary) | Allocation covers fraud and authorization (acquirer initiates pre-arbitration); collaboration covers processing errors and consumer disputes (issuer initiates pre-arbitration). |
| S10 | Dispute timeframes | Adyen | https://docs.adyen.com/risk-management/understanding-disputes/dispute-timeframes | 2026-11-09 (search summary) | Processor response windows differ from network windows and changed for some regions from 21 Jul 2025 (an 18-day window is reported). Used only as a caveat. |
| S11 | Chargeback Guide (2018 edition); Mastercard chargeback guides | Mastercard; chargeback.io; Chargeback Gurus | https://www.mastercard.us/content/dam/mccom/en-us/documents/rules/chargeback-guide.pdf | 2026-11-09 (search summary) | Mastercard lifecycle (chargeback, second presentment, pre-arbitration, arbitration); 45 calendar days for second presentment; 45 days for issuer pre-arbitration. Arbitration filing windows conflict across sources (15, 30, 45, 75 days). |
| S12 | VAMP Merchant Guidance (quoting Visa business news) | Trust Payments | https://help.trustpayments.com/hc/en-us/articles/34158381255953 | 2026-11-09 (search summary) | VDMP and VFMP retired 31 Mar 2025; VAMP enhanced 1 Apr 2025. |
| S13 | Visa monitoring programs | Solidgate | https://docs.solidgate.com/risks/monitoring-programs/visa/ | 2026-11-09 (search summary) | VAMP ratio = (TC40 fraud reports + TC15 disputes) / TC05 settled transactions; merchant "excessive" at 2.2% from 1 Jun 2025 and 1.5% from 1 Apr 2026; minimum 1,500 monthly fraud plus dispute count; fee reported at $8 per dispute from 1 Oct 2025 (one source only). |
| S14 | High Dispute, Chargeback, and Fraud Monitoring Programs | Square | https://squareup.com/help/us/en/article/7580 | 2026-11-09 (search summary) | Mastercard ECM: 100 chargebacks and 1.5% in the same month; HECM: 300 and 3%; fine schedule (month 1 none, month 2 $1,000, month 3 $1,000 ECM / $2,000 HECM). Other sources disagree on fines. |
| S15 | Mastercard Excessive Chargebacks FAQ (9 Feb 2022) | Chase Merchant Services | https://merchantservices.chase.com/content/dam/jpm/merchant-services/documents/payment-network-updates/MC_ExcessiveChargebacks_FAQ_02092022.pdf | 2026-11-09 (search summary) | Exit ECM after three consecutive months below threshold; HECM issuer recovery assessment reported (not modelled). |
| S16 | Chargeback arbitration costs; chargeback fee guides | chargeback.io; Chargebacks911 | https://www.chargeback.io/blog/chargeback-arbitration | 2026-11-09 (search summary) | Context only for fee assumptions: processor chargeback fees reported around $15 to $100; Visa arbitration ruling fee reported at $500, then $600 from 1 Apr 2025; Mastercard arbitration total reported near $575. Not used as fact; all simulator fees are assumptions (A-17). |
| S17 | ISO 4217 Currency codes | ISO | https://www.iso.org/iso-4217-currency-codes.html | not accessed in this session | Minor-unit exponents (USD, EUR, GBP: 2; JPY: 0). Widely reproduced; the page itself was not opened here. |

## Licence note for S1

S1 is CC BY 4.0. `scruple/sim/data/reason_codes.json` reuses its codes, titles, response
windows and key-evidence text, with attribution in the file header (`dataset` block).
The mapping from key-evidence text to the simulator's `EvidenceKind` values, and the
Mastercard category mapping, are ours (A-15, A-16).
