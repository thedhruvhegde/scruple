"""Golden episodes: canonical JSON traces of every workflow for fixed seeds (164).

`episodes.TRACES` builds each trace; `tests/test_golden_episodes.py` compares them
byte for byte with the files here. After an intended behaviour change, regenerate
with `uv run python -m tests.golden.regen` and review the diff.
"""
