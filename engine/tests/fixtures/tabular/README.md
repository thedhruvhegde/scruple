# tabular fixtures (hand-made)

`cases.csv` is hand-made and entirely synthetic: no real customer, merchant or payment.
Rows 4 to 7 are deliberately bad (too many decimals for USD, unknown status, duplicate
id, timestamp without an offset) so the tests can check row-level error reports.
