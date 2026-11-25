"""The honest projection of a trajectory (docs/math/10_ledger_anchored_reward.md, Theorem 4).

`recon_projection(record)` builds, from a closed recon episode, the calls of its
honest projection: keep the agent artefacts the ground truth endorses (matches of
clean pairs, and the full treatment of every break the episode cleared), escalate
every other line the episode handled, and submit a report with no claims. Theorem 4
says each anchored soft check (REC-CLEAR, REC-MATCH, REC-COVER) scores at least as
much on the projection, and the projection passes every hard check, so the reward of
any trajectory exceeds that of its projection by at most the weight share of the
checks not proven anchored (REC-TIE2: 1/6 on recon@2).

`aml_projection(record)`: the same reads and decision, citing the same transfers, with
only the indicators that hold (if any does), and a report with no claims.

`projection_gap(env, record, case_type, verifier)` replays the projection and returns
`(reward(record), reward(projection), per-check scores of both)`.
"""

from __future__ import annotations

from typing import Any

from scruple.finops.envs import get_env
from scruple.sim.aml.indicators import Indicator, holds
from scruple.verifiers.aml_record import AmlRecord
from scruple.verifiers.recon import cleared
from scruple.verifiers.recon_record import ReconRecord
from scruple.verifiers.trace import EventKind

Calls = list[tuple[str, dict[str, Any] | None]]


def _reads(record: Any) -> Calls:
    """The agent's successful read calls, in order (reads change nothing)."""
    spec = get_env(_env_of(record)).tools
    reads = {t.name for t in spec if t.scope.value == "read"}
    return [
        (str(e.tool), e.args)
        for e in record.trace
        if e.kind is EventKind.TOOL_CALL and e.ok and e.tool in reads
    ]


def _env_of(record: Any) -> str:
    return "recon" if isinstance(record, ReconRecord) else "aml"


def recon_projection(r: ReconRecord) -> Calls:
    calls: Calls = _reads(r)
    clean = {(s, lg) for s, lg in r.case.truth.clean}
    done_s: set[str] = set()
    done_b: set[str] = set()
    for ms, ml in r.outcome.matches:  # clean pairs matched one to one
        if len(ms) == 1 and len(ml) == 1 and (ms[0], ml[0]) in clean:
            calls.append(("match", {"statement_line_ids": list(ms), "ledger_line_ids": list(ml)}))
            done_s |= set(ms)
            done_b |= set(ml)
    for b in r.case.truth.breaks:  # cleared breaks keep the treatment they got
        if not cleared(r, b):
            continue
        t = next(t for t in b.treatments if _treated(r, t, b))
        if t.match is not None:
            calls.append(
                (
                    "match",
                    {"statement_line_ids": list(t.match[0]), "ledger_line_ids": list(t.match[1])},
                )
            )
        for a in t.adjustments:
            args: dict[str, Any] = {
                "kind": a.kind,
                "amount_minor": a.amount_minor,
                "statement_line_ids": list(a.statement_line_ids),
                "ledger_line_ids": list(a.ledger_line_ids),
            }
            if a.kind == "missing" and a.offset_account.startswith("ar:"):
                args["invoice_id"] = a.offset_account[3:]
            calls.append(("adjust", args))
        if t.timing:
            calls.append(("mark_timing", {"ledger_line_ids": list(t.timing)}))
        if t.escalate_statement or t.escalate_ledger:
            calls.append(
                (
                    "escalate",
                    {
                        "statement_line_ids": list(t.escalate_statement),
                        "ledger_line_ids": list(t.escalate_ledger),
                        "reason": "kept",
                    },
                )
            )
        done_s |= set(b.statement_line_ids)
        done_b |= set(b.ledger_line_ids)
    handled_s = {x.line_id for x in r.statement.lines} - set(r.outcome.unhandled_statement)
    handled_b = {x.line_id for x in r.case.ledger_lines} - set(r.outcome.unhandled_ledger)
    rest_s = sorted(handled_s - done_s)
    rest_b = sorted(handled_b - done_b)
    for i in range(0, max(len(rest_s), len(rest_b)), 20):  # the tool takes 20 ids per side
        s, lg = rest_s[i : i + 20], rest_b[i : i + 20]
        if s or lg:
            calls.append(
                (
                    "escalate",
                    {"statement_line_ids": s, "ledger_line_ids": lg, "reason": "projection"},
                )
            )
    calls.append(("submit_decision", {"explanation": "Projection of the episode."}))
    return calls


def _treated(r: ReconRecord, t: Any, b: Any) -> bool:
    from scruple.verifiers.recon import treated_as

    return treated_as(r, t, b)


def aml_projection(r: AmlRecord) -> Calls:
    calls: Calls = []
    decision = None
    for e in r.trace:
        if e.kind is not EventKind.TOOL_CALL or not e.ok:
            continue
        if e.tool in ("close_alert", "escalate", "file_sar"):
            decision = (str(e.tool), dict(e.args or {}))
            break
        if e.tool in (
            "get_alert",
            "get_account",
            "list_transactions",
            "get_transactions",
            "get_case_notes",
        ):
            calls.append((str(e.tool), e.args))
    if decision is not None:
        tool, args = decision
        if tool in ("escalate", "file_sar"):
            sid = r.case.subject_id
            accounts = {a.account_id: a for a in r.case.accounts}
            known = {t.txn_id: t for t in r.case.transfers}
            txns = [known[x] for x in args.get("txn_ids", ()) if x in known]
            own = [t for t in txns if sid in (t.src, t.dst)]
            good = [
                i
                for i in args.get("indicators", ())
                if holds(Indicator(i), r.case.subject, accounts, own, r.case.alert.raised_on)
            ]
            if good:
                args["indicators"] = good
        calls.append((tool, args))
    calls.append(("submit_decision", {"explanation": "Projection of the episode."}))
    return calls


def projection_gap(
    env: str, record: Any, case_type: str | None, verifier: str
) -> tuple[float, float, dict[str, tuple[float, float]]]:
    """(reward of the record, reward of its projection, per-check scores of both)."""
    e = get_env(env)
    v = e.verifier(verifier)
    calls = recon_projection(record) if env == "recon" else aml_projection(record)
    cls: Any = e.episode_cls
    proj = cls.replay(record.seed, case_type, calls)
    a, b = v.score(record), v.score(proj.record())
    per = {c.id: (c.score, d.score) for c, d in zip(a.checks, b.checks, strict=True)}
    return a.reward, b.reward, per


def project(
    env: str, policy: Any, seed: int, case_type: str | None, verifier: str
) -> tuple[float, float, dict[str, tuple[float, float]]]:
    """Play `policy`, then compare its reward with its honest projection's."""
    from scruple.finops.policies import run_policy

    e = get_env(env)
    ep = run_policy(e.episode_cls, policy, seed, case_type)
    return projection_gap(env, ep.record(), case_type, verifier)


def _project_item(
    item: tuple[str, Any, int, str | None, str],
) -> tuple[float, float, dict[str, tuple[float, float]]]:
    return project(*item)


__all__ = ["aml_projection", "project", "projection_gap", "recon_projection"]
