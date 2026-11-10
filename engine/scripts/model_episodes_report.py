"""Score real models' dispute episodes against reference policies on the same seeds.

Usage:
    uv run python scripts/model_episodes_report.py OUT_DIR LABEL=STORE [LABEL=STORE ...]
    uv run python scripts/model_episodes_report.py --profile all LABEL=STORE [...]

Each STORE is an episode store written by `scruple episode` (one episode per seed,
played by an external agent through the CLI). The same seeds are replayed in-process
by the reference policies (evidence, always_accept, oracle). Writes OUT_DIR/episodes.json
(per-episode rows) and OUT_DIR/SUMMARY.md. Synthetic cases; real model behaviour.

`--profile P` (merchant, balanced, compliance, or `all`; repeatable) instead re-scores
the same episodes under each verifier profile (`scruple.verifiers.profiles`) and prints
one markdown table: gate passes, mean reward and FAIR-01 failures per system and
profile. It writes nothing.
"""

from __future__ import annotations

import json
import random
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from scruple.agents.dispute_baselines import (
    always_accept,
    evidence_policy,
    oracle_policy,
    run_policy,
)
from scruple.agents.dispute_episode import DisputeEpisode
from scruple.agents.store import EpisodeStore
from scruple.verifiers.profiles import VerifierProfile, score_profile, won_against_valid_claim
from scruple.verifiers.record import EpisodeRecord

N_BOOT = 2000


def _row(label: str, score: dict[str, Any]) -> dict[str, Any]:
    sc = score["scorecard"]
    return {
        "system": label,
        "seed": score["seed"],
        "family": score["family"],
        "cost_usd": score["cost_usd_minor"] / 100,
        "resolution": score["resolution"],
        "gate": sc["gate"],
        "reward": sc["reward"],
        "quality": round(sc["quality"], 6),
        "hard_failures": sorted(sc["hard_failures"]),
        "soft_failures": sorted(c["id"] for c in sc["checks"] if not c["hard"] and not c["passed"]),
        "fair_outcome": next((c["passed"] for c in sc["checks"] if c["id"] == "OUT-01"), None),
        "tool_calls": score.get("tool_calls"),
        "tool_errors": score.get("tool_errors"),
    }


def _store_rows(label: str, store: str) -> list[dict[str, Any]]:
    """Replay each stored episode from its call log; the verifiers score it afresh."""
    st = EpisodeStore(Path(store))
    rows = []
    for meta in sorted(Path(store).glob("ep-*.json")):
        ep = st.load(meta.stem)
        if isinstance(ep, DisputeEpisode) and ep.score is not None:
            rows.append(_row(label, json.loads(ep.score.model_dump_json())))
    return rows


def _policy_rows(seeds: list[int]) -> list[dict[str, Any]]:
    rows = []
    for name, pol, ref in (
        ("ref:evidence", evidence_policy, False),
        ("ref:always_accept", always_accept, False),
        ("ref:oracle", oracle_policy, True),
    ):
        for s in seeds:
            ep = run_policy(pol, s, None, reference=ref)
            assert ep.score is not None
            rows.append(_row(name, json.loads(ep.score.model_dump_json())))
    return rows


def _store_records(store: str) -> list[EpisodeRecord]:
    st = EpisodeStore(Path(store))
    out = []
    for meta in sorted(Path(store).glob("ep-*.json")):
        ep = st.load(meta.stem)
        if isinstance(ep, DisputeEpisode) and ep.score is not None:
            out.append(ep.record())
    return out


def _policy_records(seeds: list[int]) -> list[tuple[str, EpisodeRecord]]:
    out = []
    for name, pol, ref in (
        ("ref:evidence", evidence_policy, False),
        ("ref:always_accept", always_accept, False),
        ("ref:oracle", oracle_policy, True),
    ):
        out += [(name, run_policy(pol, s, None, reference=ref).record()) for s in seeds]
    return out


