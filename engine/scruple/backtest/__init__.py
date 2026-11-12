"""Backtest a rule or agent on a partner's own dispute history (roadmap 154).

Load an export (`load_stripe`, `load_adyen`, `load_tabular`), pick a candidate
(`load_candidate`), and `run_backtest`. Every case is decided from its decision-time
view only (`DecisionView`; a peek at a later fact raises `LookAheadError`). Outcomes the
history did not produce are estimated from the calibrated simulator and labelled
`estimated`; the rest are `observed`. An LLM agent (the partner's endpoint, or a model
with the partner's prompt) is a candidate too: `scruple.backtest.llm`. See
docs/BACKTEST.md.
"""

from scruple.backtest.candidate import (
    Candidate,
    CandidateDecision,
    ModuleCandidate,
    RuleCandidate,
    load_candidate,
    rule_candidate,
)
from scruple.backtest.engine import (
    AgentFailure,
    BacktestConfig,
    BacktestReport,
    CaseLine,
    ReasonResult,
    run_backtest,
)
from scruple.backtest.history import (
    HistoricalCase,
    History,
    from_records,
    load_adyen,
    load_stripe,
    load_tabular,
)
from scruple.backtest.llm import (
    AgentReply,
    AgentRequest,
    AgentUsage,
    HttpAgent,
    LLMCandidate,
    ModelAgent,
    ReplyCache,
)
from scruple.backtest.report import to_json, to_markdown, to_pdf, write_report
from scruple.backtest.view import DecisionView, LookAheadError, PaymentIndex, build_view

__all__ = [
    "AgentFailure",
    "AgentReply",
    "AgentRequest",
    "AgentUsage",
    "BacktestConfig",
    "BacktestReport",
    "Candidate",
    "CandidateDecision",
    "CaseLine",
    "DecisionView",
    "HistoricalCase",
    "History",
    "HttpAgent",
    "LLMCandidate",
    "LookAheadError",
    "ModelAgent",
    "ModuleCandidate",
    "PaymentIndex",
    "ReasonResult",
    "ReplyCache",
    "RuleCandidate",
    "build_view",
    "from_records",
    "load_adyen",
    "load_candidate",
    "load_stripe",
    "load_tabular",
    "rule_candidate",
    "run_backtest",
    "to_json",
    "to_markdown",
    "to_pdf",
    "write_report",
]
