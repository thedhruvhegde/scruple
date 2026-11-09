"""Render an evidence pack as a PDF a reviewer can file. Uses fpdf2 (pure Python).

The PDF is a view of the JSON pack, not a separate source of truth: it prints the
pack hash so a reader can match it to the JSON. Rendering is deterministic: the
creation date is the time of the last audit entry in the pack, not the wall clock.
Core PDF fonts are Latin-1, so other characters are replaced with '?'.
"""

from __future__ import annotations

from fpdf import FPDF

from scruple.audit.report import usd
from scruple.ci.evidence import EvidencePack, last_timestamp
from scruple.gate.evalue import summary_lines


def _t(text: object) -> str:
    return str(text).encode("latin-1", "replace").decode("latin-1")


class _Doc(FPDF):
    def __init__(self, footer: str) -> None:
        super().__init__(format="A4")
        self._footer_text = footer

    def footer(self) -> None:
        self.set_y(-12)
        self.set_font("Helvetica", size=7)
        self.cell(0, 5, _t(f"{self._footer_text}  page {self.page_no()}"), align="C")


def render_pdf(pack: EvidencePack) -> bytes:
    sha = pack.pack_sha256
    doc = _Doc(f"scruple evidence pack {sha[:16]} - synthetic data")
    doc.set_creation_date(last_timestamp(pack))
    doc.set_title(_t(f"Evidence pack {pack.rule_id}@v{pack.version}"))
    doc.set_author("scruple")
    doc.set_auto_page_break(auto=True, margin=15)
    doc.add_page()
    width = doc.w - doc.l_margin - doc.r_margin

    def h(text: str) -> None:
        doc.ln(2)
        doc.set_font("Helvetica", style="B", size=12)
        doc.multi_cell(width, 7, _t(text))
        doc.set_font("Helvetica", size=9)

    def line(text: str) -> None:
        doc.set_x(doc.l_margin)
        doc.multi_cell(width, 5, _t(text))

    card = pack.scorecard
    doc.set_font("Helvetica", style="B", size=15)
    doc.multi_cell(width, 9, _t(f"Evidence pack: {pack.rule_id}@v{pack.version}"))
    doc.set_font("Helvetica", size=9)
    lc = pack.lifecycle
    line(
        f"Gate verdict: {lc.gate_verdict.upper()} (at {lc.gate_decision_id}, {lc.gate_stage}, "
        f"{lc.gate_decided_at.isoformat()})"
    )
    line(f"Current status: {lc.current_status}")
    if lc.rolled_back is not None:
        rb = lc.rolled_back
        restored = f"v{rb.restored_version}" if rb.restored_version is not None else "none"
        line(
            f"Rolled back: by {rb.actor} at {rb.at.isoformat()}"
            + (f" ({rb.decision_id})" if rb.decision_id else "")
            + f"; reason: {rb.reason}; restored: {restored}"
        )
    if lc.retired_by_version is not None:
        line(f"Retired: replaced by v{lc.retired_by_version}")
    line(f"Content hash: {pack.content_hash}")
    line(f"Pack SHA-256: {sha}")
    line(f"Config SHA-256: {pack.config_sha256}")
    line(
        "Seed commitments: "
        + ", ".join(f"{k} {v}" for k, v in sorted(pack.seed_commitments.items()))
    )
    line("Data: synthetic simulator data only. Thresholds are illustrative defaults.")

    h("Rule")
    line(card.rule_description)
    line(f"Logic: {card.rule_logic}")

    h("History")
    line(lc.statement)
    for ev in lc.events:
        if ev.decision_id:
            v = f" verdict {ev.verdict_at_decision}" if ev.verdict_at_decision else ""
            line(
                f"{ev.at.isoformat()}  {ev.decision_id}  {ev.stage}  {ev.outcome}{v}  "
                f"by {ev.actor}: {ev.detail}"
            )
        else:
            line(
                f"{ev.at.isoformat()}  {ev.event}  {ev.from_status} -> {ev.to_status}  "
                f"by {ev.actor}: {ev.detail}"
            )

    h("Decisions")
    for d in pack.decisions:
        line(f"{d.decision_id}  {d.stage.value}  {d.outcome.value}  by {d.actor}: {d.summary}")

    h("Regressions")
    if card.regressions:
        for r in card.regressions:
            line(f"[{r.kind}] {r.name}: {r.detail}")
    else:
        line("None found.")

    h("Statistics")
    s = card.statistics
    if s is not None:
        line(
            f"Mean regret improvement {usd(s.mean_improvement_usd_minor)} per case, "
            f"{s.confidence:.0%} interval [{usd(s.ci_low)}, {usd(s.ci_high)}], "
            f"{s.n_cases} held-out cases, method {s.method}."
        )
    if card.evalue is not None:
        for text in summary_lines(card.evalue):
            line(text)
    for st in pack.statistics.get("canary_stages", []):
        seq = st.get("sequential")
        if seq is not None:
            line(
                f"Canary {st['decision_id']} sequential test (p = "
                f"{seq['assignment_probability']:g}, alpha {seq['alpha']:g}): "
                f"{seq['decision']} after {seq['stopped_at']} of {seq['n_matched']} matched"
            )
        line(
            f"Canary {st['decision_id']} at {st['fraction']:.0%}: {st['outcome']}, treatment "
            f"{usd(st['treatment_mean_cost_usd_minor'])} vs control "
            f"{usd(st['control_mean_cost_usd_minor'])} "
            f"({st['matched_treatment']}/{st['matched_control']} matched)"
        )
    if pack.canary_ramp is not None:
        ramp = pack.canary_ramp
        stages = ", ".join(f"{p:g}%" for p in ramp.schedule.stages)
        line(f"Ramp: {ramp.status}, stages {stages}, current share {ramp.percent:g}%")

    h("Approvals")
    a = pack.approvals
    if a.policy is None:
        line("No approval policy in force (single approver path).")
    else:
        line(
            f"Policy: {a.policy.required} of {len(a.policy.approvers)} named approvers"
            + (f", role {a.policy.required_role} required" if a.policy.required_role else "")
        )
        for n in a.policy.approvers:
            line(f"  {n.name} ({', '.join(n.roles) or 'no role'})")
    for sat in a.satisfied:
        line(f"Satisfied: {', '.join(sat['approvers'])} (decision {sat['decision_id']})")
    if a.open_request is not None:
        line(f"Open request by {a.open_request.proposer}, {len(a.open_grants)} grant(s)")

    h("Audit chain proof")
    p = pack.audit.proof
    line(f"Segment seq {p.first_seq} to {p.last_seq} of {p.chain_entries} entries")
    line(f"Anchor (prev hash): {p.anchor_prev_hash}")
    line(f"Recomputed head: {p.recomputed_head}")
    line(f"Chain head: {p.chain_head}")
    line(f"Proof: {'OK' if p.ok else 'FAILED'}" + (f" ({p.problem})" if p.problem else ""))
    for e in pack.audit.entries:
        line(f"{e.seq:>5} {e.event_type:<22} {e.actor:<24} {e.entry_hash[:16]}")
    return bytes(doc.output())
