"""Build the data behind results/index.html from the engine's real result files.

Run from the engine directory:

    cd engine && uv run python ../results/build_data.py

It reads, and never changes:
  results/real/models/summary.json         real CFPB complaints, models vs baselines
  results/real/episodes/episodes.json      per case scores (models and reference policies)
  results/real/episodes/store-*/ep-*.json  the models' real call logs

Each call log is replayed through the engine, one call at a time, exactly as the CLI
ran it. The replay records what each call returned and what changed in the ledger,
then the verifiers score the case afresh. The script checks that the fresh score
matches episodes.json and stops if it does not. Output: results/data/*.json.
Cases are synthetic; the calls are the models' own.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from scruple.agents.dispute_episode import DisputeEpisode
from scruple.agents.errors import EpisodeError

ENGINE = Path.cwd()
SRC = ENGINE / "results" / "real"
OUT = ENGINE.parent / "results" / "data"
MODELS = ("haiku", "sonnet", "opus")
REFS = ("ref:evidence", "ref:always_accept", "ref:oracle")

# span colours, read by the page: f fg, d dim, g pine, b brass, v vermilion, s sky
Span = list[str]
Line = list[Span]


def usd(minor: int | str | None) -> str:
    if minor is None:
        return "-"
    m = int(minor)
    sign = "-" if m < 0 else ""
    return f"{sign}${abs(m) / 100:,.2f}"


EXP = {"USD": 2, "EUR": 2, "GBP": 2, "JPY": 0}  # as scruple/sim/fx.py


def money(minor: int | str | None, ccy: str) -> str:
    """An amount in the case's own currency; JPY has no minor unit."""
    if ccy == "USD" or minor is None:
        return usd(minor)
    m, e = int(minor), EXP.get(ccy, 2)
    v = abs(m) / 10**e
    return f"{'-' if m < 0 else ''}{ccy} {v:,.{e}f}"


def signed(minor: int) -> str:
    return ("+" if minor > 0 else "") + usd(minor)


def short_id(x: str) -> str:
    return x[:9] if x.startswith("ev-") else x


def args_text(tool: str, args: dict[str, Any] | None) -> str:
    if not args:
        return ""
    if tool == "get_evidence":
        return short_id(args.get("evidence_id", ""))
    if tool == "wait":
        return f"{args.get('days')} days"
    if tool == "represent":
        ids = args.get("evidence_ids") or []
        return f"{len(ids)} document{'s' if len(ids) != 1 else ''}"
    if tool == "refund":
        return usd(args.get("amount_minor"))
    if tool == "request_info":
        return ""
    if tool == "submit_decision":
        return "self-report"
    return ""


def merchant_delta(entry: Any) -> int:
    return sum(p.amount_minor for p in entry.postings if p.account.startswith("merchant"))


def ledger_lines(ep: DisputeEpisode, seen: set[int]) -> list[Line]:
    out: list[Line] = []
    for e in ep.session.journal():
        if e.entry_id in seen:
            continue
        seen.add(e.entry_id)
        d = merchant_delta(e)
        out.append(
            [
                ["ledger ", "d"],
                [f"#{e.entry_id} {e.kind}", "s"],
                ["  merchant ", "d"],
                [signed(d), "g" if d > 0 else "v" if d < 0 else "d"],
            ]
        )
    return out


