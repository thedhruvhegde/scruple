"""Evidence pack: everything a reviewer needs for one rule version, in JSON and PDF.

The JSON bundle holds the scorecard, the statistics, every decision, the canary ramp,
the approvals and a contiguous segment of the audit chain with a verification proof:
recomputing each entry hash from the segment's anchor (`prev_hash` of its first
entry) must reproduce the segment head, and the head must equal the live chain head
at export time. An auditor who anchors that head externally can check the pack
without database access.

Read-only: exporting writes nothing to storage. Deterministic: canonical, sorted JSON
with no export timestamp, so the same state always exports the same bytes; the PDF
is rendered from the JSON with a fixed creation date taken from the chain.

Secrets: the held-out and canary seeds are operator-held (docs/DECISIONS.md D-013),
so the pack carries only the config hash and seed commitments, never the config.

History: `lifecycle` states what happened to the version, in order. The gate verdict
is the verdict of the last *gate* decision (shadow, canary, activation, assurance) at
the time it was made; a manual rollback is an operator event, not a gate verdict, so
it does not turn an earlier PASS into BLOCK. The current status, every status change
and the rollback (who, when, why, which version was restored) are listed separately
(schema version 2; version 1 read the verdict from the last decision of any kind).

Partner history: a CI run with `--history` scores the candidate on two sources. The
history source is not stored in the gate database (it is a backtest of a file the
partner holds), so the pack carries it only when the caller passes it: `scruple ci
run --history ... --evidence DIR` does, and `scruple ci evidence --scorecard FILE`
takes it from that run's scorecard after checking it scored the same decisions. The
pack's scorecard then holds both sources and the overall verdict, exactly as in the
CI scorecard; without history the pack bytes are unchanged.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, SerializerFunctionWrapHandler, model_serializer

from scruple.audit.log import compute_hash
from scruple.audit.models import GENESIS_HASH, AuditEntry
from scruple.ci.history import HistoryGate
from scruple.ci.scorecard import Scorecard, build_scorecard, with_history
from scruple.gate.approval import (
    EVENT_POLICY_SET,
    EVENT_SATISFIED,
    ApprovalGrant,
    ApprovalPolicy,
    ApprovalRequest,
    project_approvals,
)
from scruple.gate.canary import CanaryState
from scruple.gate.engine import seed_commitment
from scruple.gate.models import Decision, DecisionRecord, Outcome, Stage
from scruple.hashing import canonical_json, content_hash, sha256_hex
from scruple.service import AgentGate

SCHEMA_VERSION = 2

_PASS = {Outcome.PROMOTE, Outcome.CANARY_PASSED, Outcome.ACTIVATED, Outcome.HEALTHY}


class SegmentProof(BaseModel):
    model_config = ConfigDict(frozen=True)

    ok: bool
    first_seq: int
    last_seq: int
    anchor_prev_hash: str
    recomputed_head: str
    chain_head: str
    chain_entries: int
    chain_ok: bool
    problem: str | None = None


class AuditSegment(BaseModel):
    model_config = ConfigDict(frozen=True)

    entries: tuple[AuditEntry, ...]
    proof: SegmentProof


class Approvals(BaseModel):
    model_config = ConfigDict(frozen=True)

    policy: ApprovalPolicy | None
    open_request: ApprovalRequest | None
    open_grants: tuple[ApprovalGrant, ...]
    satisfied: tuple[dict[str, Any], ...]  # approval.satisfied payloads for this version


class LifecycleEvent(BaseModel):
    """One step in the version's history, from the audit chain."""

    model_config = ConfigDict(frozen=True)

    seq: int
    at: datetime
    event: str  # audit event type
    actor: str
    decision_id: str | None = None
    stage: str | None = None
    outcome: str | None = None
    verdict_at_decision: str | None = None  # pass / hold / block, for gate decisions
    from_status: str | None = None
    to_status: str | None = None
    detail: str = ""


class Rollback(BaseModel):
    model_config = ConfigDict(frozen=True)

    decision_id: str | None
    actor: str
    at: datetime
    reason: str
    restored_version: int | None


