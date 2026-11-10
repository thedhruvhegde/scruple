"""Evaluate LLM agents on dispute episodes: batch runs, summaries, leaderboards.

Synthetic cases throughout. See docs/EVALUATING_AGENTS.md.
"""

from scruple.eval.batch import EvalConfig, import_calls, import_store, run_eval, write_summary
from scruple.eval.records import EvalRecord, Manifest

__all__ = [
    "EvalConfig",
    "EvalRecord",
    "Manifest",
    "import_calls",
    "import_store",
    "run_eval",
    "write_summary",
]
