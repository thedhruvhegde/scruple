# data card: CFPB complaint narratives (real data)

This is the only real data in scruple. Everything else (the simulator, the demo, the
gate) is synthetic. The real-data suite lives in `scruple/realdata/`; the frozen
splits are in `data/real/v1/`, the v2 additions (more products, dates, enforcement
orders) in `data/real/v2/` (section "v2 additions" below).

## Sources and licence

| set | origin | mirror (pinned commit) | licence |
|---|---|---|---|
| `cfpb2015` | CFPB Consumer Complaint Database, Kaggle snapshot "Consumer Finance Complaints" (file dated 2016-04-26) | github.com/jiegzhan/multi-class-text-classification-cnn @ `e75a4b1f`, `data/consumer_complaints.csv.zip` | US federal government work, public domain (17 USC 105). The repo's Apache-2.0 covers its code, which we do not use. |
| `cfpb2025` | CFPB database, checking/savings complaints received 2025-05-14 to 2026-04-24, a random 8,000-row sample made by the mirror's author | github.com/mhoehler22/cfpb-agglomerative-clustering @ `5ddb49ae`, `data/outputs/sample_metadata.csv` | CFPB data, public domain. The repo has no licence file; we keep only the 18 CFPB columns. |
| regs | eCFR Title 12 (govinfo bulk XML, amendment date 2026-09-14): sections 1005.6, 1005.11, 1026.12, 1026.13 | github.com/xerj-org/corpus-ecfr-title-12 @ `4dc0246c` | public domain. Regulation text only; the official interpretations (supplement I) are not included. |
| chargeback codes | network reason-code names | github.com/small-business-software-maker/chargeback-reason-codes @ `8ded3682` | CC BY 4.0, attribution to the repository author. Lookup only; its evidence and winnability fields are the author's synthesis, not network guidance. Not used by any task. |

Full commits, paths and sha256 of every file: `scruple/realdata/provenance.py`.
`scripts/fetch_real_data.sh` re-downloads them with anonymous git
(`GIT_LFS_SKIP_SMUDGE=1`, shallow fetch of the pinned commit), re-derives the two
derived CSVs (`scruple/realdata/derive.py`, reproduces the pinned hashes byte for
byte) and runs `sha256sum -c`. Then `uv run scruple realdata build-splits --src DIR`
rebuilds `data/real/v1/` byte for byte (tested in the slow suite).

## What a row is

One complaint a consumer filed with the CFPB, with the company's response. Kept
fields: complaint id, date received, product, sub-product, issue, sub-issue,
narrative, company, state, submission channel, company response, timely response,
consumer disputed. ZIP code and tags are dropped.

Narratives exist only where the consumer opted in to publication (from 2015-03-19)
and were scrubbed by the CFPB before release: names, dates, account numbers and
other identifiers are replaced with `XXXX`, `XX/XX/XXXX` and similar. scruple has
no PII redaction module yet (`scruple/ingest` and `scruple/security` do not exist
in this tree), so no additional redaction was run. Company names are real; they are
the companies the complaints are about, as published by the CFPB.

## Splits (`data/real/v1/`, seed 2026)

| set | rows with narrative | near-duplicates dropped | test (sealed) | dev | train | not committed |
|---|---|---|---|---|---|---|
| cfpb2015 | 15,167 | 56 | 3,024 | 1,511 | 6,802 | 3,774 |
| cfpb2025 | 8,000 | 11 | 1,598 | 798 | 3,198 | 2,395 |

- Near-duplicates: narratives equal after lowercasing, dropping punctuation and
  collapsing masks. The smallest complaint id is kept. No normalised narrative is in
  two splits (tested across both sets).
- Stratified: cfpb2015 by product and issue, cfpb2025 by issue and sub-issue. Within
  a stratum, rows are ordered by sha256 of `seed:id` and cut at 20% test, 10% dev,
  then 45% (cfpb2015) or 40% (cfpb2025) train. The rest is left out only to keep
  the committed data under 8 MB; it can be regenerated from the sources.
- The test split is sealed: `load_split(..., "test")` raises unless `unseal=True`.
  Fit on train, choose on dev, report test.
- `manifest.json` records counts and sha256 of each gzip file and of its JSONL
  content; the loader checks the content hash on every load.

Label counts (company response):

| | explanation | monetary relief | non-monetary relief | closed | untimely / in progress |
|---|---|---|---|---|---|
| cfpb2015 test | 1,952 | 725 | 298 | 46 | 3 |
| cfpb2025 test | 1,296 | 201 | 94 | 0 | 7 |

cfpb2015 has 47 issue labels over 4 products (credit card, bank account or service,
prepaid card, money transfers) and no sub-issues on narrative rows. cfpb2025 is one
product (checking or savings account) with 11 issues and 41 issue/sub-issue pairs
in the full sample. Narrative dates in cfpb2015 run 2015-03-19 to 2016-04-19.

