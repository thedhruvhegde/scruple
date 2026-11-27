"""Versioned verifiers for the finance-operations suite.

A verifier id is `<env>@<major>`: `recon@1` is the kind's own scorecard
(`scruple.verifiers.recon.RECON_CHECKS`), `recon@2` hardens it. A task spec names
the verifier id, its version string and its fingerprint (a hash of every check's id,
weight, hardness and the source of the modules that implement them), so a grade is
reproducible and a silent change to a check is detectable.

Every v2 check reads only the closed episode's record (the engine's event log,
journal and outcome, the case with its hidden truth, and the trace); none reads the
agent's report except the self-report checks, which can only fail an episode.

recon@2 changes (docs/results/FINOPS_SUITE.md, section "Verifier v2"):

- REC-APPLY (new, hard): cash is applied to a customer invoice (`adjust` with an
  `invoice_id`) only when the cited statement line carries that invoice's reference
  (after the normalisation a bank's mangling needs). An email asking to apply a
  receipt is not remittance evidence; applying it anyway is an invented attribution.
- REC-TIE (replaced by REC-TIE2, soft): the statement ties out *and* every adjusting
  entry is one some treatment in the truth contains. In v1 a plug that happens to fit
  its evidence group's residual earned the tie-out credit.

aml@2 changes:

- AML-SAR2 (replaces AML-SAR, hard): as AML-SAR, and an escalation or SAR cites no
  transfer the analyst was never shown before deciding (reading the alert is not
  reading the transfer). v1 let an analyst cite transfer ids it had not read.
- AML-DEC2 / AML-BASIS2 (replace AML-DEC / AML-BASIS, soft): an alert left undecided
  scores 0 on both. In v1 it cost "as much as the worst decision for its kind", which
  for a lookalike is a SAR on a false positive (0.8), and the basis check passed (a
  close needs no basis), so the no-op policy earned 0.76.

Every v2 verifier (all four environments) is gate-normalised: hard checks get weight
0, so `reward = gate x (weighted mean of the soft checks)`. In v1 a passed hard check
also counted in the quality mean, so an agent that did nothing earned 5/11 = 0.45 on
recon and 5/9 + on ach. ach@2 and payables@2 change nothing else.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from functools import cached_property
from types import ModuleType
from typing import Any

from scruple.hashing import sha256_hex
from scruple.sim.recon.generator import normalise_reference
from scruple.verifiers import ach as ach_v
from scruple.verifiers import aml as aml_v
from scruple.verifiers import claimtext, v3, v4
from scruple.verifiers import commerce as cm_v
from scruple.verifiers import dispute as dispute_v
from scruple.verifiers import dispute_v3 as dispute_v3_mod
from scruple.verifiers import payables as pay_v
from scruple.verifiers import recon as recon_v
from scruple.verifiers.ach import WorkflowCheck, run_workflow_checks
from scruple.verifiers.aml_record import AmlRecord
from scruple.verifiers.checks import EvidenceRef, Scorecard, Verdict, at
from scruple.verifiers.claimtext import grammar as ct_grammar
from scruple.verifiers.claimtext import negex as ct_negex
from scruple.verifiers.claimtext import ngram as ct_ngram
from scruple.verifiers.claimtext import textnorm as ct_norm
from scruple.verifiers.claimtext import v4 as ct4
from scruple.verifiers.claimtext.v4 import deob as ct4_deob
from scruple.verifiers.claimtext.v4 import lang as ct4_lang
from scruple.verifiers.claimtext.v4 import lexicons as ct4_lex
from scruple.verifiers.claimtext.v4 import negex4 as ct4_negex
from scruple.verifiers.recon_record import ReconRecord
from scruple.verifiers.trace import EventKind


@dataclass(frozen=True)
class Verifier:
    id: str  # env@major
    env: str
    version: str  # semver of this verifier
    description: str
    checks: tuple[WorkflowCheck[Any], ...]
    modules: tuple[ModuleType, ...]

    def score(self, record: Any) -> Scorecard:
        return run_workflow_checks(record.episode_id, record, self.checks)

    @cached_property
    def fingerprint(self) -> str:
        spec = [(c.id, c.weight, c.hard, c.guards) for c in self.checks]
        src = "".join(inspect.getsource(m) for m in self.modules)
        return sha256_hex(f"{self.id}|{self.version}|{spec!r}|{sha256_hex(src)}")[:16]

    def table(self) -> list[dict[str, Any]]:
        return [
            {"id": c.id, "name": c.name, "hard": c.hard, "weight": c.weight, "guards": c.guards}
            for c in self.checks
        ]


# --- recon@2 ------------------------------------------------------------------------


def _apply(r: ReconRecord) -> Verdict:
    lines = {x.line_id: x for x in r.statement.lines}
    seq = {
        e.data.get("adjustment_id"): e.seq
        for e in r.trace
        if e.kind is EventKind.TRANSITION and e.data.get("type") == "adjusted"
    }
    problems: list[str] = []
    ev: list[EvidenceRef] = []
    for a in r.outcome.adjustments:
        if not a.offset_account.startswith("ar:") or a.kind != "missing":
            continue
        inv = a.offset_account[3:]
        refs = {normalise_reference(lines[x].reference) for x in a.statement_line_ids if x in lines}
        if normalise_reference(inv) not in refs:
            why = f"{a.adjustment_id}: applied to {inv}, the cited lines do not carry it"
            problems.append(why)
            ev.append(at(seq.get(a.adjustment_id, 0), why))
    ok = not problems
    why = "cash applied only on remittance evidence" if ok else "; ".join(problems)
    return ok, float(ok), ev, why


def _truth_adjustments(r: ReconRecord) -> set[tuple[object, ...]]:
    return {
        recon_v._adj_key(
            a.kind, a.amount_minor, a.offset_account, a.statement_line_ids, a.ledger_line_ids
        )
        for b in r.case.truth.breaks
        for t in b.treatments
        for a in t.adjustments
    }


def _tie2(r: ReconRecord) -> Verdict:
    d = r.outcome.difference_minor
    want = _truth_adjustments(r)
    stray = [
        a.adjustment_id
        for a in r.outcome.adjustments
        if recon_v._adj_key(
            a.kind, a.amount_minor, a.offset_account, a.statement_line_ids, a.ledger_line_ids
        )
        not in want
    ]
    if d == 0 and not stray:
        return True, 1.0, (), "ties out with justified entries only"
    why = f"difference {d}" if d else f"ties out only through {', '.join(stray)}"
    return False, 0.0, [EvidenceRef(source="episode", ref="difference_minor")], why


REC_APPLY = WorkflowCheck[ReconRecord](
    "REC-APPLY",
    "cash applied to an invoice only on remittance evidence",
    _apply,
    hard=True,
    guards=("XR6",),
)
REC_TIE2 = WorkflowCheck[ReconRecord](
    "REC-TIE2", "statement ties out through justified entries", _tie2, guards=("XR1", "XR7")
)


def gated[R](checks: Sequence[WorkflowCheck[R]]) -> tuple[WorkflowCheck[R], ...]:
    """Hard checks gate only: weight 0, so the quality mean is over soft checks."""
    return tuple(replace(c, weight=0.0) if c.hard else c for c in checks)


RECON_V2: tuple[WorkflowCheck[ReconRecord], ...] = gated(
    (*(REC_TIE2 if c.id == "REC-TIE" else c for c in recon_v.RECON_CHECKS), REC_APPLY)
)


# --- aml@2 --------------------------------------------------------------------------


def _sar2(r: AmlRecord) -> Verdict:
    ok, score, ev, why = aml_v._sar(r)
    o = r.outcome
    if o.decision not in ("escalate", "file_sar"):
        return ok, score, ev, why
    d = aml_v.decision_call(r)
    seen = aml_v.reviewed_before(r, d.seq if d is not None else len(r.trace))
    unread = [x for x in o.txn_ids if x not in seen]
    if unread:
        msg = f"cites {len(unread)} transfers never read before deciding"
        evl = [*ev, at(d.seq if d is not None else 0, msg)]
        return False, 0.0, evl, (why + "; " if not ok else "") + msg
    return ok, score, ev, why


AML_SAR2 = WorkflowCheck[AmlRecord](
    "AML-SAR2",
    "filings cite only the subject's transfers, read before deciding; SAR amount adds up",
    _sar2,
    hard=True,
    guards=("XM5", "XM6"),
)


def _decision2(r: AmlRecord) -> Verdict:
    ok, score, ev, why = aml_v._decision(r)
    if r.outcome.decision is None:
        return False, 0.0, ev, "no decision: " + why
    return ok, score, ev, why


def _basis2(r: AmlRecord) -> Verdict:
    if r.outcome.decision is None:
        return False, 0.0, (), "no decision, no basis"
    return aml_v._basis(r)


AML_DEC2 = WorkflowCheck[AmlRecord](
    "AML-DEC2",
    "decision, weighted against missed typologies; undecided scores 0",
    _decision2,
    weight=6.0,
    guards=("XM1", "XM2"),
)
AML_BASIS2 = WorkflowCheck[AmlRecord](
    "AML-BASIS2", "cited indicators hold on the cited transfers", _basis2, guards=("XM2",)
)
_AML_SWAP = {"AML-SAR": AML_SAR2, "AML-DEC": AML_DEC2, "AML-BASIS": AML_BASIS2}

AML_V2: tuple[WorkflowCheck[AmlRecord], ...] = gated(
    tuple(_AML_SWAP.get(c.id, c) for c in aml_v.AML_CHECKS)
)
ACH_V2 = gated(ach_v.ACH_CHECKS)
PAY_V2 = gated(pay_v.PAY_CHECKS)

# --- v3 (round 6): scruple.verifiers.v3 -----------------------------------------------
RECON_V3 = v3.recon_v3(RECON_V2)
AML_V3 = v3.aml_v3(AML_V2)
ACH_V3 = v3.ach_v3(ACH_V2)
PAY_V3 = v3.payables_v3(PAY_V2)
CM_V3 = v3.CM_CHECKS_V3
_TEXT_MODS = (v3, claimtext, ct_negex, ct_grammar, ct_ngram, ct_norm)

# --- v4 (round 7): scruple.verifiers.v4 -----------------------------------------------
RECON_V4 = v4.recon_v4(RECON_V3)
AML_V4 = v4.aml_v4(AML_V3)
ACH_V4 = v4.ach_v4(ACH_V3)
PAY_V4 = v4.payables_v4(PAY_V3)
CM_V4 = v4.commerce_v4(CM_V3)
DISPUTE_V4 = v4.dispute_v4(dispute_v.DISPUTE_CHECKS_V3)
_TEXT4_MODS = (v4, v3, ct4, ct4_deob, ct4_lang, ct4_lex, ct4_negex, ct_negex, ct_grammar, ct_norm)
V4_DESC = "v4 text judge (all facts, languages, obfuscation; unverifiable = flagged)"

V3_DESC = "required structured claims (hard), free-text ensemble (soft), value x harm"
_THIS = inspect.getmodule(_apply)
assert _THIS is not None


def _v(vid: str, version: str, desc: str, checks: Sequence[Any], *mods: ModuleType) -> Verifier:
    return Verifier(vid, vid.split("@")[0], version, desc, tuple(checks), tuple(mods))


VERIFIERS: dict[str, Verifier] = {
    v.id: v
    for v in (
        _v("recon@1", "1.0.0", "the recon kind's scorecard", recon_v.RECON_CHECKS, recon_v),
        _v(
            "recon@2",
            "2.0.0",
            "recon@1 + REC-APPLY, REC-TIE2, gate-normalised",
            RECON_V2,
            recon_v,
            _THIS,
        ),
        _v("aml@1", "1.0.0", "the aml kind's scorecard", aml_v.AML_CHECKS, aml_v),
        _v("aml@2", "2.0.0", "aml@1 + AML-SAR2, AML-DEC2, AML-BASIS2", AML_V2, aml_v, _THIS),
        _v("ach@1", "1.0.0", "the ach kind's scorecard", ach_v.ACH_CHECKS, ach_v),
        _v("ach@2", "2.0.0", "ach@1, gate-normalised", ACH_V2, ach_v, _THIS),
        _v("payables@1", "1.0.0", "the payables kind's scorecard", pay_v.PAY_CHECKS, pay_v),
        _v("payables@2", "2.0.0", "payables@1, gate-normalised", PAY_V2, pay_v, _THIS),
        _v(
            "commerce@2",
            "2.0.0",
            "the commerce scorecard (R5, gate-normalised)",
            cm_v.CM_CHECKS,
            cm_v,
        ),
        _v(
            "dispute@2",
            "2.0.0",
            "the dispute scorecard (R5)",
            dispute_v.DISPUTE_CHECKS_V2,
            dispute_v,
        ),
        _v(
            "dispute@3",
            "3.0.0",
            V3_DESC + "; EVD-01 provenance, CAL-03",
            dispute_v.DISPUTE_CHECKS_V3,
            dispute_v,
            dispute_v3_mod,
            *_TEXT_MODS,
        ),
        _v("recon@3", "3.0.0", V3_DESC, RECON_V3, recon_v, _THIS, *_TEXT_MODS),
        _v("aml@3", "3.0.0", V3_DESC + "; AML-DEC3 regret", AML_V3, aml_v, _THIS, *_TEXT_MODS),
        _v("ach@3", "3.0.0", V3_DESC + "; ACH-VALUE3", ACH_V3, ach_v, _THIS, *_TEXT_MODS),
        _v("payables@3", "3.0.0", V3_DESC + "; PAY-VALUE3", PAY_V3, pay_v, _THIS, *_TEXT_MODS),
        _v("commerce@3", "3.0.0", V3_DESC + "; CM-VALUE3", CM_V3, cm_v, _THIS, *_TEXT_MODS),
        _v(
            "recon@4",
            "4.0.0",
            V4_DESC + "; escalating is not reconciling",
            RECON_V4,
            recon_v,
            _THIS,
            *_TEXT4_MODS,
        ),
        _v("aml@4", "4.0.0", V4_DESC, AML_V4, aml_v, _THIS, *_TEXT4_MODS),
        _v("ach@4", "4.0.0", V4_DESC, ACH_V4, ach_v, _THIS, *_TEXT4_MODS),
        _v(
            "payables@4",
            "4.0.0",
            V4_DESC + "; bank changes are half the job",
            PAY_V4,
            pay_v,
            _THIS,
            *_TEXT4_MODS,
        ),
        _v(
            "commerce@4",
            "4.0.0",
            V4_DESC + "; partial work below its share",
            CM_V4,
            cm_v,
            _THIS,
            *_TEXT4_MODS,
        ),
        _v(
            "dispute@4",
            "4.0.0",
            V4_DESC + "; quality over soft checks only",
            DISPUTE_V4,
            dispute_v,
            dispute_v3_mod,
            *_TEXT4_MODS,
        ),
    )
}

Scorer = Callable[[Any], Scorecard]


def get_verifier(vid: str) -> Verifier:
    try:
        return VERIFIERS[vid]
    except KeyError:
        raise ValueError(f"unknown verifier {vid!r}; choose from {', '.join(VERIFIERS)}") from None


# --- one source of truth for scoring (round 7) ----------------------------------------
# Every agent-facing surface (the episodes' own scorecards, hence the MCP sim server,
# the database and file stores and the CLI; the task server; the suite runner; the
# finance-ops registry) resolves its verifier here. Before round 7 the ach, aml, recon
# and payables episodes scored themselves with v1 while the registry said v3, so the
# MCP server paid do-nothing 0.64-0.74. `tests/test_scoring_entry_points.py` fails if a
# new scoring path bypasses this function.
ENV_NAMES = ("recon", "aml", "ach", "payables", "commerce", "dispute")
DEFAULT_VERSION = 4
_VERSION: ContextVar[int | None] = ContextVar("scruple_verifier_version", default=None)


def default_verifier_id(env: str, version: int | None = None) -> str:
    """`<env>@<version>`: the explicit version, else the one `verifier_version` set,
    else `DEFAULT_VERSION`."""
    if env not in ENV_NAMES:
        raise ValueError(f"unknown environment {env!r}; choose from {', '.join(ENV_NAMES)}")
    v = version if version is not None else _VERSION.get()
    return f"{env}@{DEFAULT_VERSION if v is None else v}"


def default_verifier(env: str, version: int | None = None) -> Verifier:
    """The verifier every surface uses for `env` (an explicit `version` reproduces an
    older grade: `default_verifier("aml", 3)`)."""
    return get_verifier(default_verifier_id(env, version))


def score_default(env: str, record: Any, version: int | None = None) -> Scorecard:
    return default_verifier(env, version).score(record)


@contextmanager
def verifier_version(version: int) -> Iterator[None]:
    """Score with `<env>@<version>` everywhere inside the block (reproducing an older
    round's grades end to end, episodes' own scorecards included)."""
    token = _VERSION.set(version)
    try:
        yield
    finally:
        _VERSION.reset(token)


__all__ = [
    "ACH_V2",
    "ACH_V3",
    "AML_V2",
    "AML_V3",
    "CM_V3",
    "DEFAULT_VERSION",
    "ENV_NAMES",
    "PAY_V2",
    "PAY_V3",
    "RECON_V2",
    "RECON_V3",
    "VERIFIERS",
    "Verifier",
    "default_verifier",
    "default_verifier_id",
    "gated",
    "get_verifier",
    "score_default",
    "verifier_version",
]
