"""Slack approvals: a Block Kit message per approval request, answered from Slack.

Outgoing (`slack_notify` job, queued by the dispatcher for `approval.requested`):

- a message in the tenant's channel (`chat.postMessage`, Slack Web API, bot token as
  `Authorization: Bearer`) with the rule, version, content hash, proposer, expiry and,
  with `interactive: true`, Approve and Reject buttons;
- with `links: true`, a direct message to each mapped approver who may approve this
  version (a named approver of the policy in force, not its proposer, owner or agent)
  with that approver's own signed approve and reject links. Links are not posted in the
  channel: whoever holds a link acts as the approver it was minted for.

Incoming, two ways to act, both ending in `act`:

- Slack interactivity (`POST /integrations/slack/interactions`): verified with the
  tenant's Slack signing secret (`v0` HMAC of `v0:{timestamp}:{body}`, five-minute
  tolerance, `scruple.integrations.signing.verify_slack`), single use (the signature is
  remembered), the clicking Slack user mapped to a scruple subject by the tenant's
  settings (`SlackConfig.approvers`), whose roles must grant `approve`.
- Signed links (`/integrations/approval-links/{token}`): HMAC-signed, expiring (at the
  latest when the request does), single use (`link_nonces`), bound to tenant, rule,
  version, content hash, request and approver. `GET` only shows a confirmation page
  (link previews and mail scanners fetch links; a fetch must change nothing); the
  `POST` from that page acts. The approver must still be mapped with `approve` when the
  link is used, so removing someone from the settings revokes their links.

`act` builds a `Principal` for the mapped subject (method `slack` or `approval_link`),
refuses one without `approve`, checks that the request the button or link was made
for is still the open one (same request seq and content hash), then calls
`AgentGate.grant_approval` or `reject_approval`, which enforce the named-approver list,
separation of duties (proposer, owner and learning agent can never approve), expiry
and quorum, exactly as for the API. The action and its channel are audited as
`integration.approval_action` in the same transaction; a refusal is audited as
`integration.approval_refused`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets as pysecrets
from datetime import datetime
from typing import Any, Literal
from urllib.parse import parse_qs, quote

import httpx

from scruple.auth.models import Permission, Principal
from scruple.clock import Clock, SystemClock
from scruple.gate.approval import ApprovalError, project_approvals
from scruple.integrations.config import SecretError, SlackApprover, SlackConfig
from scruple.integrations.runtime import Runtime
from scruple.integrations.safehttp import UnsafeURLError
from scruple.integrations.signing import SignatureError, verify_slack
from scruple.integrations.store import GateEvent, IntegrationStore
from scruple.jobs.worker import PermanentJobError
from scruple.registry.state import InvalidTransitionError
from scruple.service import AgentGate

log = logging.getLogger("scruple.integrations.slack")

SLACK_KIND = "slack_notify"
MAX_ATTEMPTS = 6
TIMEOUT_S = 120
POST_MESSAGE = "https://slack.com/api/chat.postMessage"
APPROVE_ACTION = "scruple_approve"
REJECT_ACTION = "scruple_reject"
LINK_ACTION = "scruple_link"
MAX_BODY = 64 * 1024
# chat.postMessage errors worth retrying (Slack Web API docs); others are configuration.
RETRYABLE = frozenset({"ratelimited", "internal_error", "fatal_error", "service_unavailable"})

Action = Literal["approve", "reject"]


class TransientSlackError(RuntimeError):
    pass


class ActionRefusedError(PermissionError):
    """The action was not carried out; the message says why (shown to the approver)."""


# --- outgoing -----------------------------------------------------------------------


def _request_lines(d: dict[str, Any]) -> list[tuple[str, str]]:
    return [
        ("Rule", f"`{d['rule_id']}` v{d['version']}"),
        ("Content hash", f"`{str(d['content_hash'])[:16]}`"),
        ("Requested by", str(d.get("proposer", d.get("actor", "")))),
        ("Expires", str(d.get("expires_at", ""))),
    ]


def button_value(event: GateEvent) -> str:
    d = event.data
    return json.dumps(
        {
            "t": event.tenant,
            "r": d["rule_id"],
            "v": d["version"],
            "q": d["request_seq"],
            "h": d["content_hash"],
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def approval_message(
    cfg: SlackConfig,
    event: GateEvent,
    *,
    channel: str | None = None,
    links: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """The `chat.postMessage` body (Block Kit) for one approval request."""
    d = event.data
    text = f"Approval requested: {d['rule_id']} v{d['version']} (scruple gate)"
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": text[:150]}},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*{k}*\n{v}"[:2_000]} for k, v in _request_lines(d)
            ],
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "Gate evidence is from scruple's synthetic simulator. "
                    "Separation of duties and the approval quorum are enforced by scruple.",
                }
            ],
        },
    ]
    elements: list[dict[str, Any]] = []
    if links is not None:
        approve, reject = links
        elements += [
            {
                "type": "button",
                "action_id": f"{LINK_ACTION}_approve",
                "text": {"type": "plain_text", "text": "Review and approve"},
                "style": "primary",
                "url": approve,
            },
            {
                "type": "button",
                "action_id": f"{LINK_ACTION}_reject",
                "text": {"type": "plain_text", "text": "Review and reject"},
                "style": "danger",
                "url": reject,
            },
        ]
    elif cfg.interactive:
        value = button_value(event)
        elements += [
            {
                "type": "button",
                "action_id": APPROVE_ACTION,
                "text": {"type": "plain_text", "text": "Approve"},
                "style": "primary",
                "value": value,
            },
            {
                "type": "button",
                "action_id": REJECT_ACTION,
                "text": {"type": "plain_text", "text": "Reject"},
                "style": "danger",
                "value": value,
                "confirm": {
                    "title": {"type": "plain_text", "text": "Reject this version?"},
                    "text": {"type": "plain_text", "text": "Rejection is final for this version."},
                    "confirm": {"type": "plain_text", "text": "Reject"},
                    "deny": {"type": "plain_text", "text": "Cancel"},
                },
            },
        ]
    if elements:
        blocks.append({"type": "actions", "block_id": "scruple_approval", "elements": elements})
    return {"channel": channel or cfg.channel, "text": text, "blocks": blocks}


def post_message(runtime: Runtime, token: str, body: dict[str, Any]) -> dict[str, Any]:
    try:
        resp = runtime.http.post(
            POST_MESSAGE,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            content=json.dumps(body).encode(),
        )
    except httpx.HTTPError as exc:
        raise TransientSlackError(type(exc).__name__) from None
    if resp.status_code == 429 or resp.status_code >= 500:
        raise TransientSlackError(f"HTTP {resp.status_code}")
    try:
        out: dict[str, Any] = resp.json()
    except ValueError:
        raise TransientSlackError("Slack answered with something other than JSON") from None
    if out.get("ok") is True:
        return out
    error = str(out.get("error", "unknown"))
    if error in RETRYABLE:
        raise TransientSlackError(error)
    raise PermanentJobError(f"chat.postMessage: {error}")


def eligible(gate: AgentGate, cfg: SlackConfig, event: GateEvent) -> list[SlackApprover]:
    """Mapped approvers who may approve this version under the policy in force."""
    policy = gate.approval_policy()
    if policy is None:
        return []
    rv = gate.registry.get(str(event.data["rule_id"]), int(event.data["version"]))
    barred = {str(event.data.get("proposer")), rv.owner, rv.provenance.agent_id}
    return [
        a
        for a in cfg.approvers
        if policy.approver(a.subject) is not None and a.subject not in barred
    ]


def mint_link(
    runtime: Runtime,
    cfg: SlackConfig,
    event: GateEvent,
    subject: str,
    action: Action,
    now: datetime,
) -> str:
    if runtime.links is None:
        raise PermanentJobError("SCRUPLE_APPROVAL_LINK_KEYS is not set: links are off")
    d = event.data
    expires = min(
        int(now.timestamp()) + cfg.link_ttl_seconds,
        int(datetime.fromisoformat(str(d["expires_at"])).timestamp()),
    )
    token = runtime.links.mint(
        {
            "t": event.tenant,
            "r": d["rule_id"],
            "v": d["version"],
            "q": d["request_seq"],
            "h": d["content_hash"],
            "a": action,
            "s": subject,
            "e": expires,
            "n": pysecrets.token_hex(16),
        }
    )
    base = str(cfg.public_base_url).rstrip("/")
    return f"{base}/integrations/approval-links/{quote(token, safe='.-_')}"


def notify(
    storage: object, runtime: Runtime, event_id: str, *, clock: Clock | None = None
) -> dict[str, Any]:
    """Post the request to the channel, and links to each eligible approver. Each part is
    remembered once sent, so a retry after a partial failure sends only the rest."""
    clock = clock or SystemClock()
    store = IntegrationStore(storage).ensure()
    event = store.event(event_id)
    if event is None or event.type != "approval.requested":
        raise PermanentJobError(f"no approval request event {event_id}")
    cfg = store.settings().slack
    if cfg is None:
        raise PermanentJobError("Slack is not configured for this tenant")
    try:
        token = runtime.secret(cfg.bot_token_env, store.tenant)
    except SecretError as exc:
        raise PermanentJobError(str(exc)) from None
    sent: list[str] = []
    now = clock.now()

    def once(target: str, body: dict[str, Any]) -> None:
        key = f"sent:{event_id}:{target}"
        if store.nonce_used(key):
            return
        try:
            post_message(runtime, token, body)
        except UnsafeURLError as exc:  # pragma: no cover - slack.com is public
            raise PermanentJobError(str(exc)) from None
        store.consume_nonce(key, "slack-sent", now)
        sent.append(target)

    once(cfg.channel, approval_message(cfg, event))
    skipped: list[str] = []
    if cfg.links:
        gate = AgentGate(storage)  # type: ignore[arg-type]
        for a in eligible(gate, cfg, event):
            if runtime.links is None:
                skipped.append(a.subject)
                continue
            links = (
                mint_link(runtime, cfg, event, a.subject, "approve", now),
                mint_link(runtime, cfg, event, a.subject, "reject", now),
            )
            once(
                a.slack_user_id, approval_message(cfg, event, channel=a.slack_user_id, links=links)
            )
    return {"event_id": event_id, "sent": sent, "links_skipped": skipped}


# --- acting -------------------------------------------------------------------------


def _principal(tenant: str, a: SlackApprover, method: Any, credential: str) -> Principal:
    return Principal(
        subject=a.subject,
        tenant=tenant,
        permissions=a.permissions,
        roles=a.roles,
        method=method,
        credential_id=credential[:512],
    )


def act(
    gate: AgentGate,
    principal: Principal,
    action: Action,
    *,
    rule_id: str,
    version: int,
    request_seq: int,
    content_hash: str,
    via: dict[str, Any],
    nonce: str,
    reason: str | None = None,
) -> str:
    """Carry out one approval action from Slack or a link. Returns a short outcome
    message; raises ActionRefusedError (audited) when it is not carried out."""
    store = IntegrationStore(gate.storage).ensure()
    base = {
        "action": action,
        "rule_id": rule_id,
        "version": version,
        "request_seq": request_seq,
        "subject": principal.subject,
        **via,
    }
    try:
        if not principal.can(Permission.APPROVE):
            raise ActionRefusedError(f"{principal.subject} does not hold the approve permission")
        with gate.storage.transaction():
            request, _ = project_approvals(gate.audit.entries(), rule_id, version)
            if request is None or request.seq != request_seq:
                raise ActionRefusedError("this approval request is no longer open")
            if request.content_hash != content_hash:
                raise ActionRefusedError("the rule changed since this request was made")
            if not store.consume_nonce(nonce, str(via.get("via", "")), gate.clock.now()):
                raise ActionRefusedError("this link or button press was already used")
            if action == "approve":
                status, rec = gate.grant_approval(rule_id, version, principal.subject)
                outcome = (
                    f"approved; activated by decision {rec.decision.decision_id}"
                    if rec is not None
                    else f"approved; {status.missing} more approval(s) needed"
                )
            else:
                text = (reason or "").strip() or f"rejected from {via.get('via')}"
                rec = gate.reject_approval(rule_id, version, principal.subject, text[:500])
                outcome = f"rejected; decision {rec.decision.decision_id}"
            gate.record_runtime_event(
                "integration.approval_action", principal.subject, {**base, "outcome": outcome}
            )
            return outcome
    except (ActionRefusedError, ApprovalError, LookupError, InvalidTransitionError) as exc:
        message = str(exc) or type(exc).__name__
        gate.record_runtime_event(
            "integration.approval_refused", principal.subject, {**base, "reason": message[:300]}
        )
        raise ActionRefusedError(message) from None


# --- Slack interactivity ----------------------------------------------------------------


def parse_interaction(body: bytes) -> dict[str, Any]:
    """The `payload` JSON of an interactivity request (form encoded)."""
    if len(body) > MAX_BODY:
        raise SignatureError("body too large")
    try:
        form = parse_qs(body.decode("utf-8"), strict_parsing=False, max_num_fields=20)
        payload = json.loads(form["payload"][0])
    except (UnicodeDecodeError, KeyError, IndexError, json.JSONDecodeError, ValueError):
        raise SignatureError("not a Slack interactivity payload") from None
    if not isinstance(payload, dict):
        raise SignatureError("not a Slack interactivity payload")
    return payload


def claimed_tenant(payload: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """The tenant our button names (unverified until the tenant's secret checks the
    signature), and the button's action, or None for an action that is not ours."""
    for a in payload.get("actions") or []:
        if not isinstance(a, dict) or a.get("action_id") not in (APPROVE_ACTION, REJECT_ACTION):
            continue
        try:
            value = json.loads(str(a.get("value", "")))
        except json.JSONDecodeError:
            return None
        if isinstance(value, dict) and isinstance(value.get("t"), str):
            return value["t"], {**value, "action_id": a["action_id"]}
    return None


