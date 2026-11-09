"""Demo: a canary ramp that breaches and rolls back in one step (synthetic data).

Run from the engine directory:  uv run python docs/pilot/demo/canary.py

The good rule passes shadow and the one-shot canary, then ramps 5% -> 10% -> 25% ->
50%. At the 25% stage the live traffic drifts (delivered-but-"not received" claims
become mostly valid), so contesting them now loses. The stage breaches, the ramp's
traffic share drops to zero and the version is rejected, all in one transaction. The
audit chain still verifies. Same as tests/test_gate_rollback.py, printed.
"""

from __future__ import annotations

from scruple.clock import SteppingClock
from scruple.gate.canary import RampSchedule
from scruple.gate.config import GateConfig
from scruple.registry import Provenance
from scruple.service import AgentGate
from scruple.sim.generator import Profile, generate_cases
from scruple.sim.library import GOOD_RULE, GOOD_RULE_ID
from scruple.storage import SQLiteStorage

CFG = GateConfig(n_cases=2_000, bootstrap_resamples=300)
SCHEDULE = RampSchedule(stages=(5.0, 10.0, 25.0, 50.0))
DRIFT = Profile(name="drift", p_valid_inr_delivered=0.75)  # synthetic drift
PROV = Provenance(agent_id="dispute-agent-01", episode_ids=("ep-0001", "ep-0002"))


def main() -> None:
    g = AgentGate(SQLiteStorage(":memory:"), SteppingClock(), CFG)
    g.bootstrap_baseline()
    g.propose(GOOD_RULE_ID, GOOD_RULE, "payments-ops", PROV)
    for rec in g.evaluate(GOOD_RULE_ID, 1):
        print(
            f"{rec.decision.stage.value:<7} {rec.decision.outcome.value:<14} {rec.decision.summary}"
        )
    state = g.start_canary_ramp(GOOD_RULE_ID, 1, SCHEDULE)
    print(f"ramp started at {state.percent:g}%")
    for stage in range(len(SCHEDULE.stages)):
        drift = stage == 2
        live = generate_cases(
            700 + stage, 2_000, DRIFT if drift else Profile(), prefix=f"l{stage}-"
        )
        rec, state = g.advance_canary(GOOD_RULE_ID, 1, live)
        label = " (traffic drifted)" if drift else ""
        print(
            f"stage {stage}{label}: {rec.decision.outcome.value:<9} -> {state.status} "
            f"at {state.percent:g}%"
        )
        if state.status != "ramping":
            break
    print(f"status: {g.registry.get(GOOD_RULE_ID, 1).status.value}")
    rep = g.verify_integrity()
    print(f"audit: {rep.chain.entries} entries, {'INTEGRITY OK' if rep.ok else 'FAILED'}")


if __name__ == "__main__":
    main()
