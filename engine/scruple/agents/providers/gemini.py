"""Google Gemini API, REST `generateContent` with function calling.

Shapes follow https://ai.google.dev/api/generate-content and
https://ai.google.dev/gemini-api/docs/function-calling: `contents` of `user` and
`model` turns made of `parts` (`text`, `functionCall`, `functionResponse`),
`systemInstruction`, `tools[].functionDeclarations`, `generationConfig`
(`maxOutputTokens`, `temperature`, `seed`), `candidates[].finishReason` and
`usageMetadata`. Model turns are echoed back verbatim, so any part metadata the
API attaches (thought signatures) is preserved.

Function parameters use the API's OpenAPI-subset `Schema`, so the tools' JSON
schemas are translated by `to_gemini_schema`: `anyOf [X, null]` becomes `X` with
`nullable`, keys outside the subset are dropped (`additionalProperties`, defaults,
`exclusiveMinimum` turned into `minimum` for integers) and string formats other
than `enum` and `date-time` move into the description.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, ClassVar

from scruple.agents.providers.base import (
    GenSettings,
    Provider,
    StopKind,
    ToolCall,
    ToolDef,
    ToolOutcome,
    Turn,
)

BASE_URL = "https://generativelanguage.googleapis.com"

_KEEP = frozenset(
    {
        "description",
        "nullable",
        "enum",
        "required",
        "minItems",
        "maxItems",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "maxProperties",
        "minProperties",
    }
)
_FORMATS = frozenset({"enum", "date-time"})
_REFUSALS = frozenset(
    {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "IMAGE_SAFETY"}
)


def to_gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Translate a JSON schema (no `$ref`s) to the Gemini `Schema` subset."""
    s = dict(schema)
    any_of = s.pop("anyOf", None)
    nullable = False
    if isinstance(any_of, list):
        options = [o for o in any_of if o.get("type") != "null"]
        nullable = len(options) < len(any_of)
        if len(options) == 1:
            merged = {**options[0], **{k: v for k, v in s.items() if k != "default"}}
            single = to_gemini_schema(merged)
            if nullable:
                single["nullable"] = True
            return single
        s.setdefault("type", "string")  # pragma: no cover - not in the dispute tools
    out: dict[str, Any] = {}
    typ = s.get("type")
    if isinstance(typ, str):
        out["type"] = typ.upper()
    for key in _KEEP:
        if key in s:
            out[key] = s[key]
    fmt = s.get("format")
    if isinstance(fmt, str):
        if fmt in _FORMATS:
            out["format"] = fmt
        else:
            hint = f"({fmt})"
            out["description"] = f"{out['description']} {hint}" if "description" in out else hint
    if "exclusiveMinimum" in s and typ == "integer":
        out["minimum"] = int(s["exclusiveMinimum"]) + 1
    if isinstance(s.get("properties"), dict):
        out["properties"] = {k: to_gemini_schema(v) for k, v in s["properties"].items()}
    if isinstance(s.get("items"), dict):
        out["items"] = to_gemini_schema(s["items"])
    if nullable:
        out["nullable"] = True
    return out


def function_declarations(tools: tuple[ToolDef, ...]) -> list[dict[str, Any]]:
    out = []
    for t in tools:
        decl: dict[str, Any] = {"name": t.name, "description": t.description}
        if t.schema.get("properties"):  # tools without arguments omit `parameters`
            decl["parameters"] = to_gemini_schema(t.schema)
        out.append(decl)
    return out


@dataclass
class GeminiProvider(Provider):
    name: ClassVar[str] = "gemini"
    wire: ClassVar[str] = "gemini-generate-content"
    key_env: ClassVar[tuple[str, ...]] = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
    seed_field: ClassVar[str | None] = "generationConfig.seed"
    docs: ClassVar[str] = "https://ai.google.dev/api/generate-content"

    def headers(self) -> dict[str, str]:
        return {"x-goog-api-key": self.api_key, **self.extra_headers}

    def path(self, model: str) -> str:
        name = model if model.startswith(("models/", "tunedModels/")) else f"models/{model}"
        return f"/v1beta/{name}:generateContent"

    def body(
        self,
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        tools: tuple[ToolDef, ...] | None,
        gen: GenSettings,
    ) -> dict[str, Any]:
        config: dict[str, Any] = {"maxOutputTokens": gen.max_tokens}
        if gen.temperature is not None:
            config["temperature"] = gen.temperature
        if gen.seed is not None:
            config["seed"] = gen.seed
        body: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": messages,
            "generationConfig": config,
        }
        if tools:
            body["tools"] = [{"functionDeclarations": function_declarations(tools)}]
            body["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO"}}
        return body

    def parse(self, data: dict[str, Any], turn: int) -> Turn:
        usage = data.get("usageMetadata") or {}
        tin = int(usage.get("promptTokenCount", 0) or 0)
        tout = int(usage.get("candidatesTokenCount", 0) or 0) + int(
            usage.get("thoughtsTokenCount", 0) or 0
        )
        candidates = data.get("candidates") or []
        if not candidates:
            if (data.get("promptFeedback") or {}).get("blockReason"):
                block = str(data["promptFeedback"]["blockReason"])
                return Turn(
                    assistant={"role": "model", "parts": [{"text": ""}]},
                    calls=[],
                    stop="refusal",
                    text=f"blocked: {block}",
                    input_tokens=tin,
                    output_tokens=tout,
                )
            raise ValueError("response has no candidates")
        cand = candidates[0]
        content = cand.get("content") or {}
        parts = content.get("parts") or []
        if not isinstance(parts, list):
            raise ValueError("candidate parts is not a list")
        calls: list[ToolCall] = []
        texts: list[str] = []
        for i, p in enumerate(parts):
            if "functionCall" in p:
                fc = p["functionCall"]
                call_id = str(fc.get("id") or f"gemini_{turn}_{i}")
                calls.append(ToolCall(call_id, str(fc.get("name")), fc.get("args") or {}))
            elif "text" in p and not p.get("thought"):
                texts.append(str(p["text"]))
        finish = str(cand.get("finishReason", "STOP"))
        stop: StopKind = "end"
        if calls:
            stop = "tool_use"
        elif finish == "MAX_TOKENS":
            stop = "max_tokens"
        elif finish in _REFUSALS:
            stop = "refusal"
        return Turn(
            assistant={"role": "model", "parts": parts or [{"text": ""}]},
            calls=calls,
            stop=stop,
            text="".join(texts),
            input_tokens=tin,
            output_tokens=tout,
        )

    def user_message(self, text: str) -> dict[str, Any]:
        return {"role": "user", "parts": [{"text": text}]}

    def tool_messages(self, results: list[ToolOutcome], turn: Turn) -> list[dict[str, Any]]:
        native_ids = {c.call_id for c in turn.calls if not c.call_id.startswith("gemini_")}
        parts = []
        for r in results:
            response = r.result if r.is_error else {"result": r.result}
            fr: dict[str, Any] = {"name": r.name, "response": json.loads(json.dumps(response))}
            if r.call_id in native_ids:
                fr["id"] = r.call_id
            parts.append({"functionResponse": fr})
        return [{"role": "user", "parts": parts}]


__all__ = ["BASE_URL", "GeminiProvider", "function_declarations", "to_gemini_schema"]
