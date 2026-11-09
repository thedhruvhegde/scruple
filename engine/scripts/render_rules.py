"""Render docs/RULES.md from the simulator's own constants.

    uv run python scripts/render_rules.py > docs/RULES.md

`tests/test_sim_rules_doc.py` fails when the committed file differs from this output.
"""

import sys

from scruple.sim import ce3, monitoring
from scruple.sim.disputes import ALWAYS_HELD, DEFAULT_MIX, FULL_MIX, EngineParams
from scruple.sim.families import FAMILY_WEIGHTS
from scruple.sim.fees import DEFAULT_FEES
from scruple.sim.fx import BASE_USD_PER_UNIT, EXPONENT, MAX_DRIFT
from scruple.sim.issuer import IssuerParams
from scruple.sim.merchants import TIER_BPS, TIER_WEIGHTS
from scruple.sim.networks import load_networks
from scruple.sim.reason_codes import load_catalogue


def src(source: str | None, assumption: str) -> str:
    return source.replace(",", ", ") if source else f"ASSUMPTION {assumption}"


def usd(minor: int) -> str:
    return f"${minor / 100:,.2f}"


rows: list[tuple[str, str, str, str]] = []
R = rows.append

# 1. network time windows
for _net, rules in sorted(load_networks().items(), key=lambda x: x[0].value):
    for w in rules.windows:
        assumption = w.note.split(":")[0] if w.assumption else ""
        if w.assumption and not assumption.startswith("A-"):
            assumption = "A-14"
        R(
            (
                f"{rules.display_name} time window",
                w.window.value,
                f"{w.days} {w.day_kind.value} days ({w.actor})",
                src(w.source, assumption),
            )
        )
    for cat, wf in rules.workflows.items():
        R(
            (
                f"{rules.display_name} workflow",
                cat.value,
                wf.value,
                src(rules.workflow_source, "A-14"),
            )
        )

# 2. CE 3.0
R(("Visa CE 3.0", "dispute condition", ce3.CE3_CODE, "S3"))
R(("Visa CE 3.0", "prior age, lower bound (exclusive)", f"{ce3.MIN_AGE_DAYS} days", "S3, S4"))
R(("Visa CE 3.0", "prior age, upper bound (exclusive)", f"{ce3.MAX_AGE_DAYS} days", "S3, S4"))
R(("Visa CE 3.0", "qualifying prior transactions", f"at least {ce3.MIN_PRIORS}", "S3, S4"))
R(("Visa CE 3.0", "matching data elements", f"at least {ce3.MIN_MATCHES} of 4", "S2"))
R(("Visa CE 3.0", "one match must be", " or ".join(sorted(ce3.STRONG)), "S2"))
R(("Visa CE 3.0", "device id minimum length", f"{ce3.MIN_DEVICE_ID_LEN} characters", "S5"))
R(("Visa CE 3.0", "issuer outcome when met and submitted", "merchant prevails", "ASSUMPTION A-28"))

# 3. monitoring
R(("Visa VAMP", "programme start", "2025-04-01 (VDMP, VFMP retired)", "S12"))
R(
    (
        "Visa VAMP",
        "minimum monthly fraud + dispute count",
        f"{int(monitoring.VAMP_MIN_COUNT.value):,}",
        src(monitoring.VAMP_MIN_COUNT.source, "A-29"),
    )
)
for r in monitoring.VAMP_RATIO:
    R(
        (
            "Visa VAMP",
            f"excessive ratio from {r.effective}",
            f"{(r.value * 100).normalize()}%",
            src(r.source, "A-29"),
        )
    )
for r in monitoring.VAMP_FEE_PER_DISPUTE_USD:
    R(
        (
            "Visa VAMP",
            f"fee per dispute from {r.effective}",
            f"{usd(int(r.value))} ({r.note})",
            src(r.source, "A-29"),
        )
    )
for name, mins, ratios in (
    ("ECM", monitoring.ECM_MIN, monitoring.ECM_RATIO),
    ("HECM", monitoring.HECM_MIN, monitoring.HECM_RATIO),
):
    R((f"Mastercard {name}", "monthly chargebacks", f"at least {int(mins[0].value)}", "S14"))
    R(
        (
            f"Mastercard {name}",
            "chargeback ratio",
            f"at least {(ratios[0].value * 100).normalize()}%",
            "S14",
        )
    )
