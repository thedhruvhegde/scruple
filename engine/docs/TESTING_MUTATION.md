# Mutation testing of the verifiers

Question: if a verifier had a bug (a flipped comparison, a wrong constant, a dropped
condition), would a test fail? Line coverage cannot answer that; mutation testing
can. Every number below comes from one real run on this repository (2026-11-09).

## Tool and scope

- **mutmut 3.8.1**, run with `uv run --with mutmut` (not a project dependency). It
  installed and ran in reasonable time, so no in-house harness was needed.
- Mutated: every module in `scruple/verifiers/` except `exploits/` (the attacks
  themselves), `adversarial.py` (attack code) and `__init__.py`: 18 modules, 2,173
  mutants. mutmut's operators flip comparisons and boolean operators, change numeric
  and string constants, swap `+=` and `-=`, replace arguments and return values with
  `None`, and so on.
- Tests run per mutant: the verifier unit tests only, `tests/test_verifiers_*.py` and
  `tests/test_adversarial.py` (about 13 s for the whole subset; mutmut runs only the
  tests that cover the mutated function). The exploit suites (`test_exploits_*`,
  `test_exploit_regression.py`, about 45 s) were left out to keep the run near an hour;
  they exercise the same checks end to end and would kill some of the survivors, so
  the score below is a lower bound for the full suite.

Configuration (a `setup.cfg` in a scratch copy of the repository, so the project's
`pyproject.toml` is untouched):

```ini
[mutmut]
source_paths = scruple/verifiers
do_not_mutate =
    scruple/verifiers/exploits/*
    scruple/verifiers/adversarial.py
    scruple/verifiers/__init__.py
pytest_add_cli_args_test_selection =
    tests/test_verifiers_calibration.py
    tests/test_verifiers_checks.py
    tests/test_verifiers_consistency.py
    tests/test_verifiers_counterfactual.py
    tests/test_verifiers_deadlines.py
    tests/test_verifiers_evidence.py
    tests/test_verifiers_ledger_invariants.py
    tests/test_verifiers_process.py
    tests/test_verifiers_scope.py
    tests/test_verifiers_self_report.py
    tests/test_adversarial.py
    tests/test_verifiers_mutants.py
also_copy =
    scruple
    tests
    data
max_children = 3
```

Reproduce: copy the tracked files to a scratch directory (`git ls-files | tar ...`),
add the file above, `uv run --with mutmut mutmut run`, then `mutmut results --all true`.
The first run took about 50 minutes (0.73 mutants per second, 3 workers on a shared
4-core container).

## Result

First run (without `tests/test_verifiers_mutants.py`):

```
2173/2173  killed 1386  no tests 25  timeout 1  suspicious 0  survived 761  skipped 0
```

**Mutation score 63.8%** ((killed + timeout) / all). Per module:

| module | mutants | killed | timeout | survived | no tests | score |
|---|---|---|---|---|---|---|
| base | 2 | 1 | 0 | 1 | 0 | 0.500 |
| calibration | 122 | 85 | 0 | 37 | 0 | 0.697 |
| checks | 307 | 182 | 0 | 101 | 24 | 0.593 |
| consistency | 95 | 68 | 0 | 27 | 0 | 0.716 |
| costs | 22 | 7 | 0 | 15 | 0 | 0.318 |
| counterfactual | 51 | 32 | 0 | 19 | 0 | 0.627 |
| deadlines_check | 185 | 110 | 0 | 75 | 0 | 0.595 |
| dispute | 15 | 15 | 0 | 0 | 0 | 1.000 |
| evidence_check | 230 | 159 | 0 | 71 | 0 | 0.691 |
| ledger | 136 | 66 | 0 | 70 | 0 | 0.485 |
| ledger_invariants | 264 | 185 | 1 | 78 | 0 | 0.705 |
| outcome | 50 | 40 | 0 | 10 | 0 | 0.800 |
| policy_constraints | 29 | 24 | 0 | 5 | 0 | 0.828 |
| process | 103 | 61 | 0 | 42 | 0 | 0.592 |
| record | 24 | 22 | 0 | 2 | 0 | 0.917 |
| scope | 141 | 91 | 0 | 50 | 0 | 0.645 |
| self_report | 338 | 184 | 0 | 154 | 0 | 0.544 |
| trace | 59 | 54 | 0 | 4 | 1 | 0.915 |
| **total** | **2173** | **1386** | **1** | **761** | **25** | **0.638** |

