"""Print a digest of every deterministic output, as JSON (used by test_determinism).

Run as `python -m tests.determinism_probe` in a fresh interpreter, so each run gets
its own PYTHONHASHSEED (set ordering of strings, dict-of-set iteration, ...).
"""

from __future__ import annotations

import json

from scruple.hashing import content_hash, sha256_hex
from scruple.sim.disputes import FULL_MIX, generate_sharded
from tests.golden.episodes import TRACES, dumps


def digests() -> dict[str, str]:
    from tests.ci_support import GOOD, cached_run

    out = {name: sha256_hex(dumps(build())) for name, build in sorted(TRACES.items())}
    out["ci_scorecard_good"] = sha256_hex(cached_run("good").scorecard.to_json())
    out["sharded_generation"] = content_hash(
        [c.model_dump(mode="json") for c in generate_sharded(5, 900, FULL_MIX, shard_size=300)]
    )
    from scruple.ci.evidence import build_evidence_pack

    run = cached_run("good")
    pack = build_evidence_pack(run.gate, GOOD.rule_id, 1)
    out["ci_evidence_pack_good"] = content_hash(pack.model_dump(mode="json"))
    return out


if __name__ == "__main__":
    print(json.dumps(digests(), sort_keys=True))