R(("Mastercard ECM", "exit", f"{monitoring.ECM_EXIT_MONTHS} months below threshold", "S15"))
R(("Mastercard ECM", "fines by month", ", ".join(usd(x) for x in monitoring.ECM_FINES), "S14"))
R(("Mastercard HECM", "fines by month", ", ".join(usd(x) for x in monitoring.HECM_FINES), "S14"))
R(
    (
        "Mastercard ECM / HECM",
        "ratio denominator",
        "previous month's settled transactions",
        "ASSUMPTION A-29",
    )
)
R(("Mastercard ECM / HECM", "fines after month 3", "stay at the month-3 amount", "ASSUMPTION A-29"))

# 4. fees
for net, fs in sorted(DEFAULT_FEES.items(), key=lambda x: x[0].value):
    for stage, amt in fs.on_enter.items():
        R((f"{net.value.title()} fee", f"on entering {stage.value}", usd(amt), "ASSUMPTION A-17"))
    R((f"{net.value.title()} fee", "arbitration loss", usd(fs.arbitration_loss), "ASSUMPTION A-17"))

# 5. issuer
ip = IssuerParams()
for stage in ip.base:
    R(
        (
            "Issuer odds",
            f"{stage.value}: base, evidence weight, detection",
            f"{ip.base[stage]}, {ip.weight[stage]}, {ip.p_detect[stage]}",
            "ASSUMPTION A-21",
        )
    )
R(
    (
        "Issuer odds",
        "supporting bonus (max 2 kinds), cap",
        f"{ip.supporting_bonus}, {ip.cap}",
        "ASSUMPTION A-21",
    )
)
R(("Issuer", "settled refund record submitted", "merchant prevails (moot)", "ASSUMPTION A-23"))
ep = EngineParams()
R(
    (
        "Engine timing",
        "issuer answers a response after",
        f"{ep.issuer_response_days} days",
        "ASSUMPTION A-25",
    )
)
R(
    (
        "Engine timing",
        "arbitration ruling after",
        f"{ep.arbitration_ruling_days} days",
        "ASSUMPTION A-25",
    )
)
R(
    (
        "Engine timing",
        "missed merchant deadline",
        "inquiry: becomes chargeback; later: lost",
        "ASSUMPTION A-25",
    )
)
R(("Calendar", "business days", "Monday to Friday, no holidays", "ASSUMPTION A-24"))
R(("Calendar", "action on the due date", "on time", "ASSUMPTION A-24"))

