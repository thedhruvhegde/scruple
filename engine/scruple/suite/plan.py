"""`scruple suite plan`: a versioned, hash-committed scenario bank.

A plan is a list of scenarios across environments, difficulty bands and case
families. Each scenario names `(env, seed, case_type)`, which regenerate its episode
exactly, plus what the report needs to slice by (family, band, adversarial
artefacts). Its hidden ground truth (the scenario's "future": which claims are
valid, which breaks exist, what the right outcome is) is not in the plan. The plan
holds a salted commitment to it, `sha256(salt | truth)`, and a hash of everything
the agent can observe (`obs_hash`). The salt is written to a separate file the
client never needs; whoever holds it can reveal and check every commitment
(`scruple suite verify --salt`).

Hashes in `plan.json`:

- `bank_hash`: sha256 over the canonical JSON of every scenario's public fields
  (no commitments). It depends only on the plan parameters and the engine, so the
  same `--seed` gives the same bank on any machine (tests pin it).
- `blocks`: one sha256 per block of 500 scenarios, commitments included, so a
  report can show exactly which block a result came from.
- `commitments_hash`: sha256 over all truth commitments, and `salt_commitment`,
  sha256 of the salt.
- `plan_hash`: sha256 over the plan body (everything above), the id a report cites.

Determinism: scenario seeds are derived as `sha256(plan seed | env | index)`, the
difficulty inside a band and the case family come from a `random.Random` seeded the
same way, and the run order is a seeded shuffle fixed before any outcome exists (the
release test reads outcomes in that order, which keeps it anytime-valid).
"""

from __future__ import annotations

import json
import random
import secrets
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from scruple.hashing import sha256_hex
from scruple.suite.envs import ENV_NAMES, get_adapter
from scruple.version import package_version

PLAN_VERSION = "1"
BLOCK = 500
SEED_BASE = 20_000_000  # public dev seed space for suites (the sealed bench is 1e9+)
BANDS: dict[str, tuple[float, float]] = {
    "easy": (0.05, 0.33),
    "medium": (0.33, 0.66),
    "hard": (0.66, 0.95),
}
DEFAULT_MIX = "easy:0.3,medium:0.4,hard:0.3"


class Scenario(BaseModel):
    model_config = ConfigDict(frozen=True)

    scenario_id: str
    index: int  # position in the run order
    env: str
    seed: int
    case_type: str | None
    band: str
    difficulty: float
    family: str
    adversarial: tuple[str, ...]  # adversarial artefact kinds the case carries
    obs_hash: str
    truth_commitment: str

    def public(self) -> dict[str, Any]:
        d = self.model_dump(mode="json")
        d.pop("truth_commitment")
        return d


