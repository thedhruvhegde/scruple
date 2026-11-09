# Rules: every simulator rule and where it comes from

Each row is one rule or parameter the dispute simulator (`scruple/sim/`) uses, with
its value and its basis: a source id from docs/SOURCES.md, or an ASSUMPTION id from
docs/ASSUMPTIONS.md when it is not sourced. Values are rendered from the code itself
(`tests/test_sim_rules_doc.py` checks this file against the code), so they cannot
drift. Sourced values are as publicly reported (see the caveats in SOURCES.md: S2 to
S16 were read through search summaries, not the primary rule text). Not legal or
network advice.

## Rules and parameters

| rule | parameter | value | source |
|---|---|---|---|
| Mastercard time window | inquiry_response | 10 business days (merchant) | ASSUMPTION A-13 |
| Mastercard time window | chargeback_filing | 120 calendar days (issuer) | ASSUMPTION A-14 |
| Mastercard time window | chargeback_response | 45 calendar days (merchant) | S1, S11 |
| Mastercard time window | pre_arbitration_filing | 45 calendar days (issuer) | S11 |
| Mastercard time window | pre_arbitration_response | 30 calendar days (merchant) | ASSUMPTION A-14 |
| Mastercard time window | arbitration_filing | 45 calendar days (network) | ASSUMPTION A-14 |
| Mastercard workflow | fraud | collaboration | ASSUMPTION A-14 |
| Mastercard workflow | authorization | collaboration | ASSUMPTION A-14 |
| Mastercard workflow | processing_error | collaboration | ASSUMPTION A-14 |
| Mastercard workflow | consumer_dispute | collaboration | ASSUMPTION A-14 |
| Visa time window | inquiry_response | 10 business days (merchant) | ASSUMPTION A-13 |
| Visa time window | chargeback_filing | 120 calendar days (issuer) | S7 |
| Visa time window | chargeback_response | 30 calendar days (merchant) | S1 |
| Visa time window | pre_arbitration_filing | 30 calendar days (issuer) | S8 |
| Visa time window | pre_arbitration_response | 30 calendar days (merchant) | S8 |
| Visa time window | arbitration_filing | 10 calendar days (network) | S8 |
| Visa workflow | fraud | allocation | S8, S9 |
| Visa workflow | authorization | allocation | S8, S9 |
| Visa workflow | processing_error | collaboration | S8, S9 |
| Visa workflow | consumer_dispute | collaboration | S8, S9 |
| Visa CE 3.0 | dispute condition | 10.4 | S3 |
| Visa CE 3.0 | prior age, lower bound (exclusive) | 120 days | S3, S4 |
| Visa CE 3.0 | prior age, upper bound (exclusive) | 365 days | S3, S4 |
| Visa CE 3.0 | qualifying prior transactions | at least 2 | S3, S4 |
| Visa CE 3.0 | matching data elements | at least 2 of 4 | S2 |
| Visa CE 3.0 | one match must be | device_id or ip | S2 |
| Visa CE 3.0 | device id minimum length | 15 characters | S5 |
| Visa CE 3.0 | issuer outcome when met and submitted | merchant prevails | ASSUMPTION A-28 |
| Visa VAMP | programme start | 2025-04-01 (VDMP, VFMP retired) | S12 |
| Visa VAMP | minimum monthly fraud + dispute count | 1,500 | S13 |
| Visa VAMP | excessive ratio from 2025-06-01 | 2.2% | S13 |
| Visa VAMP | excessive ratio from 2026-04-01 | 1.5% | S13 |
| Visa VAMP | fee per dispute from 2025-10-01 | $8.00 (one source) | S13 |
| Mastercard ECM | monthly chargebacks | at least 100 | S14 |
| Mastercard ECM | chargeback ratio | at least 1.5% | S14 |
| Mastercard HECM | monthly chargebacks | at least 300 | S14 |
| Mastercard HECM | chargeback ratio | at least 3% | S14 |
| Mastercard ECM | exit | 3 months below threshold | S15 |
| Mastercard ECM | fines by month | $0.00, $1,000.00, $1,000.00 | S14 |
| Mastercard HECM | fines by month | $0.00, $1,000.00, $2,000.00 | S14 |
| Mastercard ECM / HECM | ratio denominator | previous month's settled transactions | ASSUMPTION A-29 |
| Mastercard ECM / HECM | fines after month 3 | stay at the month-3 amount | ASSUMPTION A-29 |
| Mastercard fee | on entering chargeback | $15.00 | ASSUMPTION A-17 |
| Mastercard fee | on entering pre_arbitration | $25.00 | ASSUMPTION A-17 |
| Mastercard fee | arbitration loss | $575.00 | ASSUMPTION A-17 |
| Visa fee | on entering chargeback | $15.00 | ASSUMPTION A-17 |
| Visa fee | on entering pre_arbitration | $25.00 | ASSUMPTION A-17 |
| Visa fee | arbitration loss | $600.00 | ASSUMPTION A-17 |
| Issuer odds | inquiry: base, evidence weight, detection | 0.3, 0.5, 0.3 | ASSUMPTION A-21 |
| Issuer odds | representment: base, evidence weight, detection | 0.15, 0.7, 0.5 | ASSUMPTION A-21 |
| Issuer odds | pre_arbitration: base, evidence weight, detection | 0.1, 0.75, 0.7 | ASSUMPTION A-21 |
| Issuer odds | arbitration: base, evidence weight, detection | 0.05, 0.9, 0.95 | ASSUMPTION A-21 |
| Issuer odds | supporting bonus (max 2 kinds), cap | 0.03, 0.97 | ASSUMPTION A-21 |
| Issuer | settled refund record submitted | merchant prevails (moot) | ASSUMPTION A-23 |
| Engine timing | issuer answers a response after | 10 days | ASSUMPTION A-25 |
| Engine timing | arbitration ruling after | 30 days | ASSUMPTION A-25 |
| Engine timing | missed merchant deadline | inquiry: becomes chargeback; later: lost | ASSUMPTION A-25 |
| Calendar | business days | Monday to Friday, no holidays | ASSUMPTION A-24 |
| Calendar | action on the due date | on time | ASSUMPTION A-24 |
| Generator: evidence | always held | receipt, authorization_record | ASSUMPTION A-19 |
| Generator: evidence | required kind held (merchant right / claim valid) | 1.0 / 0.25 | ASSUMPTION A-19 |
| Generator: evidence | supporting kind held (merchant right / claim valid) | 0.6 / 0.2 | ASSUMPTION A-19 |
| Generator: evidence | FULL_MIX missing / forged rates | 0.1 / 0.2 | ASSUMPTION A-20 |
| Generator: families | suggested weights | true_fraud 0.22, friendly_fraud 0.28, service_failure 0.18, buyers_remorse 0.2, merchant_error 0.06, cardholder_mistake 0.06 | ASSUMPTION A-22 |
| Generator: mix | network weights (default) | visa 0.6, mastercard 0.4 | ASSUMPTION A-32 |
| Generator: mix | share opening as inquiry | 0.2 | ASSUMPTION A-32 |
| Generator: mix | claim valid without family weights | 0.5 | ASSUMPTION A-32 |
| Generator: mix | amount (USD minor) | max(500, lognormal(4.4, 0.9) x 100) | ASSUMPTION A-32 |
| Generator: mix | processed; opened after processing | start + 0-60 days; 3-90 days | ASSUMPTION A-32 |
| Generator: mix | FULL_MIX foreign currency | 0.2 (EUR, GBP or JPY) | ASSUMPTION A-27 |
| Generator: refunds | FULL_MIX refund race; settles | 0.1; -5 to +10 days of opening | ASSUMPTION A-23 |
| Generator: split orders | FULL_MIX split share; parcels; arrival on valid claim | 0.5; 2-3; 0.5 each | ASSUMPTION A-26 |
| Generator: partial disputes | FULL_MIX share; disputed part | 0.1; 20-80% of capture | ASSUMPTION A-26 |
| Generator: CE 3.0 histories | FULL_MIX share; priors; in window | 0.6; 2-4; 80% | ASSUMPTION A-28 |
| Generator: difficulty | family tempering; d=0.5 | w ** (2 d); equals FULL_MIX | ASSUMPTION A-30 |
| FX | minor-unit exponents | USD 2, EUR 2, GBP 2, JPY 0 | S17 |
| FX | base USD per unit; daily drift | EUR 1.08, GBP 1.27, JPY 0.0067; at most 3% | ASSUMPTION A-27 |
| Merchants | risk tier weights | low 0.6, medium 0.3, high 0.1 | ASSUMPTION A-18 |
| Merchants | chargeback bps by tier | low 10-60, medium 60-140, high 140-400 | ASSUMPTION A-18 |
| Held-out planning | cap, regression, sd per case | $100, $5, $20 | ASSUMPTION A-31 |

