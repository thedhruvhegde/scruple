"""Reject unknown inputs at the HTTP boundary.

Pydantic models with `extra="forbid"` cover JSON bodies. FastAPI itself ignores
two other kinds of unexpected input, which this dependency refuses with 422:

- query parameters the route does not declare (a typo like `?rule=` instead of
  `?rule_id=` would otherwise silently return unfiltered data), and
- a request body sent to a route that takes none.

`body_models` lists every model a route accepts, so a test can check that all of
them, nested models included, forbid extra fields.
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Request
from fastapi.dependencies.models import Dependant
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel


def _walk(dep: Dependant) -> Iterator[Dependant]:
    yield dep
    for sub in dep.dependencies:
        yield from _walk(sub)


def declared_query(route: APIRoute) -> set[str]:
    return {p.alias for d in _walk(route.dependant) for p in d.query_params}


def takes_body(route: APIRoute) -> bool:
    return any(d.body_params for d in _walk(route.dependant))


def body_models(route: APIRoute) -> list[type[BaseModel]]:
    out: list[type[BaseModel]] = []
    for d in _walk(route.dependant):
        for p in d.body_params:
            ann = p.field_info.annotation
            if isinstance(ann, type) and issubclass(ann, BaseModel):
                out.append(ann)
    return out


async def reject_unknown_inputs(request: Request) -> None:
    route = request.scope.get("route")
    if not isinstance(route, APIRoute):  # pragma: no cover - mounts and plain routes
        return
    errors: list[dict[str, object]] = [
        {
            "type": "extra_forbidden",
            "loc": ("query", name),
            "msg": "Extra inputs are not permitted",
            "input": request.query_params[name],
        }
        for name in sorted(set(request.query_params) - declared_query(route))
    ]
    if not takes_body(route) and await request.body():
        errors.append(
            {
                "type": "extra_forbidden",
                "loc": ("body",),
                "msg": "This endpoint takes no request body",
                "input": None,
            }
        )
    if errors:
        raise RequestValidationError(errors)
