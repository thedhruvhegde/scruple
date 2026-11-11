"""`scruple bench ...`: build, show, verify and score the frozen dispute benchmark.

    scruple bench show                         # the published manifest (no salt needed)
    scruple bench build --version 1 --out F    # needs SCRUPLE_HELDOUT_SALT
    scruple bench verify                       # regenerate every case, compare
    scruple bench score --agent evidence       # or module:callable, or cli:<command>
    scruple bench seal --partner NAME --out D  # a partner's own salt, manifest, handover

Exit codes: 0 ok, 1 verification failed, 2 usage, input or sealed-salt error.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from scruple.bench.core import BenchFile, build, score_agent, verify
from scruple.bench.metrics import Score, render
from scruple.bench.runner import BLOCK
from scruple.bench.spec import DEFAULT_CASES, space_from

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BENCH = ROOT / "results" / "bench" / "v1" / "manifest.json"


def _bench(args: argparse.Namespace) -> BenchFile:
    return BenchFile.load(args.bench)


def _summary(f: BenchFile) -> list[str]:
    m = f.manifest
    lines = [
        f"{m.name} v{m.version}: {m.n_cases:,} synthetic held-out dispute cases",
        f"manifest hash {f.manifest_hash}",
        f"engine {m.engine_version}; block {m.block}; salt commitment {m.salt_commitment[:16]}",
        "families: " + ", ".join(f"{k} {v}" for k, v in m.mix.families.items()),
        "difficulty: " + ", ".join(f"{k} {v}" for k, v in m.mix.tiers.items()),
        "opening stage: " + ", ".join(f"{k} {v}" for k, v in m.mix.stages.items()),
        f"valid claims: {m.mix.claim_valid}",
        "",
        "reference scores (95% intervals):",
        render(list(f.reference.values())),
    ]
    return lines


def cmd_bench_show(args: argparse.Namespace) -> int:
    f = _bench(args)
    if args.json:
        print(f.model_dump_json(indent=2))
    else:
        print("\n".join(_summary(f)))
    return 0


def cmd_bench_build(args: argparse.Namespace) -> int:
    t0 = time.perf_counter()
    f, _ = build(space_from(None), args.version, args.cases, jobs=args.jobs, block=args.block)
    f.dump(args.out)
    print("\n".join(_summary(f)))
    print(f"\nwritten to {args.out} in {time.perf_counter() - t0:.0f}s")
    return 0


def cmd_bench_verify(args: argparse.Namespace) -> int:
    f = _bench(args)
    problems = verify(f, space_from(None), jobs=args.jobs)
    for p in problems:
        print(f"PROBLEM: {p}")
    print("VERIFIED" if not problems else "VERIFICATION FAILED", f.manifest_hash)
    return 1 if problems else 0


def _delta(s: Score, ref: Score) -> dict[str, Any]:
    return {
        "reward": round(s.reward.mean - ref.reward.mean, 4),
        "cost": round(s.cost.mean - ref.cost.mean, 1),
    }


def cmd_bench_score(args: argparse.Namespace) -> int:
    f = _bench(args)
    run, _ = score_agent(f, args.agent, space_from(None), limit=args.limit, jobs=args.jobs)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(run.model_dump_json(indent=2) + "\n")
    if args.json:
        print(run.model_dump_json(indent=2))
        return 0
    s = run.score
    print(
        f"{f.manifest.name} v{run.version} ({run.manifest_hash[:16]}): {s.agent} on "
        f"{s.n_cases:,} cases{' (prefix)' if run.prefix else ''} in {run.seconds}s; synthetic"
    )
    print(render([s, *run.reference.values()]))
    if not run.prefix:
        print(
            "vs reference: "
            + json.dumps({k: _delta(s, r) for k, r in run.reference.items()}, sort_keys=True)
        )
    else:
        print("reference rows are over the full benchmark; a prefix is not directly comparable")
    return 0


def cmd_bench_seal(args: argparse.Namespace) -> int:
    from scruple.bench.seal import salt_dir_from, seal

    try:
        r = seal(
            args.partner,
            Path(args.out),
            salt_dir=salt_dir_from(given=args.salt_dir),
            version=args.version,
            n=args.cases,
            block=args.block,
            jobs=args.jobs,
            reference=args.reference,
        )
    except (ValueError, FileExistsError) as e:
        print(f"error: {e}")
        return 2
    print(f"sealed {r.partner}: {r.n_cases:,} synthetic cases, manifest hash {r.manifest_hash}")
    print(f"salt commitment {r.salt_commitment[:16]}; salt written to {r.salt_path} (0600)")
    others = ", ".join(r.checked_against) or "none"
    print(f"seeds checked disjoint from: {others}")
    print(f"wrote {r.manifest_path} and {r.note_path}")
    return 0


def add_bench_parser(sub: Any) -> None:
    p = sub.add_parser("bench", help="the frozen held-out dispute benchmark")
    ss = p.add_subparsers(dest="bench_cmd", required=True)

    def add(name: str, fn: Any, help_: str) -> argparse.ArgumentParser:
        sp: argparse.ArgumentParser = ss.add_parser(name, help=help_)
        sp.set_defaults(fn=fn)
        sp.add_argument("--bench", default=str(DEFAULT_BENCH), help="benchmark manifest file")
        sp.add_argument("--jobs", type=int, default=1, help="worker processes")
        return sp

    sh = add("show", cmd_bench_show, "print the published manifest and reference scores")
    sh.add_argument("--json", action="store_true")
    b = add("build", cmd_bench_build, "build a version from the sealed seeds (needs the salt)")
    b.add_argument("--version", type=int, default=1)
    b.add_argument("--cases", type=int, default=DEFAULT_CASES)
    b.add_argument("--block", type=int, default=BLOCK)
    b.add_argument("--out", required=True)
    add("verify", cmd_bench_verify, "regenerate every case and check the manifest")
    sc = add("score", cmd_bench_score, "score an agent on the benchmark")
    sc.add_argument(
        "--agent", required=True, help="evidence | always_accept | oracle | module:fn | cli:cmd"
    )
    sc.add_argument("--limit", type=int, default=None, help="first N cases (rounded to a block)")
    sc.add_argument("--json", action="store_true")
    sc.add_argument("--out", default=None, help="write the run as JSON")
    se = add("seal", cmd_bench_seal, "seal a partner's own benchmark: salt, manifest, handover")
    se.add_argument("--partner", required=True, help="partner code name (a-z, 0-9, -)")
    se.add_argument("--out", required=True, help="directory for manifest.json and HANDOVER.md")
    se.add_argument(
        "--salt-dir", default=None, help="0600 salt files (default ~/.scruple/partners)"
    )
    se.add_argument("--version", type=int, default=1)
    se.add_argument("--cases", type=int, default=DEFAULT_CASES)
    se.add_argument("--block", type=int, default=BLOCK)
    se.add_argument("--reference", action="store_true", help="also score the reference agents")


__all__ = ["DEFAULT_BENCH", "add_bench_parser"]