## Reason codes (card-present and card-absent)

Codes and titles from S1 (CC BY 4.0). Required evidence is our mapping of S1's
key-evidence text (A-15); the Mastercard category mapping is ours (A-16).

| code | title | category | category source | response window | window source | required evidence | evidence basis |
|---|---|---|---|---|---|---|---|
| mastercard 4808 | Authorization-Related Chargeback | authorization | ASSUMPTION A-16 | 45 days | S1 | authorization_record | S1 text; mapping ASSUMPTION A-15 |
| mastercard 4812 | Account Number Not on File | authorization | ASSUMPTION A-16 | 45 days | S1 | authorization_record | S1 text; mapping ASSUMPTION A-15 |
| mastercard 4834 | Point-of-Interaction Error (Duplicate / Incorrect Amount) | processing_error | ASSUMPTION A-16 | 45 days | S1 | settlement_record, receipt | S1 text; mapping ASSUMPTION A-15 |
| mastercard 4837 | No Cardholder Authorization | fraud | ASSUMPTION A-16 | 45 days | S1 | avs_cvv_match, device_ip_match | S1 text; mapping ASSUMPTION A-15 |
| mastercard 4841 | Cancelled Recurring or Digital Goods Transaction | consumer_dispute | ASSUMPTION A-16 | 45 days | S1 | terms_acceptance, cancellation_record | S1 text; mapping ASSUMPTION A-15 |
| mastercard 4850 | Installment Billing Dispute | consumer_dispute | ASSUMPTION A-16 | 45 days | S1 | terms_acceptance, settlement_record | S1 text; mapping ASSUMPTION A-15 |
| mastercard 4853 | Cardholder Dispute (Goods/Services Not as Described or Defective) | consumer_dispute | ASSUMPTION A-16 | 45 days | S1 | product_description, terms_acceptance | S1 text; mapping ASSUMPTION A-15 |
| mastercard 4855 | Goods or Services Not Provided | consumer_dispute | ASSUMPTION A-16 | 45 days | S1 | delivery_proof, receipt | S1 text; mapping ASSUMPTION A-15 |
| mastercard 4863 | Cardholder Does Not Recognize | fraud | ASSUMPTION A-16 | 45 days | S1 | receipt, delivery_proof | S1 text; mapping ASSUMPTION A-15 |
| mastercard 4870 | Chip Liability Shift — Counterfeit Fraud | fraud | ASSUMPTION A-16 | 45 days | S1 | emv_chip_read, authorization_record | S1 text; mapping ASSUMPTION A-15 |
| mastercard 4871 | Chip Liability Shift — Lost / Stolen / Never Received Fraud | fraud | ASSUMPTION A-16 | 45 days | S1 | emv_chip_read, authorization_record | S1 text; mapping ASSUMPTION A-15 |
| visa 10.1 | EMV Liability Shift Counterfeit Fraud | fraud | S1 | 30 days | S1 | emv_chip_read, authorization_record | S1 text; mapping ASSUMPTION A-15 |
| visa 10.2 | EMV Liability Shift — Non-Counterfeit Fraud | fraud | S1 | 30 days | S1 | emv_chip_read, authorization_record | S1 text; mapping ASSUMPTION A-15 |
| visa 10.3 | Other Fraud — Card-Present Environment | fraud | S1 | 30 days | S1 | emv_chip_read, terminal_log | S1 text; mapping ASSUMPTION A-15 |
| visa 10.4 | Other Fraud — Card-Absent Environment | fraud | S1 | 30 days | S1 | avs_cvv_match, device_ip_match | S1 text; mapping ASSUMPTION A-15 |
| visa 10.5 | Visa Fraud Monitoring Program | fraud | S1 | 30 days | S1 | avs_cvv_match, device_ip_match | S1 text; mapping ASSUMPTION A-15 |
| visa 11.1 | Card Recovery Bulletin | authorization | S1 | 30 days | S1 | authorization_record | S1 text; mapping ASSUMPTION A-15 |
| visa 11.2 | Declined Authorization | authorization | S1 | 30 days | S1 | authorization_record | S1 text; mapping ASSUMPTION A-15 |
| visa 11.3 | No Authorization | authorization | S1 | 30 days | S1 | authorization_record | S1 text; mapping ASSUMPTION A-15 |
| visa 12.1 | Late Presentment | processing_error | S1 | 30 days | S1 | presentment_record, authorization_record | S1 text; mapping ASSUMPTION A-15 |
| visa 12.5 | Incorrect Amount | processing_error | S1 | 30 days | S1 | authorization_record, receipt | S1 text; mapping ASSUMPTION A-15 |
| visa 12.6.1 | Duplicate Processing | processing_error | S1 | 30 days | S1 | settlement_record, receipt | S1 text; mapping ASSUMPTION A-15 |
| visa 13.1 | Merchandise / Services Not Received | consumer_dispute | S1 | 30 days | S1 | delivery_proof, receipt | S1 text; mapping ASSUMPTION A-15 |
| visa 13.2 | Cancelled Recurring Transaction | consumer_dispute | S1 | 30 days | S1 | terms_acceptance, cancellation_record | S1 text; mapping ASSUMPTION A-15 |
| visa 13.3 | Not as Described or Defective Merchandise | consumer_dispute | S1 | 30 days | S1 | product_description, terms_acceptance | S1 text; mapping ASSUMPTION A-15 |
| visa 13.4 | Counterfeit Merchandise | consumer_dispute | S1 | 30 days | S1 | authenticity_proof, product_description | S1 text; mapping ASSUMPTION A-15 |
| visa 13.5 | Misrepresentation | consumer_dispute | S1 | 30 days | S1 | terms_acceptance, product_description | S1 text; mapping ASSUMPTION A-15 |
| visa 13.6 | Credit Not Processed | consumer_dispute | S1 | 30 days | S1 | refund_record | S1 text; mapping ASSUMPTION A-15 |
| visa 13.7 | Cancelled Merchandise / Services | consumer_dispute | S1 | 30 days | S1 | terms_acceptance, cancellation_record | S1 text; mapping ASSUMPTION A-15 |
| visa 13.8 | Original Credit Transaction Not Accepted | consumer_dispute | S1 | 30 days | S1 | settlement_record, communication_log | S1 text; mapping ASSUMPTION A-15 |
| visa 13.9 | Non-Receipt of Cash or Load Transaction Value | consumer_dispute | S1 | 30 days | S1 | terminal_log, settlement_record | S1 text; mapping ASSUMPTION A-15 |
