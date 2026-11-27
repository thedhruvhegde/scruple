"""Mutation testing of the finance-ops and commerce verifiers (in-house, AST based).

`python -m scruple.redteam.mutation --out DIR [--per-module N] [--workers W]`

1. Enumerate mutants of each target module: comparison operators swapped (`<`/`<=`,
   `>`/`>=`, `==`/`!=`, `in`/`not in`, `is`/`is not`), `and`/`or` swapped, a `not`
   removed, an integer constant moved by one, a boolean constant flipped (this
   includes `hard=True` on a check definition), `any`/`all` swapped. A seeded sample
   of at most N per module is kept.
2. Build a corpus of closed records per environment (honest, every base policy and
   hacker knob, random search genomes; `scruple.redteam.differential.corpus`) and the
   unmutated verdict signature on it: for every record, the gate and each check's
   pass/fail (the *hard signature*) and the rounded scores (the *full signature*).
3. For each mutant, in a scratch copy of the repository with the mutated module
   written in: score the corpus (if neither signature changes, the mutant is
   *equivalent on the corpus*); otherwise run the module's test selection (unit tests
   plus the exploit and regression tests for that environment) and count it killed if
   any test fails or times out.

Kill rate = killed / distinguishable (equivalent mutants cannot be killed by any test
that only observes verdicts and scores). The hard-verdict kill rate restricts to
mutants that change the hard signature: those are the ones an agent could exploit.
"""

from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]

TARGETS: dict[str, tuple[str, tuple[str, ...]]] = {
    # module path -> (env whose corpus it scores, test files)
    "scruple/verifiers/recon.py": (
        "recon",
        ("tests/test_recon_verifier.py", "tests/test_redteam_regressions.py"),
    ),
    "scruple/verifiers/aml.py": (
        "aml",
        ("tests/test_aml_verifier.py", "tests/test_redteam_regressions.py"),
    ),
    "scruple/verifiers/ach.py": (
        "ach",
        ("tests/test_ach_verifier.py", "tests/test_redteam_regressions.py"),
    ),
    "scruple/verifiers/payables.py": (
        "payables",
        (
            "tests/test_payables_verifier.py",
            "tests/test_payables_bank_change.py",
            "tests/test_redteam_regressions.py",
        ),
    ),
    "scruple/verifiers/commerce.py": (
        "commerce",
        ("tests/test_commerce_episode.py", "tests/test_redteam_regressions.py"),
    ),
    "scruple/finops/verifiers.py": (
        "recon+aml",
        (
            "tests/test_finops_tasks.py",
            "tests/test_finops_policies.py",
            "tests/test_redteam_regressions.py",
        ),
    ),
}

_CMP_SWAP: dict[type[ast.cmpop], type[ast.cmpop]] = {
    ast.Lt: ast.LtE,
    ast.LtE: ast.Lt,
    ast.Gt: ast.GtE,
    ast.GtE: ast.Gt,
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.In: ast.NotIn,
    ast.NotIn: ast.In,
    ast.Is: ast.IsNot,
    ast.IsNot: ast.Is,
}


@dataclass(frozen=True)
class Mutant:
    module: str
    index: int  # the node's position in a fixed walk order
    op: str
    line: int
    before: str
    after: str

    @property
    def id(self) -> str:
        return f"{Path(self.module).stem}:{self.line}:{self.op}:{self.index}"


def _walk(tree: ast.AST) -> list[ast.AST]:
    return list(ast.walk(tree))


def _docstring_nodes(tree: ast.AST) -> set[int]:
    out: set[int] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Module | ast.FunctionDef | ast.ClassDef | ast.AsyncFunctionDef):
            b = n.body
            if b and isinstance(b[0], ast.Expr) and isinstance(b[0].value, ast.Constant):
                out.add(id(b[0].value))
    return out


