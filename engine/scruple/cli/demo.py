"""`agent-gate demo`: the end-to-end story, on synthetic data, deterministically.

The default demo gates with the anytime-valid e-value statistics on a held-out set
of `DEMO_HELD_OUT` cases, sized by the gate's own sample-size plan (the bad rule's
rarest tested case type needs about 27,000 held-out cases). `--stats bootstrap`
runs the original story on 3,000 cases with the bootstrap and slice checks.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from typing import Literal

from scruple.audit.report import usd
from scruple.clock import SteppingClock
from scruple.gate.config import GateConfig
from scruple.gate.evalue import EValueEvidence
from scruple.gate.models import DecisionRecord, ShadowMetrics
from scruple.registry import Provenance
from scruple.service import AgentGate
from scruple.sim.library import BAD_RULE, BAD_RULE_ID, GOOD_RULE, GOOD_RULE_ID
from scruple.storage import SQLiteStorage

APPROVER = "model-risk@demo.bank"
AGENT = "dispute-agent-01"
# Held-out size for the e-value demo: at least the plan's requirement for both
# rules (26,912 for the bad rule at the default MDR and sd assumptions).
DEMO_HELD_OUT = 30_000


def demo_config(stats: Literal["evalue", "bootstrap"]) -> GateConfig:
    if stats == "evalue":
        return GateConfig(stats="evalue", n_cases=DEMO_HELD_OUT)
    return GateConfig()


def _evalue_lines(ev: EValueEvidence) -> None:
    print(
        f"  e-value gate: {len(ev.family)} case type(s) changed, e-BH at {ev.alpha:g}; "
        f"plan needs {ev.required_total_cases} held-out cases, have {ev.n_cases}."
    )
    for t in ev.types:
        flag = "REGRESSED" if t.regressed else ("improved" if t.improved else "no call")
        print(
            f"    {t.case_type:<24} n={t.n:>5} (needs {t.required_n}) "
            f"change {usd(t.mean_diff):>8}/case  e(worse)={t.regression_e_value:<9.3g} "
            f"e(better)={t.improvement_e_value:<9.3g} {flag}"
        )


def _shadow_story(s: ShadowMetrics) -> None:
    if s.evalue is not None:
        _evalue_lines(s.evalue)


def _h(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def _line(rec: DecisionRecord) -> None:
    d = rec.decision
    print(f"  [{d.decision_id}] {d.stage.value:<10} -> {d.outcome.value.upper():<14} {d.summary}")


def print_redteam() -> bool:
    from scruple.gate.redteam import run_redteam

    results = run_redteam()
    print(f"  {'attack':<30} {'naive verifier':<16} {'agent-gate':<18} caught by")
    for r in results:
        naive = "FOOLED" if r.naive_fooled else "not fooled"
        gate = r.gate_outcome if r.defended else f"{r.gate_outcome} (MISSED)"
        print(f"  {r.name:<30} {naive:<16} {gate:<18} {r.detail[:60]}")
    ok = all(r.defended for r in results)
    print(
        f"  => {sum(r.defended for r in results)}/{len(results)} attacks stopped. "
        "Known weaknesses: docs/VERIFIER_FINDINGS.md"
    )
    return ok


def run_demo(
    out: Path, print_reports: bool = True, stats: Literal["evalue", "bootstrap"] = "evalue"
) -> int:
    out.mkdir(parents=True, exist_ok=True)
    reports = out / "reports"
    shutil.rmtree(reports, ignore_errors=True)
    reports.mkdir()
    db = out / "demo.db"
    for p in (db, out / "tampered.db"):
        p.unlink(missing_ok=True)

    cfg = demo_config(stats)
    gate = AgentGate(SQLiteStorage(str(db)), SteppingClock(), cfg)
    print("agent-gate demo: change control for agent learning (synthetic data only)")
    if cfg.stats == "evalue":
        print(
            f"Statistics: anytime-valid e-values on {cfg.n_cases:,} held-out cases "
            "(sized by the gate's sample-size plan)."
        )
    else:
        print(f"Statistics: seeded bootstrap on {cfg.n_cases:,} held-out cases.")
    print("A dispute-handling agent has learned two new rules from experience.")
    print("Neither may go live until it has been verified, staged and approved.")

    _h("1. Current policy (imported baseline, already approved)")
    gate.bootstrap_baseline()
    for r in gate.registry.active_rules():
        print(f"  {r.ref:<34} {r.spec.describe()}")

    _h("2. The agent proposes a GOOD rule")
    prov = Provenance(
        agent_id=AGENT,
        episode_ids=tuple(f"ep-{i:04d}" for i in range(37)),
        notes="Disputes with delivery confirmation were usually invalid.",
    )
    gate.propose(GOOD_RULE_ID, GOOD_RULE, owner="payments-ops", provenance=prov)
    print(f"  {GOOD_RULE_ID}: {GOOD_RULE.describe()}")
    good = gate.evaluate(GOOD_RULE_ID, 1)
    for rec in good:
        _line(rec)
    s = good[0].decision.shadow
    assert s is not None
    print(
        f"  Held-out replay: mean loss per case {usd(s.baseline.mean_regret_usd_minor)} -> "
        f"{usd(s.candidate.mean_regret_usd_minor)}, no slice worse, no violations."
    )
    _shadow_story(s)
    c = good[-1].decision.canary
    if c is not None and c.sequential is not None:
        q = c.sequential
        print(
            f"  Canary: sequential two-sample test on {q.n_matched} matched live cases "
            f"(p = {q.assignment_probability:g}): {q.decision.replace('_', ' ')}."
        )
    print("  Status: canary passed. It is NOT live yet: a human approver must sign off.")

    _h("3. The agent proposes a SUBTLY BAD rule (judged against the same current policy)")
    prov_bad = Provenance(
        agent_id=AGENT,
        episode_ids=tuple(f"ep-{i:04d}" for i in range(41)),
        notes="This always works; make it an override.",
        claimed_metrics={"win_rate": 0.97},
    )
    gate.propose(BAD_RULE_ID, BAD_RULE, owner="payments-ops", provenance=prov_bad)
    print(f"  {BAD_RULE_ID}: {BAD_RULE.describe()}")
    print(
        "  The logic is the same as the good rule's; only the priority differs. "
        "The agent claims 97% wins (ignored)."
    )
    bad = gate.evaluate(BAD_RULE_ID, 1)
    for rec in bad:
        _line(rec)
    bs = bad[0].decision.shadow
    assert bs is not None
    worse = [sl for sl in bs.slices if sl.delta > 0]
    print(f"  On average it looks fine: {usd(bs.mean_improvement_usd_minor)} better per case.")
    for sl in worse:
        print(
            f"  But edge case '{sl.scenario}' got worse: {usd(sl.baseline_mean_regret)} -> "
            f"{usd(sl.candidate_mean_regret)} per case ({sl.changed_cases} cases)."
        )
    for conflict in bs.conflicts:
        print(
            f"  It silently overrides safeguard {conflict.overridden_rule} "
            f"on {conflict.cases} cases."
        )
    _shadow_story(bs)
    print(
        f"  New policy violations: {bs.candidate.policy_findings - bs.baseline.policy_findings}."
        " Status: rejected. Nothing went live."
    )

    _h("4. A model-risk approver reads the evidence and activates the good rule")
    act = gate.approve(GOOD_RULE_ID, 1, APPROVER)
    _line(act)
    print("  (The rule's owner and the agent that learned it are not allowed to approve.)")

    _h("5. Reversibility: roll back the good rule, then re-check the live policy")
    rb = gate.rollback(GOOD_RULE_ID, APPROVER, "quarterly rollback drill")
    _line(rb)
    print(
        f"  Active rules now: {len(gate.registry.active_rules())} "
        f"({GOOD_RULE_ID} removed, prior policy restored)"
    )
    assurance = gate.run_assurance()
    if assurance:
        _line(assurance)

    _h("6. Red team: attacks that try to make bad rules score well")
    redteam_ok = print_redteam()

    _h("7. Audit trail: hash chain, projections and tamper detection")
    rep = gate.verify_integrity()
    print(
        f"  {rep.chain.entries} audit entries, head {rep.chain.head_hash[:16]}: "
        f"{'INTEGRITY OK' if rep.ok else 'FAILED'}"
    )
    for rec in gate.decisions():
        (reports / f"{rec.decision.decision_id}.md").write_text(
            gate.report(rec.decision.decision_id) or ""
        )
    tampered = out / "tampered.db"
    src = sqlite3.connect(str(db))
    dst = sqlite3.connect(str(tampered))
    src.backup(dst)
    src.close()
    dst.execute("DROP TRIGGER audit_no_update")
    dst.execute(
        "UPDATE audit_events SET payload_json = replace(payload_json, "
        "'\"block\"', '\"promote\"') WHERE event_type = 'decision.recorded'"
    )
    dst.execute("UPDATE rule_versions SET status='active' WHERE rule_id=?", (BAD_RULE_ID,))
    dst.commit()
    dst.close()
    print(
        "  An insider copies the database, rewrites the BLOCK decision as PROMOTE and "
        "flips the bad rule to active..."
    )
    t = AgentGate(SQLiteStorage(str(tampered)), SteppingClock()).verify_integrity()
    print(f"  verify on tampered copy: {'INTEGRITY OK' if t.ok else 'TAMPERING DETECTED'}")
    for problem in t.problems[:3]:
        print(f"    - {problem}")
    tamper_caught = not t.ok

    _h("8. Evidence reports")
    print(f"  {len(gate.decisions())} reports written to {reports}/")
    if print_reports:
        for rec in (good[0], bad[0]):
            print("\n" + "-" * 78)
            print(gate.report(rec.decision.decision_id))

    ok = rep.ok and tamper_caught and redteam_ok and bad[0].decision.outcome.value == "block"
    print("\nDEMO RESULT:", "all checks behaved as expected" if ok else "UNEXPECTED RESULT")
    return 0 if ok else 1
