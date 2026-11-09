"""The frozen held-out dispute benchmark (`scruple bench`). See `scruple.bench.core`."""

from scruple.bench.core import REFERENCE_AGENTS, BenchFile, ScoreRun, build, score_agent, verify
from scruple.bench.spec import DEFAULT_CASES, BenchManifest

__all__ = [
    "DEFAULT_CASES",
    "REFERENCE_AGENTS",
    "BenchFile",
    "BenchManifest",
    "ScoreRun",
    "build",
    "score_agent",
    "verify",
]
