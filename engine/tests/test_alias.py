"""The old import path still works and yields the same module objects."""

import importlib
import warnings


def test_agent_gate_alias_returns_same_modules() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        old = importlib.import_module("agent_gate.sim.engine")
    new = importlib.import_module("scruple.sim.engine")
    assert old is new
