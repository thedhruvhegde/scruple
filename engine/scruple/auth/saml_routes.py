"""HTTP routes for SAML sign-on, mounted next to the console when SAML is configured.

    GET  /auth/saml/login?next=/console/...   public: redirect to the IdP
    POST /auth/saml/acs                        signed("saml"): the IdP's POST; checks the
                                               assertion, sets the session cookie
    GET  /auth/saml/metadata                   public: this SP's metadata XML
    POST /auth/saml/logout                     public: clears the session cookie

The ACS authenticates the request itself (the assertion's XML signature, checked by
`scruple.auth.saml`), so it declares `signed("saml")` like the provider webhooks.
A refused login answers 401 with a short plain message and sets nothing.
"""

from __future__ import annotations

from typing import Annotated
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response

from scruple.api.strict import RAW_BODY
from scruple.auth.deps import public, signed
from scruple.auth.saml import MAX_RESPONSE_BYTES, SAMLError, SAMLServiceProvider
from scruple.auth.session import COOKIE, SCOPE_PREFIX, SessionSigner

PREFIX = "/auth/saml"
NO_STORE = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


def saml_router(sp: SAMLServiceProvider, sessions: SessionSigner) -> APIRouter:
    router = APIRouter(prefix=PREFIX, include_in_schema=False)

    @router.get("/login", dependencies=[public()])
    def login(next: Annotated[str | None, Query(max_length=512)] = None) -> Response:
        return RedirectResponse(sp.login_redirect(next), status_code=303, headers=NO_STORE)

    @router.get("/metadata", dependencies=[public()])
    def metadata() -> Response:
        return Response(sp.metadata(), media_type="application/samlmetadata+xml")

    @router.post("/acs", dependencies=[signed("saml")], openapi_extra={RAW_BODY: True})
    async def acs(request: Request) -> Response:
        ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype != "application/x-www-form-urlencoded":
            raise HTTPException(415, "the IdP must POST a form")
        body = await request.body()
        if len(body) > 2 * MAX_RESPONSE_BYTES:
            raise HTTPException(413, "SAML response too large")
        try:
            form = parse_qs(body.decode("ascii"), keep_blank_values=True)
        except UnicodeDecodeError:
            raise HTTPException(400, "form is not ASCII") from None
        if any(len(v) != 1 for v in form.values()):
            raise HTTPException(422, "a field was given more than once")
        fields = {k: v[0] for k, v in form.items()}
        try:
            who, next_path = sp.consume(fields.get("SAMLResponse"), fields.get("RelayState"))
        except SAMLError as exc:
            return PlainTextResponse(f"sign-in refused: {exc}", status_code=401, headers=NO_STORE)
        token = sessions.issue(who.subject, who.tenant, who.roles, who.idp)
        resp = RedirectResponse(next_path, status_code=303, headers=NO_STORE)
        resp.set_cookie(
            COOKIE,
            token,
            max_age=sessions.ttl_seconds,
            httponly=True,
            samesite="lax",
            secure=sp.config.acs_url.startswith("https://"),
            path=SCOPE_PREFIX,
        )
        return resp

    @router.post("/logout", dependencies=[public()])
    def logout() -> Response:
        resp = PlainTextResponse("signed out of the scruple console", headers=NO_STORE)
        resp.delete_cookie(COOKIE, path=SCOPE_PREFIX)
        return resp

    return router


__all__ = ["PREFIX", "saml_router"]