## Known biases

- **Self-selected.** Only consumers who chose to escalate to a regulator, and of
  those only the ones who consented to publish the narrative. This is not a random
  sample of disputes, chargebacks or error notices, and nothing here estimates how
  often anything happens in the wider population.
- **Concentrated.** Five large issuers account for about 47-49% of rows in every split.
- **The outcome is the company's own report.** "Company response to consumer" is
  self-reported, not audited, and it is not a network or court outcome. "Timely"
  means the company answered the CFPB within 15 days, not Reg E or Reg Z deadlines.
- **Labels are the consumer's choice.** Product and issue are picked by the consumer
  on the form, so they are noisy. Issue-routing scores are agreement with that
  choice, not accuracy against a ground truth.
- **Two taxonomies.** cfpb2015 uses the pre-2017 product and issue taxonomy,
  cfpb2025 the current one. Labels are never pooled across them.
- **Age.** cfpb2015 is about ten years old; products and issuer practices have changed.
- **cfpb2025 is a third party's sample.** Its README says random; we could not check how.

## What it can and cannot show

Can: whether a model routes a real narrative to the issue the consumer chose; how
well it predicts whether the company reported relief; whether it tells a deposit,
prepaid or transfer complaint (Reg E) from a credit card one (Reg Z) and cites a
real paragraph of the right regulation.

Cannot: dollar exposure, chargeback win rates, network deadlines, compliance with
Reg E 10/45-day or Reg Z 30/90-day timelines (no amounts, transaction dates or
provisional credit dates), false-positive cost, anything about merchants, or
whether the company's response was correct.

## Tasks and label mappings (`scruple/realdata/tasks.py`, `labels.py`)

The grader is always a CFPB field, never a model. Test-split instance counts:

| task | input | label | test n |
|---|---|---|---|
| `routing-2015` | narrative | issue (47 labels) | 3,024 |
| `routing-2025` | narrative | issue and sub-issue (allowed pairs only) | 1,598 |
| `outcome-2015` | narrative, product, sub-product, issue, sub-issue, company | relief vs explanation | 2,975 |
| `outcome-2025` | same | relief vs explanation | 1,591 |
| `regime-2015` | narrative | reg_e or reg_z, plus a citation | 764 |

Outcome mapping (company response to consumer): "Closed with monetary relief",
"Closed with non-monetary relief" and "Closed with relief" are relief; "Closed with
explanation" is no relief; every other value ("Closed", "Closed without relief",
"Untimely response", "In progress") is dropped.

Regime mapping (cfpb2015 only, from product and issue):

| product | issues | label |
|---|---|---|
| Credit card | Billing disputes; Identity theft / Fraud / Embezzlement; Transaction issue | reg_z (12 CFR 1026.13) |
| Bank account or service | Using a debit or ATM card; Making/receiving payments, sending money | reg_e (12 CFR 1005.11) |
| Prepaid card | Unauthorized transactions/trans. issues; Fraud or scam | reg_e (1005.11 as modified by 1005.18(e)) |

Everything else is excluded. Money transfers are excluded on purpose: remittance
transfers have their own error procedure (12 CFR 1005.33, not in `data/real/regs`)
and many consumer money transfers are not electronic fund transfers under subpart A.
cfpb2025 is excluded because every row is a deposit account (always reg_e). The
label follows the product the consumer picked, so it is not a legal finding about
the facts; ASSUMPTION: the selected issues are mostly transaction errors or
unauthorized transactions (not hand-checked). "Neither regime" is not offered,
because no field grades it.

Citations must be written `12 CFR <section>(<paragraph>)...`. A citation "exists"
when its paragraph id is defined in the shipped text of 1005.6, 1005.11, 1026.12 or
1026.13 (142 ids, `scruple/realdata/regs.py`). It is "consistent" when it also
belongs to the predicted regime's part (1005 for reg_e, 1026 for reg_z).
`strict_accuracy` needs the right regime and a consistent citation.

Answers are strict JSON objects with exactly the asked keys. A malformed or missing
answer counts as wrong: an invalid class for classification, and probability
`1 - y` (the worst possible) for outcome prediction.

## Evaluating a model

```bash
# 1. prompts (deterministic sample by sha256(seed:id); test is the sealed split)
uv run scruple realdata prompt --task regime-2015 --split test --n 300 --seed 1 --out p.jsonl
# 2a. any model or agent writes a.jsonl: {"id": ..., "answer": "<raw reply>"} per line
# 2b. or a hosted model through scruple's runners (needs ANTHROPIC_API_KEY / OPENAI_API_KEY)
uv run scruple realdata run --prompts p.jsonl --provider anthropic --out a.jsonl
# 3. grade; prompted ids with no answer count as wrong
uv run scruple realdata grade --task regime-2015 --answers a.jsonl --prompts p.jsonl --out card.json
```

