"""The console as its own app: `scruple console --db ... --port 8090`.

The same router can instead be mounted under the HTTP API at /console (one line in
`scruple.api.app`). Here the app serves only the console: no OpenAPI schema, no docs
pages, `/` redirects to `/console/`.

Authentication is the HTTP API's: `AuthMiddleware` with an authenticator built from
settings (API keys in the gate's database, OIDC if configured, `X-Actor` trust only
with the development-only `auth_dev_actor_header`), and the same fail-closed route
guard, so a console route that declares no permission is unreachable.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI
from fastapi.responses import RedirectResponse

from scruple.api.strict import reject_unknown_inputs
from scruple.auth import guard_route, public
from scruple.auth.authn import Authenticator
from scruple.auth.limits import Limits
from scruple.auth.middleware import AuthMiddleware
from scruple.console.routes import PREFIX, console_router
from scruple.console.security import apply_headers
from scruple.console.sources import ConsoleSources
from scruple.logging import RequestIDMiddleware
from scruple.service import AgentGate
from scruple.version import package_version


def create_console_app(
    gate: AgentGate,
    sources: ConsoleSources | None = None,
    *,
    secret: bytes | None = None,
    auth: Authenticator | None = None,
) -> FastAPI:
    app = FastAPI(
        title="scruple console",
        version=package_version(),
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        dependencies=[Depends(guard_route), Depends(reject_unknown_inputs)],
    )
    from scruple.settings import load_settings

    settings = load_settings()
    if auth is None:
        from scruple.auth.config import build_authenticator

        auth = build_authenticator(settings, gate.storage)
    app.add_middleware(AuthMiddleware, authenticator=auth, limits=Limits.from_settings(settings))
    app.add_middleware(RequestIDMiddleware)
    app.include_router(console_router(gate, sources, secret=secret))

    @app.get("/", include_in_schema=False, dependencies=[public()])
    def root() -> RedirectResponse:
        return apply_headers(RedirectResponse(f"{PREFIX}/", status_code=307))

    return app


__all__ = ["create_console_app"]
