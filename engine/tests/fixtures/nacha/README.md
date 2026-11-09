# nacha fixtures

- `return-WEB.ach`, `return-PPD-custom-reason-code.ach`, `ppd-debit.ach`: copied verbatim
  from https://github.com/moov-io/ach `test/testdata/` at commit
  `c7025ec7f2c14fbd7a32a9b1d7939dbd3648f985` (2026-11-07), fetched 2026-11-09.
  Apache License 2.0, copyright The Moov Authors; see `LICENSE.moov-io-ach`.
  They are that library's published test files; names in them ("Paul Jones",
  "Bob Marley", "Jane Doe") are its placeholders, not real customers.
- In the repository two of the files have trailing spaces stripped (lines shorter than
  94 characters); the parser accepts them only with `allow_trimmed=True`.
- Other test files are generated in `tests/test_ingest_nacha.py` (synthetic).