`run` makes one plain completion per prompt (no tools, temperature 0) through
`scruple.agents.runners` (retries, timeouts, model defaults); a failed request is
recorded with an empty answer and graded as wrong. It has only been exercised
against fake transports. Baseline numbers: docs/results/REAL_BASELINES.md.

## v2 additions (`data/real/v2/`, `scruple/realdata/v2.py`)

v2 adds files next to v1 and changes nothing in it. About 3.7 MB committed.

| file | rows | what | derived from |
|---|---|---|---|
| `cfpb_cc2024/meta.csv.gz` | 21,508 | credit card dispute complaints received 2024-08-04 to 2026-07-15, current taxonomy, no narrative | `ccdb_disputes_raw.zip` |
| `cfpb_cc2024/narratives.jsonl.gz` | 3,000 | narratives of a deterministic sample of the rows above (first 3,000 by sha256(`2026:<complaint id>`)) | same |
| `timeliness/cfpb2015_payments.csv.gz` | 135,313 | dates and response fields of every 2012-2015 payments row (with or without narrative) | v1 `payments_subset.csv` |
| `timeliness/cfpb2025_checking.csv.gz` | 8,000 | dates and response fields of the 2025-2026 checking sample | v1 `checking_complaints_sample.csv` |
| `enforcement/cfpb_enforcement_orders.csv` | 359 | CFPB enforcement orders 2012-2023: caption, forum, docket, order dates, ordered and suspended amounts (penalty, redress, disgorgement, other relief) | `Disp.csv` |

Meta columns: complaint id, date received, date sent to company (ISO dates),
product, sub-product, issue, sub-issue, company, state, submitted via, company
response, timely response. ZIP code, tags, consent and the public response are dropped.

| source | mirror (pinned commit) | licence |
|---|---|---|
| card disputes 2024-2026 | github.com/THEpranavsomalraju/card-dispute-prd @ `aaa20954`, `ccdb_disputes_raw.zip` (sha256 `0b121cda...`). The author pulled the CFPB API on 2026-08-04 (trailing 24 months), kept credit card rows with a narrative whose issue is a purchase or investigation problem. | CFPB data, public domain. The repo has no licence file; we keep only the CFPB columns and drop the author's keyword tags P1-P6. |
| enforcement orders | github.com/LukasNichols/CFPB_Enforcement_Relief_And_Penalties @ `13014e6e`, `Disp.csv` (sha256 `5cd6578c...`). CFPB records released under FOIA to the Data Liberation Project. | Government records, public domain. The repo licenses files its author generated under CC BY-SA 4.0, so we redistribute the derived table under CC BY-SA 4.0, attributed to Lukas Nichols and the Data Liberation Project. |

Rebuild: `uv run scruple realdata fetch-v2 --dest DIR` (anonymous git, pinned
commits, sha256 checked), put the v1 raw files in the same DIR
(`scripts/fetch_real_data.sh DIR`), then `uv run scruple realdata build-v2 --src DIR`
rewrites `data/real/v2/` byte for byte (tested in the slow suite). `manifest.json`
holds the sha256 of every file and of its decompressed content; loaders check the
content hash on every load.

What the card-dispute set covers: product credit card only (general purpose 19,541,
store card 1,967); issues "Problem with a purchase shown on your statement" 20,441
and "Problem with a company's investigation into an existing problem" 1,067; top
sub-issues "isn't resolving a dispute about a purchase" 15,226, "charged for
something you did not purchase" 4,590. Responses: explanation 14,429, monetary
relief 5,645, non-monetary 1,415, untimely 19. Timely = No on 157. Every row has a
narrative upstream; only the 3,000-row sample's narratives are committed.

Known limits of the v2 files:

- The card-dispute pull is a third party's filter of the CFPB API (credit card
  dispute issues with a narrative, per its README); we could not re-run it.
- 1,644 of the 135,313 2012-2015 rows have a date sent to company one day before
  the date received (as published; probably a time-zone artefact). They are kept.
- The enforcement table has no statute or regulation column, so it cannot say which
  orders concern Reg E or Reg Z. It is context only; no task or result uses it to
  make a claim about a regulation. Some captions are empty in the source.
- No prepaid or money-transfer complaints newer than 2016 were found on a reachable
  public mirror with a usable licence; the GitHub repositories found for those
  products held only READMEs or dashboards.
- Searched and not used: cfarm/scrape-enforcement-actions (a scraper, CC0, no
  data), BrandonJenkins09/Federal-Enforcement-Datasets (README only, data sold
  elsewhere), Duckky153/consumer-complaint-operations (a dashboard bundle),
  aastharijal/CFPB-Money-Transfer-Analysis and sheha61-boop/cfpb-payment-complaints-analysis
  (README only).

No new card-network reason-code reference with a licence was found beyond the CC
BY 4.0 table already pinned (S1 in docs/SOURCES.md).
