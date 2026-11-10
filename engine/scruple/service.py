"""AgentGate: the one place where registry, gate, audit and reports meet.

Both the API and the CLI call this class; neither touches storage directly.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel

from scruple.audit.log import AuditLog, VerifyResult
from scruple.audit.report import render_report
from scruple.clock import Clock, SystemClock
from scruple.domain import RuleSpec
from scruple.gate.approval import (
    EVENT_GRANTED,
    EVENT_POLICY_SET,
    EVENT_REQUESTED,
    EVENT_SATISFIED,
    ApprovalPolicy,
    ApprovalStatus,
    NoOpenRequestError,
    check_grant,
    evaluate_approvals,
    new_grant,
    new_request,
    project_approvals,
    project_policy,
)
from scruple.gate.approval import ApprovalError as ApprovalError
from scruple.gate.canary import (
    EVENT_BREACHED,
    EVENT_COMPLETED,
    EVENT_STAGE_HELD,
    EVENT_STAGE_PASSED,
    EVENT_STARTED,
    Arm,
    CanaryState,
    RampSchedule,
    assign,
    evaluate_stage,
    project_state,
)
from scruple.gate.config import GateConfig
from scruple.gate.engine import Verdict, evaluate_canary, evaluate_shadow, policy_from
from scruple.gate.models import CheckResult, Decision, DecisionRecord, Outcome, Stage
from scruple.gate.provider import StatsProvider, provider_for
from scruple.gate.regression import golden_cases
from scruple.gate.shadow import ShadowRecorder
from scruple.hashing import content_hash
from scruple.registry import Provenance, Registry, RuleStatus, RuleVersion
from scruple.registry.state import InvalidTransitionError
from scruple.sim.engine import Simulator
from scruple.sim.generator import generate_cases
from scruple.sim.library import BASELINE_RULES
from scruple.sim.models import DisputeCase
from scruple.storage import Storage, open_storage
from scruple.verifiers.ledger import verify_ledger
from scruple.verifiers.policy_constraints import verify_policy

SYSTEM_ACTOR = "agent-gate"


EVENT_REJECTED = "approval.rejected"

# ApprovalError now lives with the approval policy (scruple.gate.approval) and is
# re-exported here unchanged, so `from scruple.service import ApprovalError` works.


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
        return cls(open_storage(path), clock, config)

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
        registry, so status always reflects the latest decision.

        The verdicts are pure, so both are computed first; every write (the status
        changes, decisions and audit entries) then happens in one transaction, so a
        failure part-way leaves the version a candidate with nothing recorded."""
        rv = self.registry.get(rule_id, version)
        if rv.status is not RuleStatus.CANDIDATE:
            raise InvalidTransitionError(f"{rv.ref} is {rv.status.value}, not candidate")
        active = self.registry.active_rules()  # candidate replaces its own rule_id, if any
        shadow = evaluate_shadow(rv, active, self.config)
        canary = (
            evaluate_canary(rv, active, self.config) if shadow.outcome is Outcome.PROMOTE else None
        )
        records = []
        with self.storage.transaction():
            if self.registry.get(rule_id, version).status is not RuleStatus.CANDIDATE:
                raise InvalidTransitionError(f"{rv.ref} changed while it was being evaluated")
            self.registry.transition(
                rule_id, version, RuleStatus.SHADOW, actor, "shadow evaluation started"
            )
            rec = self._record(Stage.SHADOW, rv, shadow, actor)
            records.append(rec)
            link = {"decision_id": rec.decision.decision_id}
            if shadow.outcome is Outcome.BLOCK:
                self.registry.transition(
                    rule_id,
                    version,
                    RuleStatus.REJECTED,
                    actor,
                    "blocked by shadow evaluation",
                    link,
                )
                return records
            if shadow.outcome is Outcome.HOLD or canary is None:
                self.registry.transition(
                    rule_id,
                    version,
                    RuleStatus.CANDIDATE,
                    actor,
                    "held: insufficient evidence",
                    link,
                )
                return records
            self.registry.transition(
                rule_id, version, RuleStatus.CANARY, actor, "shadow evaluation passed", link
            )
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

    def approve(
        self, rule_id: str, version: int, approver: str, *, reason: str | None = None
    ) -> DecisionRecord:
        if self.approval_policy() is not None:
            raise ApprovalError("an approval policy is in force: use request/grant approval")
        rv = self.registry.get(rule_id, version)
        self._require_approver(approver)
        if approver in (rv.owner, rv.provenance.agent_id):
            raise ApprovalError("segregation of duties: owner/agent cannot approve own rule")
        self._require_ready_for_activation(rv)
        last = self._latest(rule_id, version, Stage.CANARY)
        assert last is not None
        verdict = Verdict(
            Outcome.ACTIVATED,
            f"Activated by {approver}.",
            (
                f"approved after canary decision {last.decision.decision_id}",
                *((f"reason: {reason}",) if reason else ()),
            ),
            (),
        )
        with self.storage.transaction():  # the decision and the activation, or neither
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
        with self.storage.transaction():  # the decision and the rollback, or neither
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
        with self.storage.transaction():  # the failed check and its rollback, or neither
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

    # --- approval workflow (N of M named approvers) --------------------------------

    def set_approval_policy(self, policy: ApprovalPolicy, actor: str) -> ApprovalPolicy:
        """Put an approval policy in force. Audited; the chain holds its history.
        Only operators in `GateConfig.approvers` may set it, so a named approver
        cannot lower the quorum they are part of."""
        if actor not in self.config.approvers:
            raise ApprovalError(f"{actor} may not change the approval policy")
        self.audit.append(EVENT_POLICY_SET, actor, {"policy": policy.model_dump(mode="json")})
        return policy

    def approval_policy(self) -> ApprovalPolicy | None:
        return project_policy(self.audit.entries())

    def request_approval(self, rule_id: str, version: int, proposer: str) -> ApprovalStatus:
        policy = self._policy_or_raise()
        rv = self.registry.get(rule_id, version)
        self._require_ready_for_activation(rv)
        now = self.clock.now()
        request, _ = project_approvals(self.audit.entries(), rule_id, version)
        if request is not None and now < request.expires_at:
            raise ApprovalError(f"{rv.ref} already has an open approval request")
        req = new_request(policy, rv, proposer, now)
        self.audit.append(
            EVENT_REQUESTED,
            proposer,
            {
                "rule_id": rule_id,
                "version": version,
                "content_hash": rv.content_hash,
                "proposer": proposer,
                "requested_at": req.requested_at.isoformat(),
                "expires_at": req.expires_at.isoformat(),
            },
        )
        return self.approval_status(rule_id, version)

    def grant_approval(
        self, rule_id: str, version: int, approver: str, *, reason: str | None = None
    ) -> tuple[ApprovalStatus, DecisionRecord | None]:
        """Record one approval. When the policy is satisfied, activate in the same
        transaction and return the activation decision."""
        policy = self._policy_or_raise()
        rv = self.registry.get(rule_id, version)
        self._require_ready_for_activation(rv)
        with self.storage.transaction():
            now = self.clock.now()
            request, grants = project_approvals(self.audit.entries(), rule_id, version)
            named = check_grant(policy, request, rv, grants, approver, now)
            assert request is not None
            grant = new_grant(policy, named, now)
            self.audit.append(
                EVENT_GRANTED,
                approver,
                {
                    "rule_id": rule_id,
                    "version": version,
                    "content_hash": rv.content_hash,
                    "approver": approver,
                    "roles": list(named.roles),
                    "granted_at": grant.granted_at.isoformat(),
                    "expires_at": grant.expires_at.isoformat(),
                    "request_seq": request.seq,
                    **({"reason": reason} if reason else {}),
                },
            )
            request, grants = project_approvals(self.audit.entries(), rule_id, version)
            status = evaluate_approvals(policy, request, grants, now)
            if not status.satisfied:
                return status, None
            last = self._latest(rule_id, version, Stage.CANARY)
            assert last is not None
            verdict = Verdict(
                Outcome.ACTIVATED,
                f"Activated by {len(status.counted)} of {len(policy.approvers)} named approvers.",
                (
                    f"approved by {', '.join(status.counted)}",
                    f"policy: {policy.required} of {len(policy.approvers)}"
                    + (f", role {policy.required_role} required" if policy.required_role else ""),
                    f"approved after canary decision {last.decision.decision_id}",
                ),
                (),
            )
            rec = self._record(Stage.ACTIVATION, rv, verdict, approver)
            self.audit.append(
                EVENT_SATISFIED,
                approver,
                {
                    "rule_id": rule_id,
                    "version": version,
                    "content_hash": rv.content_hash,
                    "approvers": list(status.counted),
                    "request_seq": request.seq if request else None,
                    "decision_id": rec.decision.decision_id,
                },
            )
            self.registry.activate(
                rule_id, version, approver, f"approved; decision {rec.decision.decision_id}"
            )
        return status, rec

    def reject_approval(
        self, rule_id: str, version: int, actor: str, reason: str
    ) -> DecisionRecord:
        """Refuse activation of a version waiting for approval, with a reason.

        Only an approver may reject: a named approver of the policy in force, else one
        of `GateConfig.approvers`. The version moves canary -> rejected (final) with a
        BLOCK decision at the activation stage, and an `approval.rejected` event holds
        the reason; all three commit together or not at all."""
        policy = self.approval_policy()
        if policy is not None:
            if policy.approver(actor) is None:
                raise ApprovalError(f"{actor} is not a named approver")
        else:
            self._require_approver(actor)
        if not reason.strip():
            raise ApprovalError("a rejection needs a reason")
        rv = self.registry.get(rule_id, version)
        self._require_ready_for_activation(rv)
        verdict = Verdict(
            Outcome.BLOCK,
            f"Rejected at approval by {actor}: {reason}",
            (f"rejected by {actor}", f"reason: {reason}"),
            (),
        )
        with self.storage.transaction():
            rec = self._record(Stage.ACTIVATION, rv, verdict, actor)
            self.audit.append(
                EVENT_REJECTED,
                actor,
                {
                    "rule_id": rule_id,
                    "version": version,
                    "content_hash": rv.content_hash,
                    "reason": reason,
                    "decision_id": rec.decision.decision_id,
                },
            )
            self.registry.transition(
                rule_id,
                version,
                RuleStatus.REJECTED,
                actor,
                f"rejected at approval; decision {rec.decision.decision_id}",
                {"decision_id": rec.decision.decision_id},
            )
        return rec

    def approval_status(self, rule_id: str, version: int) -> ApprovalStatus:
        policy = self._policy_or_raise()
        request, grants = project_approvals(self.audit.entries(), rule_id, version)
        return evaluate_approvals(policy, request, grants, self.clock.now())

    def _policy_or_raise(self) -> ApprovalPolicy:
        policy = self.approval_policy()
        if policy is None:
            raise NoOpenRequestError("no approval policy is in force")
        return policy

    def _require_ready_for_activation(self, rv: RuleVersion) -> None:
        last = self._latest(rv.rule_id, rv.version, Stage.CANARY)
        if (
            rv.status is not RuleStatus.CANARY
            or last is None
            or last.decision.outcome is not Outcome.CANARY_PASSED
        ):
            raise ApprovalError(f"{rv.ref} has not passed canary")
        ramp = self.canary_state(rv.rule_id, rv.version)
        if ramp is not None and ramp.status != "completed":
            # A started ramp must finish: approving before its first stage was
            # evaluated would skip the staged rollout entirely.
            raise ApprovalError(f"{rv.ref} has not passed canary: its ramp is {ramp.status}")

    # --- staged canary ramp -----------------------------------------------------

    def start_canary_ramp(
        self, rule_id: str, version: int, schedule: RampSchedule, actor: str = SYSTEM_ACTOR
    ) -> CanaryState:
        """Begin a staged ramp for a version that passed the one-shot canary."""
        rv = self.registry.get(rule_id, version)
        last = self._latest(rule_id, version, Stage.CANARY)
        if (
            rv.status is not RuleStatus.CANARY
            or last is None
            or last.decision.outcome is not Outcome.CANARY_PASSED
        ):
            raise InvalidTransitionError(f"{rv.ref} has not passed canary")
        if self.canary_state(rule_id, version) is not None:
            raise InvalidTransitionError(f"{rv.ref} already has a canary ramp")
        self.audit.append(
            EVENT_STARTED,
            actor,
            {
                "rule_id": rule_id,
                "version": version,
                "content_hash": rv.content_hash,
                "schedule": schedule.model_dump(mode="json"),
                "percent": schedule.stages[0],
            },
        )
        state = self.canary_state(rule_id, version)
        assert state is not None
        return state

    def advance_canary(
        self,
        rule_id: str,
        version: int,
        cases: Sequence[DisputeCase] | None = None,
        actor: str = SYSTEM_ACTOR,
        provider: StatsProvider | None = None,
    ) -> tuple[DecisionRecord, CanaryState]:
        """Evaluate the current stage on a batch of live traffic and apply the result.
        Without `cases`, a seeded synthetic batch per stage is generated."""
        state = self.canary_state(rule_id, version)
        if state is None or state.status != "ramping":
            raise InvalidTransitionError(f"{rule_id}@v{version} has no ramp in progress")
        rv = self.registry.get(rule_id, version)
        if rv.status is not RuleStatus.CANARY:
            raise InvalidTransitionError(f"{rv.ref} is {rv.status.value}, not canary")
        if cases is None:
            cases = generate_cases(
                self.config.canary_seed + 1_000 + state.stage_index,
                self.config.canary_n_cases,
                self.config.canary_profile,
                prefix=f"r{state.stage_index}-",
            )
        active = [r for r in self.registry.active_rules() if r.rule_id != rule_id]
        res = evaluate_stage(
            rv,
            active,
            cases,
            state.schedule,
            state.stage_index,
            provider or provider_for(self.config),
            self.config,
        )
        verdict = Verdict(
            res.gate_outcome,
            res.summary,
            res.reasons,
            res.checks,
            canary=res.metrics(self.config.canary_profile.name),
        )
        base = {
            "rule_id": rule_id,
            "version": version,
            "content_hash": rv.content_hash,
            "stage_index": res.stage_index,
            "percent": res.percent,
        }
        with self.storage.transaction():
            rec = self._record(Stage.CANARY, rv, verdict, actor)
            base["decision_id"] = rec.decision.decision_id
            if res.outcome == "advance":
                nxt = res.stage_index + 1
                self.audit.append(
                    EVENT_STAGE_PASSED,
                    actor,
                    {**base, "next_stage_index": nxt, "next_percent": state.schedule.stages[nxt]},
                )
            elif res.outcome == "complete":
                self.audit.append(EVENT_COMPLETED, actor, base)
            elif res.outcome == "hold":
                self.audit.append(EVENT_STAGE_HELD, actor, base)
            else:
                self._canary_breach(rv, rec, base, actor)
        new_state = self.canary_state(rule_id, version)
        assert new_state is not None
        return rec, new_state

    def canary_state(self, rule_id: str, version: int) -> CanaryState | None:
        return project_state(self.audit.entries(), rule_id, version)

    def canary_arm(self, rule_id: str, version: int, case_id: str) -> Arm:
        """Which policy a live case should get right now. Baseline unless ramping."""
        state = self.canary_state(rule_id, version)
        if state is None or state.status == "breached":
            return Arm.BASELINE
        return assign(case_id, state.content_hash, state.percent)

    def breach_canary(self, rule_id: str, version: int, actor: str, reason: str) -> DecisionRecord:
        """Kill switch: stop a ramp now (for example on an external alert). Same
        one-step rollback as an automatic breach. Needs an approver or the system."""
        if actor != SYSTEM_ACTOR:
            self._require_approver(actor)
        state = self.canary_state(rule_id, version)
        if state is None or state.status != "ramping":
            raise InvalidTransitionError(f"{rule_id}@v{version} has no ramp in progress")
        rv = self.registry.get(rule_id, version)
        verdict = Verdict(
            Outcome.ROLLBACK,
            f"Canary stopped by {actor} at {state.percent:g}%: {reason}",
            (f"manual breach: {reason}",),
            (),
        )
        with self.storage.transaction():
            rec = self._record(Stage.CANARY, rv, verdict, actor)
            base = {
                "rule_id": rule_id,
                "version": version,
                "content_hash": rv.content_hash,
                "stage_index": state.stage_index,
                "percent": state.percent,
                "decision_id": rec.decision.decision_id,
                "manual": True,
                "reason": reason,
            }
            self._canary_breach(rv, rec, base, actor)
        return rec

    def _canary_breach(
        self, rv: RuleVersion, rec: DecisionRecord, base: dict[str, Any], actor: str
    ) -> None:
        """One step, one transaction: mark the ramp breached (traffic share drops to
        zero) and reject the version, reusing the canary auto-rollback transition."""
        with self.storage.transaction():
            self.audit.append(EVENT_BREACHED, actor, base)
            self.registry.transition(
                rv.rule_id,
                rv.version,
                RuleStatus.REJECTED,
                actor,
                f"automatic rollback: canary breach at {base['percent']:g}%",
                {
                    "decision_id": rec.decision.decision_id,
                    "auto_rollback": True,
                    "stage_index": base["stage_index"],
                    "percent": base["percent"],
                },
            )

    # --- shadow mode (read-only) -------------------------------------------------

    def shadow_recorder(
        self, rule_id: str, version: int, provider: StatsProvider | None = None
    ) -> ShadowRecorder:
        """A recorder that scores live decisions for this candidate without acting.
        Reads the registry once; the recorder holds no handle to storage."""
        rv = self.registry.get(rule_id, version)
        active = [r for r in self.registry.active_rules() if r.rule_id != rule_id]
        return ShadowRecorder(
            rv, active, provider or provider_for(self.config), self.config.confidence
        )

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
            try:
                self._project_entry(e.event_type, e.payload, expected, decision_hashes)
            except (KeyError, TypeError, ValueError):
                # A tampered payload must be reported, never crash the check.
                problems.append(f"audit entry seq {e.seq}: payload does not fit {e.event_type}")
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

    @staticmethod
    def _project_entry(
        event_type: str,
        p: dict[str, Any],
        expected: dict[tuple[str, int], tuple[str, str]],
        decision_hashes: dict[str, str],
    ) -> None:
        """Fold one audit entry into the expected registry and decision hashes."""
        if event_type == "decision.recorded":
            decision_hashes[p["decision_id"]] = p["decision_sha256"]
            return
        if not event_type.startswith("rule."):
            return
        key = (p["rule_id"], int(p["version"]))
        h = p["content_hash"]
        if event_type in (
            "rule.registered",
            "rule.baseline_imported",
            "rule.status_changed",
            "rule.activated",
        ):
            expected[key] = (p.get("to_status", p.get("status", "")), h)
        if event_type == "rule.activated" and p.get("retired_version"):
            old = (p["rule_id"], int(p["retired_version"]))
            expected[old] = ("retired", expected.get(old, ("", ""))[1])
        if event_type == "rule.rolled_back":
            expected[key] = ("retired", h)
            if p.get("restored_version"):
                old = (p["rule_id"], int(p["restored_version"]))
                expected[old] = ("active", expected.get(old, ("", ""))[1])

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
