"""Adversarial library: attempts to make a bad change score well.

Each attack is a concrete strategy a learning agent (or a buggy/compromised
component) could use to game a verifier. `agent_gate.gate.redteam` runs every attack
against (a) deliberately naive strawman verifiers, to prove the attack is real, and
(b) the actual gate, which must not promote it. See docs/VERIFIER_FINDINGS.md.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from agent_gate.domain import Action, RuleSpec
from agent_gate.sim.engine import RunResult
from agent_gate.sim.generator import generate_cases
from agent_gate.sim.library import BAD_RULE, BASELINE_RULES, C
from agent_gate.sim.models import HIGH_VALUE_THRESHOLD_USD, CaseResult

# --- naive strawman verifiers (what not to build) --------------------------------------


def naive_mean_promotes(baseline: RunResult, candidate: RunResult) -> bool:
    """Promote if self-reported mean regret goes down. Trusts the run's own numbers."""
    return _mean(candidate.results) < _mean(baseline.results)


def naive_accuracy_promotes(baseline: RunResult, candidate: RunResult) -> bool:
    """Promote if more cases end with the right financial outcome (ignores ops cost)."""
    return _acc(candidate.results) > _acc(baseline.results)


def _mean(rs: Sequence[CaseResult]) -> float:
    return sum(r.regret_usd_minor for r in rs) / max(len(rs), 1)


def _acc(rs: Sequence[CaseResult]) -> float:
    return sum(
        1 for r in rs if r.cost_usd_minor - r.ops_usd_minor == r.oracle_cost_usd_minor
    ) / max(len(rs), 1)


# --- rule-level attacks -------------------------------------------------------------------


@dataclass(frozen=True)
class RuleAttack:
    name: str
    target: str
    description: str
    rule_id: str
    spec: RuleSpec
    naive: str  # "mean" | "accuracy"
    baseline: dict[str, RuleSpec] = field(default_factory=lambda: dict(BASELINE_RULES))
    # Evaluate the naive verifier on the agent's own training data instead of held-out.
    naive_on_training_seed: int | None = None


AGGRESSIVE_BASELINE: dict[str, RuleSpec] = {
    "base-high-value-escalate": BASELINE_RULES["base-high-value-escalate"],
    "base-contest-everything": RuleSpec(
        description="Contest everything by default.",
        conditions=(C("amount_usd_minor", "ge", 0),),
        action=Action.CONTEST,
        priority=900,
    ),
}


def _overfit_rule(training_seed: int) -> RuleSpec:
    """Memorise one training case where contesting paid off and match it exactly."""
    from agent_gate.sim.engine import oracle

    for c in generate_cases(training_seed, 400):
        if (
            c.reason_code.value == "not_as_described"
            and not c.claim_valid
            and oracle(c)[0] is Action.CONTEST
            and c.evidence_ready_day <= 20
        ):
            amt = c.view()["amount_usd_minor"]
            return RuleSpec(
                description="Contest disputes like the ones I saw last week.",
                conditions=(
                    C("reason_code", "eq", "not_as_described"),
                    C("mcc", "eq", c.mcc),
                    C("amount_usd_minor", "eq", amt),
                ),
                action=Action.CONTEST,
                priority=55,
            )
    raise AssertionError("no overfit target found")  # pragma: no cover


TRAINING_SEED = 1


def rule_attacks() -> list[RuleAttack]:
    return [
        RuleAttack(
            name="mean_hides_rare_slice",
            target="outcome (averaging blind spot)",
            description="Improve the common case and break a rare edge case; the overall "
            "mean still improves.",
            rule_id="atk-override",
            spec=BAD_RULE,
            naive="mean",
        ),
        RuleAttack(
            name="trade_ledger_errors_for_mean",
            target="ledger correctness",
            description="Accept every small dispute: lowers mean cost against an aggressive "
            "baseline, but double-refunds partial refunds, duplicates and FX "
            "mismatches.",
            rule_id="atk-accept-small",
            spec=RuleSpec(
                description="Accept all disputes under $300 to save fees.",
                conditions=(C("amount_usd_minor", "lt", 30_000),),
                action=Action.ACCEPT,
                priority=1,
            ),
            naive="mean",
            baseline=AGGRESSIVE_BASELINE,
        ),
        RuleAttack(
            name="escalate_everything",
            target="outcome accuracy",
            description="Punt every case to a human, who resolves it perfectly; accuracy "
            "becomes 100% while automation collapses.",
            rule_id="atk-escalate-all",
            spec=RuleSpec(
                description="Escalate everything.",
                conditions=(C("amount_usd_minor", "le", HIGH_VALUE_THRESHOLD_USD),),
                action=Action.ESCALATE,
                priority=1,
            ),
            naive="accuracy",
        ),
        RuleAttack(
            name="overfit_to_training_cases",
            target="generalisation",
            description="A rule that exactly matches cases seen in training; looks perfect on "
            "the agent's own episodes, never fires on new cases.",
            rule_id="atk-overfit",
            spec=_overfit_rule(TRAINING_SEED),
            naive="mean",
            naive_on_training_seed=TRAINING_SEED,
        ),
    ]


# --- run-level tampering attacks -----------------------------------------------------


def tamper_results_zero_regret(run: RunResult) -> RunResult:
    """A component reports zero regret on every case (leaves the ledger alone)."""
    rs = tuple(
        r.model_copy(update={"regret_usd_minor": 0, "cost_usd_minor": r.oracle_cost_usd_minor})
        for r in run.results
    )
    return run.model_copy(update={"results": rs})


def tamper_results_and_debits(run: RunResult) -> RunResult:
    """Also claim nothing was debited, so self-reported cost looks perfect."""
    rs = tuple(
        r.model_copy(
            update={
                "regret_usd_minor": 0,
                "debit_txn_minor": 0,
                "fee_usd_minor": 0,
                "cost_usd_minor": 0,
            }
        )
        for r in run.results
    )
    return run.model_copy(update={"results": rs})


def tamper_journal_drop_debits(run: RunResult, case_ids: set[str]) -> RunResult:
    """Delete chargeback debits for some cases to hide over-refunds."""
    journal = tuple(
        e for e in run.journal if not (e.kind == "chargeback_debit" and e.case_id in case_ids)
    )
    return run.model_copy(update={"journal": journal})