def handle_interaction(
    gate_for: Any,
    tenant_store: IntegrationStore,
    runtime: Runtime,
    body: bytes,
    timestamp: str | None,
    signature: str | None,
    payload: dict[str, Any],
    button: dict[str, Any],
    now: float,
) -> str:
    """Verify, map and act. Raises SignatureError (401) or ActionRefusedError (shown)."""
    cfg = tenant_store.settings().slack
    if cfg is None:
        raise SignatureError("Slack is not configured for this tenant")
    try:
        keys = [runtime.secret(name, tenant_store.tenant) for name in cfg.signing_secret_envs]
    except SecretError:
        raise SignatureError("the tenant's Slack signing secret is not set") from None
    verify_slack(keys, timestamp, signature, body, now)
    team = (payload.get("team") or {}).get("id")
    user = (payload.get("user") or {}).get("id")
    if team != cfg.team_id:
        raise SignatureError("request from another Slack workspace")
    gate: AgentGate = gate_for(tenant_store.tenant)
    approver = cfg.approver_for(str(user))
    principal = (
        _principal(tenant_store.tenant, approver, "slack", f"slack:{team}:{user}")
        if approver is not None
        else None
    )
    if principal is None:
        gate.record_runtime_event(
            "integration.approval_refused",
            "slack",
            {"via": "slack", "slack_team": team, "slack_user": user, "reason": "unmapped user"},
        )
        raise ActionRefusedError("your Slack user is not mapped to a scruple approver")
    action: Action = "approve" if button["action_id"] == APPROVE_ACTION else "reject"
    return act(
        gate,
        principal,
        action,
        rule_id=str(button["r"]),
        version=int(button["v"]),
        request_seq=int(button["q"]),
        content_hash=str(button["h"]),
        via={"via": "slack", "slack_team": team, "slack_user": user},
        nonce="slack:" + hashlib.sha256(str(signature).encode()).hexdigest(),
        reason=f"rejected in Slack by {principal.subject}" if action == "reject" else None,
    )


