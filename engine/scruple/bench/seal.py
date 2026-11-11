"""`scruple bench seal --partner NAME`: a partner's own sealed benchmark and its handover.

Each design partner gets its own held-out salt, so its benchmark cases are its own:
nobody else (no other partner, and not the house benchmark v1) can enumerate them, and
their seeds are disjoint from every other sealed set. `seal`:

1. draws a fresh salt (`secrets.token_urlsafe(48)`, 64 characters) and writes it to
   `<salt dir>/<partner>.salt` with mode 0600, created exclusively (an existing
   partner is refused, never overwritten). The salt directory must be outside any git
   work tree (default `~/.scruple/partners`, or `SCRUPLE_PARTNER_SALT_DIR`);
2. checks that the partner's seeds (and case-id prefixes) for all `n` cases are
   disjoint from those of every other partner salt in that directory and of the house
   salt when `SCRUPLE_HELDOUT_SALT` is set. Seeds are HMAC-SHA256 of the salt and the
   index, so a collision is not expected; the check makes it a fact for these sets;
3. builds the benchmark manifest from that salt (`scruple.bench.core.build`, name
   `scruple-dispute-bench/<partner>`), with reference scores only if asked (they cost
   three full runs);
4. writes `manifest.json` (public: no salt, no seeds) and `HANDOVER.md` (what was
   sealed, its hashes, how to verify and score, and how the salt travels) into `out`.

The salt is never printed, logged or written anywhere else. Synthetic cases only.
"""

from __future__ import annotations

import os
import re
import secrets
from collections.abc import Mapping, Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from scruple.bench.core import BenchFile, build
from scruple.bench.runner import BLOCK
from scruple.bench.spec import DEFAULT_CASES, bench_cases
from scruple.sim.splits import SeedSpace

PARTNER = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
SALT_DIR_ENV = "SCRUPLE_PARTNER_SALT_DIR"
HOUSE_SALT_ENV = "SCRUPLE_HELDOUT_SALT"
DEFAULT_SALT_DIR = "~/.scruple/partners"


class SealResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    partner: str
    manifest_path: str
    note_path: str
    salt_path: str
    manifest_hash: str
    salt_commitment: str
    n_cases: int
    checked_against: tuple[str, ...]  # other sets the seeds were checked disjoint from


def salt_dir_from(env: Mapping[str, str] | None = None, given: str | None = None) -> Path:
    raw = given or (os.environ if env is None else env).get(SALT_DIR_ENV) or DEFAULT_SALT_DIR
    return Path(raw).expanduser().resolve()


def _in_git_tree(path: Path) -> Path | None:
    for p in (path, *path.parents):
        if (p / ".git").exists():
            return p
    return None


