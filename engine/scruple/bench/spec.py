"""Which cases a frozen benchmark version holds, and how they regenerate (174).

A benchmark version is a fixed number of `dispute` episodes drawn from the sealed
held-out seed namespace (`scruple.sim.splits`). Case `i` of version `v` is episode

    seed      = SeedSpace(salt).seed(HELDOUT, v * INDEX_STRIDE + i)
    id prefix = SeedSpace(salt).id_prefix(HELDOUT, v * INDEX_STRIDE + i)
    case      = the first case of that seed's stream under the episode mix

so the cases regenerate deterministically from the salt and the version, and the
case ids an agent sees carry the prefix, never the seed. The offset keeps benchmark
indices clear of the small held-out indices other runs use.

Only the manifest is published: the version, the case count, the mix hash, a salted
commitment to the salt, a commitment to the seeds, a hash over every case (ground
truth included), the case-family and difficulty mix, and reference scores. Its
content hash (`manifest_hash`) names the benchmark. Without the salt nobody can
enumerate the cases; with it `regenerate` rebuilds them and `cases_digest` must
equal the manifest's.

Difficulty tier (per case, from ground truth, published only as counts):

- `uninformative`: every required document kind is one every merchant holds, so the
  evidence says nothing about who is right;
- `aligned`: the evidence points the right way (a valid claim and some required
  kind missing, or an invalid claim and every required kind held as a clean,
  consistent document);
- `misleading`: the evidence points the wrong way.

Synthetic data only. The tiers are our definition (ASSUMPTION, see
`scruple.sim.difficulty` for the coverage measure they use).
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from scruple.agents.env import DISPUTE_MIX, select_dispute
from scruple.hashing import canonical_json, content_hash, sha256_hex
from scruple.sim.difficulty import coverage, informative
from scruple.sim.disputes import CardDispute
from scruple.sim.splits import DOMAIN, SeedSpace, Split

_FROZEN = ConfigDict(frozen=True, extra="forbid")

INDEX_STRIDE = 1_000_000_000  # held-out index of case i in version v: v * stride + i
DEFAULT_CASES = 10_000
Tier = Literal["uninformative", "aligned", "misleading"]
TIERS: tuple[Tier, ...] = ("aligned", "misleading", "uninformative")


@dataclass(frozen=True)
class BenchCase:
    """One benchmark episode: what `DisputeEpisode` needs. Never printed to agents."""

    index: int  # position in the benchmark, 0-based
    seed: int
    id_prefix: str


def heldout_index(version: int, i: int) -> int:
    if version < 1:
        raise ValueError("benchmark versions start at 1")
    if not 0 <= i < INDEX_STRIDE:
        raise ValueError("case index out of range")
    return version * INDEX_STRIDE + i


def bench_cases(space: SeedSpace, version: int, indices: Iterable[int]) -> list[BenchCase]:
    """Seeds and id prefixes for the given case positions (needs the salt)."""
    out = []
    for i in indices:
        h = heldout_index(version, i)
        out.append(BenchCase(i, space.seed(Split.HELDOUT, h), space.id_prefix(Split.HELDOUT, h)))
    return out


def case_of(bc: BenchCase) -> CardDispute:
    return select_dispute(bc.seed, None, bc.id_prefix)


def tier_of(case: CardDispute) -> Tier:
    if not informative(case):
        return "uninformative"
    full = coverage(case) >= 1.0
    return "misleading" if full == case.truth.claim_valid else "aligned"


def case_digest(case: CardDispute) -> str:
    """SHA-256 of one case (ground truth included)."""
    return sha256_hex(canonical_json(case.model_dump(mode="json")))


def cases_digest(digests: Sequence[str]) -> str:
    """One hash over the per-case digests, in benchmark order."""
    h = hashlib.sha256()
    for d in digests:
        h.update(d.encode())
        h.update(b"\n")
    return h.hexdigest()


def seeds_commitment(cases: Sequence[BenchCase]) -> str:
    """A hash naming the seeds without listing them (one-way, like `SplitManifest`)."""
    h = hashlib.sha256()
    for c in cases:
        h.update(sha256_hex(f"{DOMAIN}/seed/{c.seed}").encode())
    return h.hexdigest()


class CaseInfo(BaseModel):
    """What the manifest counts about one case (never published per case)."""

    model_config = _FROZEN

    index: int
    digest: str
    family: str
    tier: Tier
    stage: str
    network: str
    claim_valid: bool


def info_of(bc: BenchCase) -> CaseInfo:
    c = case_of(bc)
    return CaseInfo(
        index=bc.index,
        digest=case_digest(c),
        family=c.truth.family.value,
        tier=tier_of(c),
        stage=c.opening_stage.value,
        network=c.network.value,
        claim_valid=c.truth.claim_valid,
    )


class Mix(BaseModel):
    """What the benchmark holds, as counts only."""

    model_config = _FROZEN

    families: dict[str, int]
    tiers: dict[str, int]
    stages: dict[str, int]  # opening stage
    networks: dict[str, int]
    claim_valid: int


def mix_of(infos: Sequence[CaseInfo]) -> Mix:
    def count(xs: Iterable[str]) -> dict[str, int]:
        return dict(sorted(Counter(xs).items()))

    return Mix(
        families=count(i.family for i in infos),
        tiers=count(i.tier for i in infos),
        stages=count(i.stage for i in infos),
        networks=count(i.network for i in infos),
        claim_valid=sum(i.claim_valid for i in infos),
    )


class BenchManifest(BaseModel):
    """The published identity of a benchmark version. Hash it with `manifest_hash`."""

    model_config = _FROZEN

    name: str = "scruple-dispute-bench"
    version: int = Field(ge=1)
    n_cases: int = Field(ge=1)
    index_stride: int = INDEX_STRIDE
    block: int = Field(ge=1)  # batch checks and digests per block of consecutive cases
    mix_hash: str  # the episode generator mix (`DISPUTE_MIX`)
    salt_commitment: str
    seeds_commitment: str
    cases_digest: str
    block_digests: tuple[str, ...]
    mix: Mix
    engine_version: str  # scruple version the cases were generated with
    synthetic: bool = True

    @property
    def manifest_hash(self) -> str:
        return content_hash(self)


def block_digests(digests: Sequence[str], block: int) -> tuple[str, ...]:
    return tuple(cases_digest(digests[i : i + block]) for i in range(0, len(digests), block))


def require_salt(space: SeedSpace) -> None:
    if space.sealed:
        raise PermissionError(
            "the benchmark is sealed: set SCRUPLE_HELDOUT_SALT to the salt it was built with"
        )


def check_salt(space: SeedSpace, manifest: BenchManifest) -> None:
    require_salt(space)
    if space.salt_commitment() != manifest.salt_commitment:
        raise PermissionError("wrong salt: its commitment does not match the manifest")


def make_manifest(
    space: SeedSpace,
    version: int,
    cases: Sequence[BenchCase],
    infos: Sequence[CaseInfo],
    block: int,
    engine_version: str,
) -> BenchManifest:
    commitment = space.salt_commitment()
    assert commitment is not None
    digests = [i.digest for i in infos]
    return BenchManifest(
        version=version,
        n_cases=len(cases),
        block=block,
        mix_hash=content_hash(DISPUTE_MIX),
        salt_commitment=commitment,
        seeds_commitment=seeds_commitment(cases),
        cases_digest=cases_digest(digests),
        block_digests=block_digests(digests, block),
        mix=mix_of(infos),
        engine_version=engine_version,
    )


def space_from(salt: str | SecretStr | None) -> SeedSpace:
    return SeedSpace(salt) if salt is not None else SeedSpace.from_settings()


__all__ = [
    "DEFAULT_CASES",
    "INDEX_STRIDE",
    "TIERS",
    "BenchCase",
    "BenchManifest",
    "CaseInfo",
    "Mix",
    "Tier",
    "bench_cases",
    "block_digests",
    "case_digest",
    "case_of",
    "cases_digest",
    "check_salt",
    "heldout_index",
    "info_of",
    "make_manifest",
    "mix_of",
    "require_salt",
    "seeds_commitment",
    "space_from",
    "tier_of",
]
