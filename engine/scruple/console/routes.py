"""The console router: server-rendered pages over `AgentGate`, plus downloads.

Mount it under the HTTP API (`app.include_router(console_router(gates))`) or run it
on its own (`scruple console`, see `scruple.console.app`). Every route declares the
permission it needs with `requires(...)` (scruple/console/permissions.py, the
engine's RBAC); the static files are `public()`. Each request sees only its caller's
tenant: `gate` is either one gate or a function from tenant id to that tenant's gate.
Every POST carries a CSRF token; every response carries a strict CSP. Approvals go
through the service, which enforces named approvers and separation of duties; the
console only relays its refusals.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from collections.abc import Callable
from importlib.resources import files
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from scruple.api.strict import RAW_BODY
from scruple.auth import public
from scruple.console import fmt
from scruple.console.permissions import Principal, requires
from scruple.console.security import Csrf, apply_headers, load_secret, read_form
from scruple.console.sources import NAME_PATTERN, AgentRun, ConsoleSources
from scruple.console.views import (
    RuleDiff,
    agent_diff,
    agent_summary,
    case_view,
    episode_rows,
    inbox,
    payload_text,
    rule_diff,
    version_detail,
    version_rows,
)
from scruple.gate.approval import ApprovalError
from scruple.registry import InvalidTransitionError, RuleNotFoundError, RuleVersion
from scruple.registry.models import RULE_ID_PATTERN
from scruple.service import AgentGate

PREFIX = "/console"
STATIC: dict[str, str] = {
    "console.css": "text/css; charset=utf-8",
    "console.js": "text/javascript; charset=utf-8",
    "fonts/martian-mono.woff2": "font/woff2",
    "fonts/plex-sans-condensed-400.woff2": "font/woff2",
    "fonts/plex-sans-condensed-500.woff2": "font/woff2",
    "fonts/plex-sans-condensed-600.woff2": "font/woff2",
    "fonts/OFL-martian-mono.txt": "text/plain; charset=utf-8",
    "fonts/OFL-ibm-plex-sans-condensed.txt": "text/plain; charset=utf-8",
}
REF = re.compile(r"^(?P<rule>[a-z0-9][a-z0-9-]{1,63})@v(?P<v>[1-9][0-9]{0,8})$")
Message = Literal["approved", "activated", "rejected", "requested"]
MESSAGES: dict[str, str] = {
    "approved": "Approval recorded. The policy needs more approvals before activation.",
    "activated": "Approval recorded. The version is now active.",
    "rejected": "Rejection recorded. The version is rejected (final).",
    "requested": "Approval requested. Named approvers can now approve or reject.",
}

Name = Annotated[str, Path(pattern=NAME_PATTERN)]
RuleId = Annotated[str, Path(pattern=RULE_ID_PATTERN)]
Version = Annotated[int, Path(ge=1, le=2**63 - 1)]
EpisodeId = Annotated[str, Path(pattern=r"^ep-[0-9a-f]{12}$")]
DecisionId = Annotated[str, Path(pattern=r"^dec-[0-9]{5,12}$")]
Ref = Annotated[str | None, Query(max_length=80)]


class _Form(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    csrf: str = Field(min_length=64, max_length=64)
    # No `actor` field: who acts is the authenticated caller, never a typed-in name.


class DecisionForm(_Form):
    reason: str = Field(min_length=3, max_length=500)


class RequestForm(_Form):
    pass


def environment() -> Environment:
    env = Environment(
        loader=PackageLoader("scruple.console", "templates"),
        autoescape=select_autoescape(default=True, default_for_string=True),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(fmt.FILTERS)
    return env


def _single(only: AgentGate) -> Callable[[str], AgentGate]:
    """One gate serves one tenant; a caller from any other tenant sees nothing."""

    def gates(tenant: str) -> AgentGate:
        if tenant != getattr(only.storage, "tenant", tenant):
            raise HTTPException(404, "not found")
        return only

    return gates


def console_router(
    gate: AgentGate | Callable[[str], AgentGate],
    sources: ConsoleSources | None = None,
    *,
    secret: bytes | None = None,
) -> APIRouter:
    gates: Callable[[str], AgentGate] = _single(gate) if isinstance(gate, AgentGate) else gate

    def gate_of(principal: Principal) -> AgentGate:
        return gates(principal.tenant)

    src = sources if sources is not None else ConsoleSources()
    csrf = Csrf(secret if secret is not None else load_secret())
    env = environment()
    router = APIRouter(prefix=PREFIX, include_in_schema=False)
    diffs: OrderedDict[tuple[str, ...], RuleDiff] = OrderedDict()

    def render(
        request: Request,
        template: str,
        principal: Principal,
        status: int = 200,
        **ctx: Any,
    ) -> HTMLResponse:
        sid, new = Csrf.session(request)
        html = env.get_template(template).render(
            P=PREFIX,
            csrf_token=csrf.token_for(sid),
            principal=principal,
            path=request.url.path,
            **ctx,
        )
        resp = HTMLResponse(html, status_code=status)
        if new:
            Csrf.set_cookie(request, resp, sid)
        apply_headers(resp)
        return resp

    def error(request: Request, principal: Principal, status: int, detail: str) -> HTMLResponse:
        return render(request, "error.html", principal, status, status_code=status, detail=detail)

    def download(body: bytes | str, media: str, filename: str) -> Response:
        resp = Response(body, media_type=media)
        resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        return apply_headers(resp)

    def agent_or_none(name: str) -> AgentRun | None:
        return src.agents.get(name)

    def version_or_none(gate: AgentGate, rule_id: str, version: int) -> RuleVersion | None:
        try:
            return gate.registry.get(rule_id, version)
        except RuleNotFoundError:
            return None

    def parse_ref(gate: AgentGate, ref: str | None) -> RuleVersion | None:
        m = REF.match(ref or "")
        return version_or_none(gate, m["rule"], int(m["v"])) if m else None

    # --- static files (public: stylesheet, script, fonts) --------------------------

    @router.get("/static/{name:path}", dependencies=[public()])
    def static(name: str) -> Response:
        if name not in STATIC:
            raise HTTPException(404, "not found")
        data = files("scruple.console").joinpath("static", *name.split("/")).read_bytes()
        return apply_headers(Response(data, media_type=STATIC[name]), cache=True)

    # --- 181 runs and scores -------------------------------------------------------

    @router.get("/", response_class=HTMLResponse)
    def overview(
        request: Request, principal: Annotated[Principal, requires("read")]
    ) -> HTMLResponse:
        gate = gate_of(principal)
        rows = version_rows(gate)
        return render(
            request,
            "overview.html",
            principal,
            rows=rows,
            agents=[agent_summary(run) for run in src.agents.values()],
            decisions=gate.decisions()[-12:][::-1],
            reports=len(src.reports),
            pending=len(inbox(gate, gate.clock.now()).pending),
        )

    @router.get("/agents/{name}", response_class=HTMLResponse)
    def agent_page(
        request: Request, name: Name, principal: Annotated[Principal, requires("read")]
    ) -> HTMLResponse:
        run = agent_or_none(name)
        if run is None:
            return error(request, principal, 404, f"unknown agent version {name}")
        others = [n for n in src.agents if n != name]
        return render(
            request,
            "agent.html",
            principal,
            run=run,
            summary=agent_summary(run),
            rows=episode_rows(run),
            others=others,
        )

    @router.get("/rules/{rule_id}/versions/{version}", response_class=HTMLResponse)
    def version_page(
        request: Request,
        rule_id: RuleId,
        version: Version,
        principal: Annotated[Principal, requires("read")],
    ) -> HTMLResponse:
        gate = gate_of(principal)
        if version_or_none(gate, rule_id, version) is None:
            return error(request, principal, 404, f"unknown rule version {rule_id}@v{version}")
        detail = version_detail(gate, rule_id, version)
        siblings = [v for v in gate.registry.versions(rule_id) if v.version != version]
        return render(
            request, "version.html", principal, d=detail, siblings=siblings, payload=payload_text
        )

    # --- 182 case viewer -------------------------------------------------------------

    @router.get("/agents/{name}/episodes/{episode_id}", response_class=HTMLResponse)
    def case_page(
        request: Request,
        name: Name,
        episode_id: EpisodeId,
        principal: Annotated[Principal, requires("read")],
    ) -> HTMLResponse:
        run = agent_or_none(name)
        rec = run.record(episode_id) if run is not None else None
        if run is None or rec is None:
            return error(request, principal, 404, f"unknown episode {episode_id} for {name}")
        view = case_view(rec, run.scorecards[episode_id])
        return render(request, "case.html", principal, run=run, v=view)

    # --- 183 diffs ----------------------------------------------------------------------

    @router.get("/diff/agents", response_class=HTMLResponse)
    def diff_agents(
        request: Request,
        principal: Annotated[Principal, requires("read")],
        a: Ref = None,
        b: Ref = None,
        case: Annotated[str | None, Query(pattern=r"^ep-[0-9a-f]{12}$")] = None,
    ) -> HTMLResponse:
        names = list(src.agents)
        a = a or (names[0] if names else None)
        b = b or (names[1] if len(names) > 1 else a)
        ra, rb = agent_or_none(a or ""), agent_or_none(b or "")
        if ra is None or rb is None:
            return error(request, principal, 404, "pick two known agent versions")
        d = agent_diff(ra, rb)
        pick = case or (d.changed[0].key if d.changed else None)
        side = None
        if pick is not None:
            xa, xb = ra.record(pick), rb.record(pick)
            if xa is not None and xb is not None:
                side = (case_view(xa, ra.scorecards[pick]), case_view(xb, rb.scorecards[pick]))
        return render(
            request, "diff_agents.html", principal, d=d, names=names, pick=pick, side=side
        )

    @router.get("/diff/rules", response_class=HTMLResponse)
    def diff_rules(
        request: Request,
        principal: Annotated[Principal, requires("read")],
        a: Ref = None,
        b: Ref = None,
        case: Annotated[str | None, Query(max_length=40, pattern=r"^[a-z0-9-]+$")] = None,
    ) -> HTMLResponse:
        gate = gate_of(principal)
        versions = gate.registry.all_versions()
        refs = [v.ref for v in versions]
        if a is None and b is None and versions:
            newest = max(versions, key=lambda v: v.created_at)
            same = [v for v in versions if v.rule_id == newest.rule_id and v is not newest]
            active = [v for v in gate.registry.active_rules() if v.ref != newest.ref]
            base = same[-1] if same else (active[0] if active else newest)
            a, b = base.ref, newest.ref
        va, vb = parse_ref(gate, a), parse_ref(gate, b)
        if va is None or vb is None:
            return error(request, principal, 404, "pick two known rule versions (rule@vN)")
        active_refs = tuple(sorted(v.ref for v in gate.registry.active_rules()))
        key = (principal.tenant, va.ref, va.content_hash, vb.ref, vb.content_hash, *active_refs)
        d = diffs.get(key)
        if d is None:
            d = rule_diff(gate, va, vb, src.replay_seed, src.replay_cases)
            diffs[key] = d
            while len(diffs) > 8:
                diffs.popitem(last=False)
        pick = case or (d.changed[0].key if d.changed else None)
        side = None
        if pick is not None and pick in d.cases:
            side = (d.trace(pick, 0), d.trace(pick, 1))
        return render(
            request,
            "diff_rules.html",
            principal,
            d=d,
            refs=refs,
            pick=pick,
            case=d.cases.get(pick or ""),
            side=side,
        )

    # --- 184 approvals inbox -------------------------------------------------------------

    def inbox_page(
        request: Request,
        principal: Principal,
        status: int = 200,
        message: str | None = None,
        problem: str | None = None,
    ) -> HTMLResponse:
        gate = gate_of(principal)
        box = inbox(gate, gate.clock.now())
        return render(
            request,
            "approvals.html",
            principal,
            status,
            box=box,
            message=message,
            problem=problem,
            payload=payload_text,
        )

    @router.get("/approvals", response_class=HTMLResponse)
    def approvals(
        request: Request,
        principal: Annotated[Principal, requires("read")],
        msg: Message | None = None,
    ) -> HTMLResponse:
        return inbox_page(request, principal, message=MESSAGES.get(msg or ""))

    async def form_of(
        request: Request, model: type[_Form]
    ) -> _Form | Callable[[Principal], HTMLResponse]:
        """The validated form, or a function that renders why it was refused."""
        try:
            raw = await read_form(request)
            csrf.verify(request, raw.get("csrf"))
        except HTTPException as exc:
            status, detail = exc.status_code, str(exc.detail)
            return lambda p: error(request, p, status, detail)
        try:
            return model.model_validate(raw)
        except ValidationError as exc:
            why = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
            return lambda p: inbox_page(request, p, 422, problem=why)

    async def act(
        request: Request,
        principal: Principal,
        model: type[_Form],
        fn: Callable[[_Form, str], str],
    ) -> Response:
        form = await form_of(request, model)
        if not isinstance(form, _Form):
            return form(principal)
        try:
            msg = fn(form, principal.name)
        except ApprovalError as exc:
            return inbox_page(request, principal, 403, problem=str(exc))
        except RuleNotFoundError as exc:
            return inbox_page(request, principal, 404, problem=str(exc))
        except InvalidTransitionError as exc:
            return inbox_page(request, principal, 409, problem=str(exc))
        resp = RedirectResponse(f"{PREFIX}/approvals?msg={msg}", status_code=303)
        return apply_headers(resp)

    raw = {RAW_BODY: True}  # the handler reads and checks the form body itself

    @router.post("/approvals/{rule_id}/{version}/approve", openapi_extra=raw)
    async def approve(
        request: Request,
        rule_id: RuleId,
        version: Version,
        principal: Annotated[Principal, requires("approve")],
    ) -> Response:
        gate = gate_of(principal)

        def run(form: _Form, actor: str) -> str:
            assert isinstance(form, DecisionForm)
            if gate.approval_policy() is not None:
                _, rec = gate.grant_approval(rule_id, version, actor, reason=form.reason)
                return "approved" if rec is None else "activated"
            gate.approve(rule_id, version, actor, reason=form.reason)
            return "activated"

        return await act(request, principal, DecisionForm, run)

    @router.post("/approvals/{rule_id}/{version}/reject", openapi_extra=raw)
    async def reject(
        request: Request,
        rule_id: RuleId,
        version: Version,
        principal: Annotated[Principal, requires("approve")],
    ) -> Response:
        gate = gate_of(principal)

        def run(form: _Form, actor: str) -> str:
            assert isinstance(form, DecisionForm)
            gate.reject_approval(rule_id, version, actor, form.reason)
            return "rejected"

        return await act(request, principal, DecisionForm, run)

    @router.post("/approvals/{rule_id}/{version}/request", openapi_extra=raw)
    async def request_approval(
        request: Request,
        rule_id: RuleId,
        version: Version,
        principal: Annotated[Principal, requires("approve")],
    ) -> Response:
        gate = gate_of(principal)

        def run(_form: _Form, actor: str) -> str:
            gate.request_approval(rule_id, version, actor)
            return "requested"

        return await act(request, principal, RequestForm, run)

    # --- downloads: evidence packs, decision reports ---------------------------------------

    @router.get("/rules/{rule_id}/versions/{version}/evidence.json")
    def evidence_json(
        request: Request,
        rule_id: RuleId,
        version: Version,
        principal: Annotated[Principal, requires("export")],
    ) -> Response:
        from scruple.ci.evidence import build_evidence_pack

        gate = gate_of(principal)
        if version_or_none(gate, rule_id, version) is None:
            return error(request, principal, 404, f"unknown rule version {rule_id}@v{version}")
        try:
            pack = build_evidence_pack(gate, rule_id, version)
        except ValueError as exc:
            return error(request, principal, 404, str(exc))
        return download(pack.to_json(), "application/json", f"evidence-{rule_id}-v{version}.json")

    @router.get("/rules/{rule_id}/versions/{version}/evidence.pdf")
    def evidence_pdf(
        request: Request,
        rule_id: RuleId,
        version: Version,
        principal: Annotated[Principal, requires("export")],
    ) -> Response:
        from scruple.ci.evidence import build_evidence_pack
        from scruple.ci.evidence_pdf import render_pdf

        gate = gate_of(principal)
        if version_or_none(gate, rule_id, version) is None:
            return error(request, principal, 404, f"unknown rule version {rule_id}@v{version}")
        try:
            pack = build_evidence_pack(gate, rule_id, version)
        except ValueError as exc:
            return error(request, principal, 404, str(exc))
        return download(render_pdf(pack), "application/pdf", f"evidence-{rule_id}-v{version}.pdf")

    @router.get("/decisions/{decision_id}/report.md")
    def decision_report(
        request: Request,
        decision_id: DecisionId,
        principal: Annotated[Principal, requires("export")],
    ) -> Response:
        md = gate_of(principal).report(decision_id)
        if md is None:
            return error(request, principal, 404, f"unknown decision {decision_id}")
        return download(md, "text/markdown; charset=utf-8", f"{decision_id}.md")

    # --- backtest reports ----------------------------------------------------------------------

    @router.get("/backtests", response_class=HTMLResponse)
    def backtests(
        request: Request, principal: Annotated[Principal, requires("read")]
    ) -> HTMLResponse:
        loaded = []
        for entry in src.reports.values():
            try:
                loaded.append((entry, entry.load()))
            except (OSError, ValueError):
                continue
        return render(request, "backtests.html", principal, reports=loaded)

    @router.get("/backtests/{name}", response_class=HTMLResponse)
    def backtest_page(
        request: Request, name: Name, principal: Annotated[Principal, requires("read")]
    ) -> HTMLResponse:
        entry = src.reports.get(name)
        if entry is None:
            return error(request, principal, 404, f"unknown backtest report {name}")
        try:
            report = entry.load()
        except (OSError, ValueError) as exc:
            return error(request, principal, 422, f"report {name} does not load: {exc}")
        return render(request, "backtest.html", principal, entry=entry, r=report)

    @router.get("/backtests/{name}/report.json")
    def backtest_json(
        request: Request, name: Name, principal: Annotated[Principal, requires("export")]
    ) -> Response:
        entry = src.reports.get(name)
        if entry is None:
            return error(request, principal, 404, f"unknown backtest report {name}")
        return download(entry.path.read_bytes(), "application/json", f"backtest-{name}.json")

    return router


__all__ = ["PREFIX", "STATIC", "DecisionForm", "RequestForm", "console_router", "environment"]