def _write_salt(path: Path, salt: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(salt + "\n")
    os.chmod(path, 0o600)


def read_salt(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def heldout_keys(space: SeedSpace, version: int, n: int) -> tuple[set[int], set[str]]:
    """The seeds and case-id prefixes of the first `n` cases of `version`."""
    cases = bench_cases(space, version, range(n))
    return {c.seed for c in cases}, {c.id_prefix for c in cases}


def _others(salt_dir: Path, partner: str, env: Mapping[str, str]) -> list[tuple[str, SeedSpace]]:
    out = [
        (p.stem, SeedSpace(read_salt(p)))
        for p in sorted(salt_dir.glob("*.salt"))
        if p.stem != partner
    ]
    house = env.get(HOUSE_SALT_ENV)
    if house:
        out.append(("house benchmark salt", SeedSpace(house)))
    return out


def check_disjoint(
    space: SeedSpace, others: Sequence[tuple[str, SeedSpace]], version: int, n: int
) -> tuple[str, ...]:
    """Names of the sets checked; raises if any seed, prefix or commitment is shared."""
    seeds, prefixes = heldout_keys(space, version, n)
    for name, other in others:
        if other.salt_commitment() == space.salt_commitment():
            raise ValueError(f"the new salt equals the salt of {name}")
        s2, p2 = heldout_keys(other, version, n)
        if seeds & s2 or prefixes & p2:
            raise ValueError(f"held-out cases overlap with {name}; seal again")
    return tuple(name for name, _ in others)


def handover_note(f: BenchFile, partner: str, salt_path: Path, checked: Sequence[str]) -> str:
    m = f.manifest
    ref = (
        ", ".join(sorted(f.reference))
        if f.reference
        else "not computed (run `scruple bench seal --reference` or score the reference agents)"
    )
    others = ", ".join(checked) if checked else "none (first sealed set)"
    mix = "; ".join(f"{k} {v}" for k, v in sorted(m.mix.families.items()))
    return "\n".join(
        [
            f"# Benchmark handover: {partner}",
            "",
            "A sealed set of SYNTHETIC dispute cases for this partner only. Nothing here is",
            "partner data, and nothing here is a result.",
            "",
            "| field | value |",
            "|---|---|",
            f"| benchmark | {m.name} v{m.version} |",
            f"| cases | {m.n_cases:,} (blocks of {m.block}) |",
            f"| manifest hash | {f.manifest_hash} |",
            f"| salt commitment | {m.salt_commitment} |",
            f"| seeds commitment | {m.seeds_commitment} |",
            f"| cases digest | {m.cases_digest} |",
            f"| engine version | {m.engine_version} |",
            f"| families | {mix} |",
            f"| reference scores | {ref} |",
            f"| seeds checked disjoint from | {others} |",
            "",
            "## The salt",
            "",
            f"- Held by scruple in `{salt_path.name}` (mode 0600, outside any repository).",
            "- It is not in this note or in manifest.json. It travels by a separate channel",
            "  from this note (for example a password manager share), never in the same",
            "  email or ticket, and never into a repository or a CI log.",
            "- Whoever holds it can enumerate every case: keep it with the people who run",
            "  the benchmark, not with the people whose agent is being scored.",
            "",
            "## Verify and score",
            "",
            "```",
            'export SCRUPLE_HELDOUT_SALT="$(cat <salt file>)"',
            "uv run scruple bench verify --bench manifest.json   # regenerates every case",
            "uv run scruple bench score --bench manifest.json --agent evidence",
            "```",
            "",
            "A wrong salt is refused (its commitment does not match), and a changed",
            "generator fails `verify` block by block.",
            "",
        ]
    )


def seal(
    partner: str,
    out: Path,
    *,
    salt_dir: Path,
    version: int = 1,
    n: int = DEFAULT_CASES,
    block: int = BLOCK,
    jobs: int = 1,
    reference: bool = False,
    env: Mapping[str, str] | None = None,
) -> SealResult:
    if not PARTNER.match(partner):
        raise ValueError("partner name: lowercase letters, digits and hyphens, 2 to 63 chars")
    env = os.environ if env is None else env
    repo = _in_git_tree(salt_dir)
    if repo is not None:
        raise ValueError(f"the salt directory is inside a git work tree ({repo}); pick another")
    salt_path = salt_dir / f"{partner}.salt"
    if salt_path.exists():
        raise FileExistsError(f"{partner} is already sealed; its salt file exists")
    salt = secrets.token_urlsafe(48)
    space = SeedSpace(salt)
    checked = check_disjoint(space, _others(salt_dir, partner, env), version, n)
    agents: tuple[str, ...] = ("evidence", "always_accept", "oracle") if reference else ()
    f, _ = build(space, version, n, jobs=jobs, block=block, agents=agents)
    manifest = f.manifest.model_copy(update={"name": f"scruple-dispute-bench/{partner}"})
    f = f.model_copy(update={"manifest": manifest, "manifest_hash": manifest.manifest_hash})
    _write_salt(salt_path, salt)  # only once everything else worked
    out.mkdir(parents=True, exist_ok=True)
    mpath, npath = out / "manifest.json", out / "HANDOVER.md"
    f.dump(mpath)
    npath.write_text(handover_note(f, partner, salt_path, checked), encoding="utf-8")
    return SealResult(
        partner=partner,
        manifest_path=str(mpath),
        note_path=str(npath),
        salt_path=str(salt_path),
        manifest_hash=f.manifest_hash,
        salt_commitment=manifest.salt_commitment,
        n_cases=manifest.n_cases,
        checked_against=checked,
    )


__all__ = [
    "PARTNER",
    "SALT_DIR_ENV",
    "SealResult",
    "check_disjoint",
    "handover_note",
    "heldout_keys",
    "read_salt",
    "salt_dir_from",
    "seal",
]
