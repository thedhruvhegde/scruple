# real situations: results

Two studies on real, public CFPB data. Labels first, because they decide what the
numbers mean.

- **Real**: the complaints (CFPB Consumer Complaint Database, public domain), their
  dates, products, companies, responses and timely flags; the regulation text (eCFR).
- **Simulated**: in the scenario study, the card network, the reason code, the dates,
  the merchant's documents and the issuer. Every simulated part is listed as an
  ASSUMPTION in the scenario file.
- **Ground truth** in the scenario study is the complaint narrative taken at face
  value (or, in two cases, the issuer's finding that the narrative disputes). It is
  not a court, network or audited outcome.

Data card: docs/data/CFPB.md (v1 and the v2 additions). Code: `scruple/realdata/`
(`scenarios.py`, `situations.py`, `timeliness.py`). Scenarios:
`scruple/sim/scenarios/real/`.

## 1. real-life dispute scenarios (64)

Each scenario is one real complaint narrative turned into the merchant's side of the
same dispute in the scenario DSL. The grounding block cites the complaint id (checked
against the committed data: v1 train or dev, never the sealed test split, or the v2
card-dispute sample), copies its product, issue and company response, lists the
dollar amounts the narrative states (each checked to appear in the narrative as
`{$x}`), whether the consumer says they were refunded, what the merchant did, the
evidence the consumer mentions, paraphrased facts, the assumptions, and the Reg E or
Reg Z paragraphs (each checked to exist in `data/real/regs/`). The expected action
cites network rules (docs/SOURCES.md S1, S2) and simulator assumptions (A-15, A-22 ...).

| | count |
|---|---|
| scenarios | 64, one complaint each |
| from cfpb2015 (2015-2016 narratives) | 23 |
| from cfpb2025 (2025-2026 checking/savings) | 16 |
| from the v2 card-dispute sample (2024-2026) | 25 |
| Reg Z (credit card) / Reg E (debit, prepaid, transfers) | 38 / 26 |
| expected accept / represent / wait | 52 / 11 / 1 |
| families: service failure, true fraud, merchant error, buyer's remorse, friendly fraud | 36, 12, 9, 4, 3 |
| network Visa / Mastercard (assumed, except 3 where the narrative names it) | 52 / 12 |

The mix leans to accept because these are complaints written by the cardholder, who
is usually describing a claim that is valid on its face. The 11 represent cases are
where the narrative itself shows the merchant has an answer: a carrier delivery to
the address, accepted renewal terms, a successor business honouring the service, a
refund that already settled, or an order tied to the cardholder's own device.

### who gets each real situation right

A play is right when the case ends correctly against ground truth (the cardholder
ends with exactly what is owed), with the expected resolution, and with no more
dispute fees than the answer key. The reference policies play through the public
dispute tools (`episode.call`), like an external agent. Issuer: `ScriptedIssuer`
(rules by ground truth). Intervals: Wilson 95% over the 64 scenarios. Simulated
outcomes on real situations; not real dispute outcomes.

| player | right | 95% CI | correct outcome | first action matches | fees (USD) |
|---|---|---|---|---|---|
| answer key (the scenario's own expected step, straight into the engine) | 64/64 | 0.943-1.000 | 64 | 64 | 1,020 |
| `evidence` (reads everything, represents only with covered required evidence) | 62/64 | 0.893-0.991 | 64 | 61 | 1,070 |
| `oracle` (reads ground truth; a declared reference run) | 60/64 | 0.850-0.975 | 60 | 59 | 1,020 |
| `always_accept` | 53/64 | 0.718-0.901 | 53 | 52 | 945 |
| `represent_all` (strawman: represent with every genuine-looking document) | 11/64 | 0.099-0.282 | 64 | 11 | 2,360 |

Where each policy fails:

- `evidence` misses both wrong-amount cases (12.5): a $30 gas sale captured at $60
  (complaint 16861992) and an airline ticket authorised at $170 that posted at $350
  (19524578). The receipt and authorization record it holds are exactly the 12.5
  required evidence, so it represents, loses, and concedes at pre-arbitration: the
  cardholder is paid, at $25 more in fees. It cannot see that its own receipt confirms
  the overcharge.
- `oracle` misses the four settled-refund cases (11365221, 1776074, 13640997,
  11541817). It knows the claim is valid and concedes, but the merchant already
  refunded, so conceding pays the cardholder twice. Knowing who is right is not
  enough; the ledger matters.
- `always_accept` misses all 11 represent cases (4 buyer's remorse, 3 friendly fraud,
  4 settled refunds).
- `represent_all` is right only on the 11 represent cases: contesting a valid claim
  still pays the cardholder but adds two stages of fees.

By slice (right / n):

| slice | evidence | oracle | always_accept | represent_all |
|---|---|---|---|---|
| Reg Z | 37/38 | 35/38 | 29/38 | 9/38 |
| Reg E | 25/26 | 25/26 | 24/26 | 2/26 |
| merchant error | 7/9 | 9/9 | 9/9 | 0/9 |
| service failure | 36/36 | 32/36 | 32/36 | 4/36 |
| true fraud | 12/12 | 12/12 | 12/12 | 0/12 |
| buyer's remorse + friendly fraud | 7/7 | 7/7 | 0/7 | 7/7 |

Reproduce: `uv run scruple realdata scenarios run --out situations.json` (seconds,
deterministic; `scruple realdata scenarios check` validates every file, its
grounding and its answer key).

### evaluating an LLM agent

```bash
uv run scruple realdata scenarios prompt --out prompts.jsonl --store .scruple/episodes
# per prompt, the agent plays through the episode CLI:
uv run scruple episode start --kind scenario --case-type <scenario id> --seed 0
uv run scruple episode call <episode_id> <tool> '<json args>'
uv run scruple episode submit <episode_id> '{"explanation": "..."}'
# answers.jsonl: {"id": "<scenario id>", "episode_id": "<episode id>"} per line
uv run scruple realdata scenarios grade --answers answers.jsonl --prompts prompts.jsonl --out card.json
```

The agent sees the cardholder's facts as the complaint states them (paraphrased) and
the case through the tools; never the family, the assumptions or the answer (tested).
Prompted ids with no closed episode, or an episode for another scenario, count as not
right. No LLM agent has been run on this suite yet; there are no model numbers here.

## 2. complaint-handling timeliness (real data)

This is the CFPB's own "Timely response?" flag (the company answered the CFPB within
15 days) and the days from the CFPB receiving a complaint to sending it to the
company. It is complaint handling timeliness. It is NOT Reg E (12 CFR 1005.11(c)) or
Reg Z (12 CFR 1026.13(c)) error-investigation timeliness: the data has no notice,
provisional credit or resolution dates.

Company size is the company's complaint count in the same dataset (a volume proxy,
not assets): the 5 largest, ranks 6-25, the rest. Wilson 95% intervals; differences
use Newcombe's hybrid score interval.