The 25 "no tests" mutants are in code no selected test reaches (24 in `checks.py`:
the `at()` helper's defaults, `from_report`, parts of `Scorecard.summary`).

## Killing the important survivors

"Important" here means the mutant changes a hard verdict (a check that passes should
fail or the reverse) in a way an agent could exploit or a reviewer would trust
wrongly. `tests/test_verifiers_mutants.py` adds 15 tests aimed at 21 of them; the
targeted mutants were then re-run (`mutmut run <names>`):

| mutant (mutmut id) | change | after the new tests |
|---|---|---|
| `ledger_invariants._floors` 3 | network or processor below zero: `or` -> `and` | killed |
| `ledger_invariants._floors` 10, 11 | fee payee floor `< 0` -> `<= 0`, `< 1` | killed |
| `ledger_invariants._floors` 31 | cardholder over-credit `> 0` -> `> 1` (one minor unit slips) | killed |
| `checks._integrity` 14 | empty trace: `not trace or ...` -> `and` | killed |
| `checks.from_report` 4 | a finding for another case fails this one (`in` -> `not in`) | killed |
| `process._process` 2 | a refused `get_evidence` counts as reading the document | killed |
| `evidence_check._evidence` 80 | any upload of a kind the merchant never held is "fabricated" (`and` -> `or`) | killed |
| `scope._names` 9, 10 | names nested in argument values are not searched | killed |
| `scope._names` 8 | `out |= ...` -> `out = ...` | **survived** |
| `self_report.contradictions` 3, 4 | the resolution is replaced by `open` | killed |
| `self_report.contradictions` 18, 19, 24 | "refunded" claim checked against the wrong condition | killed |
| `self_report.contradictions` 30, 31, 36 | "escalated" claim checked against the wrong condition | killed |
| `self_report.contradictions` 42 | every non-decided case contradicts any claim (`and` -> `or`) | killed |
| `self_report.contradictions` 6 | default resolution label `open` -> `OPEN` (message only) | survived |

Rerun result: 19 of 21 killed. Score after the new tests: (1386 + 19 + 1) / 2173 =
**64.7%**. `scope._names` 8 survives although a one-off emulation of the same change,
run in process, makes the new SCOPE-01 test fail; the likely cause is mutmut's
per-function test selection, not settled. `contradictions` 6 only changes text.

## What the 742 remaining survivors are

A heuristic split of the diff of each remaining survivor (regular expressions over the
changed line; approximate):

| kind | survivors | matters for a verdict? |
|---|---|---|
| logic, thresholds or soft scores | 359 | some do: see below |
| message text (explanations, problem labels) | 237 | no |
| evidence pointers (trace seq numbers, notes on `EvidenceRef`) | 110 | no verdict, but the evidence a reviewer reads would be wrong |
| score of a hard check (`1.0 if ok` and the like) | 36 | no: `FnCheck` forces a hard check's score to 0 or 1 |

Known important survivors still open (examples, not exhaustive):

- `deadlines_check.windows`: several mutants of the window bookkeeping (`key[:2] ==`,
  `current is None`, `met = True` initial values) survive the unit tests; DL-01's
  verdict comes mostly from the `deadline_missed` transitions, so many are equivalent
  for DL-01 but not for DL-02 partial credit, which has no boundary test.
- `evidence_check._evidence`: `seen_at > call.seq` -> `>=` (citing a document in the
  same event it appears) and the `held` set computed from forged documents (27, 36, 12)
  survive; no unit test cites a document before it is visible.
- `process._process`: the order cursor (`e.seq > cursor` -> `>=`, initial cursor) and
  the partial-credit counters survive; PROC-01's order requirement has no test where
  the steps happen in the wrong order with repeats.
- `ledger.py` (the original run-level ledger verifier, 0.485) and `costs.py` (0.318):
  mostly tested through the gate suites, which were not in the selected subset.

These are listed so the next round can target them; they are not claimed as fixed.
