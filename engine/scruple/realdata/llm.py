"""Answer real-data prompts with a hosted model through the existing agent runners.

Reuses `scruple.agents.runners` (HTTP client, auth headers, retries with backoff,
timeouts, model defaults) but makes a single plain completion per prompt, with no
tools: the reply text is the answer, graded strictly by the task. No live call is
made in tests; they pass an `httpx.MockTransport`.

A failed request (HTTP error, timeout, malformed response) is recorded with an
empty answer and an `error`, so it is graded as malformed (wrong), never dropped.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict

from scruple.agents.runners.anthropic import AnthropicRunner
from scruple.agents.runners.anthropic import default_model as anthropic_default
from scruple.agents.runners.base import Runner, RunnerConfig, RunnerHTTPError
from scruple.agents.runners.openai import OpenAIRunner

Provider = Literal["anthropic", "openai"]


class PromptRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    task: str
    split: str
    system: str
    prompt: str


class AnswerRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    answer: str
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None


def make_runner(
    provider: Provider,
    *,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    transport: httpx.BaseTransport | None = None,
    max_tokens: int = 256,
    sleep: Callable[[float], None] | None = None,
) -> Runner:
    if provider == "anthropic":
        cfg = RunnerConfig(model=model or anthropic_default(), max_tokens=max_tokens, temperature=0)
        kw: dict[str, Any] = {"api_key": api_key, "transport": transport, "sleep": sleep}
        if base_url:
            kw["base_url"] = base_url
        return AnthropicRunner(cfg, **kw)
    if provider == "openai":
        if not model:
            raise ValueError("the OpenAI runner has no default model: pass --model")
        cfg = RunnerConfig(model=model, max_tokens=max_tokens, temperature=0)
        kw = {"api_key": api_key, "transport": transport, "sleep": sleep}
        if base_url:
            kw["base_url"] = base_url
        return OpenAIRunner(cfg, **kw)
    raise ValueError(f"unknown provider {provider!r}")


def _complete(runner: Runner, system: str, prompt: str) -> tuple[str, int, int]:
    cfg = runner.config
    if isinstance(runner, AnthropicRunner):
        body: dict[str, Any] = {
            "model": cfg.model,
            "max_tokens": cfg.max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
        }
        if cfg.temperature is not None:
            body["temperature"] = cfg.temperature
        data = runner._post("/v1/messages", body)
        content = data["content"]
        if not isinstance(content, list):
            raise ValueError("response content is not a list")
        text = "".join(str(b.get("text", "")) for b in content if b.get("type") == "text")
        usage = data.get("usage") or {}
        return text, int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))
    if isinstance(runner, OpenAIRunner):
        body = {
            "model": cfg.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            runner._max_tokens_field: cfg.max_tokens,
        }
        if cfg.temperature is not None:
            body["temperature"] = cfg.temperature
        data = runner._post("/chat/completions", body)
        message = data["choices"][0]["message"]
        text = str(message.get("content") or "")
        usage = data.get("usage") or {}
        return text, int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))
    raise TypeError(f"unsupported runner {type(runner).__name__}")


def answer_prompts(runner: Runner, prompts: Iterable[PromptRecord]) -> list[AnswerRecord]:
    out: list[AnswerRecord] = []
    for p in prompts:
        try:
            text, tin, tout = _complete(runner, p.system, p.prompt)
        except (httpx.HTTPError, RunnerHTTPError) as exc:
            out.append(AnswerRecord(id=p.id, answer="", model=runner.config.model, error=str(exc)))
            continue
        except (ValueError, LookupError, TypeError, AttributeError) as exc:
            out.append(AnswerRecord(id=p.id, answer="", model=runner.config.model, error=repr(exc)))
            continue
        out.append(
            AnswerRecord(
                id=p.id,
                answer=text,
                model=runner.config.model,
                input_tokens=tin,
                output_tokens=tout,
            )
        )
    return out
