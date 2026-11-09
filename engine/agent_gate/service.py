"""AgentGate: the one place where registry, gate, audit and reports meet.

Both the API and the CLI call this class; neither touches storage directly.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from pydantic import BaseModel

from agent_gate.audit.log import AuditLog, VerifyResult
from agent_gate.audit.report import render_report
from agent_gate.clock import Clock, SystemClock
from agent_gate.domain import RuleSpec
from agent_gate.gate.config import GateConfig
from agent_gate.gate.engine import Verdict, evaluate_canary, evaluate_shadow, policy_from
from agent_gate.gate.models import CheckResult, Decision, DecisionRecord, Outcome, Stage
from agent_gate.gate.regression import golden_cases
from agent_gate.hashing import content_hash
from agent_gate.registry import Provenance, Registry, RuleStatus, RuleVersion
from agent_gate.registry.state import InvalidTransitionError
from agent_gate.sim.engine import Simulator
from agent_gate.sim.generator import generate_cases
from agent_gate.sim.library import BASELINE_RULES
from agent_gate.storage import SQLiteStorage, Storage
from agent_gate.verifiers.ledger import verify_ledger
from agent_gate.verifiers.policy_constraints import verify_policy

SYSTEM_ACTOR = "agent-gate"


class ApprovalError(PermissionError):
    pass


class IntegrityReport(BaseModel):
    ok: bool
    chain: VerifyResult
    problems: list[str]


class AgentGate:
    def __init__(
        self, storage: Storage, clock: Clock | None = None, config: GateConfig | None = None
    ) -> None:
        self.storage = storage
        self.clock = clock or SystemClock()
        self.config = config or GateConfig()
        self.audit = AuditLog(storage, self.clock)
        self.registry = Registry(storage, self.audit, self.clock)

    @classmethod
    def open(
        cls, path: str, clock: Clock | None = None, config: GateConfig | None = None
    ) -> AgentGate:
        return cls(SQLiteStorage(path), clock, config)

    # --- setup ---------------------------------------------------------------

    def bootstrap_baseline(self, actor: str = SYSTEM_ACTOR) -> list[RuleVersion]:
        """Import the synthetic baseline policy. Idempotent."""
        created = []
        for rule_id, spec in BASELINE_RULES.items():
            if not self.registry.versions(rule_id):
                created.append(self.registry.import_baseline(rule_id, spec, "ops-team", actor))
        return created

    def propose(
        self, rule_id: str, spec: RuleSpec, owner: str, provenance: Provenance
    ) -> RuleVersion:
        return self.registry.register_candidate(rule_id, spec, owner, provenance)

    # --- promotion pipeline -----------------------------------------------------

    def evaluate(
        self, rule_id: str, version: int, actor: str = SYSTEM_ACTOR
    ) -> list[DecisionRecord]:
        """Shadow replay, then (if promoted) canary. Applies every outcome via the
        registry, so status always reflects the latest decision."""
        rv = self.registry.get(rule_id, version)
        if rv.status is not RuleStatus.CANDIDATE:
            raise InvalidTransitionError(f"{rv.ref} is {rv.status.value}, not candidate")
        active = self.registry.active_rules()  # candidate replaces its own rule_id, if any
        self.registry.transition(
            rule_id, version, RuleStatus.SHADOW, actor, "shadow evaluation started"
        )
        records = []
        shadow = evaluate_shadow(rv, active, self.config)
        rec = self._record(Stage.SHADOW, rv, shadow, actor)
        records.append(rec)
        link = {"decision_id": rec.decision.decision_id}
        if shadow.outcome is Outcome.BLOCK:
            self.registry.transition(
                rule_id, version, RuleStatus.REJECTED, actor, "blocked by shadow evaluation", link
            )
            return records
        if shadow.outcome is Outcome.HOLD:
            self.registry.transition(
                rule_id, version, RuleStatus.CANDIDATE, actor, "held: insufficient evidence", link
            )
            return records
        self.registry.transition(
            rule_id, version, RuleStatus.CANARY, actor, "shadow evaluation passed", link
        )
        canary = evaluate_canary(rv, active, self.config)
        rec = self._record(Stage.CANARY, rv, canary, actor)
        records.append(rec)
        if canary.outcome is Outcome.ROLLBACK:
            self.registry.transition(
                rule_id,
                version,
                RuleStatus.REJECTED,
                actor,
                "automatic rollback: canary regression",
                {"decision_id": rec.decision.decision_id, "auto_rollback": True},
            )
        return records

    def approve(self, rule_id: str, version: int, approver: str) -> DecisionRecord:
        rv = self.registry.get(rule_id, version)
        self._require_approver(approver)
        if approver in (rv.owner, rv.provenance.agent_id):
            raise ApprovalError("segregation of duties: owner/agent cannot approve own rule")
        last = self._latest(rule_id, version, Stage.CANARY)
        if (
            rv.status is not RuleStatus.CANARY
            or last is None
            or last.decision.outcome is not Outcome.CANARY_PASSED
        ):
            raise ApprovalError(f"{rv.ref} has not passed canary")
        verdict = Verdict(
            Outcome.ACTIVATED,
            f"Activated by {approver}.",
            (f"approved after canary decision {last.decision.decision_id}",),
            (),
        )
        rec = self._record(Stage.ACTIVATION, rv, verdict, approver)
        self.registry.activate(
            rule_id, version, approver, f"approved; decision {rec.decision.decision_id}"
        )
        return rec

    def rollback(self, rule_id: str, actor: str, reason: str) -> DecisionRecord:
        if actor != SYSTEM_ACTOR:
            self._require_approver(actor)
        current = self.registry.active_version(rule_id)
        if current is None:
            raise InvalidTransitionError(f"{rule_id} has no active version")
        restored_to = current.rollback_to
        verdict = Verdict(
            Outcome.ROLLED_BACK,
            f"Rolled back by {actor}: {reason}",
            (
                f"retired {current.ref}",
                f"restored {rule_id}@v{restored_to}"
                if restored_to
                else "no previous version; rule removed from the policy",
            ),
            (),
        )
        rec = self._record(Stage.ROLLBACK, current, verdict, actor)
        self.registry.rollback(rule_id, actor, f"{reason}; decision {rec.decision.decision_id}")
        return rec

    def run_assurance(
        self, actor: str = SYSTEM_ACTOR, seed: int | None = None, auto_rollback: bool = True
    ) -> DecisionRecord | None:
        """Re-check the live policy: golden suite plus a fresh replay for hard
        violations. On failure, roll back the most recently activated rule."""
        active = self.registry.active_rules()
        if not active:
            return None
        policy = policy_from(active)
        problems: list[str] = []
        g = golden_cases()
        run = Simulator([x.case for x in g]).run(policy)
        acts = {r.case_id: r.action for r in run.results}
        problems += [
            f"golden {x.case.case_id}: {acts[x.case.case_id].value} not allowed"
            for x in g
            if acts[x.case.case_id] not in x.allowed
        ]
        cases = generate_cases(
            seed if seed is not None else self.config.canary_seed + 1,
            self.config.n_cases,
            self.config.canary_profile,
            prefix="a",
        )
        live = Simulator(cases).run(policy)
        hard = [*verify_ledger(cases, live).findings, *verify_policy(cases, live).findings]
        problems += [f"{f.case_id}: {f.kind}" for f in hard[:20]]
        latest = self._latest_activated()
        target = latest or active[-1]
        checks = (
            CheckResult(
                name="assurance_golden_and_hard_checks",
                kind="hard",
                passed=not problems,
                detail=f"{len(problems)} problem(s)",
            ),
        )
        if not problems:
            return self._record(
                Stage.ASSURANCE,
                target,
                Verdict(
                    Outcome.HEALTHY,
                    "Live policy passed the regression and constraint suites.",
                    ("no golden regressions", "no hard violations"),
                    checks,
                ),
                actor,
            )
        rec = self._record(
            Stage.ASSURANCE,
            target,
            Verdict(
                Outcome.ROLLBACK,
                "Live policy failed assurance checks.",
                tuple(problems[:10]),
                checks,
            ),
            actor,
        )
        if auto_rollback and latest is not None:
            self.rollback(
                latest.rule_id,
                SYSTEM_ACTOR,
                f"assurance failure, decision {rec.decision.decision_id}",
            )
        return rec

    # --- reads -----------------------------------------------------------------

    def decision(self, decision_id: str) -> DecisionRecord | None:
        body = self.storage.get_decision(decision_id)
        return None if body is None else DecisionRecord.model_validate_json(body)

    def decisions(self, rule_id: str | None = None) -> list[DecisionRecord]:
        return [DecisionRecord.model_validate_json(b) for b in self.storage.list_decisions(rule_id)]

    def report(self, decision_id: str) -> str | None:
        rec = self.decision(decision_id)
        return None if rec is None else render_report(rec)

    # --- integrity -------------------------------------------------------------

    def verify_integrity(self, expected_head: str | None = None) -> IntegrityReport:
        """Hash chain + rule table matches what the audit log says happened +
        every stored decision matches the hash recorded in the audit log."""
        entries = self.audit.entries()
        chain = self.audit.verify(expected_head)
        problems: list[str] = (
            [] if chain.ok else [f"audit chain: {chain.problem} (seq {chain.first_bad_seq})"]
        )
        expected: dict[tuple[str, int], tuple[str, str]] = {}
        decision_hashes: dict[str, str] = {}
        for e in entries:
            p: dict[str, Any] = e.payload
            if e.event_type == "decision.recorded":
                decision_hashes[p["decision_id"]] = p["decision_sha256"]
                continue
            if not e.event_type.startswith("rule."):
                continue
            key = (p["rule_id"], int(p["version"]))
            h = p["content_hash"]
            if e.event_type in (
                "rule.registered",
                "rule.baseline_imported",
                "rule.status_changed",
                "rule.activated",
            ):
                expected[key] = (p.get("to_status", p.get("status", "")), h)
            if e.event_type == "rule.activated" and p.get("retired_version"):
                old = (p["rule_id"], int(p["retired_version"]))
                expected[old] = ("retired", expected.get(old, ("", ""))[1])
            if e.event_type == "rule.rolled_back":
                expected[key] = ("retired", h)
                if p.get("restored_version"):
                    old = (p["rule_id"], int(p["restored_version"]))
                    expected[old] = ("active", expected.get(old, ("", ""))[1])
        actual = {(r.rule_id, r.version): r for r in self.registry.all_versions()}
        for key in sorted(set(expected) | set(actual)):
            if key not in actual:
                problems.append(f"{key[0]}@v{key[1]} in audit log but missing from registry")
                continue
            r = actual[key]
            if content_hash(r.spec) != r.content_hash:
                problems.append(f"{r.ref}: stored rule content does not match its hash")
            if key not in expected:
                problems.append(f"{r.ref}: exists in registry but never appears in audit log")
                continue
            status, h = expected[key]
            if r.status.value != status:
                problems.append(f"{r.ref}: status {r.status.value} but audit log says {status}")
            if h and r.content_hash != h:
                problems.append(f"{r.ref}: content hash differs from audit log")
        stored = {d.decision.decision_id: d for d in self.decisions()}
        for did in sorted(set(decision_hashes) | set(stored)):
            if did not in stored:
                problems.append(f"decision {did} in audit log but missing from store")
            elif did not in decision_hashes:
                problems.append(f"decision {did} has no audit log entry")
            elif content_hash(stored[did].decision) != decision_hashes[did]:
                problems.append(f"decision {did} was modified after it was recorded")
        return IntegrityReport(ok=not problems, chain=chain, problems=problems)

    # --- internals -------------------------------------------------------------

    def _require_approver(self, actor: str) -> None:
        if actor not in self.config.approvers:
            raise ApprovalError(f"{actor} is not an authorised approver")

    def _latest(self, rule_id: str, version: int, stage: Stage) -> DecisionRecord | None:
        found = [
            d
            for d in self.decisions(rule_id)
            if d.decision.version == version and d.decision.stage is stage
        ]
        return found[-1] if found else None

    def _latest_activated(self) -> RuleVersion | None:
        live = {(r.rule_id, r.version): r for r in self.registry.active_rules()}
        order: dict[tuple[str, int], int] = defaultdict(int)
        for e in self.audit.entries():
            if e.event_type == "rule.activated":
                order[(e.payload["rule_id"], int(e.payload["version"]))] = e.seq
        candidates = [(order[k], k) for k in live if k in order]
        return live[max(candidates)[1]] if candidates else None

    def _record(self, stage: Stage, rv: RuleVersion, v: Verdict, actor: str) -> DecisionRecord:
        with self.storage.transaction():
            decision = Decision(
                decision_id=f"dec-{self.storage.count_decisions() + 1:05d}",
                stage=stage,
                outcome=v.outcome,
                rule_id=rv.rule_id,
                version=rv.version,
                content_hash=rv.content_hash,
                rule_description=rv.spec.description,
                rule_logic=rv.spec.describe(),
                owner=rv.owner,
                agent_id=rv.provenance.agent_id,
                episodes=len(rv.provenance.episode_ids),
                actor=actor,
                created_at=self.clock.now(),
                summary=v.summary,
                reasons=v.reasons,
                checks=v.checks,
                shadow=v.shadow,
                canary=v.canary,
                approvers=self.config.approvers,
            )
            sha = content_hash(decision)
            entry = self.audit.append(
                "decision.recorded",
                actor,
                {
                    "decision_id": decision.decision_id,
                    "stage": stage.value,
                    "outcome": v.outcome.value,
                    "rule_id": rv.rule_id,
                    "version": rv.version,
                    "content_hash": rv.content_hash,
                    "decision_sha256": sha,
                },
            )
            rec = DecisionRecord(
                decision=decision,
                decision_sha256=sha,
                audit_seq=entry.seq,
                audit_entry_hash=entry.entry_hash,
            )
            self.storage.insert_decision(
                decision.decision_id, rv.rule_id, rv.version, rec.model_dump_json()
            )
        return rec