def profile_table(pairs: list[str], profiles: list[VerifierProfile]) -> str:
    """Gate passes, mean reward and FAIR-01 failures per system under each profile."""
    recs: list[tuple[str, EpisodeRecord]] = []
    for p in pairs:
        label, store = p.split("=", 1)
        recs += [(label, r) for r in _store_records(store)]
    seeds = sorted({r.seed for _, r in recs})
    recs += _policy_records(seeds)
    systems = list(dict.fromkeys(label for label, _ in recs))
    head = "| system | n | valid-claim wins (true fraud) |" + "".join(
        f" {p.value}: gate | {p.value}: reward |" for p in profiles
    )
    lines = [head, "|---|---|---|" + "---|---|" * len(profiles)]
    for s in systems:
        rs = [r for label, r in recs if label == s]
        wins = [r for r in rs if won_against_valid_claim(r)]
        fraud = sum(r.case.truth.family.value == "true_fraud" for r in wins)
        row = f"| {s} | {len(rs)} | {len(wins)} ({fraud}) |"
        for p in profiles:
            cards = [score_profile(r, p) for r in rs]
            gate = sum(c.gate for c in cards)
            fair = sum("FAIR-01" in c.hard_failures for c in cards)
            reward = sum(c.reward for c in cards) / len(cards)
            note = f" (FAIR-01 x{fair})" if fair else ""
            row += f" {gate}/{len(rs)}{note} | {reward:.3f} |"
        lines.append(row)
    return "\n".join(lines)


def _paired_cost(a: dict[int, float], b: dict[int, float]) -> tuple[float, float, float]:
    seeds = sorted(set(a) & set(b))
    d = [a[s] - b[s] for s in seeds]
    rng = random.Random(0)
    boots = sorted(sum(d[rng.randrange(len(d))] for _ in d) / len(d) for _ in range(N_BOOT))
    return sum(d) / len(d), boots[int(0.025 * N_BOOT)], boots[int(0.975 * N_BOOT) - 1]


def main(out_dir: str, pairs: list[str]) -> None:
    rows: list[dict[str, Any]] = []
    for p in pairs:
        label, store = p.split("=", 1)
        rows += _store_rows(label, store)
    seeds = sorted({r["seed"] for r in rows})
    rows += _policy_rows(seeds)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "episodes.json").write_text(json.dumps(rows, indent=1, sort_keys=True) + "\n")

    systems = list(dict.fromkeys(r["system"] for r in rows))
    by = {s: [r for r in rows if r["system"] == s] for s in systems}
    ev_cost = {r["seed"]: r["cost_usd"] for r in by["ref:evidence"]}
    lines = [
        "# real models on simulated disputes",
        "",
        f"Seeds {seeds[0]}-{seeds[-1]} ({len(seeds)} cases, one per seed), FULL_MIX engine.",
        "Models played through the `scruple episode` CLI only; references replay the same seeds.",
        "Cost = merchant net loss in USD (lower is better). Gate = no hard verifier failure.",
        "",
        "| system | n | mean cost $ | vs evidence ref, 95% ci | gate pass | mean reward |"
        " won/partial | fair outcome (OUT-01) | top hard failures |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for s in systems:
        rs = by[s]
        n = len(rs)
        cost = sum(r["cost_usd"] for r in rs) / n
        if s == "ref:evidence":
            cmp = ""
        else:
            m, lo, hi = _paired_cost({r["seed"]: r["cost_usd"] for r in rs}, ev_cost)
            cmp = f"{m:+.2f} [{lo:+.2f}, {hi:+.2f}]"
        gate = sum(r["gate"] for r in rs)
        reward = sum(r["reward"] for r in rs) / n
        won = sum(r["resolution"] in ("won", "partial") for r in rs)
        fair = sum(bool(r["fair_outcome"]) for r in rs)
        fails = Counter(f for r in rs for f in r["hard_failures"]).most_common(4)
        ftxt = ", ".join(f"{k} x{v}" for k, v in fails) or "none"
        lines.append(
            f"| {s} | {n} | {cost:.2f} | {cmp} | {gate}/{n} | {reward:.3f} | {won}/{n} "
            f"| {fair}/{n} | {ftxt} |"
        )
    lines.append("")
    (out / "SUMMARY.md").write_text("\n".join(lines))
    print("\n".join(lines))


def _cli(argv: list[str]) -> None:
    profiles: list[VerifierProfile] = []
    rest: list[str] = []
    it = iter(argv)
    for a in it:
        if a == "--profile":
            v = next(it)
            profiles += list(VerifierProfile) if v == "all" else [VerifierProfile(v)]
        else:
            rest.append(a)
    if profiles:
        print(profile_table(rest, list(dict.fromkeys(profiles))))
    else:
        main(rest[0], rest[1:])


if __name__ == "__main__":
    _cli(sys.argv[1:])
