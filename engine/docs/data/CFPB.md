# data card: CFPB complaint narratives (real data)

This is the only real data in scruple. Everything else (the simulator, the demo, the
gate) is synthetic. The real-data suite lives in `scruple/realdata/`; the frozen
splits are in `data/real/v1/`.

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
