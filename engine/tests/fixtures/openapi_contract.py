"""Contract checks: a Pydantic wire model against a vendored OpenAPI schema.

For every field a model declares we check that the property exists in the schema,
that the Python type matches the schema type (integer/string/boolean/array/object/
$ref/anyOf/enum), and that fields the schema marks optional or nullable are optional
or nullable in the model too (otherwise real data would fail to parse).
"""

from __future__ import annotations

import json
import types
from pathlib import Path
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel

Schemas = dict[str, dict[str, Any]]

_SCALARS: dict[type, str] = {int: "integer", str: "string", bool: "boolean", float: "number"}


def load_schemas(path: Path) -> Schemas:
    spec = json.loads(path.read_text(encoding="utf-8"))
    schemas: Schemas = spec["components"]["schemas"]
    return schemas


def _unwrap(ann: Any) -> Any:
    while get_origin(ann) is Annotated:
        ann = get_args(ann)[0]
    return ann


def _members(ann: Any) -> list[Any]:
    ann = _unwrap(ann)
    if get_origin(ann) in (Union, types.UnionType):
        return [_unwrap(a) for a in get_args(ann)]
    return [ann]


def _ref_name(prop: dict[str, Any]) -> str | None:
    ref = prop.get("$ref")
    return ref.rsplit("/", 1)[-1] if isinstance(ref, str) else None


def _alternatives(prop: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("anyOf", "oneOf"):
        if key in prop:
            alts: list[dict[str, Any]] = prop[key]
            return alts
    return [prop]


def _type_matches(ann: Any, prop: dict[str, Any], names: dict[type, str]) -> str | None:
    """None when `ann` (a single, non-None type) fits `prop`; otherwise a reason."""
    origin = get_origin(ann)
    if ann is Any:
        return None
    if isinstance(ann, type) and issubclass(ann, BaseModel):
        want = names.get(ann)
        got = _ref_name(prop)
        return None if want is not None and got == want else f"expected $ref {want}, got {got}"
    if origin is Literal:
        if prop.get("type") != "string":
            return "Literal needs a string schema"
        enum = set(prop.get("enum", []))
        missing = set(get_args(ann)) - enum
        return None if enum and not missing else f"Literal values {missing} not in enum"
    if origin is tuple:
        if prop.get("type") != "array":
            return "tuple needs an array schema"
        return _type_matches(_unwrap(get_args(ann)[0]), prop["items"], names)
    if origin is dict or ann is dict:
        ok = prop.get("type") == "object" or "$ref" in prop
        return None if ok else "dict needs an object schema"
    if ann in _SCALARS:
        return None if prop.get("type") == _SCALARS[ann] else f"{ann} vs {prop.get('type')}"
    return f"unsupported annotation {ann!r}"


def check_model(
    model: type[BaseModel], schema: str, schemas: Schemas, names: dict[type, str]
) -> list[str]:
    """All contract violations of `model` against `schemas[schema]` (empty list = ok)."""
    problems: list[str] = []
    sch = schemas[schema]
    props: dict[str, Any] = sch.get("properties", {})
    required = set(sch.get("required", []))
    for fname, finfo in model.model_fields.items():
        name = finfo.alias or fname
        where = f"{model.__name__}.{name}"
        if name not in props:
            problems.append(f"{where}: not in schema {schema}")
            continue
        prop = props[name]
        members = _members(finfo.annotation)
        accepts_none = type(None) in members
        if name not in required and finfo.is_required():
            problems.append(f"{where}: optional in spec but required in model")
        if prop.get("nullable") and not accepts_none:
            problems.append(f"{where}: nullable in spec but model rejects None")
        alts = _alternatives(prop)
        for m in members:
            if m is type(None):
                continue
            reasons = [_type_matches(m, alt, names) for alt in alts]
            if all(r is not None for r in reasons):
                problems.append(f"{where}: {m!r} fits no schema alternative ({reasons[0]})")
    return problems