def respond(runtime: Runtime, payload: dict[str, Any], text: str) -> None:
    """Best effort: tell the clicking user what happened (ephemeral, via response_url)."""
    url = payload.get("response_url")
    if not isinstance(url, str) or not url.startswith("https://hooks.slack.com/"):
        return
    try:
        runtime.http.post(
            url,
            headers={"Content-Type": "application/json"},
            content=json.dumps(
                {"response_type": "ephemeral", "replace_original": False, "text": text[:3_000]}
            ).encode(),
        )
    except (httpx.HTTPError, UnsafeURLError):
        log.warning("could not answer a Slack interaction through response_url")


# --- signed links -------------------------------------------------------------------------


def open_link(runtime: Runtime, token: str, now: float) -> tuple[str, dict[str, Any]]:
    """(key id, claims) of a genuine, unexpired link. Raises SignatureError."""
    if runtime.links is None:
        raise SignatureError("approval links are not configured")
    kid, claims = runtime.links.verify(token)
    required = {"t": str, "r": str, "v": int, "q": int, "h": str, "a": str, "s": str, "e": int}
    for k, kind in required.items():
        if not isinstance(claims.get(k), kind):
            raise SignatureError("malformed link")
    if claims["a"] not in ("approve", "reject") or not isinstance(claims.get("n"), str):
        raise SignatureError("malformed link")
    if now >= claims["e"]:
        raise SignatureError("this link has expired")
    return kid, claims


