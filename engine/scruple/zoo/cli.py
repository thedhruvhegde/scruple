"""`scruple zoo ...`: train the agent zoo on train/dev, score it once on the benchmark.

    scruple zoo train [--train 1500 --dev 400 --jobs 4]   # writes the models (pickle)
                                                          # and results/zoo/train.json
    scruple zoo eval [--limit 2000 --jobs 4]              # needs SCRUPLE_HELDOUT_SALT;
                                                          # writes results/zoo/eval.json

`train` never needs the salt. `eval` loads the models `train` wrote (a local pickle,
never committed: load only files you made), plays them and the reference policies on
the same benchmark cases and prints the results table. Everything is synthetic.
"""

from __future__ import annotations

import argparse
import json
import pickle
import time
from pathlib import Path
from typing import Any

from scruple.bench.cli import DEFAULT_BENCH
from scruple.bench.core import BenchFile
from scruple.bench.spec import space_from
from scruple.zoo.evaluate import REFERENCES, agents_for, play, render, summarise
from scruple.zoo.train import LEARNERS, TrainedAgent, ZooConfig, train_zoo

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODELS = ROOT / ".scruple" / "zoo" / "models.pkl"
DEFAULT_OUT = ROOT / "results" / "zoo"
DEFAULT_CACHE = ROOT / ".scruple" / "zoo" / "played"
NOTE = (
    "Synthetic. Every case comes from the scruple dispute simulator; the issuer is its "
    "assumed acceptance model (docs/ASSUMPTIONS.md A-21). Agents trained on the public "
    "train namespace, tuned on dev, scored once on the frozen held-out benchmark."
)


def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def cmd_zoo_train(args: argparse.Namespace) -> int:
    cfg = ZooConfig(n_train=args.train, n_dev=args.dev, seed=args.seed, rollouts=args.rollouts)
    learners = tuple(args.learners.split(",")) if args.learners else LEARNERS
    t0 = time.perf_counter()
    trained, shared = train_zoo(cfg, jobs=args.jobs, learners=learners, proxy=not args.no_proxy)
    secs = time.perf_counter() - t0
    models = Path(args.models)
    models.parent.mkdir(parents=True, exist_ok=True)
    models.write_bytes(pickle.dumps(trained))
    summary = {
        "note": NOTE,
        "synthetic": True,
        "config": {
            "n_train": cfg.n_train,
            "n_dev": cfg.n_dev,
            "seed": cfg.seed,
            "rollouts": cfg.rollouts,
            "train_split": "train",
            "dev_split": "dev",
        },
        "shared": {
            "explore_episodes": len(shared.explore),
            "explore_seconds": round(shared.explore_seconds, 1),
            "branch_episodes": shared.branches.episodes,
            "branch_rows": len(shared.branches.rows),
            "branch_seconds": round(shared.branches.seconds, 1),
            "confidence": shared.confidence,
        },
        "wall_seconds": round(secs, 1),
        "agents": {k: v.summary() for k, v in trained.items()},
    }
    _write(Path(args.out) / "train.json", summary)
    for k, v in trained.items():
        print(
            f"{k:22} dev {v.dev_value:.4f} config {v.config} claim_win={v.claim_win} "
            f"train {v.train_episodes} eps {v.train_seconds:.0f}s"
        )
    print(f"models written to {models}; summary to {Path(args.out) / 'train.json'}")
    return 0


def load_models(path: str | Path) -> dict[str, TrainedAgent]:
    obj = pickle.loads(Path(path).read_bytes())  # noqa: S301 - a file `zoo train` wrote
    if not isinstance(obj, dict):
        raise ValueError(f"{path}: not a zoo models file")
    return obj


