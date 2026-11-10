"""Anytime-valid statistics for promotion decisions. See docs/math/README.md.

Public API:

* sequential: ``BettingEProcess``, ``BettingConfidenceSequence``, ``sequential_test``,
  ``normalise_differences``
* stratified: ``StratifiedMonitor``, ``e_bh``, ``e_bonferroni``, ``e_closed_mean``
* planning: ``StratumSpec``, ``plan``, ``stratum_cases_needed``
* decision: ``LossModel``, ``NormalPrior``, ``composed_decision``, ``bayes_threshold``
* hacking: ``AuditModel``, ``Incentive``, ``gap_tail_bound``, ``expected_gap_bound``,
  ``minimal_audit_rate``, ``best_response``
* gate adapter: ``LossPair``, ``StatsGateConfig``, ``StatsVerdict``, ``evaluate_pairs``
* variance reduction (docs/math/09): ``StratifiedBettingEProcess``,
  ``ControlVariateEProcess``, ``PostStratifiedEProcess``, ``crn_report``,
  ``stratified_cases_needed``, ``cv_cases_needed``, ``VRGateConfig``, ``evaluate_pairs_vr``
"""

from .decision import (
    ComposedDecision,
    LossModel,
    NormalPrior,
    bayes_threshold,
    composed_decision,
    normal_posterior,
)
from .gate_adapter import LossPair, StatsGateConfig, StatsVerdict, TypeVerdict, evaluate_pairs
from .hacking import (
    AuditModel,
    Incentive,
    best_response,
    expected_gap_bound,
    gap_tail_bound,
    minimal_audit_rate,
)
from .planning import Plan, StratumSpec, plan, stratum_cases_needed
from .sequential import (
    BettingConfidenceSequence,
    BettingEProcess,
    normalise_differences,
    sequential_test,
)
from .stratified import StratifiedMonitor, e_bh, e_bonferroni, e_closed_mean
from .variance import (
    ControlVariateEProcess,
    CRNReport,
    PostStratifiedEProcess,
    StratifiedBettingEProcess,
    VRGateConfig,
    VRVerdict,
    crn_report,
    cv_cases_needed,
    evaluate_pairs_vr,
    stratified_cases_needed,
)

__all__ = [
    "AuditModel",
    "BettingConfidenceSequence",
    "BettingEProcess",
    "CRNReport",
    "ComposedDecision",
    "ControlVariateEProcess",
    "Incentive",
    "LossModel",
    "LossPair",
    "NormalPrior",
    "Plan",
    "PostStratifiedEProcess",
    "StatsGateConfig",
    "StatsVerdict",
    "StratifiedBettingEProcess",
    "StratifiedMonitor",
    "StratumSpec",
    "TypeVerdict",
    "VRGateConfig",
    "VRVerdict",
    "bayes_threshold",
    "best_response",
    "composed_decision",
    "crn_report",
    "cv_cases_needed",
    "e_bh",
    "e_bonferroni",
    "e_closed_mean",
    "evaluate_pairs",
    "evaluate_pairs_vr",
    "expected_gap_bound",
    "gap_tail_bound",
    "minimal_audit_rate",
    "normal_posterior",
    "normalise_differences",
    "plan",
    "sequential_test",
    "stratified_cases_needed",
    "stratum_cases_needed",
]