def parse_mix(text: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for part in text.split(","):
        name, _, w = part.strip().partition(":")
        if name not in BANDS:
            raise ValueError(f"unknown difficulty band {name!r}; choose from {', '.join(BANDS)}")
        out[name] = float(w)
    tot = sum(out.values())
    if tot <= 0 or any(v < 0 for v in out.values()):
        raise ValueError("difficulty weights must be non-negative and not all zero")
    return {k: v / tot for k, v in out.items()}


def parse_caps(text: str | None) -> dict[str, float]:
    if not text:
        return {}
    out = {}
    for part in text.split(","):
        env, _, v = part.partition("=")
        if env not in ENV_NAMES:
            raise ValueError(f"unknown environment {env!r} in --cap")
        out[env] = float(v)
    return out


def _alloc(n: int, weights: dict[str, float]) -> dict[str, int]:
    """Largest-remainder allocation of n over weights (deterministic)."""
    raw = {k: n * w for k, w in weights.items()}
    out = {k: int(v) for k, v in raw.items()}
    rest = n - sum(out.values())
    for k in sorted(raw, key=lambda k: (-(raw[k] - out[k]), k))[:rest]:
        out[k] += 1
    return out


def _describe(env: str, ep: Any, truth: Any) -> tuple[str, tuple[str, ...]]:
    """(family, adversarial artefact kinds) for slicing; from the generated case."""
    arts = [*getattr(ep, "inbox", ()), *getattr(ep, "notes", ())]
    adv = sorted(
        {str(getattr(a, "kind", "artefact")) for a in arts if getattr(a, "adversarial", False)}
    )
    match env:
        case "dispute":
            fam = str(truth["family"])
            if truth.get("forged"):
                adv.append("forged_evidence")
        case "recon":
            kinds = sorted({b["kind"] for b in truth.get("breaks", [])})
            fam = kinds[0] if kinds else "clean"
        case "aml" | "ach":
            fam = str(truth["family"])
        case "payables":
            kinds = sorted({i["kind"] for i in truth.get("invoices", [])} - {"clean", "recurring"})
            fam = kinds[0] if kinds else "clean"
            adv += [k for k in kinds if k in ("phantom_vendor", "duplicate")]
            if truth.get("changes"):
                adv.append("bank_change")
        case "commerce":
            sc = sorted(truth.get("scenarios", []))
            fam = "+".join(sc) if sc else "baseline"
            adv += [s for s in sc if s in ("injection", "beneficiary_change", "refund_bait")]
        case _:
            fam = "default"
    return fam, tuple(sorted(set(adv)))


def _seed_for(plan_seed: int, env: str, i: int) -> int:
    return SEED_BASE + int(sha256_hex(f"{plan_seed}|{env}|{i}")[:8], 16) % 10_000_000


def _build(item: tuple[str, int, int, str, float, float, str]) -> dict[str, Any]:
    """One scenario (picklable, for the process pool)."""
    env, plan_seed, i, band, lo, hi, salt = item
    a = get_adapter(env)
    seed = _seed_for(plan_seed, env, i)
    rng = random.Random(f"{plan_seed}|{env}|{i}|d")
    d = round(lo + (hi - lo) * rng.random(), 4)
    ct = a.case_type_at(d, rng)
    ep = a.make(seed, ct)
    truth = a.truth(ep)
    fam, adv = _describe(env, ep, truth)
    return {
        "scenario_id": f"sc-{env}-" + sha256_hex(f"{PLAN_VERSION}|{env}|{seed}|{ct}")[:12],
        "env": env,
        "seed": seed,
        "case_type": ct,
        "band": band,
        "difficulty": d,
        "family": fam,
        "adversarial": adv,
        "obs_hash": a.obs_hash(ep),
        "truth_commitment": a.truth_hash(ep, salt),
    }


def _generator_info(envs: Sequence[str]) -> dict[str, Any]:
    from scruple.finops.envs import SUITE_VERSION, get_env

    info: dict[str, Any] = {"finops_suite_version": SUITE_VERSION}
    for e in envs:
        if e in ("recon", "aml", "ach", "payables"):
            v = get_env(e).verifier()
            info[e] = {"verifier": v.id, "fingerprint": v.fingerprint}
        else:
            info[e] = {"verifier": f"{e}-scorecard"}
    return info


def build_plan(
    envs: Sequence[str],
    size: int,
    *,
    seed: int = 0,
    mix: str = DEFAULT_MIX,
    caps: dict[str, float] | None = None,
    salt: str | None = None,
    workers: int = 1,
) -> tuple[dict[str, Any], str]:
    """(plan, salt). Deterministic given the arguments and the salt."""
    from scruple.finops.parallel import pmap

    for e in envs:
        if e not in ENV_NAMES:
            raise ValueError(f"unknown environment {e!r}; choose from {', '.join(ENV_NAMES)}")
    if size < 1:
        raise ValueError("size must be at least 1")
    envs = list(dict.fromkeys(envs))
    weights = parse_mix(mix)
    caps = caps or {}
    salt = salt or secrets.token_hex(16)
    per_env = _alloc(size, {e: 1.0 / len(envs) for e in envs})
    work: list[tuple[str, int, int, str, float, float, str]] = []
    for e in envs:
        i = 0
        for band, k in _alloc(per_env[e], weights).items():
            lo, hi = BANDS[band]
            cap = caps.get(e)
            if cap is not None:  # scale the band into [0, cap]
                lo, hi = lo * cap, hi * cap
            for _ in range(k):
                work.append((e, seed, i, band, lo, hi, salt))
                i += 1
    t0 = time.perf_counter()
    rows = pmap(_build, work, workers=workers)
    gen_s = time.perf_counter() - t0
    order = list(range(len(rows)))
    random.Random(f"{seed}|order").shuffle(order)
    scenarios = [Scenario(index=pos, **rows[j]) for pos, j in enumerate(order)]
    ids = [s.scenario_id for s in scenarios]
    if len(set(ids)) != len(ids):
        raise ValueError("scenario id collision; use another --seed")
    public = [s.public() for s in scenarios]
    body: dict[str, Any] = {
        "suite": "scruple-suite",
        "plan_version": PLAN_VERSION,
        "scruple_version": package_version(),
        "params": {"envs": envs, "size": size, "seed": seed, "mix": weights, "caps": caps},
        "generator": _generator_info(envs),
        "n": len(scenarios),
        "bank_hash": sha256_hex(json.dumps(public, sort_keys=True)),
        "commitments_hash": sha256_hex("".join(s.truth_commitment for s in scenarios)),
        "salt_commitment": sha256_hex(salt),
        "blocks": [
            sha256_hex(
                json.dumps(
                    [s.model_dump(mode="json") for s in scenarios[b : b + BLOCK]], sort_keys=True
                )
            )
            for b in range(0, len(scenarios), BLOCK)
        ],
        "synthetic": True,
        "scenarios": [s.model_dump(mode="json") for s in scenarios],
    }
    body["plan_hash"] = plan_hash(body)
    body["stats"] = {
        "generation_s": round(gen_s, 3),
        "scenarios_per_s": round(len(scenarios) / gen_s, 1) if gen_s > 0 else None,
        "workers": workers,
    }
    return body, salt


def plan_hash(plan: dict[str, Any]) -> str:
    body = {k: v for k, v in plan.items() if k not in ("plan_hash", "stats")}
    return sha256_hex(json.dumps(body, sort_keys=True))


def write_plan(plan: dict[str, Any], salt: str, out: Path, salt_out: Path | None = None) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(plan, indent=1, sort_keys=True) + "\n")
    sp = salt_out or out.with_suffix(".salt")
    sp.write_text(salt + "\n")
    return sp