class Lifecycle(BaseModel):
    model_config = ConfigDict(frozen=True)

    gate_verdict: str  # verdict of the last gate decision, as it was decided
    gate_decision_id: str
    gate_stage: str
    gate_decided_at: datetime
    current_status: str
    rolled_back: Rollback | None
    retired_by_version: int | None  # a newer version replaced it on activation
    events: tuple[LifecycleEvent, ...]
    statement: str  # the history in one paragraph, for the PDF and reviewers


class EvidencePack(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: int = SCHEMA_VERSION
    data: str = "synthetic"
    rule_id: str
    version: int
    content_hash: str
    status: str
    lifecycle: Lifecycle
    scorecard: Scorecard
    decisions: tuple[Decision, ...]
    statistics: dict[str, Any]
    canary_ramp: CanaryState | None
    approvals: Approvals
    config_sha256: str
    seed_commitments: dict[str, str]
    audit: AuditSegment
    # The tenant policy in force when the pack was built (scruple.policy); omitted
    # without one, so packs without a policy are byte-identical to before.
    policy_sha256: str | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if data.get("policy_sha256") is None:
            data.pop("policy_sha256", None)
        return data

    @property
    def pack_sha256(self) -> str:
        return sha256_hex(canonical_json(self.model_dump(mode="json")))

    def to_json(self) -> str:
        """Pack plus its own hash. `pack_sha256` covers everything else."""
        body = self.model_dump(mode="json")
        body["pack_sha256"] = self.pack_sha256
        return json.dumps(body, indent=2, sort_keys=True) + "\n"


def verify_segment(
    entries: Sequence[AuditEntry], chain_head: str, chain_entries: int, chain_ok: bool
) -> SegmentProof:
    if not entries:
        return SegmentProof(
            ok=False,
            first_seq=0,
            last_seq=0,
            anchor_prev_hash=GENESIS_HASH,
            recomputed_head=GENESIS_HASH,
            chain_head=chain_head,
            chain_entries=chain_entries,
            chain_ok=chain_ok,
            problem="empty segment",
        )
    prev = entries[0].prev_hash
    problem = None
    for i, e in enumerate(entries):
        if e.seq != entries[0].seq + i:
            problem = f"sequence gap at seq {e.seq}"
            break
        if e.prev_hash != prev:
            problem = f"prev_hash mismatch at seq {e.seq}"
            break
        if compute_hash(e) != e.entry_hash:
            problem = f"entry {e.seq} does not match its hash"
            break
        prev = e.entry_hash
    if problem is None and prev != chain_head:
        problem = "segment head differs from the chain head"
    return SegmentProof(
        ok=problem is None and chain_ok,
        first_seq=entries[0].seq,
        last_seq=entries[-1].seq,
        anchor_prev_hash=entries[0].prev_hash,
        recomputed_head=prev,
        chain_head=chain_head,
        chain_entries=chain_entries,
        chain_ok=chain_ok,
        problem=problem,
    )


def _verdict_at(outcome: Outcome) -> str:
    if outcome in _PASS:
        return "pass"
    return "hold" if outcome is Outcome.HOLD else "block"


def build_lifecycle(
    records: Sequence[DecisionRecord],
    entries: Sequence[AuditEntry],
    rule_id: str,
    version: int,
    status: str,
) -> Lifecycle:
    """The version's history from its decisions and the audit chain (pure)."""
    gate_records = [r for r in records if r.decision.stage is not Stage.ROLLBACK]
    last = (gate_records or list(records))[-1].decision
    by_id = {r.decision.decision_id: r.decision for r in records}
    events: list[LifecycleEvent] = []
    rollback: Rollback | None = None
    retired_by: int | None = None
    pending_rollback_decision: str | None = None
    for e in entries:
        p = e.payload
        if p.get("rule_id") != rule_id:
            continue
        v = int(p.get("version", -1))
        if e.event_type == "rule.activated" and p.get("retired_version") == version:
            retired_by = v
            events.append(
                LifecycleEvent(
                    seq=e.seq,
                    at=e.at,
                    event=e.event_type,
                    actor=e.actor,
                    from_status="active",
                    to_status="retired",
                    detail=f"retired when v{v} was activated",
                )
            )
            continue
        if v != version:
            continue
        if e.event_type == "decision.recorded":
            d = by_id.get(str(p.get("decision_id")))
            if d is None:
                continue
            if d.stage is Stage.ROLLBACK:
                pending_rollback_decision = d.decision_id
            events.append(
                LifecycleEvent(
                    seq=e.seq,
                    at=d.created_at,
                    event=e.event_type,
                    actor=d.actor,
                    decision_id=d.decision_id,
                    stage=d.stage.value,
                    outcome=d.outcome.value,
                    verdict_at_decision=None
                    if d.stage is Stage.ROLLBACK
                    else _verdict_at(d.outcome),
                    detail=d.summary,
                )
            )
        elif e.event_type.startswith("rule."):
            reason = str(p.get("reason", ""))
            events.append(
                LifecycleEvent(
                    seq=e.seq,
                    at=e.at,
                    event=e.event_type,
                    actor=e.actor,
                    from_status=p.get("from_status"),
                    to_status=p.get("to_status", p.get("status")),
                    detail=reason,
                )
            )
            if e.event_type == "rule.rolled_back":
                rv = p.get("restored_version")
                rollback = Rollback(
                    decision_id=pending_rollback_decision,
                    actor=e.actor,
                    at=e.at,
                    reason=reason,
                    restored_version=None if rv is None else int(rv),
                )
    verdict = _verdict_at(last.outcome)
    parts = [
        f"Gate verdict {verdict.upper()} at {last.decision_id} ({last.stage.value}, "
        f"{last.outcome.value}, {last.created_at.isoformat()})."
    ]
    activated = next((x for x in events if x.event == "rule.activated"), None)
    if activated is not None and activated.to_status == "active":
        parts.append(f"Activated by {activated.actor} at {activated.at.isoformat()}.")
    if rollback is not None:
        restored = (
            f"v{rollback.restored_version} restored"
            if rollback.restored_version is not None
            else "no earlier version to restore"
        )
        parts.append(
            f"Rolled back by {rollback.actor} at {rollback.at.isoformat()}"
            + (f" ({rollback.decision_id})" if rollback.decision_id else "")
            + f": {rollback.reason}; {restored}."
        )
    if retired_by is not None:
        parts.append(f"Retired when v{retired_by} was activated.")
    parts.append(f"Current status: {status}.")
    return Lifecycle(
        gate_verdict=verdict,
        gate_decision_id=last.decision_id,
        gate_stage=last.stage.value,
        gate_decided_at=last.created_at,
        current_status=status,
        rolled_back=rollback,
        retired_by_version=retired_by,
        events=tuple(events),
        statement=" ".join(parts),
    )


def build_evidence_pack(
    gate: AgentGate,
    rule_id: str,
    version: int,
    history: HistoryGate | None = None,
    fail_on_hold: bool = True,
) -> EvidencePack:
    """The pack for one version. `history` is the partner-history source of the CI run
    that gated it (`Scorecard.history`); with it, the pack's scorecard is the CI
    scorecard with both sources, and `data` says which data each comes from.
    `fail_on_hold` (the run's `[ci]` setting) applies to the combined exit code only,
    so a pack without history is byte-identical to before."""
    rv = gate.registry.get(rule_id, version)
    records = [r for r in gate.decisions(rule_id) if r.decision.version == version]
    if not records:
        raise ValueError(f"{rv.ref} has no decisions to put in an evidence pack")
    entries = gate.audit.entries()
    mine = [
        e
        for e in entries
        if e.payload.get("rule_id") == rule_id and int(e.payload.get("version", -1)) == version
    ]
    start = mine[0].seq if mine else records[0].audit_seq
    segment = [e for e in entries if e.seq >= start]
    chain = gate.audit.verify()
    proof = verify_segment(segment, gate.audit.head(), len(entries), chain.ok)

    request, grants = project_approvals(entries, rule_id, version)
    policy_events = [e for e in entries if e.event_type == EVENT_POLICY_SET]
    policy = (
        ApprovalPolicy.model_validate(policy_events[-1].payload["policy"])
        if policy_events
        else None
    )
    satisfied = tuple(e.payload for e in mine if e.event_type == EVENT_SATISFIED)
    # The scorecard's verdict is the gate's: a manual rollback is listed in the
    # lifecycle and the decisions, not scored as a gate verdict.
    gate_records = [r for r in records if r.decision.stage is not Stage.ROLLBACK] or records
    card = build_scorecard(gate_records, gate.config, gate.audit.head())
    if history is not None:
        card = with_history(card, history, fail_on_hold)
    stats: dict[str, Any] = {
        "method": card.statistics.method if card.statistics else None,
        "shadow": None if card.statistics is None else card.statistics.model_dump(mode="json"),
        "canary_stages": [
            {
                "decision_id": r.decision.decision_id,
                "outcome": r.decision.outcome.value,
                "fraction": r.decision.canary.fraction,
                "matched_treatment": r.decision.canary.matched_treatment,
                "matched_control": r.decision.canary.matched_control,
                "treatment_mean_cost_usd_minor": r.decision.canary.treatment_mean_cost_usd_minor,
                "control_mean_cost_usd_minor": r.decision.canary.control_mean_cost_usd_minor,
                **(
                    {"sequential": r.decision.canary.sequential.model_dump(mode="json")}
                    if r.decision.canary.sequential is not None
                    else {}
                ),
            }
            for r in records
            if r.decision.canary is not None
        ],
    }
    if card.evalue is not None:  # e-values, confidence sequences, required n, alpha shares
        stats["evalue"] = card.evalue.model_dump(mode="json")
    return EvidencePack(
        data=card.data if history is not None else "synthetic",
        rule_id=rule_id,
        version=version,
        content_hash=rv.content_hash,
        status=rv.status.value,
        lifecycle=build_lifecycle(records, entries, rule_id, version, rv.status.value),
        scorecard=card,
        decisions=tuple(r.decision for r in records),
        statistics=stats,
        canary_ramp=gate.canary_state(rule_id, version),
        approvals=Approvals(
            policy=policy, open_request=request, open_grants=grants, satisfied=satisfied
        ),
        config_sha256=content_hash(gate.config),
        seed_commitments={
            "eval": seed_commitment(gate.config.eval_seed),
            "canary": seed_commitment(gate.config.canary_seed),
        },
        audit=AuditSegment(entries=tuple(segment), proof=proof),
        policy_sha256=gate.config.policy_sha256,
    )


def history_from_scorecard(card: Scorecard, pack: EvidencePack) -> HistoryGate | None:
    """The history source of `card`, after checking the card scored exactly the pack's
    gate decisions (same rule, version and decision ids): a scorecard from another run
    cannot lend its history to this pack. Raises ValueError on a mismatch."""
    if (card.rule_id, card.version) != (pack.rule_id, pack.version):
        raise ValueError(
            f"scorecard is for {card.rule_id}@v{card.version}, not {pack.rule_id}@v{pack.version}"
        )
    mine = [d.decision_id for d in pack.scorecard.decisions]
    theirs = [d.decision_id for d in card.decisions]
    if mine != theirs:
        raise ValueError(
            "scorecard decisions differ from the database's: " + ", ".join(theirs or ["none"])
        )
    return card.history


def load_pack(text: str) -> tuple[EvidencePack, str]:
    """Parse a pack and return it with the hash it claims. Callers compare the claim
    with `pack.pack_sha256` to detect edits."""
    body = json.loads(text)
    claimed = str(body.pop("pack_sha256", ""))
    return EvidencePack.model_validate(body), claimed


def last_timestamp(pack: EvidencePack) -> datetime:
    return pack.audit.entries[-1].at if pack.audit.entries else pack.decisions[-1].created_at
