"""Sealed seed splits: train, dev and held-out generators that cannot overlap (033).

A generator seed is a 64-bit integer. Its top two bits name the namespace:

    01 train      seed = 01 || low62(SHA-256("scruple-split/train/<index>"))
    10 dev        seed = 10 || low62(SHA-256("scruple-split/dev/<index>"))
    11 heldout    seed = 11 || low62(HMAC-SHA256(salt, "scruple-split/heldout/<index>"))

so the three namespaces are disjoint by construction, and an ordinary small seed
(top bits 00, e.g. `generate_cases(7, ...)`) belongs to none of them. Train and dev
seeds are public; held-out seeds need the secret salt. Without it the held-out
namespace cannot be enumerated: guessing one seed is a 62-bit search, and the salt
never appears in a manifest (only a salted SHA-256 commitment to it does).

Case ids would leak a seed (`cd-<seed>-<i>`), so split runs pass `generate_cases` an
`id_prefix` derived the same way (`tr-`, `dv-`, `ho-` plus 12 hex characters). Knowing
a held-out case id does not give its seed.

The salt comes from `Settings.heldout_salt` (env `SCRUPLE_HELDOUT_SALT` only, never a
file) or is passed in; it must be at least 16 characters.

A `SplitManifest` records which split, seed indices, mix and start date a run used,
a commitment per seed and the SHA-256 of every generated case (truth included).
`manifest_hash` is its content hash: a report quoting it proves which split it ran on,
and `verify_manifest` re-checks a manifest against cases (and, given the salt,
regenerates them).

What this does not stop: someone holding the salt, or an agent that reads the
held-out cases during training (that is a process control: keep the salt out of the
training environment). Synthetic data only.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Sequence
from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from scruple.hashing import canonical_json, content_hash, sha256_hex
from scruple.settings import Settings, load_settings
from scruple.sim.disputes import DEFAULT_MIX, START_DATE, CardDispute, CaseMix, generate_cases

_FROZEN = ConfigDict(frozen=True, extra="forbid")

MIN_SALT_CHARS = 16
_LOW62 = (1 << 62) - 1
DOMAIN = "scruple-split"


class Split(StrEnum):
    TRAIN = "train"
    DEV = "dev"
    HELDOUT = "heldout"


TAG: dict[Split, int] = {Split.TRAIN: 0b01, Split.DEV: 0b10, Split.HELDOUT: 0b11}
_BY_TAG = {v: k for k, v in TAG.items()}
PREFIX: dict[Split, str] = {Split.TRAIN: "tr", Split.DEV: "dv", Split.HELDOUT: "ho"}


class SealedSplitError(PermissionError):
    """The held-out namespace was asked for without the salt."""


def split_of(seed: int) -> Split | None:
    """Which namespace a seed belongs to (None for an ordinary seed)."""
    if not 0 <= seed < (1 << 64):
        return None
    return _BY_TAG.get(seed >> 62)


def _salt_bytes(salt: str | bytes | SecretStr | None) -> bytes | None:
    if salt is None:
        return None
    if isinstance(salt, SecretStr):
        salt = salt.get_secret_value()
    raw = salt.encode("utf-8") if isinstance(salt, str) else bytes(salt)
    if len(raw) < MIN_SALT_CHARS:
        raise ValueError(f"the held-out salt must be at least {MIN_SALT_CHARS} characters")
    return raw


class SeedSpace:
    """Seeds and case-id prefixes for each split. Only the held-out side needs the salt."""

    def __init__(self, salt: str | bytes | SecretStr | None = None) -> None:
        self._salt = _salt_bytes(salt)

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> SeedSpace:
        s = settings if settings is not None else load_settings()
        return cls(s.heldout_salt)

    @property
    def sealed(self) -> bool:
        """True when this space cannot produce held-out seeds (no salt)."""
        return self._salt is None

    def __repr__(self) -> str:  # never print the salt
        return f"SeedSpace(sealed={self.sealed})"

    def _digest(self, split: Split, label: str, index: int) -> bytes:
        if index < 0:
            raise ValueError("seed index must be non-negative")
        msg = f"{DOMAIN}/{split.value}/{label}{index}".encode()
        if split is Split.HELDOUT:
            if self._salt is None:
                raise SealedSplitError("the held-out split needs the salt (SCRUPLE_HELDOUT_SALT)")
            return hmac.new(self._salt, msg, hashlib.sha256).digest()
        return hashlib.sha256(msg).digest()

    def seed(self, split: Split, index: int) -> int:
        low = int.from_bytes(self._digest(split, "", index)[:8], "big") & _LOW62
        return (TAG[split] << 62) | low

    def seeds(self, split: Split, indices: Sequence[int]) -> tuple[int, ...]:
        return tuple(self.seed(split, i) for i in indices)

    def id_prefix(self, split: Split, index: int) -> str:
        return f"{PREFIX[split]}-{self._digest(split, 'id/', index).hex()[:12]}"

    def is_member(self, seed: int, split: Split, index: int) -> bool:
        """Whether `seed` is this space's seed for (split, index)."""
        return self.seed(split, index) == seed

    def salt_commitment(self) -> str | None:
        """A salted hash naming the salt without revealing it (None when sealed)."""
        if self._salt is None:
            return None
        return hmac.new(self._salt, f"{DOMAIN}/commitment".encode(), hashlib.sha256).hexdigest()

    def generate(
        self,
        split: Split,
        indices: Sequence[int],
        n_per_seed: int,
        mix: CaseMix = DEFAULT_MIX,
        start: date = START_DATE,
    ) -> tuple[list[CardDispute], SplitManifest]:
        """Cases for `n_per_seed` cases from each seed index, plus their manifest."""
        if not indices:
            raise ValueError("at least one seed index")
        if len(set(indices)) != len(indices):
            raise ValueError("seed indices must be unique")
        if n_per_seed < 1:
            raise ValueError("n_per_seed must be positive")
        cases: list[CardDispute] = []
        for i in indices:
            cases.extend(
                generate_cases(
                    self.seed(split, i), n_per_seed, mix, start, id_prefix=self.id_prefix(split, i)
                )
            )
        manifest = SplitManifest(
            split=split,
            indices=tuple(indices),
            n_per_seed=n_per_seed,
            mix_hash=content_hash(mix),
            start=start,
            salt_commitment=self.salt_commitment() if split is Split.HELDOUT else None,
            seed_commitments=tuple(
                sha256_hex(f"{DOMAIN}/seed/{s}") for s in self.seeds(split, indices)
            ),
            cases_hash=cases_hash(cases),
            n_cases=len(cases),
        )
        return cases, manifest