def render(
    tool: str,
    args: dict[str, Any] | None,
    res: dict[str, Any] | None,
    err: str | None,
    before: dict[str, Any],
    ccy: str,
) -> list[Line]:
    """What the call returned, as short terminal lines (abridged, never invented)."""
    if err is not None:
        return [[["refused ", "v"], [err, "v"]]]
    assert res is not None
    L: list[Line] = []
    if tool == "get_case":
        c, st = res["case"], res["status"]
        L.append([["case   ", "d"], [f"{c['network']} {c['reason_code']}", "f"], ["  " + c["reason_title"], "d"]])
        L.append([["amount ", "d"], [money(c["amount_minor"], c["currency"]), "f"], ["  opened ", "d"], [c["opened_on"], "f"]])
        L.append([["needs  ", "d"], [", ".join(c["required_evidence"]) or "none", "b"]])
        dl = st.get("deadline")
        stage = [["stage  ", "d"], [st["stage"], "f"]]
        if dl:
            stage += [["  due ", "d"], [f"{dl['due']} ({dl['days_left']} days)", "f"]]
        L.append(stage)
        if st.get("allowed_actions"):
            L.append([["can    ", "d"], [" ".join(st["allowed_actions"]), "f"]])
    elif tool == "list_evidence":
        for d in res["evidence"]:
            L.append([[short_id(d["evidence_id"]) + "  ", "d"], [d["kind"], "f"], ["  " + d["issued_on"], "d"]])
        if not res["evidence"]:
            L.append([["no documents", "d"]])
    elif tool == "get_evidence":
        f = res.get("fields", {})
        L.append([[res["kind"], "f"], ["  issued " + res["issued_on"], "d"]])
        kv = "  ".join(f"{k}={v}" for k, v in f.items() if k != "ref")
        if kv:
            L.append([[kv[:90], "d"]])
    elif tool == "get_ledger_view":
        bal = res.get("balances", {})
        m = sum(v for k, v in bal.items() if k.startswith("merchant"))
        L.append([[f"{len(res['entries'])} entries", "f"], ["  merchant balance ", "d"], [usd(m), "g" if m >= 0 else "v"]])
    elif tool == "request_info":
        s = res.get("statement") or ""
        L.append([["cardholder says ", "d"], [f'"{s}"' if s else "(nothing)", "b"]])
        if res.get("new_evidence_ids"):
            L.append([["new documents ", "d"], [" ".join(map(short_id, res["new_evidence_ids"])), "f"]])
    elif tool in ("represent", "accept", "refund", "escalate"):
        L.append([[res.get("stage_before") or "-", "f"], [" -> ", "d"], [res.get("stage_after") or "-", "g"]])
    elif tool == "upload_evidence":
        L.append([["uploaded ", "d"], [short_id(res.get("evidence_id", "")), "b"]])
    elif tool == "wait":
        st = res["status"]
        L.append([["day ", "d"], [before["today"], "f"], [" -> ", "d"], [st["today"], "f"], ["  stage ", "d"], [st["stage"], "f"]])
        old = len(before.get("history", []))
        for h in st.get("history", [])[old:]:
            L.append([["event  ", "d"], [f"{h['event']}", "b"], [f"  {h['from_stage']} -> {h['to_stage']}  {h['on']}", "d"]])
        lapsed = before.get("deadline")
        if lapsed and args and int(args.get("days", 0)) > int(lapsed["days_left"]):
            L.append([["window ", "d"], [f"{lapsed['window']} was due {lapsed['due']}, {lapsed['days_left']} days left before this wait", "v"]])
    elif tool == "submit_decision":
        L.append([["closed  ", "d"], [res["resolution"], "f"], ["  path ", "d"], [" > ".join(res["path"]), "d"]])
        L.append([["cost    ", "d"], [usd(res["cost_usd_minor"]), "f"], ["  fees ", "d"], [usd(res["fees_usd_minor"]), "d"]])
        L.append(
            [
                ["owed    ", "d"],
                [money(res["liability_minor"], ccy), "f"],
                ["  cardholder got ", "d"],
                [money(res["cardholder_net_minor"], ccy), "g" if res["cardholder_net_minor"] == res["liability_minor"] else "v"],
            ]
        )
    return L


def replay(model: str, path: Path) -> dict[str, Any]:
    rec = json.loads(path.read_text())
    ep = DisputeEpisode(rec["seed"], rec["case_type"], nonce=rec["nonce"])
    seen: set[int] = set()
    ledger_lines(ep, seen)  # entries that exist before the agent acts
    pre = [
        {"entry": e.entry_id, "kind": e.kind, "merchant": merchant_delta(e)} for e in ep.session.journal()
    ]
    case = ep.session.case_view()
    start = ep.session.status()
    steps = []
    report = None
    for i, c in enumerate(rec["calls"]):
        tool, args = c["tool"], c["args"]
        before = ep.session.status()
        res, err = None, None
        try:
            res = ep.call(tool, args)
        except EpisodeError as exc:
            err = str(exc)
        lines = render(tool, args, res, err, before, case["currency"]) + ledger_lines(ep, seen)
        step: dict[str, Any] = {
            "i": i + 1,
            "tool": tool,
            "arg": args_text(tool, args),
            "day": before["today"],
            "stage": before["stage"],
            "ok": err is None,
            "out": lines,
        }
        if tool == "represent" and args:
            step["cited"] = [short_id(x) for x in args.get("evidence_ids", [])]
        if tool == "request_info" and args and args.get("question"):
            step["question"] = args["question"]
        if tool == "submit_decision" and args:
            report = args
        steps.append(step)
        if ep.closed:
            break
    score = ep.score
    if score is None:
        sys.exit(f"{model} {path.name}: episode did not close on replay")
    sd = json.loads(score.model_dump_json())
    truth = ep.session.truth.model_dump(mode="json")
    checks = [
        {"id": k["id"], "name": k["name"], "hard": k["hard"], "passed": k["passed"], "why": k["explanation"]}
        for k in sd["scorecard"]["checks"]
    ]
    return {
        "model": model,
        "seed": rec["seed"],
        "episode_id": ep.episode_id,
        "store_file": f"engine/results/real/episodes/store-{model}/{path.name}",
        "case": {
            "network": case["network"],
            "reason_code": case["reason_code"],
            "reason_title": case["reason_title"],
            "amount_minor": case["amount_minor"],
            "currency": case["currency"],
            "amount": money(case["amount_minor"], case["currency"]),
            "opened_on": case["opened_on"],
            "opening_stage": case["opening_stage"],
            "required_evidence": case["required_evidence"],
            "deadline": start.get("deadline"),
        },
        "pre_ledger": pre,
        "truth": {
            "family": truth["family"],
            "claim_valid": truth["claim_valid"],
            "liability_minor": truth["liability_minor"],
            "liability": money(truth["liability_minor"], case["currency"]),
        },
        "steps": steps,
        "report": report,
        "result": {
            "resolution": sd["resolution"],
            "path": sd["path"],
            "cost_minor": sd["cost_usd_minor"],
            "fees_minor": sd["fees_usd_minor"],
            "cardholder_net_minor": sd["cardholder_net_minor"],
            "liability_minor": sd["liability_minor"],
            "cardholder_net": money(sd["cardholder_net_minor"], case["currency"]),
            "liability": money(sd["liability_minor"], case["currency"]),
            "correct": sd["correct"],
            "trace_events": sd["trace_events"],
            "trace_head": sd["trace_head"],
            "gate": score.gate,
            "reward": round(score.reward, 6),
        },
        "checks": checks,
    }


