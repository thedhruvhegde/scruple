"""The console as its own app: `scruple console --db ... --port 8090`.

The same router can instead be mounted under the HTTP API at /console (one line in
`scruple.api.app`). Here the app serves only the console: no OpenAPI schema, no docs
pages, `/` redirects to `/console/`.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI
from fastapi.responses import RedirectResponse

from scruple.api.strict import reject_unknown_inputs
from scruple.console.routes import PREFIX, console_router
from scruple.console.security import apply_headers
from scruple.console.sources import ConsoleSources
from scruple.logging import RequestIDMiddleware
from scruple.service import AgentGate
from scruple.version import package_version


def create_console_app(
    gate: AgentGate, sources: ConsoleSources | None = None, *, secret: bytes | None = None
) -> FastAPI:
    app = FastAPI(
        title="scruple console",
        version=package_version(),
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        dependencies=[Depends(reject_unknown_inputs)],
    )
    app.add_middleware(RequestIDMiddleware)
    app.include_router(console_router(gate, sources, secret=secret))

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return apply_headers(RedirectResponse(f"{PREFIX}/", status_code=307))

    return app


__all__ = ["create_console_app"]
