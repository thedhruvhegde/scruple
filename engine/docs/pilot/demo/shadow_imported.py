"""Demo: shadow mode on imported cases from a case store (SYNTHETIC history here).

Run from the engine directory, after `scruple ingest` wrote the store:

    uv run python docs/pilot/demo/shadow_imported.py demo-output/pilot/synthetic.db \\
        docs/pilot/demo/fraud.json

Week 4 of the pilot, replayed: every stored case is sent as production's decision on
the day it opened (the case as it looked then: open, no outcome), with the action the
history shows. The candidate is scored on what is observable. Then the outcomes
"arrive" (the stored, resolved versions) and costs are scored, each metric labelled
observed or estimated. Nothing is written anywhere.
"""

from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

from scruple.backtest import rule_candidate
from scruple.backtest.history import load_store
from scruple.calibration import CalibrationConfig, calibrate
from scruple.calibration.facts import contested, is_inquiry_level
from scruple.domain import Action
from scruple.gate.shadow_imported import ImportedDecision, ImportedOutcome, ImportedShadowRecorder
from scruple.ingest.models import CaseState, LedgerMovement
from scruple.sim.models import Stage


def main(store: str, candidate: str) -> None:
    hist = load_store(store)
    moves: dict[str, list[LedgerMovement]] = {}
    for m in hist.movements:
        moves.setdefault(m.case_external_id, []).append(m)
    profile = calibrate([hc.case for hc in hist.cases], hist.movements, CalibrationConfig()).profile
    rec = ImportedShadowRecorder(
        rule_candidate(json.loads(Path(candidate).read_text())),
        profile=profile,
        synthetic=hist.synthetic,
    )
    decisions = []
    for hc in hist.cases:
        c = hc.case
        stage = Stage.INQUIRY if is_inquiry_level(c) else Stage.CHARGEBACK
        at_open = c.model_copy(
            update={"stage": stage, "state": CaseState.OPEN, "evidence_submitted": False}
        )
        decisions.append(
            ImportedDecision(
                case=at_open,
                action=Action.CONTEST if contested(c) else Action.ACCEPT,
                decided_at=c.opened_at + timedelta(hours=2),
                movements=tuple(
                    m for m in moves.get(c.external_id, []) if m.occurred_at <= c.opened_at
                ),
            )
        )
    b = rec.record(decisions)
    print(f"[{rec.summary().data}] {hist.label[:60]}...")
    print(f"recorded {b.recorded} decisions: candidate agrees on {b.agree}")
    before = rec.summary()
    print(f"before outcomes: {before.resolved} resolved, production cost "
          f"{before.metric('production_cost').value}")  # fmt: skip
    o = rec.observe(
        [
            ImportedOutcome(case=hc.case, movements=tuple(moves.get(hc.case.external_id, [])))
            for hc in hist.cases
        ]
    )
    print(f"outcomes arrived: {o.updated} updated, {o.resolved} resolved")
    s = rec.summary()
    for m in s.metrics:
        has_band = m.low is not None and m.high is not None
        band = f" [{m.low:,.0f} .. {m.high:,.0f}]" if has_band else ""
        val = "-" if m.value is None else f"{m.value:,.2f}"
        print(f"  {m.basis:<9} {m.name:<30} {val}{band} (n={m.n})")
    for f in s.findings:
        cand = "" if f.candidate is None else f", candidate {f.candidate}"
        print(f"  finding {f.kind:<8} {f.check}: production {f.production}{cand}")


if __name__ == "__main__":
    main(*sys.argv[1:3])