def _variants(node: ast.AST) -> Iterator[tuple[str, ast.AST]]:
    if isinstance(node, ast.Compare):
        for i, o in enumerate(node.ops):
            swap = _CMP_SWAP.get(type(o))
            if swap is not None:
                c = copy.deepcopy(node)
                c.ops[i] = swap()
                yield f"cmp:{type(o).__name__}->{swap.__name__}", c
    elif isinstance(node, ast.BoolOp):
        b = copy.deepcopy(node)
        b.op = ast.Or() if isinstance(node.op, ast.And) else ast.And()
        yield f"bool:{type(node.op).__name__}", b
    elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        yield "not:removed", copy.deepcopy(node.operand)
    elif isinstance(node, ast.Constant):
        v = node.value
        if isinstance(v, bool):
            yield f"const:{v}->{not v}", ast.Constant(value=not v)
        elif isinstance(v, int):
            yield f"const:{v}->{v + 1}", ast.Constant(value=v + 1)
            if v != 0:
                yield f"const:{v}->{v - 1}", ast.Constant(value=v - 1)
        elif isinstance(v, float):
            yield f"const:{v}->{v * 2 if v else 1.0}", ast.Constant(value=v * 2 if v else 1.0)
    elif (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in ("any", "all")
    ):
        k = copy.deepcopy(node)
        assert isinstance(k.func, ast.Name)
        k.func.id = "all" if node.func.id == "any" else "any"
        yield f"call:{node.func.id}", k


def enumerate_mutants(module: str) -> list[Mutant]:
    src = (ROOT / module).read_text()
    tree = ast.parse(src)
    docs = _docstring_nodes(tree)
    out: list[Mutant] = []
    for idx, node in enumerate(_walk(tree)):
        if id(node) in docs:
            continue
        for op, new in _variants(node):
            ln = getattr(node, "lineno", 0)
            out.append(Mutant(module, idx, op, ln, ast.unparse(node)[:80], ast.unparse(new)[:80]))
    return out


class _Replace(ast.NodeTransformer):
    def __init__(self, target: int, op: str) -> None:
        self.i = -1
        self.target = target
        self.op = op
        self.order: dict[int, int] = {}

    def run(self, tree: ast.AST) -> ast.AST:
        self.order = {id(n): i for i, n in enumerate(_walk(tree))}
        out: ast.AST = self.visit(tree)
        return out

    def generic_visit(self, node: ast.AST) -> ast.AST:
        if self.order.get(id(node)) == self.target:
            for op, new in _variants(node):
                if op == self.op:
                    return ast.copy_location(new, node)
        return super().generic_visit(node)


def mutated_source(m: Mutant) -> str:
    tree = ast.parse((ROOT / m.module).read_text())
    new = _Replace(m.index, m.op).run(tree)
    ast.fix_missing_locations(new)
    return ast.unparse(new)


# --- corpus and verdict signatures ---------------------------------------------------------


def build_corpus(out: Path, seeds: int = 6, n_random: int = 30) -> None:
    from scruple.redteam.differential import corpus
    from scruple.redteam.envs import play

    out.mkdir(parents=True, exist_ok=True)
    for env in ("recon", "aml", "ach", "payables", "commerce"):
        lines = []
        for g in corpus(env, n_random, rng_seed=7):
            for s in range(40_000, 40_000 + seeds):
                lines.append(play(env, g, s).model_dump_json())
        (out / f"{env}.jsonl").write_text("\n".join(lines) + "\n")


def signatures(corpus_dir: Path, envs: str, hard_only: bool = False) -> tuple[str, str]:
    """(hard signature, full signature) of the current code on the stored corpus."""
    from scruple.finops.envs import get_env
    from scruple.verifiers.ach_record import AchRecord
    from scruple.verifiers.aml_record import AmlRecord
    from scruple.verifiers.commerce import score_commerce
    from scruple.verifiers.commerce_record import CommerceRecord
    from scruple.verifiers.payables_record import PayablesRecord
    from scruple.verifiers.recon_record import ReconRecord

    types: dict[str, Any] = {
        "recon": ReconRecord,
        "aml": AmlRecord,
        "ach": AchRecord,
        "payables": PayablesRecord,
        "commerce": CommerceRecord,
    }
    hard, full = hashlib.sha256(), hashlib.sha256()
    for env in envs.split("+"):
        cls = types[env]
        scorers: list[Any] = (
            [score_commerce] if env == "commerce" else [v.score for v in get_env(env).verifiers()]
        )
        for line in (corpus_dir / f"{env}.jsonl").read_text().splitlines():
            if not line:
                continue
            r = cls.model_validate_json(line)
            for sc in scorers:
                try:
                    card = sc(r)
                except Exception as exc:  # a mutant that crashes the verifier is visible
                    hard.update(f"crash:{type(exc).__name__}".encode())
                    full.update(f"crash:{type(exc).__name__}".encode())
                    continue
                hv = [
                    card.gate,
                    *[(c.id, c.passed) for c in card.checks if c.hard or not hard_only],
                ]
                hard.update(json.dumps(hv).encode())
                full.update(
                    json.dumps(
                        [round(card.reward, 6), *[(c.id, round(c.score, 6)) for c in card.checks]]
                    ).encode()
                )
    return hard.hexdigest(), full.hexdigest()


