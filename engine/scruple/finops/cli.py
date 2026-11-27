"""`scruple tasks ...` (task packaging and grading for labs) and `scruple finops ...`
(the suite's environments, coverage and experiments).

    scruple tasks export --env recon --n 1000 --difficulty 0.2:0.8 --out DIR [--workers 8]
    scruple tasks verify DIR
    scruple tasks rollout --tasks DIR --policy honest --out traj.jsonl
    scruple tasks run-model --tasks DIR --provider anthropic --model M --out RUN [--transport fake]
    scruple tasks grade --tasks DIR --trajectories traj.jsonl [--out grades.jsonl] [--workers 8]
    scruple tasks serve --tasks DIR [--port 8077]
    scruple tasks bench --env recon --n 400

    scruple finops envs
    scruple finops coverage --seeds 1000 [--env recon] [--json]
    scruple finops experiments --seeds 1000 --out results/finops
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _range(text: str) -> tuple[float, float]:
    lo, sep, hi = text.partition(":")
    a = float(lo)
    b = float(hi) if sep else a
    return a, b


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True))


def cmd_tasks(args: argparse.Namespace) -> int:
    from scruple.finops import tasks as tk
    from scruple.finops.parallel import default_workers, grade_many

    match args.tasks_cmd:
        case "export":
            m = tk.export_tasks(
                args.env,
                args.n,
                Path(args.out),
                difficulty=_range(args.difficulty),
                seed0=args.seed0,
                case_types=args.case_type or None,
                verifier=args.verifier,
                measure=not args.no_measure,
                horizon=args.horizon,
                workers=args.workers or default_workers(),
                keyed=getattr(args, "keyed", False),
            )
            _print({k: m[k] for k in ("env", "n", "seeds", "verifier", "files")})
            return 0
        case "verify":
            problems = tk.verify_manifest(Path(args.dir))
            _print({"ok": not problems, "problems": problems})
            return 0 if not problems else 1
        case "rollout":
            specs = tk.load_tasks(Path(args.tasks))
            out = Path(args.out)
            with out.open("w") as f:
                for s in specs.values():
                    f.write(tk.rollout(s, args.policy).model_dump_json() + "\n")
            print(f"{len(specs)} trajectories -> {out}", file=sys.stderr)
            return 0
        case "run-model":
            from scruple.agents.providers import RunSettings
            from scruple.finops.rollout import run_models

            todo = list(tk.load_tasks(Path(args.tasks)).values())[: args.limit or None]
            summary = run_models(
                todo,
                args.provider,
                args.model,
                Path(args.out),
                transport=args.transport,
                base_url=args.base_url,
                concurrency=args.concurrency,
                rpm=args.rpm,
                settings=RunSettings(timeout_s=args.timeout),
            )
            _print({k: v for k, v in summary.items() if k != "runs"})
            return 0
        case "grade":
            specs = tk.load_tasks(Path(args.tasks))
            salt = None
            if args.salt:
                salt = Path(args.salt).read_text().strip()
            trajs = list(tk.read_trajectories(Path(args.trajectories)))
            if salt is not None:
                lines = [
                    tk.grade_safe(specs.get(t.task_id), t, salt=salt).model_dump_json()
                    for t in trajs
                ]
            else:
                pairs = [
                    (specs[t.task_id].model_dump_json(), t.model_dump_json())
                    for t in trajs
                    if t.task_id in specs
                ]
                graded = iter(grade_many(pairs, workers=args.workers or default_workers()))
                lines = [
                    next(graded) if t.task_id in specs else tk.grade_safe(None, t).model_dump_json()
                    for t in trajs
                ]
            body = "".join(x + "\n" for x in lines)
            if args.out:
                Path(args.out).write_text(body)
            else:
                sys.stdout.write(body)
            rewards = [json.loads(x)["reward"] for x in lines]
            print(
                f"graded {len(lines)}: mean reward {sum(rewards) / max(1, len(rewards)):.4f}",
                file=sys.stderr,
            )
            return 0
        case "serve":  # pragma: no cover - blocks
            from scruple.finops.server import serve

            salt_file = getattr(args, "salt", None)
            salt = Path(salt_file).read_text().strip() if salt_file else None
            serve(
                tk.load_tasks(Path(args.tasks)),
                host=args.host,
                port=args.port,
                salt=salt,
                full_grades=getattr(args, "full_grades", False),
            )
            return 0
        case "bench":
            from scruple.finops.parallel import bench

            _print(bench(args.env, args.n, policy=args.policy, workers=args.workers or None))
            return 0
    raise AssertionError(args.tasks_cmd)  # pragma: no cover


def add_tasks_parser(sub: Any) -> None:
    from scruple.finops.envs import ENVS

    envs = list(ENVS)
    p = sub.add_parser("tasks", help="export, grade and serve finance-operations RL tasks")
    p.set_defaults(fn=cmd_tasks)
    ts = p.add_subparsers(dest="tasks_cmd", required=True)
    ex = ts.add_parser("export", help="write portable task specs (JSONL) and a manifest")
    ex.add_argument("--env", required=True, choices=envs)
    ex.add_argument("--n", type=int, required=True)
    ex.add_argument("--out", required=True)
    ex.add_argument("--difficulty", default="0:1", help="nominal difficulty, lo:hi or one value")
    ex.add_argument("--seed0", type=int, default=0, help="first dev seed")
    ex.add_argument("--case-type", action="append", default=[], help="explicit case types (cycled)")
    ex.add_argument("--verifier", default=None, help="verifier id (default: the env's)")
    ex.add_argument("--horizon", action="store_true", help="also tighten the tool-call budget")
    ex.add_argument("--no-measure", action="store_true", help="skip the measured difficulty")
    ex.add_argument("--workers", type=int, default=0, help="processes (default: cores - 1)")
    ex.add_argument(
        "--keyed",
        action="store_true",
        help="key every case by the export salt: the public seed does not rebuild it",
    )
    vf = ts.add_parser("verify", help="check an export directory against its manifest")
    vf.add_argument("dir")
    ro = ts.add_parser("rollout", help="play a scripted policy on every task (trajectories)")
    ro.add_argument("--tasks", required=True)
    ro.add_argument("--policy", default="honest")
    ro.add_argument("--out", required=True)
    rm = ts.add_parser("run-model", help="play the tasks with an LLM provider, then grade")
    from scruple.agents.providers import provider_names

    rm.add_argument("--tasks", required=True)
    rm.add_argument("--provider", required=True, choices=list(provider_names()))
    rm.add_argument("--model", required=True)
    rm.add_argument("--out", required=True)
    rm.add_argument("--transport", choices=["http", "fake"], default="http")
    rm.add_argument("--base-url", default=None)
    rm.add_argument("--concurrency", type=int, default=4)
    rm.add_argument("--rpm", type=float, default=None)
    rm.add_argument("--timeout", type=float, default=120.0)
    rm.add_argument("--limit", type=int, default=0, help="only the first N tasks")
    gr = ts.add_parser("grade", help="grade trajectories by replay (JSONL in, JSONL out)")
    gr.add_argument("--tasks", required=True, help="tasks.jsonl or an export directory")
    gr.add_argument("--trajectories", required=True)
    gr.add_argument("--out", default=None)
    gr.add_argument("--salt", default=None, help="commitments.salt: also check the truth")
    gr.add_argument("--workers", type=int, default=0)
    sv = ts.add_parser("serve", help="HTTP grader and remote environment (FastAPI)")
    sv.add_argument("--tasks", required=True)
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8077)
    sv.add_argument("--salt", default=None, help="commitments.salt (needed for keyed tasks)")
    sv.add_argument(
        "--full-grades", action="store_true", help="operators: full grades on live episodes"
    )
    bn = ts.add_parser("bench", help="throughput: episodes/s and grades/s on this machine")
    bn.add_argument("--env", required=True, choices=envs)
    bn.add_argument("--n", type=int, default=400)
    bn.add_argument("--policy", default="honest")
    bn.add_argument("--workers", type=int, default=0)


def cmd_finops(args: argparse.Namespace) -> int:
    match args.finops_cmd:
        case "envs":
            from scruple.finops.envs import ENVS

            _print(
                [
                    {
                        "env": e.name,
                        "title": e.title,
                        "parity": e.parity,
                        "knobs": sorted(e.knobs),
                        "tools": [t.name for t in e.tools],
                        "verifiers": {v.id: v.table() for v in e.verifiers()},
                        "default_verifier": e.default_verifier,
                        "policies": list(e.policies()),
                    }
                    for e in ENVS.values()
                ]
            )
            return 0
        case "coverage":
            from scruple.finops.exploits import coverage, render
            from scruple.finops.parallel import default_workers

            rows = coverage(
                range(args.start, args.start + args.seeds),
                envs=args.env or None,
                workers=args.workers or default_workers(),
            )
            if args.json:
                _print([r.model_dump() for r in rows])
            else:
                print(render(rows))
            return 0
        case "anchoring":
            from scruple.finops.experiments import anchoring_check
            from scruple.finops.parallel import default_workers

            seeds = range(args.start, args.start + args.seeds)
            _print(anchoring_check(args.env, seeds, workers=args.workers or default_workers()))
            return 0
        case "sweep":
            from scruple.finops.experiments import sweep
            from scruple.finops.parallel import default_workers

            seeds = range(args.start, args.start + args.seeds)
            _print(
                sweep(
                    args.env,
                    args.policy,
                    args.knob,
                    seeds,
                    base=args.base,
                    workers=args.workers or default_workers(),
                )
            )
            return 0
        case "experiments":
            from scruple.finops.report import run_all

            run_all(
                Path(args.out), seeds=args.seeds, train=args.train, workers=args.workers or None
            )
            return 0
    raise AssertionError(args.finops_cmd)  # pragma: no cover


def add_finops_parser(sub: Any) -> None:
    p = sub.add_parser("finops", help="the finance-operations environment suite")
    p.set_defaults(fn=cmd_finops)
    fs = p.add_subparsers(dest="finops_cmd", required=True)
    fs.add_parser("envs", help="environments, tools, knobs, verifiers and policies (JSON)")
    cv = fs.add_parser("coverage", help="run every exploit against every verifier version")
    cv.add_argument("--seeds", type=int, default=200)
    cv.add_argument("--start", type=int, default=0)
    cv.add_argument("--env", action="append", default=[])
    cv.add_argument("--json", action="store_true")
    cv.add_argument("--workers", type=int, default=0)
    an = fs.add_parser("anchoring", help="every knob policy against its honest projection")
    an.add_argument("--env", choices=["recon", "aml"], required=True)
    an.add_argument("--seeds", type=int, default=200)
    an.add_argument("--start", type=int, default=0)
    an.add_argument("--workers", type=int, default=0)
    sw = fs.add_parser("sweep", help="reward of one policy as one knob rises")
    sw.add_argument("--env", choices=["recon", "aml"], required=True)
    sw.add_argument("--policy", required=True)
    sw.add_argument("--knob", choices=["adv", "noise"], required=True)
    sw.add_argument("--base", default="@0.5")
    sw.add_argument("--seeds", type=int, default=1000)
    sw.add_argument("--start", type=int, default=0)
    sw.add_argument("--workers", type=int, default=0)
    xp = fs.add_parser("experiments", help="the FINOPS_SUITE.md experiments (JSON + markdown)")
    xp.add_argument("--out", required=True)
    xp.add_argument("--seeds", type=int, default=1000, help="dev seeds per policy")
    xp.add_argument("--train", type=int, default=200, help="train seeds for the hill climb")
    xp.add_argument("--workers", type=int, default=0)


__all__ = ["add_finops_parser", "add_tasks_parser", "cmd_finops", "cmd_tasks"]