# 6. generator
m = DEFAULT_MIX
R(
    (
        "Generator: evidence",
        "always held",
        ", ".join(k.value for k in ALWAYS_HELD),
        "ASSUMPTION A-19",
    )
)
R(
    (
        "Generator: evidence",
        "required kind held (merchant right / claim valid)",
        f"{m.p_required_held_invalid} / {m.p_required_held_valid}",
        "ASSUMPTION A-19",
    )
)
R(
    (
        "Generator: evidence",
        "supporting kind held (merchant right / claim valid)",
        f"{m.p_supporting_held_invalid} / {m.p_supporting_held_valid}",
        "ASSUMPTION A-19",
    )
)
R(
    (
        "Generator: evidence",
        "FULL_MIX missing / forged rates",
        f"{FULL_MIX.p_missing_evidence} / {FULL_MIX.p_forged_evidence}",
        "ASSUMPTION A-20",
    )
)
R(
    (
        "Generator: families",
        "suggested weights",
        ", ".join(f"{f.value} {w}" for f, w in FAMILY_WEIGHTS.items()),
        "ASSUMPTION A-22",
    )
)
R(
    (
        "Generator: mix",
        "network weights (default)",
        ", ".join(f"{n.value} {w}" for n, w in m.network_weights.items()),
        "ASSUMPTION A-32",
    )
)
R(("Generator: mix", "share opening as inquiry", str(m.p_inquiry), "ASSUMPTION A-32"))
R(("Generator: mix", "claim valid without family weights", str(m.p_claim_valid), "ASSUMPTION A-32"))
R(
    (
        "Generator: mix",
        "amount (USD minor)",
        "max(500, lognormal(4.4, 0.9) x 100)",
        "ASSUMPTION A-32",
    )
)
R(
    (
        "Generator: mix",
        "processed; opened after processing",
        "start + 0-60 days; 3-90 days",
        "ASSUMPTION A-32",
    )
)
R(
    (
        "Generator: mix",
        "FULL_MIX foreign currency",
        f"{FULL_MIX.p_foreign_currency} (EUR, GBP or JPY)",
        "ASSUMPTION A-27",
    )
)
R(
    (
        "Generator: refunds",
        "FULL_MIX refund race; settles",
        f"{FULL_MIX.p_refund_race}; -5 to +10 days of opening",
        "ASSUMPTION A-23",
    )
)
R(
    (
        "Generator: split orders",
        "FULL_MIX split share; parcels; arrival on valid claim",
        f"{FULL_MIX.p_split_shipment}; 2-3; 0.5 each",
        "ASSUMPTION A-26",
    )
)
R(
    (
        "Generator: partial disputes",
        "FULL_MIX share; disputed part",
        f"{FULL_MIX.p_partial_dispute}; 20-80% of capture",
        "ASSUMPTION A-26",
    )
)
R(
    (
        "Generator: CE 3.0 histories",
        "FULL_MIX share; priors; in window",
        f"{FULL_MIX.p_ce3_history}; 2-4; 80%",
        "ASSUMPTION A-28",
    )
)
R(
    (
        "Generator: difficulty",
        "family tempering; d=0.5",
        "w ** (2 d); equals FULL_MIX",
        "ASSUMPTION A-30",
    )
)
R(("FX", "minor-unit exponents", ", ".join(f"{c} {e}" for c, e in EXPONENT.items()), "S17"))
R(
    (
        "FX",
        "base USD per unit; daily drift",
        ", ".join(f"{c} {v}" for c, v in BASE_USD_PER_UNIT.items() if c != "USD")
        + f"; at most {(MAX_DRIFT * 100).normalize()}%",
        "ASSUMPTION A-27",
    )
)
R(
    (
        "Merchants",
        "risk tier weights",
        ", ".join(f"{t.value} {w}" for t, w in TIER_WEIGHTS.items()),
        "ASSUMPTION A-18",
    )
)
R(
    (
        "Merchants",
        "chargeback bps by tier",
        ", ".join(f"{t.value} {lo}-{hi}" for t, (lo, hi) in TIER_BPS.items()),
        "ASSUMPTION A-18",
    )
)
R(("Held-out planning", "cap, regression, sd per case", "$100, $5, $20", "ASSUMPTION A-31"))

codes = []
for c in load_catalogue().codes:
    codes.append(
        (
            f"{c.network.value} {c.code}",
            c.title,
            c.category.value,
            src(c.category_source, "A-16"),
            f"{c.response_days} days",
            src(c.response_days_source, "A-14"),
            ", ".join(k.value for k in c.required_evidence),
            "S1 text; mapping ASSUMPTION A-15",
        )
    )

out = [
    "# Rules: every simulator rule and where it comes from",
    "",
    "Each row is one rule or parameter the dispute simulator (`scruple/sim/`) uses, with",
    "its value and its basis: a source id from docs/SOURCES.md, or an ASSUMPTION id from",
    "docs/ASSUMPTIONS.md when it is not sourced. Values are rendered from the code itself",
    "(`tests/test_sim_rules_doc.py` checks this file against the code), so they cannot",
    "drift. Sourced values are as publicly reported (see the caveats in SOURCES.md: S2 to",
    "S16 were read through search summaries, not the primary rule text). Not legal or",
    "network advice.",
    "",
    "## Rules and parameters",
    "",
    "| rule | parameter | value | source |",
    "|---|---|---|---|",
    *[f"| {a} | {b} | {c} | {d} |" for a, b, c, d in rows],
    "",
    "## Reason codes (card-present and card-absent)",
    "",
    "Codes and titles from S1 (CC BY 4.0). Required evidence is our mapping of S1's",
    "key-evidence text (A-15); the Mastercard category mapping is ours (A-16).",
    "",
    "| code | title | category | category source | response window | window source "
    "| required evidence | evidence basis |",
    "|---|---|---|---|---|---|---|---|",
    *[f"| {' | '.join(r)} |" for r in codes],
    "",
]
sys.stdout.write("\n".join(out))