def redeem_link(
    gate: AgentGate,
    store: IntegrationStore,
    runtime: Runtime,
    kid: str,
    claims: dict[str, Any],
    reason: str | None,
) -> str:
    cfg = store.settings().slack
    approver = cfg.approver_named(claims["s"]) if cfg is not None else None
    if approver is None or cfg is None or not cfg.links:
        gate.record_runtime_event(
            "integration.approval_refused",
            "approval-link",
            {"via": "approval_link", "subject": claims["s"], "reason": "approver not mapped"},
        )
        raise ActionRefusedError(f"{claims['s']} is no longer a mapped approver for this tenant")
    principal = _principal(store.tenant, approver, "approval_link", f"link:{kid}")
    return act(
        gate,
        principal,
        claims["a"],
        rule_id=claims["r"],
        version=claims["v"],
        request_seq=claims["q"],
        content_hash=claims["h"],
        via={"via": "approval_link", "link_key": kid},
        nonce="link:" + claims["n"],
        reason=reason,
    )


__all__ = [
    "APPROVE_ACTION",
    "REJECT_ACTION",
    "SLACK_KIND",
    "ActionRefusedError",
    "TransientSlackError",
    "act",
    "approval_message",
    "button_value",
    "claimed_tenant",
    "eligible",
    "handle_interaction",
    "mint_link",
    "notify",
    "open_link",
    "parse_interaction",
    "post_message",
    "redeem_link",
    "respond",
]
