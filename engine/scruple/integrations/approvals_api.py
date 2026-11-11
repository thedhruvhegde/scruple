"""Routes that authenticate the request themselves: Slack interactivity and approval links.

    POST /integrations/slack/interactions        signed("slack-signature")
    GET  /integrations/approval-links/{token}    signed("approval-link")  confirmation page
    POST /integrations/approval-links/{token}    signed("approval-link")  act

No API key or token is involved: Slack's signature or scruple's link signature is the
credential, and the identity is the approver the tenant's settings map it to
(`scruple.integrations.slack`). A tenant named in a request is only looked at if it
has integration settings, so an unauthenticated request can never create a tenant.
"""

from __future__ import annotations

import html
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from scruple.api.strict import RAW_BODY
from scruple.auth import signed
from scruple.integrations.runtime import Runtime
from scruple.integrations.signing import SLACK_SIGNATURE, SLACK_TIMESTAMP, SignatureError
from scruple.integrations.slack import (
    MAX_BODY,
    ActionRefusedError,
    claimed_tenant,
    handle_interaction,
    open_link,
    parse_interaction,
    redeem_link,
    respond,
)
from scruple.integrations.store import IntegrationStore
from scruple.service import AgentGate

PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",  # the token is in the URL: never pass it on
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
    "form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
}


async def _body(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > MAX_BODY):
        raise HTTPException(413, "body too large")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY:
            raise HTTPException(413, "body too large")
    return bytes(body)


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    doc = (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{html.escape(title)}</title>"
        "<style>body{font:16px/1.5 system-ui,sans-serif;max-width:40rem;margin:2rem auto;"
        "padding:0 1rem;background:#fff;color:#111}button{font:inherit;padding:.5rem 1rem}"
        "textarea{width:100%;font:inherit}</style></head><body>"
        f"<h1>{html.escape(title)}</h1>{body}</body></html>"
    )
    return HTMLResponse(doc, status_code=status, headers=PAGE_HEADERS)


def approvals_router(
    gate_for: Callable[[str], AgentGate],
    root_storage: object,
    runtime: Runtime | Callable[[], Runtime] | None = None,
    now: Callable[[], float] = time.time,
) -> APIRouter:
    router = APIRouter(prefix="/integrations", tags=["integrations"])

    def rt() -> Runtime:
        if runtime is None:
            return Runtime.from_env()
        return runtime if isinstance(runtime, Runtime) else runtime()

    def known(tenant: str) -> IntegrationStore | None:
        if tenant not in IntegrationStore.tenants(root_storage):
            return None
        view = root_storage.for_tenant(tenant)  # type: ignore[attr-defined]
        return IntegrationStore(view)

    @router.post(
        "/slack/interactions",
        openapi_extra={RAW_BODY: True},
        dependencies=[signed("slack-signature")],
    )
    async def slack_interactions(request: Request) -> JSONResponse:
        body = await _body(request)
        try:
            payload = parse_interaction(body)
        except SignatureError as exc:
            raise HTTPException(400, str(exc)) from None
        found = claimed_tenant(payload)
        if found is None:
            return JSONResponse({"ok": True})  # not our button (a link button's click)
        store = known(found[0])
        if store is None:
            raise HTTPException(401, "signature rejected")
        runtime_now = rt()
        try:
            text = handle_interaction(
                gate_for,
                store,
                runtime_now,
                body,
                request.headers.get(SLACK_TIMESTAMP),
                request.headers.get(SLACK_SIGNATURE),
                payload,
                found[1],
                now(),
            )
        except SignatureError as exc:
            raise HTTPException(401, f"signature rejected: {exc}") from None
        except ActionRefusedError as exc:
            text = f"Not done: {exc}"
        respond(runtime_now, payload, text)
        return JSONResponse({"ok": True, "text": text})

    def _open(token: str) -> tuple[str, dict[str, Any], IntegrationStore]:
        try:
            kid, claims = open_link(rt(), token, now())
        except SignatureError as exc:
            raise _RefusedError(str(exc)) from None
        store = known(claims["t"])
        if store is None:
            raise _RefusedError("this link is not valid")
        if store.nonce_used("link:" + claims["n"]):
            raise _RefusedError("this link was already used")
        return kid, claims, store

    @router.get("/approval-links/{token}", dependencies=[signed("approval-link")])
    def link_page(token: str) -> HTMLResponse:
        try:
            _, claims, _ = _open(token)
        except _RefusedError as exc:
            return _page("Link not usable", f"<p>{html.escape(str(exc))}</p>", 410)
        verb = "Approve" if claims["a"] == "approve" else "Reject"
        reason = (
            "<p><label>Reason (required)<br><textarea name='reason' rows='3' required "
            "maxlength='500'></textarea></label></p>"
            if claims["a"] == "reject"
            else ""
        )
        body = (
            f"<p>{html.escape(verb)} <b>{html.escape(claims['r'])}</b> version "
            f"{int(claims['v'])} (content hash <code>{html.escape(claims['h'][:16])}</code>) "
            f"as <b>{html.escape(claims['s'])}</b>.</p>"
            "<p>Nothing has happened yet. The link works once.</p>"
            f"<form method='post'><input type='hidden' name='confirm' value='yes'>{reason}"
            f"<button type='submit'>{html.escape(verb)}</button></form>"
        )
        return _page(f"{verb} {claims['r']} v{int(claims['v'])}", body)

    @router.post(
        "/approval-links/{token}",
        openapi_extra={RAW_BODY: True},
        dependencies=[signed("approval-link")],
    )
    async def link_act(token: str, request: Request) -> HTMLResponse:
        form = parse_qs((await _body(request)).decode("utf-8", "replace"), max_num_fields=5)
        if form.get("confirm") != ["yes"]:
            return _page("Not done", "<p>Confirm from the link's page.</p>", 400)
        reason = (form.get("reason") or [""])[0].strip()
        try:
            kid, claims, store = _open(token)
            if claims["a"] == "reject" and not reason:
                raise _RefusedError("a rejection needs a reason")
            gate = gate_for(store.tenant)
            outcome = redeem_link(gate, store, rt(), kid, claims, reason or None)
        except (_RefusedError, ActionRefusedError) as exc:
            return _page("Not done", f"<p>{html.escape(str(exc))}</p>", 409)
        return _page("Done", f"<p>{html.escape(outcome)}</p>")

    return router


class _RefusedError(Exception):
    pass


__all__ = ["approvals_router"]