def cmd_zoo_eval(args: argparse.Namespace) -> int:
    bench = BenchFile.load(args.bench)
    trained = {} if args.references_only else load_models(args.models)
    if args.agents:
        keep = set(args.agents.split(","))
        trained = {k: v for k, v in trained.items() if k in keep}
    agents = agents_for(trained, REFERENCES)
    t0 = time.perf_counter()
    cache = None if args.no_cache else Path(args.cache)
    rows, batch, secs = play(
        bench, space_from(None), agents, limit=args.limit, jobs=args.jobs, cache=cache
    )
    summary = summarise(rows, batch=batch, b=args.bootstrap)
    if args.references_only:  # warms the cache; the published file needs every agent
        print(render(summary, {}))
        return 0
    n = len(rows["evidence"])
    train_json = Path(args.out) / "train.json"
    train = json.loads(train_json.read_text())["agents"] if train_json.exists() else {}
    for k, v in trained.items():
        train.setdefault(k, v.summary())
    subset = (
        "all cases"
        if n == bench.manifest.n_cases
        else f"first {n} cases (benchmark positions 0-{n - 1}), the same for every agent"
    )
    result: dict[str, Any] = {
        "note": NOTE,
        "synthetic": True,
        "benchmark": {
            "manifest_hash": bench.manifest_hash,
            "version": bench.manifest.version,
            "n_cases_scored": n,
            "n_cases_total": bench.manifest.n_cases,
            "subset": subset,
        },
        "bootstrap": {"resamples": args.bootstrap, "seed": 0, "interval": "95% percentile"},
        "money": "USD minor units (cents)",
        "baseline": "evidence",
        "play_seconds": {k: round(v, 1) for k, v in secs.items()},
        "wall_seconds": round(time.perf_counter() - t0, 1),
        "agents": summary,
        "training": {k: train.get(k, {}) for k in summary},
    }
    _write(Path(args.out) / "eval.json", result)
    print(
        f"zoo on {bench.manifest.name} v{bench.manifest.version} "
        f"({bench.manifest_hash[:16]}), {subset}; synthetic"
    )
    print(render(summary, train))
    return 0


def add_zoo_parser(sub: Any) -> None:
    p = sub.add_parser("zoo", help="train and score many kinds of dispute agents")
    ss = p.add_subparsers(dest="zoo_cmd", required=True)
    t = ss.add_parser("train", help="train on train seeds, tune on dev (no salt needed)")
    t.set_defaults(fn=cmd_zoo_train)
    t.add_argument("--train", type=int, default=1_500, help="train episodes per learner")
    t.add_argument("--dev", type=int, default=400, help="dev episodes per setting")
    t.add_argument("--seed", type=int, default=0)
    t.add_argument("--rollouts", type=int, default=32, help="planner rollouts per plan")
    t.add_argument("--learners", default="", help=f"subset of {','.join(LEARNERS)}")
    t.add_argument("--no-proxy", action="store_true", help="skip the proxy-objective twins")
    t.add_argument("--jobs", type=int, default=1)
    t.add_argument("--models", default=str(DEFAULT_MODELS))
    t.add_argument("--out", default=str(DEFAULT_OUT))
    e = ss.add_parser("eval", help="score the trained zoo on the benchmark (needs the salt)")
    e.set_defaults(fn=cmd_zoo_eval)
    e.add_argument("--bench", default=str(DEFAULT_BENCH))
    e.add_argument("--models", default=str(DEFAULT_MODELS))
    e.add_argument("--limit", type=int, default=None, help="first N cases (whole blocks)")
    e.add_argument("--agents", default="", help="comma-separated subset of trained agents")
    e.add_argument("--bootstrap", type=int, default=2_000)
    e.add_argument("--jobs", type=int, default=1)
    e.add_argument("--out", default=str(DEFAULT_OUT))
    e.add_argument("--cache", default=str(DEFAULT_CACHE), help="played-episode cache (local)")
    e.add_argument("--no-cache", action="store_true", help="replay every agent")
    e.add_argument("--references-only", action="store_true", help="play only the references")


__all__ = ["add_zoo_parser", "load_models"]