# --- running ---------------------------------------------------------------------------------


def _copy_repo(dst: Path) -> None:
    files = subprocess.run(
        ["git", "ls-files", "scruple", "tests", "data", "pyproject.toml"],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    extra = [
        "scruple/redteam/__init__.py",
        "scruple/redteam/envs.py",
        "scruple/redteam/genome.py",
        "scruple/redteam/search.py",
        "scruple/redteam/differential.py",
        "scruple/redteam/mutation.py",
        "scruple/verifiers/nversion.py",
        "tests/test_redteam_regressions.py",
        "tests/test_redteam_search.py",
    ]
    for f in dict.fromkeys(files + extra):
        s = ROOT / f
        if not s.is_file():
            continue
        d = dst / f
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(s, d)


def _python() -> str:
    return sys.executable


def _run_one(m: Mutant, work: Path, corpus_dir: Path, base: dict[str, tuple[str, str]],
             timeout: int, new_tests: bool = True,
             extra: tuple[str, ...] = ()) -> dict[str, Any]:  # fmt: skip
    env, tests = TARGETS[m.module]
    tests = (*tests, *extra)
    if not new_tests:  # the suite as it was before round 5
        tests = tuple(t for t in tests if "redteam" not in t)
    target = work / m.module
    original = target.read_text()
    row: dict[str, Any] = {**asdict(m), "id": m.id}
    try:
        try:
            target.write_text(mutated_source(m))
        except Exception as exc:  # pragma: no cover - an unparsable mutant
            row["status"] = f"invalid:{type(exc).__name__}"
            return row
        envs = {"recon+aml": "recon+aml"}.get(env, env)
        sig = subprocess.run(  # noqa: S603
            [_python(), "-I", "-c",
             "import sys, json; sys.path.insert(0, '.');"
             "from pathlib import Path;"
             "from scruple.redteam.mutation import signatures;"
             f"print(json.dumps(signatures(Path({str(corpus_dir)!r}), {envs!r})))"],
            cwd=work, capture_output=True, text=True, timeout=timeout,
        )  # fmt: skip
        if sig.returncode != 0:
            row["hard_changed"] = row["full_changed"] = True
            row["oracle"] = "crash"
        else:
            h, f = json.loads(sig.stdout.strip().splitlines()[-1])
            row["hard_changed"] = h != base[envs][0]
            row["full_changed"] = f != base[envs][1]
        if not row["hard_changed"] and not row["full_changed"]:
            row["status"] = "equivalent"
            return row
        t = subprocess.run(  # noqa: S603
            [_python(), "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider",
             *[t for t in tests if (work / t).exists()]],
            cwd=work, capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "PYTHONPATH": str(work)},
        )  # fmt: skip
        row["status"] = "killed" if t.returncode != 0 else "survived"
        if t.returncode not in (0, 1):
            row["pytest_rc"] = t.returncode
    except subprocess.TimeoutExpired:
        row["status"] = "killed"
        row["timeout"] = True
    finally:
        target.write_text(original)
    return row


