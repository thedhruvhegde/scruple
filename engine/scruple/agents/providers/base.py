"""The provider interface: one wire format behind a neutral tool-use turn.

A `Provider` knows how to talk to one family of APIs (Anthropic Messages, OpenAI
style Chat Completions, Gemini generateContent). It builds the request body for a
conversation held in its own native message format, parses a response into a
neutral `Turn` (text, tool calls, a normalised stop reason, token usage), and
writes tool results back in its native format. The episode loop
(`scruple.agents.providers.runner`) is the same for every provider.

Keys come from environment variables only (`Provider.key_env`); an explicit
`api_key` argument exists for tests.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from scruple.agents.dispute_tools import DISPUTE_TOOLS

StopKind = Literal["tool_use", "end", "max_tokens", "refusal"]


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    schema: dict[str, Any]


def dispute_tool_defs() -> tuple[ToolDef, ...]:
    return tuple(ToolDef(t.name, t.description, t.json_schema()) for t in DISPUTE_TOOLS)


@dataclass
class ToolCall:
    call_id: str
    name: str
    args: Any  # a dict when well formed; anything else is passed on and refused


@dataclass
class Turn:
    assistant: dict[str, Any]  # native message to append to the history
    calls: list[ToolCall]
    stop: StopKind
    text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    text_calls: bool = False  # tool calls parsed from text (no native ids to answer)


@dataclass
class ToolOutcome:
    call_id: str
    name: str
    result: dict[str, Any]
    is_error: bool


@dataclass(frozen=True)
class GenSettings:
    """Sampling settings for one request."""

    max_tokens: int
    temperature: float | None = None
    seed: int | None = None


class ProviderError(Exception):
    """A request failed for good (after retries)."""


class ProviderHTTPError(ProviderError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status


class ProviderInfo(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    wire: str
    base_url: str
    key_env: tuple[str, ...]
    key_required: bool
    seed: str | None = Field(description="request field used for a sampling seed, if any")
    docs: str


@dataclass
class Provider(ABC):
    """One API family. Subclasses set the class attributes and the wire hooks."""

    base_url: str
    api_key: str = ""
    extra_headers: dict[str, str] = field(default_factory=dict)

    name: ClassVar[str] = "provider"
    wire: ClassVar[str] = "none"
    key_env: ClassVar[tuple[str, ...]] = ()
    key_required: ClassVar[bool] = True
    seed_field: ClassVar[str | None] = None
    docs: ClassVar[str] = ""

    @classmethod
    def key_from_env(cls) -> str:
        for var in cls.key_env:
            value = os.environ.get(var, "")
            if value:
                return value
        return ""

    def check_key(self) -> None:
        if self.key_required and not self.api_key:
            env = " or ".join(self.key_env) or "an API key"
            raise ValueError(f"{self.name} needs an API key: set {env}")

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name=self.name,
            wire=self.wire,
            base_url=self.base_url,
            key_env=self.key_env,
            key_required=self.key_required,
            seed=self.seed_field,
            docs=self.docs,
        )

    # --- wire hooks ------------------------------------------------------------------

    @abstractmethod
    def headers(self) -> dict[str, str]: ...

    @abstractmethod
    def path(self, model: str) -> str: ...

    @abstractmethod
    def body(
        self,
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        tools: tuple[ToolDef, ...] | None,
        gen: GenSettings,
    ) -> dict[str, Any]: ...

    @abstractmethod
    def parse(self, data: dict[str, Any], turn: int) -> Turn: ...

    @abstractmethod
    def user_message(self, text: str) -> dict[str, Any]: ...

    @abstractmethod
    def tool_messages(self, results: list[ToolOutcome], turn: Turn) -> list[dict[str, Any]]: ...


__all__ = [
    "GenSettings",
    "Provider",
    "ProviderError",
    "ProviderHTTPError",
    "ProviderInfo",
    "StopKind",
    "ToolCall",
    "ToolDef",
    "ToolOutcome",
    "Turn",
    "dispute_tool_defs",
]