def cases_hash(cases: Sequence[CardDispute]) -> str:
    """SHA-256 over every case in order, ground truth included."""
    h = hashlib.sha256()
    for c in cases:
        h.update(canonical_json(c.model_dump(mode="json")).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


class SplitManifest(BaseModel):
    """What a run was generated from. Quote `manifest_hash` in reports."""

    model_config = _FROZEN

    version: int = 1
    split: Split
    indices: tuple[int, ...] = Field(min_length=1)
    n_per_seed: int = Field(ge=1)
    mix_hash: str
    start: date
    salt_commitment: str | None
    seed_commitments: tuple[str, ...]
    cases_hash: str
    n_cases: int = Field(ge=1)

    @property
    def manifest_hash(self) -> str:
        return content_hash(self)


def verify_manifest(
    manifest: SplitManifest,
    cases: Sequence[CardDispute],
    space: SeedSpace | None = None,
    mix: CaseMix | None = None,
) -> tuple[str, ...]:
    """Problems found (empty: the cases are what the manifest says).

    Without `space` only the case hash and count are checked. With it (and the salt
    for a held-out manifest) the cases are regenerated and the commitments checked."""
    out: list[str] = []
    if len(cases) != manifest.n_cases:
        out.append(f"expected {manifest.n_cases} cases, got {len(cases)}")
    if cases_hash(cases) != manifest.cases_hash:
        out.append("cases hash does not match the manifest")
    if mix is not None and content_hash(mix) != manifest.mix_hash:
        out.append("mix does not match the manifest")
    if space is None:
        return tuple(out)
    if manifest.split is Split.HELDOUT and space.salt_commitment() != manifest.salt_commitment:
        out.append("salt commitment does not match (wrong or missing salt)")
        return tuple(out)
    want = tuple(
        sha256_hex(f"{DOMAIN}/seed/{s}") for s in space.seeds(manifest.split, manifest.indices)
    )
    if want != manifest.seed_commitments:
        out.append("seed commitments do not match")
    if mix is not None:
        regen, _ = space.generate(
            manifest.split, manifest.indices, manifest.n_per_seed, mix, manifest.start
        )
        if cases_hash(regen) != manifest.cases_hash:
            out.append("regenerated cases differ from the manifest")
    return tuple(out)


def overlap(a: Sequence[CardDispute], b: Sequence[CardDispute]) -> dict[str, int]:
    """Case ids and transaction ids two runs share (0 and 0 for disjoint splits)."""
    return {
        "case_ids": len({c.case_id for c in a} & {c.case_id for c in b}),
        "txn_ids": len({c.txn_id for c in a} & {c.txn_id for c in b}),
    }
