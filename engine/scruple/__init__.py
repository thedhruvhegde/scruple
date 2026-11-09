"""scruple: simulation and hack-resistant verifiers for agents that move money."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from scruple.agents.sdk import Scorecard, evaluate

__all__ = ["Scorecard", "evaluate"]


def __getattr__(name: str) -> Any:
    # Lazy, so `import scruple` stays cheap for the gate, API and CLI.
    if name in __all__:
        from scruple.agents import sdk

        return getattr(sdk, name)
    raise AttributeError(f"module 'scruple' has no attribute {name!r}")