def main() -> None:
    rows = json.loads((SRC / "episodes" / "episodes.json").read_text())
    by = {(r["system"], r["seed"]): r for r in rows}
    seeds = sorted({r["seed"] for r in rows})
    (OUT / "episodes").mkdir(parents=True, exist_ok=True)
    index: list[dict[str, Any]] = []
    for model in MODELS:
        for path in sorted((SRC / "episodes" / f"store-{model}").glob("ep-*.json")):
            ep = replay(model, path)
            row = by[(model, ep["seed"])]
            r = ep["result"]
            # the fresh replay must agree with the published scores
            fails = sorted(c["id"] for c in ep["checks"] if c["hard"] and not c["passed"])
            soft = sorted(c["id"] for c in ep["checks"] if not c["hard"] and not c["passed"])
            got = (r["cost_minor"] / 100, r["resolution"], r["gate"], fails, soft)
            want = (row["cost_usd"], row["resolution"], row["gate"], row["hard_failures"], row["soft_failures"])
            if got != want:
                sys.exit(f"replay mismatch {model} {ep['seed']}: {got} != {want}")
            ep["refs"] = {
                k.split(":")[1]: {"cost_usd": by[(k, ep["seed"])]["cost_usd"], "resolution": by[(k, ep["seed"])]["resolution"]}
                for k in REFS
            }
            name = f"{model}-{ep['seed']}.json"
            (OUT / "episodes" / name).write_text(json.dumps(ep, separators=(",", ":")) + "\n")
            index.append(
                {
                    "model": model,
                    "seed": ep["seed"],
                    "family": row["family"],
                    "claim_valid": ep["truth"]["claim_valid"],
                    "resolution": row["resolution"],
                    "cost_usd": row["cost_usd"],
                    "gate": row["gate"],
                    "fair": row["fair_outcome"],
                    "hard": row["hard_failures"],
                    "soft": row["soft_failures"],
                    "calls": len(ep["steps"]),
                }
            )
    refs = [
        {k: r[k] for k in ("system", "seed", "family", "cost_usd", "resolution", "gate", "fair_outcome")}
        for r in rows
        if r["system"] in REFS
    ]
    summary = (SRC / "episodes" / "SUMMARY.md").read_text()
    table = {}
    for line in summary.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 9 and cells[0] in MODELS + REFS:
            table[cells[0]] = {
                "n": int(cells[1]),
                "mean_cost": float(cells[2]),
                "vs_evidence": cells[3],
                "gate": cells[4],
                "reward": float(cells[5]),
                "won": cells[6],
                "fair": cells[7],
            }
    (OUT / "episodes.json").write_text(
        json.dumps({"seeds": seeds, "episodes": index, "refs": refs, "summary": table}, indent=1) + "\n"
    )
    real = json.loads((SRC / "models" / "summary.json").read_text())
    (OUT / "real_models.json").write_text(json.dumps(real, indent=1, sort_keys=True) + "\n")
    print(f"wrote {len(index)} episodes, {len(table)} summary rows to {OUT}")


if __name__ == "__main__":
    main()
