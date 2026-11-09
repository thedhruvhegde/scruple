"""CI integration: gate a candidate rule in a pull request and report the result."""

from scruple.ci.config import CIConfig, load_candidate, load_config
from scruple.ci.runner import CIRun, run_candidate
from scruple.ci.scorecard import Scorecard, Verdict, build_scorecard, load_scorecard

__all__ = [
    "CIConfig",
    "CIRun",
    "Scorecard",
    "Verdict",
    "build_scorecard",
    "load_candidate",
    "load_config",
    "load_scorecard",
    "run_candidate",
]
