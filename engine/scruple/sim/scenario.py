"""Scenario DSL: hand-written dispute edge cases in YAML (roadmap 034).

A scenario file is YAML -> `Scenario` (pydantic, strict: unknown keys, duplicate keys,
wrong types and impossible cases are refused with the file, line and field path) ->
`CardDispute` via `Scenario.build()`, and runs through `DisputeEngine` with
`run_scenario`. Synthetic data only; every scenario must cite docs/SOURCES.md ids
(`S1`..) or docs/ASSUMPTIONS.md ids (`A-13`..) for the rules it exercises.

    id: visa-10-4-ce3-friendly-fraud          # lowercase, digits, dashes
    title: ...
    sources: [S2, S3, A-28]
    case:
      network: visa
      reason_code: "10.4"
      family: friendly_fraud                   # sets claim_valid (families.CLAIM_VALID)
      amount_minor: 12000                      # disputed amount, txn currency
      processed_on: 2026-01-05                 # default 2026-01-05
      opened_after_days: 20                    # opened_on = processed_on + this
      opening_stage: chargeback                # or inquiry
      documents:                               # receipt and authorization record are
        - {kind: avs_cvv_match}                # added unless include_always_held: false
        - {kind: device_ip_match, label: device, fields: {matches_cardholder: "Y"}}
        - {kind: delivery_proof, forged: backdated}
      fingerprint: {account_id: ..., ip: ..., shipping_address: ..., device_id: ...}
      prior_transactions:                      # same card at this merchant
        - {age_days: 200}                      # fingerprint: same (default) or new
      shipments:                               # a split order (A-26)
        - {amount_minor: 4000, delivered_day: 3, disputed: true}
      refund_in_flight: {settles_day: 2}       # days after opened_on; amount defaults
    issuer: {representment: false}             # scripted stages; otherwise the issuer
                                               # rules by ground truth (TruthIssuer)
    history:                                   # steps already taken, in day order
      - {day: 0, action: represent, evidence: [device]}
    expected:                                  # the decision under test
      day: 12                                  # days after opened_on
      action: accept                           # accept|represent|escalate|refund|wait
      evidence: [avs_cvv_match, device]        # labels or kinds (represent only)
      resolution: accepted                     # optional: final resolution
      why: one or two sentences tying the action to the cited rules
    checks: {ce3_eligible: true}               # optional extra assertions

`run_scenario` sets a `ManualClock` to the opening date, plays `history` then
`expected` (each on its day, after `sync`), drains the engine and reports problems:
a refused step, an outcome that is not correct against ground truth
(`CaseOutcome.correct`), a different resolution, or a failed check.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from scruple.sim.ce3 import Fingerprint, PriorTransaction
from scruple.sim.deadlines import ManualClock
from scruple.sim.disputes import (
    ALWAYS_HELD,
    Accept,
    ActionResult,
    AgentAction,
    CardDispute,
    CaseOutcome,
    DisputeEngine,
    Escalate,
    PendingRefund,
    Refund,
    Represent,
    Shipment,
    Truth,
    drain,
)
from scruple.sim.evidence import EvidenceDocument, EvidenceKind, Forgery, forge, make_document
from scruple.sim.families import CLAIM_VALID, CaseFamily, statement
from scruple.sim.models import Resolution, Stage
from scruple.sim.networks import Network

_STRICT = ConfigDict(frozen=True, extra="forbid")
SCENARIO_DIR = Path(__file__).parent / "scenarios"
SOURCE_PATTERN = r"^(S\d+|A-\d+)$"
ActionName = Literal["accept", "represent", "escalate", "refund", "wait"]


class ScenarioError(ValueError):
    """A scenario that cannot be loaded or built. The message names file, line, field."""


class DocSpec(BaseModel):
    model_config = _STRICT

    kind: EvidenceKind
    label: str | None = Field(default=None, pattern=r"^[a-z0-9_-]+$")
    issued_day: int | None = Field(default=None, strict=True)  # days after processed_on
    fields: dict[str, str] = Field(default_factory=dict)
    forged: Forgery | None = None


class ShipmentSpec(BaseModel):
    model_config = _STRICT

    amount_minor: int = Field(gt=0, strict=True)
    delivered_day: int | None = Field(default=None, ge=0, strict=True)  # after processing
    disputed: bool = Field(strict=True)


class PriorSpec(BaseModel):
    model_config = _STRICT

    age_days: int = Field(gt=0, strict=True)  # processed this many days before the case
    disputed: bool = Field(default=False, strict=True)
    fingerprint: Literal["same", "new"] | Fingerprint = "same"


class RefundSpec(BaseModel):
    model_config = _STRICT

    amount_minor: int | None = Field(default=None, gt=0, strict=True)
    settles_day: int = Field(strict=True)  # days after opened_on (may be negative)


class CaseSpec(BaseModel):
    model_config = _STRICT

    network: Network
    reason_code: str
    family: CaseFamily
    amount_minor: int = Field(gt=0, strict=True)
    captured_minor: int | None = Field(default=None, gt=0, strict=True)
    currency: str = "USD"
    settlement_currency: str = "USD"
    processed_on: date = date(2026, 1, 5)
    opened_after_days: int = Field(default=10, ge=0, strict=True)
    opening_stage: Stage = Stage.CHARGEBACK
    merchant_id: str = "m-001"
    mcc: str = Field(default="5999", pattern=r"^\d{4}$")
    include_always_held: bool = Field(default=True, strict=True)
    documents: tuple[DocSpec, ...] = ()
    missing_evidence: tuple[EvidenceKind, ...] = ()
    shipments: tuple[ShipmentSpec, ...] = ()
    refund_in_flight: RefundSpec | None = None
    fingerprint: Fingerprint | None = None
    prior_transactions: tuple[PriorSpec, ...] = ()
    liability_minor: int | None = Field(default=None, ge=0, strict=True)

    @model_validator(mode="after")
    def _labels_unique(self) -> CaseSpec:
        labels = [d.label for d in self.documents if d.label is not None]
        if len(set(labels)) != len(labels):
            raise ValueError("document labels must be unique")
        if self.prior_transactions and self.fingerprint is None:
            raise ValueError("prior_transactions need the case fingerprint")
        return self


class Step(BaseModel):
    model_config = _STRICT

    day: int = Field(default=0, ge=0, strict=True)  # days after opened_on
    action: ActionName
    evidence: tuple[str, ...] = ()
    amount_minor: int | None = Field(default=None, gt=0, strict=True)  # refund only

    @model_validator(mode="after")
    def _shape(self) -> Step:
        if self.evidence and self.action != "represent":
            raise ValueError(f"evidence is only for represent, not {self.action}")
        if self.amount_minor is not None and self.action != "refund":
            raise ValueError("amount_minor is only for refund")
        return self


class Expected(Step):
    resolution: Resolution | None = None
    why: str = Field(min_length=20)


class Checks(BaseModel):
    model_config = _STRICT

    ce3_eligible: bool | None = Field(default=None, strict=True)
    flawed_documents: int | None = Field(default=None, ge=0, strict=True)
    incidents: int | None = Field(default=None, ge=0, strict=True)
    cardholder_net_minor: int | None = Field(default=None, strict=True)


class Scenario(BaseModel):
    model_config = _STRICT

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$", max_length=80)
    title: str = Field(min_length=5)
    sources: tuple[str, ...] = Field(min_length=1)
    case: CaseSpec
    issuer: dict[Stage, bool] = Field(default_factory=dict)
    history: tuple[Step, ...] = ()
    expected: Expected
    checks: Checks = Checks()

    @model_validator(mode="after")
    def _consistent(self) -> Scenario:
        import re

        bad = [s for s in self.sources if not re.match(SOURCE_PATTERN, s)]
        if bad:
            raise ValueError(f"sources must be S<n> or A-<n> ids, got {bad}")
        days = [s.day for s in (*self.history, self.expected)]
        if days != sorted(days):
            raise ValueError("history and expected steps must be in day order")
        if set(self.issuer) - {
            Stage.INQUIRY,
            Stage.REPRESENTMENT,
            Stage.PRE_ARBITRATION,
            Stage.ARBITRATION,
        }:
            raise ValueError(
                "issuer may script inquiry, representment, pre_arbitration, arbitration"
            )
        known = self.evidence_refs()
        for step in (*self.history, self.expected):
            for ref in step.evidence:
                if ref not in known:
                    raise ValueError(
                        f"evidence {ref!r} names no document label or kind; known: {sorted(known)}"
                    )
        return self

    @property
    def case_id(self) -> str:
        return f"sc-{self.id}"

    def evidence_refs(self) -> set[str]:
        """Labels and kinds a step may name (refund records appear during a run)."""
        c = self.case
        refs = {d.label for d in c.documents if d.label} | {d.kind.value for d in c.documents}
        if c.include_always_held:
            refs |= {k.value for k in ALWAYS_HELD}
        if c.shipments:
            refs |= {EvidenceKind.DELIVERY_PROOF.value} | {
                f"parcel-{j + 1}" for j, s in enumerate(c.shipments) if s.delivered_day is not None
            }
        if c.refund_in_flight is not None or any(
            s.action == "refund" for s in (*self.history, self.expected)
        ):
            refs.add(EvidenceKind.REFUND_RECORD.value)
        return refs

    # --- building --------------------------------------------------------------
    def _ref(self, label: str) -> str:
        return hashlib.sha256(f"{self.id}/{label}".encode()).hexdigest()[:12]

    def _default_fields(self, kind: EvidenceKind, issued: date, label: str) -> dict[str, str]:
        c = self.case
        f = {
            "ref": self._ref(label),
            "amount_minor": str(c.captured_minor or c.amount_minor),
            "currency": c.currency,
        }
        match kind:
            case EvidenceKind.DELIVERY_PROOF:
                f |= {"carrier": "ups", "delivered_on": issued.isoformat()}
            case EvidenceKind.AUTHORIZATION_RECORD:
                f |= {"auth_code": self._ref(label)[:6], "approved": "Y"}
            case EvidenceKind.AVS_CVV_MATCH:
                f |= {"avs": "Y", "cvv": "M"}
            case EvidenceKind.DEVICE_IP_MATCH:
                mismatch = c.family is CaseFamily.TRUE_FRAUD
                f |= {
                    "ip": "203.0.113.10",
                    "device_id": f"dev-{self._ref(label)}",
                    "matches_cardholder": "N" if mismatch else "Y",
                }
            case EvidenceKind.REFUND_RECORD:
                f |= {"refund_id": f"rf-{self._ref(label)}", "settled_on": issued.isoformat()}
            case EvidenceKind.PRIOR_UNDISPUTED_HISTORY:
                f = {
                    "prior_txn_ids": ",".join(
                        f"{self.case_id}-p{j + 1}" for j in range(len(c.prior_transactions))
                    ),
                    "currency": c.currency,
                }
            case _:
                pass
        return f

    def documents(self) -> tuple[tuple[EvidenceDocument, ...], dict[str, str], dict[str, Forgery]]:
        """(documents, label -> doc id, forged doc id -> how)."""
        c = self.case
        specs = list(c.documents)
        if c.include_always_held:
            have = {d.kind for d in specs}
            specs = [DocSpec(kind=k) for k in ALWAYS_HELD if k not in have] + specs
        docs: list[EvidenceDocument] = []
        labels: dict[str, str] = {}
        forged: dict[str, Forgery] = {}
        for idx, spec in enumerate(specs):
            label = spec.label or f"{spec.kind.value}-{idx}"
            default_day = 3 if spec.kind is EvidenceKind.DELIVERY_PROOF else 0
            issued = c.processed_on + timedelta(
                days=default_day if spec.issued_day is None else spec.issued_day
            )
            fields = {**self._default_fields(spec.kind, issued, label), **spec.fields}
            d = make_document(self.case_id, spec.kind, issued, fields, idx)
            if spec.forged is not None:
                d = forge(d, spec.forged, c.processed_on)
                forged[d.doc_id] = spec.forged
            docs.append(d)
            if spec.label:
                labels[spec.label] = d.doc_id
        for j, s in enumerate(c.shipments):
            if s.delivered_day is None:
                continue
            day = c.processed_on + timedelta(days=s.delivered_day)
            fields = {
                "shipment_id": f"{self.case_id}-s{j + 1}",
                "shipment_amount_minor": str(s.amount_minor),
                "currency": c.currency,
                "carrier": "ups",
                "delivered_on": day.isoformat(),
            }
            d = make_document(
                self.case_id, EvidenceKind.DELIVERY_PROOF, day, fields, len(specs) + j
            )
            docs.append(d)
            labels[f"parcel-{j + 1}"] = d.doc_id
        return tuple(docs), labels, forged

    def build(self) -> CardDispute:
        """The `CardDispute` this scenario describes (validated by the case model)."""
        c = self.case
        docs, _, forged = self.documents()
        valid = CLAIM_VALID[c.family]
        opened = c.processed_on + timedelta(days=c.opened_after_days)
        ships = tuple(
            Shipment(
                shipment_id=f"{self.case_id}-s{j + 1}",
                amount_minor=s.amount_minor,
                delivered_on=(
                    None
                    if s.delivered_day is None
                    else c.processed_on + timedelta(days=s.delivered_day)
                ),
                disputed=s.disputed,
            )
            for j, s in enumerate(c.shipments)
        )
        if c.liability_minor is not None:
            liability = c.liability_minor
        elif ships:
            liability = sum(x.amount_minor for x in ships if x.disputed and x.delivered_on is None)
        else:
            liability = c.amount_minor if valid else 0
        captured = c.captured_minor or (sum(x.amount_minor for x in ships) if ships else None)
        rf = c.refund_in_flight
        race = (
            None
            if rf is None
            else PendingRefund(
                amount_minor=rf.amount_minor or c.amount_minor,
                settles_on=opened + timedelta(days=rf.settles_day),
            )
        )
        priors = tuple(
            PriorTransaction(
                txn_id=f"{self.case_id}-p{j + 1}",
                processed_on=c.processed_on - timedelta(days=p.age_days),
                disputed=p.disputed,
                fingerprint=_prior_fingerprint(p, c.fingerprint, j),
            )
            for j, p in enumerate(c.prior_transactions)
        )
        return CardDispute(
            case_id=self.case_id,
            txn_id=f"tx-{self.id}",
            merchant_id=c.merchant_id,
            mcc=c.mcc,
            network=c.network,
            reason_code=c.reason_code,
            currency=c.currency,
            settlement_currency=c.settlement_currency,
            amount_minor=c.amount_minor,
            captured_minor=captured or c.amount_minor,
            processed_on=c.processed_on,
            opened_on=opened,
            opening_stage=c.opening_stage,
            documents=docs,
            refund_in_flight=race,
            shipments=ships,
            fingerprint=c.fingerprint,
            prior_transactions=priors,
            truth=Truth(
                claim_valid=valid,
                liability_minor=liability,
                forged=forged,
                missing_evidence=c.missing_evidence,
                family=c.family,
                statement=statement(self.case_id, c.family),
            ),
        )


def _prior_fingerprint(p: PriorSpec, case_fp: Fingerprint | None, j: int) -> Fingerprint:
    if isinstance(p.fingerprint, Fingerprint):
        return p.fingerprint
    assert case_fp is not None  # checked by CaseSpec
    if p.fingerprint == "same":
        return case_fp
    return Fingerprint(
        account_id=f"acct-other-{j}",
        ip=f"192.0.2.{j + 1}",
        shipping_address=f"addr-other-{j}",
        device_id=f"device-other-000{j}-xx",
    )


# --- issuer ---------------------------------------------------------------------------
class ScriptedIssuer:
    """Rules by ground truth (merchant prevails iff the claim is invalid), except for the
    stages a scenario scripts. Deterministic."""

    def __init__(self, script: Mapping[Stage, bool] | None = None) -> None:
        self.script = dict(script or {})

    def merchant_prevails(
        self, case: CardDispute, stage: Stage, evidence: tuple[EvidenceDocument, ...]
    ) -> bool:
        if stage in self.script:
            return self.script[stage]
        return not case.truth.claim_valid


# --- loading --------------------------------------------------------------------------
class _StrictLoader(yaml.SafeLoader):
    """Safe YAML that refuses duplicate keys."""


def _no_duplicates(loader: _StrictLoader, node: yaml.MappingNode, deep: bool = False) -> Any:
    seen: set[object] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r}", key_node.start_mark
            )
        seen.add(key)
    return loader.construct_mapping(node, deep=deep)


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicates)


def _line_of(root: yaml.Node | None, loc: Sequence[int | str]) -> int | None:
    """1-based line of the YAML node at a pydantic error location (best effort)."""
    node = root
    line = None if node is None else node.start_mark.line + 1
    for part in loc:
        if isinstance(node, yaml.MappingNode):
            nxt = next((v for k, v in node.value if k.value == str(part)), None)
            if nxt is None:
                break
            node = nxt
        elif isinstance(node, yaml.SequenceNode) and isinstance(part, int):
            if part >= len(node.value):
                break
            node = node.value[part]
        else:
            break
        line = node.start_mark.line + 1
    return line


def parse_scenario(text: str, name: str = "<scenario>") -> Scenario:
    """Parse and validate one scenario; raise `ScenarioError` naming file, line, field."""
    try:
        root = yaml.compose(text, Loader=_StrictLoader)
        data = yaml.load(text, Loader=_StrictLoader)  # noqa: S506 - SafeLoader subclass
    except yaml.YAMLError as exc:
        raise ScenarioError(f"{name}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ScenarioError(f"{name}: a scenario must be a mapping at top level")
    try:
        sc = Scenario.model_validate(data)
    except ValidationError as exc:
        lines = []
        for e in exc.errors():
            loc = [p for p in e["loc"] if isinstance(p, int | str)]
            where = ".".join(str(p) for p in loc) or "<root>"
            ln = _line_of(root, loc)
            lines.append(f"{name}:{ln if ln else '?'}: {where}: {e['msg']}")
        raise ScenarioError("\n".join(lines)) from exc
    try:
        sc.build()
    except (ValueError, KeyError) as exc:
        ln = _line_of(root, ["case"])
        raise ScenarioError(f"{name}:{ln}: case: cannot build: {exc}") from exc
    return sc


def load_scenario(path: str | Path) -> Scenario:
    p = Path(path)
    return parse_scenario(p.read_text(encoding="utf-8"), str(p))


def load_scenarios(directory: str | Path = SCENARIO_DIR) -> list[Scenario]:
    """Every `*.yaml` scenario in a directory, sorted by id. Ids must be unique."""
    out = [load_scenario(p) for p in sorted(Path(directory).glob("*.yaml"))]
    ids = [s.id for s in out]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise ScenarioError(f"{directory}: duplicate scenario ids {dupes}")
    return sorted(out, key=lambda s: s.id)


# --- running --------------------------------------------------------------------------
class ScenarioRun(BaseModel):
    model_config = ConfigDict(frozen=True)

    scenario_id: str
    results: tuple[ActionResult, ...]
    outcome: CaseOutcome | None
    problems: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.problems


def _action(step: Step, ids: tuple[str, ...]) -> AgentAction | None:
    match step.action:
        case "accept":
            return Accept()
        case "represent":
            return Represent(evidence_ids=ids)
        case "escalate":
            return Escalate()
        case "refund":
            assert step.amount_minor is not None
            return Refund(amount_minor=step.amount_minor)
        case _:
            return None


def run_scenario(sc: Scenario) -> ScenarioRun:
    """Play the scenario through a fresh engine and report what went wrong (if anything)."""
    case = sc.build()
    _, labels, _ = sc.documents()
    clock = ManualClock()
    clock.set_date(case.opened_on)
    engine = DisputeEngine([case], clock, issuer=ScriptedIssuer(sc.issuer))
    problems: list[str] = []
    results: list[ActionResult] = []
    for n, step in enumerate((*sc.history, sc.expected)):
        clock.set_date(case.opened_on + timedelta(days=step.day))
        engine.sync()
        if step.action == "refund" and step.amount_minor is None:
            step = step.model_copy(update={"amount_minor": case.amount_minor})
        evidence = engine.observe(case.case_id).evidence
        ids: list[str] = []
        for ref in step.evidence:
            if ref in labels:
                ids.append(labels[ref])
                continue
            matches = [d.doc_id for d in evidence if d.kind.value == ref]
            if len(matches) != 1:
                problems.append(f"step {n}: evidence {ref!r} matches {len(matches)} documents")
                continue
            ids.append(matches[0])
        action = _action(step, tuple(ids))
        if action is None:
            continue
        res = engine.act(case.case_id, action)
        results.append(res)
        if not res.ok:
            problems.append(f"step {n} ({step.action}) refused: {res.error} {res.detail}")
    drain(engine, clock)
    out = engine.outcome(case.case_id)
    if out is None:  # pragma: no cover - drain resolves every case
        problems.append("case did not resolve")
    else:
        if not out.correct:
            problems.append(
                f"incorrect outcome: cardholder net {out.cardholder_net_minor}, "
                f"liability {out.liability_minor}"
            )
        want = sc.expected.resolution
        if want is not None and out.resolution is not want:
            problems.append(f"resolution {out.resolution.value}, expected {want.value}")
        ck = sc.checks
        if (
            ck.cardholder_net_minor is not None
            and out.cardholder_net_minor != ck.cardholder_net_minor
        ):
            problems.append(
                f"cardholder net {out.cardholder_net_minor} != {ck.cardholder_net_minor}"
            )
    ck = sc.checks
    if ck.ce3_eligible is not None and case.ce3().eligible is not ck.ce3_eligible:
        problems.append(f"ce3 eligible is {case.ce3().eligible}, expected {ck.ce3_eligible}")
    if ck.flawed_documents is not None:
        from scruple.sim.evidence import document_problems

        n_bad = sum(
            1
            for d in case.documents
            if document_problems(d, case.captured_minor, case.currency, case.processed_on)
        )
        if n_bad != ck.flawed_documents:
            problems.append(f"{n_bad} flawed documents, expected {ck.flawed_documents}")
    if ck.incidents is not None and len(engine.incidents) != ck.incidents:
        problems.append(f"{len(engine.incidents)} incidents, expected {ck.incidents}")
    return ScenarioRun(
        scenario_id=sc.id, results=tuple(results), outcome=out, problems=tuple(problems)
    )
