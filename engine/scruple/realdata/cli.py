"""`scruple realdata ...`: verify data, write prompts, grade answers, run baselines.

    scruple realdata verify
    scruple realdata prompt --task regime-2015 --split test --n 200 --seed 1 --out p.jsonl
    scruple realdata grade --task regime-2015 --answers a.jsonl [--prompts p.jsonl] --out card.json
    scruple realdata run --task regime-2015 --prompts p.jsonl --provider anthropic --out a.jsonl
    scruple realdata baselines [--task T] [--out results/real]
    scruple realdata build-splits --src ~/.cache/scruple/real-data
    scruple realdata fetch-v2 --dest ~/.cache/scruple/real-data
    scruple realdata build-v2 --src ~/.cache/scruple/real-data

Prompt files have one JSON object per line: id, task, split, system, prompt.
Answer files: id, answer (the raw model reply). Missing answers for prompted ids
count as wrong when --prompts is given.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from scruple.realdata.tasks import TASKS, get_task, sample

RESULTS_DIR = Path("results/real")


def _write_json(path: Path | None, obj: Any) -> None:
    text = json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if path is None:
        sys.stdout.write(text)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        obj = json.loads(line)
        if not isinstance(obj, dict):
            raise ValueError(f"{path}:{n}: not a JSON object")
        rows.append(obj)
    return rows


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8",
    )


def cmd_verify(_args: argparse.Namespace) -> int:
    from scruple.realdata.regs import paragraph_ids
    from scruple.realdata.splits import SPLITS, load_manifest, load_split

    m = load_manifest()
    for ds in m["datasets"]:
        for s in SPLITS:
            rows = load_split(ds, s, unseal=True)
            print(f"{ds}/{s}: {len(rows)} rows, sha256 ok")
    print(f"regs: {len(paragraph_ids())} paragraph ids")
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    from scruple.realdata.splits import build_splits

    m = build_splits(Path(args.src), Path(args.out))
    for ds, e in m["datasets"].items():
        sizes = ", ".join(f"{s} {v['n']}" for s, v in e["splits"].items())
        print(f"{ds}: {sizes}")
    return 0


def cmd_prompt(args: argparse.Namespace) -> int:
    task = get_task(args.task)
    insts = sample(task.instances(args.split, unseal=args.split == "test"), args.n, args.seed)
    rows = [
        {
            "id": i.id,
            "task": task.id,
            "split": args.split,
            "system": task.system_prompt,
            "prompt": task.render_prompt(i),
        }
        for i in insts
    ]
    _write_jsonl(Path(args.out), rows)
    print(f"wrote {len(rows)} prompts for {task.id}/{args.split} to {args.out}")
    return 0


def _answers(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in _read_jsonl(path):
        i, a = row.get("id"), row.get("answer")
        if not isinstance(i, str) or not isinstance(a, str):
            raise ValueError(f"{path}: every line needs string 'id' and 'answer'")
        if i in out:
            raise ValueError(f"{path}: duplicate answer for {i}")
        out[i] = a
    return out


def cmd_grade(args: argparse.Namespace) -> int:
    task = get_task(args.task)
    answers = _answers(Path(args.answers))
    by_id = {i.id: i for i in task.instances(args.split, unseal=args.split == "test")}
    if args.prompts:
        ids = [str(r["id"]) for r in _read_jsonl(Path(args.prompts))]
    else:
        ids = sorted(answers)
    missing = [i for i in ids if i not in by_id]
    if missing or set(answers) - set(ids):
        bad = missing or sorted(set(answers) - set(ids))
        raise KeyError(f"ids not in {task.id}/{args.split} or not prompted: {bad[:5]}")
    card = task.grade([by_id[i] for i in ids], answers, n_boot=args.n_boot, seed=args.seed)
    card["split"] = args.split
    card["answers_file"] = Path(args.answers).name
    _write_json(Path(args.out) if args.out else None, card)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from scruple.realdata.llm import PromptRecord, answer_prompts, make_runner

    prompts = [PromptRecord.model_validate(r) for r in _read_jsonl(Path(args.prompts))]
    runner = make_runner(
        args.provider, model=args.model, base_url=args.base_url, max_tokens=args.max_tokens
    )
    try:
        answers = answer_prompts(runner, prompts)
    finally:
        runner.close()
    _write_jsonl(Path(args.out), [a.model_dump() for a in answers])
    errors = sum(1 for a in answers if a.error)
    print(f"wrote {len(answers)} answers ({errors} request errors) to {args.out}")
    return 0


def cmd_baselines(args: argparse.Namespace) -> int:
    from scruple.realdata.baselines import BASELINES, baseline_results

    names = [args.task] if args.task else sorted(TASKS)
    which = args.baseline or list(BASELINES)
    for name in names:
        res = baseline_results(name, baselines=which, n_boot=args.n_boot, seed=args.seed)
        out = Path(args.out) / f"{name}.json"
        _write_json(out, res)
        print(f"{name}: wrote {out}")
    return 0


def cmd_build_v2(args: argparse.Namespace) -> int:
    from scruple.realdata.v2 import build_v2

    m = build_v2(Path(args.src), Path(args.out))
    for rel, meta in m["files"].items():
        print(f"v2/{rel}: {meta['rows']} rows, sha256 {meta['sha256'][:16]}")
    return 0


def cmd_fetch_v2(args: argparse.Namespace) -> int:  # pragma: no cover - network
    from scruple.realdata.v2 import fetch_v2

    for path in fetch_v2(Path(args.dest)):
        print(f"fetched {path}, sha256 ok")
    return 0


def add_realdata_parser(sub: Any) -> None:
    p = sub.add_parser("realdata", help="real-data evals on public CFPB complaints")
    rs = p.add_subparsers(dest="realdata_cmd", required=True)

    v = rs.add_parser("verify", help="check committed splits and regs against the manifest")
    v.set_defaults(fn=cmd_verify)

    b = rs.add_parser("build-splits", help="rebuild data/real from the fetched sources")
    b.add_argument("--src", required=True, help="dir written by scripts/fetch_real_data.sh")
    b.add_argument("--out", default="data/real")
    b.set_defaults(fn=cmd_build)

    b2 = rs.add_parser("build-v2", help="rebuild data/real/v2 from the fetched sources")
    b2.add_argument("--src", required=True, help="dir with the v1 and v2 raw inputs")
    b2.add_argument("--out", default="data/real")
    b2.set_defaults(fn=cmd_build_v2)

    f2 = rs.add_parser("fetch-v2", help="download the v2 raw inputs (anonymous git)")
    f2.add_argument("--dest", required=True)
    f2.set_defaults(fn=cmd_fetch_v2)

    pr = rs.add_parser("prompt", help="write a JSONL of prompts for a model")
    pr.add_argument("--task", required=True, choices=sorted(TASKS))
    pr.add_argument("--split", default="dev", choices=["train", "dev", "test"])
    pr.add_argument("--n", type=int, default=None, help="sample size (default: all)")
    pr.add_argument("--seed", type=int, default=0)
    pr.add_argument("--out", required=True)
    pr.set_defaults(fn=cmd_prompt)

    g = rs.add_parser("grade", help="grade a JSONL of answers and write a scorecard")
    g.add_argument("--task", required=True, choices=sorted(TASKS))
    g.add_argument("--answers", required=True)
    g.add_argument("--prompts", default=None, help="prompt file; unanswered prompts are wrong")
    g.add_argument("--split", default="test", choices=["train", "dev", "test"])
    g.add_argument("--n-boot", type=int, default=1000)
    g.add_argument("--seed", type=int, default=0)
    g.add_argument("--out", default=None, help="scorecard path (default: stdout)")
    g.set_defaults(fn=cmd_grade)

    r = rs.add_parser("run", help="answer a prompt file with a hosted model (needs an API key)")
    r.add_argument("--prompts", required=True)
    r.add_argument("--provider", choices=["anthropic", "openai"], default="anthropic")
    r.add_argument("--model", default=None)
    r.add_argument("--base-url", default=None)
    r.add_argument("--max-tokens", type=int, default=256)
    r.add_argument("--out", required=True)
    r.set_defaults(fn=cmd_run)

    bl = rs.add_parser("baselines", help="fit baselines on train, report on the sealed test")
    bl.add_argument("--task", default=None, choices=sorted(TASKS))
    bl.add_argument("--baseline", action="append", default=None)
    bl.add_argument("--n-boot", type=int, default=1000)
    bl.add_argument("--seed", type=int, default=0)
    bl.add_argument("--out", default=str(RESULTS_DIR))
    bl.set_defaults(fn=cmd_baselines)
