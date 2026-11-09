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
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from scruple.audit.log import compute_hash
from scruple.audit.models import GENESIS_HASH, AuditEntry
from scruple.ci.scorecard import Scorecard, build_scorecard
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
from scruple.gate.models import Decision
from scruple.hashing import canonical_json, content_hash, sha256_hex
from scruple.service import AgentGate

SCHEMA_VERSION = 1


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


class EvidencePack(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: int = SCHEMA_VERSION
    data: str = "synthetic"
    rule_id: str
    version: int
    content_hash: str
    status: str
    scorecard: Scorecard
    decisions: tuple[Decision, ...]
    statistics: dict[str, Any]
    canary_ramp: CanaryState | None
    approvals: Approvals
    config_sha256: str
    seed_commitments: dict[str, str]
    audit: AuditSegment

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


def build_evidence_pack(gate: AgentGate, rule_id: str, version: int) -> EvidencePack:
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
    card = build_scorecard(records, gate.config, gate.audit.head())
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
        rule_id=rule_id,
        version=version,
        content_hash=rv.content_hash,
        status=rv.status.value,
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
    )


def load_pack(text: str) -> tuple[EvidencePack, str]:
    """Parse a pack and return it with the hash it claims. Callers compare the claim
    with `pack.pack_sha256` to detect edits."""
    body = json.loads(text)
    claimed = str(body.pop("pack_sha256", ""))
    return EvidencePack.model_validate(body), claimed


def last_timestamp(pack: EvidencePack) -> datetime:
    return pack.audit.entries[-1].at if pack.audit.entries else pack.decisions[-1].created_at
