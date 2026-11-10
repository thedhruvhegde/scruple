"""Provider-agnostic model runners for dispute episodes.

`make_provider(name)` gives a `Provider` for one API family (Anthropic, OpenAI,
Gemini, Mistral, or any OpenAI-compatible server); `ProviderRunner` drives a
`DisputeEpisode` with it under a strategy's `ApiPlan`, with retries, a shared rate
limit and token and cost accounting. `FakeModel` answers in every wire format
without a network, for tests and for checking the harness end to end.
"""

from scruple.agents.providers.base import (
    GenSettings,
    Provider,
    ProviderError,
    ProviderHTTPError,
    ToolCall,
    ToolDef,
    ToolOutcome,
    Turn,
    dispute_tool_defs,
)
from scruple.agents.providers.fake import FakeModel
from scruple.agents.providers.http import ProviderClient, RateLimiter
from scruple.agents.providers.registry import PROVIDERS, make_provider, provider_names
from scruple.agents.providers.runner import (
    TRANSIENT_STOPS,
    CallLog,
    EpisodeRun,
    Price,
    ProviderRunner,
    RunSettings,
)

__all__ = [
    "PROVIDERS",
    "TRANSIENT_STOPS",
    "CallLog",
    "EpisodeRun",
    "FakeModel",
    "GenSettings",
    "Price",
    "Provider",
    "ProviderClient",
    "ProviderError",
    "ProviderHTTPError",
    "ProviderRunner",
    "RateLimiter",
    "RunSettings",
    "ToolCall",
    "ToolDef",
    "ToolOutcome",
    "Turn",
    "dispute_tool_defs",
    "make_provider",
    "provider_names",
]
