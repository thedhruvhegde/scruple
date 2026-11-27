"""Whole-environment leak probes (round 6): can an agent rebuild the hidden case?

These are not genomes: they attack the environment's surface, not its verifier.

- `inversion_probe(env, n, keyed)`: an episode is started on a seed the attacker does
  not know; the attacker sees what the agent sees on its first call (the episode id
  and the observation) and scans seeds `0..scan-1` with the public generator, matching
  (a) the episode id (the pre-round-6 id was `sha256(seed, case_type)`) and (b) the
  observable case content. A match whose regenerated truth equals the episode's
  hidden truth is a recovery. Keyed episodes must recover 0; an unkeyed (public)
  episode is rebuildable by content by design, and is reported as such.
- `rehearsal_probe(kind, n)`: through the MCP sim server's own episode table, start
  (kind, seed), submit at once, read the returned score; start the same (kind, seed)
  again. Counts second starts whose case equals the first (a rehearsed case) and
  returned scores that name hidden truth (`expected`, `family`, `breaks`, `truth`,
  check explanations).
"""

from __future__ import annotations

import json
import math
from typing import Any

from scruple.agents.keys import new_salt
from scruple.hashing import content_hash, sha256_hex

TRUTH_KEYS = ("expected", "family", "breaks", "truth", "suspicious", "explanation", "correct")
FIRST = {"recon": "get_task", "aml": "get_alert", "ach": "get_case", "payables": "get_batch"}


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (round(max(0.0, (c - h) / d), 4), round(min(1.0, (c + h) / d), 4))


def legacy_episode_id(seed: int, case_type: str | None, nonce: int = 0) -> str:
    """The pre-round-6 id, all the attacker can compute: no secret."""
    return "ep-" + content_hash({"seed": seed, "case_type": case_type, "nonce": nonce})[:12]


def _view(ep: Any) -> Any:
    if hasattr(ep, "case_view"):
        return ep.case_view()
    v = dict(ep.session.case_view())
    v.pop("case_id", None)  # a keyed id differs by construction; compare the content
    return v


def _view_hash(ep: Any) -> str:
    return sha256_hex(json.dumps(_view(ep), sort_keys=True, default=str))


def _truth(ep: Any) -> Any:
    if hasattr(ep, "case"):
        return ep.case.truth
    return ep.session.engine.case(ep.session.case_id).truth.model_dump(mode="json")


def inversion_probe(
    env: str, n: int = 200, *, keyed: bool = True, scan: int = 5_000, seed0: int = 1_000
) -> dict[str, Any]:
    """Seeds `seed0 + i*7919 mod scan` are the hidden ones (all inside the scanned
    range, so an unkeyed episode is always findable)."""
    from scruple.finops.envs import get_env

    e = get_env(env)
    ct = "@0.5"
    table_id: dict[str, int] = {}
    table_view: dict[str, int] = {}
    for s in range(scan):
        table_id[legacy_episode_id(s, f"{env}:{ct}", 0)] = s
        table_view.setdefault(_view_hash(e.new_episode(s, ct)), s)
    by_id = by_view = truth_hits = 0
    for i in range(n):
        hidden = (seed0 + i * 7919) % scan
        ep = e.new_episode(hidden, ct, salt=new_salt() if keyed else None)
        out = ep.call(FIRST[env])
        eid = out.get("episode_id") or ep.observe().episode_id
        s_id = table_id.get(eid)
        s_view = table_view.get(_view_hash(ep))
        by_id += s_id is not None
        by_view += s_view is not None
        found = s_id if s_id is not None else s_view
        if found is not None and e.new_episode(found, ct).case.truth == ep.case.truth:
            truth_hits += 1
    return {
        "probe": "seed inversion",
        "env": env,
        "keyed": keyed,
        "n": n,
        "scan": scan,
        "id_matched": by_id,
        "content_matched": by_view,
        "truth_recovered": truth_hits,
        "rate": round(truth_hits / n, 4),
        "ci95": wilson(truth_hits, n),
    }


def _leaks(x: Any) -> bool:
    if isinstance(x, dict):
        return any(k in TRUTH_KEYS or _leaks(v) for k, v in x.items())
    if isinstance(x, list):
        return any(_leaks(v) for v in x)
    return False


def rehearsal_probe(kind: str, n: int = 200, *, seed0: int = 0) -> dict[str, Any]:
    from scruple.mcp.sim_server import EpisodeTable

    t = EpisodeTable()
    same = leaked = 0
    kinds = ("dispute",) if kind == "dispute" else (kind,)
    for s in range(seed0, seed0 + n):
        probe: Any = t.start_dispute(s, None) if kind == "dispute" else t.start_kind(kind, s, None)
        score = t.call(probe.episode_id, "submit_decision", {"explanation": "probe"}, kinds=kinds)
        real: Any = t.start_dispute(s, None) if kind == "dispute" else t.start_kind(kind, s, None)
        same += _truth(probe) == _truth(real) and _view_hash(probe) == _view_hash(real)
        leaked += _leaks(score)
    return {
        "probe": "MCP rehearsal",
        "env": kind,
        "n": n,
        "same_case_on_restart": same,
        "same_ci95": wilson(same, n),
        "score_names_truth": leaked,
        "leak_ci95": wilson(leaked, n),
    }


def run_all(n: int = 200, scan: int = 5_000) -> list[dict[str, Any]]:
    out = []
    for env in FIRST:
        out.append(inversion_probe(env, n, keyed=True, scan=scan))
        out.append(inversion_probe(env, n, keyed=False, scan=scan))
    for kind in ("aml", "recon", "ach", "payables", "commerce", "dispute"):
        out.append(rehearsal_probe(kind, n))
    return out


__all__ = ["inversion_probe", "rehearsal_probe", "run_all", "wilson"]