def load_plan(path: Path) -> dict[str, Any]:
    plan: dict[str, Any] = json.loads(path.read_text())
    if plan.get("suite") != "scruple-suite":
        raise ValueError(f"{path} is not a scruple suite plan")
    return plan


def scenarios_of(plan: dict[str, Any]) -> list[Scenario]:
    return [Scenario.model_validate(s) for s in plan["scenarios"]]


def verify_plan(
    plan: dict[str, Any], *, salt: str | None = None, limit: int | None = None
) -> list[str]:
    """Problems with a plan (empty when it checks out): the plan hash, the bank hash,
    and, for every scenario (or the first `limit`), that the regenerated case has the
    same observable hash and, given the salt, the same truth commitment."""
    problems: list[str] = []
    if plan_hash(plan) != plan.get("plan_hash"):
        problems.append("plan_hash does not match the plan body")
    scen = scenarios_of(plan)
    public = [s.public() for s in scen]
    if sha256_hex(json.dumps(public, sort_keys=True)) != plan.get("bank_hash"):
        problems.append("bank_hash does not match the scenarios")
    if salt is not None and sha256_hex(salt) != plan.get("salt_commitment"):
        problems.append("the salt does not match salt_commitment")
        salt = None
    for s in scen[: limit if limit is not None else len(scen)]:
        a = get_adapter(s.env)
        ep = a.make(s.seed, s.case_type)
        if a.obs_hash(ep) != s.obs_hash:
            problems.append(f"{s.scenario_id}: regenerated case differs (obs_hash)")
        if salt is not None and a.truth_hash(ep, salt) != s.truth_commitment:
            problems.append(f"{s.scenario_id}: truth commitment does not match")
    return problems


__all__ = [
    "BANDS",
    "DEFAULT_MIX",
    "PLAN_VERSION",
    "Scenario",
    "build_plan",
    "load_plan",
    "parse_caps",
    "parse_mix",
    "plan_hash",
    "scenarios_of",
    "verify_plan",
    "write_plan",
]
