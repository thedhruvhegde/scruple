"""Reading policy files: YAML, TOML or JSON, strictly.

- At most `MAX_BYTES`, strict UTF-8.
- YAML through `yaml.SafeLoader` (no tags, no objects) with duplicate keys refused:
  PyYAML keeps the last of two equal keys silently, which would let a reviewer read
  one threshold while the gate uses another. JSON duplicates are refused too; TOML
  refuses them itself.
- The document must be a mapping, validated as `Policy` (unknown keys are errors).
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from scruple.policy.models import Policy

MAX_BYTES = 256 * 1024
FORMATS = ("yaml", "toml", "json")


class PolicyFileError(ValueError):
    """The file cannot be read as a policy. The message says where and why."""


class _StrictLoader(yaml.SafeLoader):
    pass


def _mapping(loader: _StrictLoader, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    out: dict[Any, Any] = {}
    for k_node, v_node in node.value:
        key = loader.construct_object(k_node, deep=deep)
        if key in out:
            raise PolicyFileError(f"duplicate key {key!r} (line {k_node.start_mark.line + 1})")
        out[key] = loader.construct_object(v_node, deep=deep)
    return out


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def _no_dupes(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise PolicyFileError(f"duplicate key {k!r}")
        out[k] = v
    return out


def format_of(name: str) -> str:
    suffix = Path(name).suffix.lower()
    if suffix in (".yaml", ".yml"):
        return "yaml"
    if suffix in (".toml", ".json"):
        return suffix[1:]
    raise PolicyFileError(f"{name}: a policy file ends in .yaml, .yml, .toml or .json")


def parse_policy(data: bytes, fmt: str, source: str = "<policy>") -> Policy:
    if fmt not in FORMATS:
        raise PolicyFileError(f"unknown policy format {fmt!r}")
    if len(data) > MAX_BYTES:
        raise PolicyFileError(f"{source}: larger than {MAX_BYTES} bytes")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise PolicyFileError(f"{source}: not UTF-8") from None
    try:
        if fmt == "yaml":
            doc = yaml.load(text, Loader=_StrictLoader)  # noqa: S506 - a SafeLoader subclass
        elif fmt == "toml":
            doc = tomllib.loads(text)
        else:
            doc = json.loads(text, object_pairs_hook=_no_dupes)
    except PolicyFileError as exc:
        raise PolicyFileError(f"{source}: {exc}") from None
    except (yaml.YAMLError, tomllib.TOMLDecodeError, json.JSONDecodeError) as exc:
        raise PolicyFileError(f"{source}: not valid {fmt}: {exc}") from None
    if not isinstance(doc, dict):
        raise PolicyFileError(f"{source}: the policy must be a mapping at the top level")
    try:
        policy = Policy.model_validate(doc)
        policy.gate_config()  # bands must exist under the policy's band edges
    except ValidationError as exc:
        lines = [
            f"  {'.'.join(str(p) for p in e['loc']) or '(top level)'}: {e['msg']}"
            for e in exc.errors()
        ]
        raise PolicyFileError(f"{source}: invalid policy\n" + "\n".join(lines)) from None
    except ValueError as exc:
        raise PolicyFileError(f"{source}: invalid policy: {exc}") from None
    return policy


def load_policy(path: str | Path) -> Policy:
    p = Path(path)
    fmt = format_of(p.name)
    try:
        size = p.stat().st_size
        if size > MAX_BYTES:
            raise PolicyFileError(f"{p}: larger than {MAX_BYTES} bytes")
        data = p.read_bytes()
    except OSError as exc:
        raise PolicyFileError(f"{p}: {exc.strerror or exc}") from None
    return parse_policy(data, fmt, str(p))


def dump_policy(policy: Policy, fmt: str = "yaml") -> str:
    """The canonical text of a policy (defaults filled in, keys sorted)."""
    body = policy.model_dump(mode="json", exclude_none=True)
    if fmt == "json":
        return json.dumps(body, indent=2, sort_keys=True) + "\n"
    if fmt == "yaml":
        out: str = yaml.safe_dump(body, sort_keys=True, allow_unicode=True)
        return out
    raise PolicyFileError(f"cannot write policies as {fmt!r} (yaml or json)")


__all__ = [
    "FORMATS",
    "MAX_BYTES",
    "PolicyFileError",
    "dump_policy",
    "format_of",
    "load_policy",
    "parse_policy",
]
