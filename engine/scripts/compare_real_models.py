"""Compare real models against the real-data baselines on the SAME test items.

Usage:
    uv run python scripts/compare_real_models.py results/real/models

The directory holds, per task, `<task>.prompts.jsonl` (written by
`scruple realdata prompt`) and `<task>.<model>.answers.jsonl` (one reply per id).
Baselines are fitted on train, chosen on dev, then scored on exactly the prompted
test ids. A paired bootstrap over items gives the difference between each model
and the TF-IDF baseline on the task's primary metric. Writes summary.json and
SUMMARY.md into the directory. Deterministic for fixed inputs.
"""

from __future__ import annotations

import json
import random
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from scruple.realdata import metrics as M
from scruple.realdata.baselines import BASELINES
from scruple.realdata.tasks import OutcomeTask, Task, get_task

N_BOOT = 2000
SEED = 0


def _ids(path: Path) -> list[str]:
    return [json.loads(line)["id"] for line in path.read_text().splitlines() if line.strip()]


def _answers(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if line.strip():
            row = json.loads(line)
            out[row["id"]] = row["answer"]
    return out


def _per_item(task: Task, insts: Sequence[Any], answers: dict[str, str]) -> list[Any]:
    """Per-item outcome: (gold, p) for outcome tasks, else 1/0 correctness."""
    rows: list[Any] = []
    for inst in insts:
        parsed = task.try_parse(answers.get(inst.id))
        if isinstance(task, OutcomeTask):
            p = float(parsed["p_relief"]) if parsed is not None else 0.5
            rows.append((int(inst.gold["relief"]), p))
        else:
            card = task.grade([inst], {inst.id: answers.get(inst.id, "")}, n_boot=1, seed=0)
            rows.append(1 if _primary(card) == 1.0 else 0)
    return rows


def _primary(card: dict[str, Any]) -> float:
    for key in ("issue", None):
        node = card.get(key, card) if key else card
        if isinstance(node, dict) and "accuracy" in node:
            return float(node["accuracy"]["value"])
    raise KeyError("no accuracy in card")


def _metric(task: Task) -> tuple[str, Callable[[list[Any]], float | None]]:
    if isinstance(task, OutcomeTask):
        return "auroc", lambda rows: M.auroc([g for g, _ in rows], [p for _, p in rows])
    return "accuracy", lambda rows: sum(rows) / len(rows) if rows else None


def _paired(a: list[Any], b: list[Any], f: Callable[[list[Any]], float | None]) -> dict[str, Any]:
    rng = random.Random(SEED)
    n = len(a)
    base_a, base_b = f(a), f(b)
    diffs: list[float] = []
    for _ in range(N_BOOT):
        idx = [rng.randrange(n) for _ in range(n)]
        va, vb = f([a[i] for i in idx]), f([b[i] for i in idx])
        if va is not None and vb is not None:
            diffs.append(va - vb)
    diffs.sort()
    lo, hi = diffs[int(0.025 * len(diffs))], diffs[int(0.975 * len(diffs)) - 1]
    point = None if base_a is None or base_b is None else round(base_a - base_b, 6)
    return {"diff": point, "ci95": [round(lo, 6), round(hi, 6)], "n_boot": len(diffs)}


def main(directory: str) -> None:
    root = Path(directory)
    summary: dict[str, Any] = {"note": "Real CFPB data; models answered blind; see SUMMARY.md"}
    for prompts in sorted(root.glob("*.prompts.jsonl")):
        name = prompts.name.removesuffix(".prompts.jsonl")
        task = get_task(name)
        ids = set(_ids(prompts))
        test = [i for i in task.instances("test", unseal=True) if i.id in ids]
        train, dev = task.instances("train"), task.instances("dev")
        metric_name, f = _metric(task)
        entries: dict[str, Any] = {}
        per_item: dict[str, list[Any]] = {}
        for bname in BASELINES:
            b = BASELINES[bname](task)
            b.fit(train, dev)
            ans = dict(zip([i.id for i in test], b.answer(test), strict=True))
            per_item[bname] = _per_item(task, test, ans)
            entries[bname] = {"kind": "baseline", metric_name: f(per_item[bname])}
        for af in sorted(root.glob(f"{name}.*.answers.jsonl")):
            model = af.name.removeprefix(name + ".").removesuffix(".answers.jsonl")
            ans = _answers(af)
            per_item[model] = _per_item(task, test, ans)
            valid = sum(task.try_parse(ans.get(i.id)) is not None for i in test)
            entries[model] = {
                "kind": "model",
                metric_name: f(per_item[model]),
                "valid_format": valid,
                "vs_tfidf_lr": _paired(per_item[model], per_item["tfidf_lr"], f),
            }
        summary[name] = {"n": len(test), "metric": metric_name, "entries": entries}

    (root / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    lines = [
        "# real models vs baselines, same items",
        "",
        "Real CFPB complaints (public domain). Sealed test split, seeded sample.",
        "Models answered blind through the harness described in docs/results/REAL_MODELS.md.",
        f"Paired bootstrap over items, {N_BOOT} resamples, seed {SEED}.",
        "",
    ]
    for name, block in summary.items():
        if not isinstance(block, dict):
            continue
        m = block["metric"]
        lines += [
            f"## {name} (n={block['n']}, {m})",
            "",
            f"| system | {m} | vs tfidf_lr (95% ci) |",
        ]
        lines += ["|---|---|---|"]
        for sys_name, e in block["entries"].items():
            v = e[m]
            cmp = e.get("vs_tfidf_lr")
            c = f"{cmp['diff']:+.3f} [{cmp['ci95'][0]:+.3f}, {cmp['ci95'][1]:+.3f}]" if cmp else ""
            lines.append(
                f"| {sys_name} | {v:.3f} | {c} |"
                if v is not None
                else f"| {sys_name} | n/a | {c} |"
            )
        lines.append("")
    (root / "SUMMARY.md").write_text("\n".join(lines))
    print("\n".join(lines))


if __name__ == "__main__":
    main(sys.argv[1])