def classify(out: Path, workers: int = 3, timeout: int = 240) -> list[dict[str, Any]]:
    """Second pass over a finished run: which distinguishable mutants change a *hard*
    verdict (the gate or a hard check's pass/fail; soft pass flags excluded)."""
    rows: list[dict[str, Any]] = json.loads((out / "mutants.json").read_text())
    corpus_dir = (out / "corpus").resolve()
    base = {e: signatures(corpus_dir, e, hard_only=True)[0]
            for e in ("recon", "aml", "ach", "payables", "commerce", "recon+aml")}  # fmt: skip
    todo = [r for r in rows if r.get("status") in ("killed", "survived")]
    works = []
    for i in range(workers):
        w = Path(tempfile.mkdtemp(prefix=f"cls{i}-", dir=out))
        _copy_repo(w)
        works.append(w)

    def one(i: int) -> None:
        for r in todo[i::workers]:
            m = Mutant(r["module"], r["index"], r["op"], r["line"], r["before"], r["after"])
            env = TARGETS[m.module][0]
            target = works[i] / m.module
            original = target.read_text()
            try:
                target.write_text(mutated_source(m))
                sig = subprocess.run(  # noqa: S603
                    [_python(), "-I", "-c",
                     "import sys, json; sys.path.insert(0, '.');"
                     "from pathlib import Path;"
                     "from scruple.redteam.mutation import signatures;"
                     f"print(signatures(Path({str(corpus_dir)!r}), {env!r}, True)[0])"],
                    cwd=works[i], capture_output=True, text=True, timeout=timeout,
                )  # fmt: skip
                r["hard_verdict_changed"] = (
                    sig.returncode != 0 or sig.stdout.strip().splitlines()[-1] != base[env]
                )
            except subprocess.TimeoutExpired:
                r["hard_verdict_changed"] = True
            finally:
                target.write_text(original)

    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(one, range(workers)))
    (out / "mutants.json").write_text(json.dumps(rows, indent=1, sort_keys=True) + "\n")
    for w in works:
        shutil.rmtree(w, ignore_errors=True)
    return rows


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m scruple.redteam.mutation")
    p.add_argument("--out", required=True)
    p.add_argument("--per-module", type=int, default=80)
    p.add_argument("--workers", type=int, default=3)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--timeout", type=int, default=240)
    p.add_argument("--modules", default=",".join(TARGETS))
    p.add_argument("--only", default="", help="comma-separated mutant ids to (re)run")
    p.add_argument("--only-file", default="", help="file with mutant ids, one per line")
    p.add_argument("--no-new-tests", action="store_true", help="the pre-round-5 suite only")
    p.add_argument("--extra-tests", default="", help="comma-separated test files added to all")
    a = p.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    corpus_dir = out / "corpus"
    if not (corpus_dir / "recon.jsonl").exists():
        build_corpus(corpus_dir)
    base = {e: signatures(corpus_dir, e) for e in ("recon", "aml", "ach", "payables", "commerce",
                                                   "recon+aml")}  # fmt: skip
    rng = random.Random(a.seed)
    todo: list[Mutant] = []
    only = set(filter(None, a.only.split(",")))
    if a.only_file:
        only |= set(filter(None, Path(a.only_file).read_text().split()))
    for mod in a.modules.split(","):
        ms = enumerate_mutants(mod)
        if only:
            todo += [m for m in ms if m.id in only]
            continue
        rng.shuffle(ms)
        todo += sorted(ms[: a.per_module], key=lambda m: m.index)
    works = []
    for i in range(a.workers):
        w = Path(tempfile.mkdtemp(prefix=f"mut{i}-", dir=out))
        _copy_repo(w)
        works.append(w)
    rows: list[dict[str, Any]] = []
    queues = [todo[i :: a.workers] for i in range(a.workers)]

    def worker(i: int) -> list[dict[str, Any]]:
        res = []
        for m in queues[i]:
            res.append(
                _run_one(
                    m,
                    works[i],
                    corpus_dir.resolve(),
                    base,
                    a.timeout,
                    not a.no_new_tests,
                    tuple(filter(None, a.extra_tests.split(","))),
                )
            )
            with (out / "progress.jsonl").open("a") as fh:
                fh.write(json.dumps(res[-1]) + "\n")
        return res

    with ThreadPoolExecutor(a.workers) as ex:
        for r in ex.map(worker, range(a.workers)):
            rows += r
    (out / "mutants.json").write_text(json.dumps(rows, indent=1, sort_keys=True) + "\n")
    for w in works:
        shutil.rmtree(w, ignore_errors=True)
    print(json.dumps(summarise(rows), indent=1))
    return 0


def summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for mod in sorted({r["module"] for r in rows}):
        rs = [r for r in rows if r["module"] == mod]
        dist = [r for r in rs if r.get("status") in ("killed", "survived")]
        hard = [r for r in dist if r.get("hard_verdict_changed", r.get("hard_changed"))]
        out[mod] = {
            "mutants": len(rs),
            "equivalent_on_corpus": sum(r.get("status") == "equivalent" for r in rs),
            "distinguishable": len(dist),
            "killed": sum(r["status"] == "killed" for r in dist),
            "hard_changing": len(hard),
            "hard_killed": sum(r["status"] == "killed" for r in hard),
            "survivors": [r["id"] for r in dist if r["status"] == "survived"],
        }
    return out


if __name__ == "__main__":
    raise SystemExit(main())
