"""Formal checks of the money invariants (bounded, under an abstraction).

Each environment has a model written once as guarded commands (`ops`) that runs on
plain Python values or on z3 terms:

- `dispute`: one card dispute (lifecycle, deadlines, refunds incl. races and partial
  disputes, fees, ledger); `fx`: FX conversion with `decimal` semantics over integers;
- `conformance`: steps the real engines next to the models on random action
  sequences, so a model cannot drift from the code.

docs/FORMAL.md says what is proved, to which depth, and what is not. z3 is a dev
dependency: nothing here imports it at package import time.
"""
