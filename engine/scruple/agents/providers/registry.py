"""Provider registry: name to provider class and default endpoint.

Keys are read from environment variables only (each provider's `key_env`). Base
URLs can be overridden per run (`base_url`); the generic `openai-compatible`
provider has no default and needs one (`--base-url`, or
`SCRUPLE_OPENAI_COMPAT_BASE_URL`).
"""

from __future__ import annotations

import os

from scruple.agents.providers.anthropic import BASE_URL as ANTHROPIC_URL
from scruple.agents.providers.anthropic import AnthropicProvider
from scruple.agents.providers.base import Provider
from scruple.agents.providers.chat import (
    MISTRAL_URL,
    OPENAI_URL,
    CompatProvider,
    GroqProvider,
    MistralProvider,
    OllamaProvider,
    OpenAIProvider,
    TogetherProvider,
    VLLMProvider,
)
from scruple.agents.providers.gemini import BASE_URL as GEMINI_URL
from scruple.agents.providers.gemini import GeminiProvider

COMPAT_URL_ENV = "SCRUPLE_OPENAI_COMPAT_BASE_URL"

PROVIDERS: dict[str, tuple[type[Provider], str | None]] = {
    "anthropic": (AnthropicProvider, ANTHROPIC_URL),
    "openai": (OpenAIProvider, OPENAI_URL),
    "gemini": (GeminiProvider, GEMINI_URL),
    "mistral": (MistralProvider, MISTRAL_URL),
    "openai-compatible": (CompatProvider, None),
    "vllm": (VLLMProvider, "http://localhost:8000/v1"),
    "ollama": (OllamaProvider, "http://localhost:11434/v1"),
    "together": (TogetherProvider, "https://api.together.xyz/v1"),
    "groq": (GroqProvider, "https://api.groq.com/openai/v1"),
}


def provider_names() -> tuple[str, ...]:
    return tuple(PROVIDERS)


def make_provider(
    name: str,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    require_key: bool = True,
) -> Provider:
    """Build a provider. `api_key=None` reads the provider's environment variables."""
    try:
        cls, default_url = PROVIDERS[name]
    except KeyError:
        raise ValueError(f"unknown provider {name!r}; choose from {', '.join(PROVIDERS)}") from None
    url = base_url or (os.environ.get(COMPAT_URL_ENV) if name == "openai-compatible" else None)
    url = url or default_url
    if not url:
        raise ValueError(f"{name} needs a base URL: pass --base-url or set {COMPAT_URL_ENV}")
    key = api_key if api_key is not None else cls.key_from_env()
    provider = cls(base_url=url, api_key=key)
    if require_key:
        provider.check_key()
    return provider


__all__ = ["COMPAT_URL_ENV", "PROVIDERS", "make_provider", "provider_names"]
