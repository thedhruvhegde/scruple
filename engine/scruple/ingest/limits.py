"""Size and shape limits for hostile input, and a JSON loader that enforces them.

Every external payload is treated as hostile: bounded in bytes before decoding,
bounded in nesting depth, strict UTF-8, no NaN/Infinity, no duplicate keys.
"""

from __future__ import annotations

import json
from typing import Any

from scruple.ingest.errors import InputTooLargeError, MalformedInputError

MAX_JSON_BYTES = 2 * 1024 * 1024  # a single API object or webhook body
MAX_JSON_DEPTH = 32
MAX_FILE_BYTES = 64 * 1024 * 1024  # a bank file or a CSV import


def check_size(data: bytes | str, max_bytes: int, what: str = "input") -> None:
    n = len(data.encode("utf-8")) if isinstance(data, str) else len(data)
    if n > max_bytes:
        raise InputTooLargeError(f"{what} is {n} bytes, limit is {max_bytes}")


def decode_utf8(data: bytes, what: str = "input") -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise MalformedInputError(f"{what} is not valid UTF-8 at byte {e.start}") from None


def _no_constants(name: str) -> Any:
    raise MalformedInputError(f"JSON constant {name} is not allowed")


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise MalformedInputError(f"duplicate JSON key {k[:64]!r}")
        out[k] = v
    return out


def _depth(obj: Any, limit: int) -> None:
    stack: list[tuple[Any, int]] = [(obj, 1)]
    while stack:
        cur, d = stack.pop()
        if d > limit:
            raise InputTooLargeError(f"JSON nesting deeper than {limit}")
        if isinstance(cur, dict):
            stack.extend((v, d + 1) for v in cur.values())
        elif isinstance(cur, list):
            stack.extend((v, d + 1) for v in cur)


def _bracket_depth(text: str, limit: int) -> None:
    """Cheap pre-scan so a deeply nested payload never reaches the recursive decoder."""
    depth = 0
    in_str = False
    esc = False
    for ch in text:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "[{":
            depth += 1
            if depth > limit:
                raise InputTooLargeError(f"JSON nesting deeper than {limit}")
        elif ch in "]}":
            depth -= 1


def load_json(
    data: bytes | str,
    *,
    max_bytes: int = MAX_JSON_BYTES,
    max_depth: int = MAX_JSON_DEPTH,
    what: str = "JSON",
) -> Any:
    """Parse JSON with byte, depth, constant and duplicate-key limits."""
    check_size(data, max_bytes, what)
    text = decode_utf8(data, what) if isinstance(data, bytes) else data
    _bracket_depth(text, max_depth)
    try:
        obj = json.loads(text, parse_constant=_no_constants, object_pairs_hook=_no_duplicates)
    except MalformedInputError:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError) as e:
        raise MalformedInputError(f"{what} is not valid JSON: {str(e)[:120]}") from None
    _depth(obj, max_depth)
    return obj


def load_json_object(data: bytes | str, **kw: Any) -> dict[str, Any]:
    obj = load_json(data, **kw)
    if not isinstance(obj, dict):
        raise MalformedInputError("expected a JSON object at the top level")
    return obj
