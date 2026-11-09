# bank statement fixtures

## real public samples

- `svb_us_example.bai2`, `citi_example.bai2`, `nwb_example.bai2`,
  `account_trailer_amount_blank_example.bai2`: copied verbatim from
  https://github.com/ministryofjustice/bai2 `tests/data/` at commit
  `6696a720196b028fcfad4b89fd88fa756bf124de` (2025-11-04), fetched 2026-11-09.
  MIT licence, copyright HM Government (Ministry of Justice Digital & Technology);
  see `LICENSE.ministryofjustice-bai2`. They are that library's published test data,
  not real customer statements.
- `prowide_camt.053.001.07.xml`: copied verbatim from
  https://github.com/prowide/prowide-iso20022
  `iso20022-core/src/test/resources/camt.053.001.07.xml` at commit
  `18723858561ed3e7e8f3cf4dc26f1e0cdefc3496` (2026-11-01), fetched 2026-11-09.
  Apache License 2.0; see `LICENSE.prowide-iso20022`. It has balances but no entries.

## hand-made

- `handmade_camt.053.001.02.xml`: HAND-MADE and synthetic, built from the camt.053.001.02
  element names. Three entries (credit, chargeback debit, return with reason AC04);
  opening balance plus entries equals closing balance. No public camt.053 sample with
  entries under a permissive licence was found on GitHub (the ones found were GPL or AGPL).