| dataset | rows | companies | untimely | rate (95% CI) |
|---|---|---|---|---|
| 2011-12 to 2016-04, four payments products (all rows, narrative or not) | 135,313 | 603 | 1,404 | 1.04% (0.99-1.09) |
| 2025-05 to 2026-04, checking/savings (third-party 8,000-row sample) | 8,000 | 225 | 77 | 0.96% (0.77-1.20) |
| 2024-08 to 2026-07, credit card disputes with narrative | 21,508 | 257 | 157 | 0.73% (0.63-0.85) |

By company size (untimely / n, rate, 95% CI):

| dataset | top 5 | ranks 6-25 | the rest | rest minus top 5 |
|---|---|---|---|---|
| payments 2011-2016 | 731 / 72,668, 1.01% (0.94-1.08) | 270 / 48,246, 0.56% (0.50-0.63) | 403 / 14,399, 2.80% (2.54-3.08) | +1.79 pp (1.53-2.08) |
| checking 2025-2026 | 23 / 3,716, 0.62% (0.41-0.93) | 9 / 3,069, 0.29% (0.15-0.56) | 45 / 1,215, 3.70% (2.78-4.92) | +3.09 pp (2.11-4.32) |
| card disputes 2024-2026 | 65 / 10,291, 0.63% (0.50-0.80) | 39 / 9,406, 0.41% (0.30-0.57) | 53 / 1,811, 2.93% (2.24-3.81) | +2.30 pp (1.59-3.19) |

In all three sets, the companies outside the 25 largest answer late about 3 to 6
times as often as the largest ones, and the interval for the difference excludes
zero. Ranks 6-25 are the most timely in every set. The top 5 are not uniform: in the
2011-2016 set Bank of America has 366 of 17,897 late (2.0%) while Capital One has 39
of 12,132 (0.3%); in the two recent sets three of the five largest have no late
response at all, and the late ones concentrate in Block (20 of 402 checking
complaints) and Synchrony (41 of 1,599 card disputes), with Bank of America at 24 of
1,606.

By product (untimely rate): payments 2011-2016: money transfers 1.92% (1.53-2.40),
bank accounts 1.13% (1.05-1.21), prepaid 1.17% (0.82-1.68), credit card 0.90%
(0.83-0.97). Card disputes 2024-2026: store cards 1.27% (0.86-1.87), general purpose
0.68% (0.57-0.80). Checking 2025-2026: checking 0.83% (0.64-1.08), other banking
products 2.46% (1.56-3.85).

Routing: the CFPB sent 54% of 2011-2016 complaints to the company within a day
(median 1 day, 90th percentile 6, maximum 993); in the two recent sets over 92% go the
same or next day (median 0). 1,644 of the 2011-2016 rows are sent one day before they
were received, as published (probably a time-zone artefact).

What this cannot say: whether any company met a Reg E or Reg Z deadline; anything
about complaints not sent to the CFPB; causes. The sets are self-selected, the 2025
set is a third party's sample, and size here is complaint volume.

Reproduce: `uv run scruple realdata timeliness --out timeliness.json` (deterministic,
reads `data/real/v2/`).
