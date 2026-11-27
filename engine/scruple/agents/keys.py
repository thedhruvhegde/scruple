"""Episode keys: what an agent sees must not let it rebuild the case (round 6, issue 1).

The generators are public and the seed space is small, so anything an agent sees
that is a *public* function of the seed (an id, a case number, an RNG stream) lets
it regenerate the case offline and read the hidden truth. Two layers stop that:

- **Episode ids** are always an HMAC under a secret: the episode's salt when it has
  one, else the run secret (`SCRUPLE_EPISODE_SALT`, or 32 random bytes drawn once per
  process). An id is stable for a fixed (seed, case type, nonce, secret) and is never
  a function of the seed alone. Before round 6 it was `sha256(seed, case_type)`.
- **Keyed episodes** (`salt` given) generate the case, the issuer's replies and
  every other seeded stream from `HMAC(salt, stream, seed)` instead of the seed, with
  separate streams for the case and for the issuer (so knowing one case does not
  predict the issuer), and replace every seed-bearing id with a keyed tag. The same
  (seed, salt) gives the same episode; without the salt the case is unknowable.

An episode with no salt is a *public* case: anyone with the generator and the seed can
rebuild it, and content matching over a small seed range finds the seed. That mode
stays for reproducing earlier results; anything an agent sees without being allowed
its truth (the MCP sim server, `scruple serve`, keyed task exports) is keyed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import cache

from scruple.hashing import content_hash

ENV_VAR = "SCRUPLE_EPISODE_SALT"
SEED_BITS = 62

type Salt = str | bytes


@cache
def run_secret() -> bytes:
    """The process's id secret: `SCRUPLE_EPISODE_SALT` when set, else random."""
    raw = os.environ.get(ENV_VAR)
    return raw.encode() if raw else secrets.token_bytes(32)


def new_salt() -> str:
    """A fresh salt for one keyed episode (MCP starts, `serve`)."""
    return secrets.token_hex(16)


def _key(salt: Salt | None) -> bytes:
    if salt is None:
        return run_secret()
    return salt.encode() if isinstance(salt, str) else salt


def _mac(salt: Salt | None, payload: object) -> str:
    msg = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(_key(salt), msg, hashlib.sha256).hexdigest()


_FORCED_ID: ContextVar[str | None] = ContextVar("scruple_forced_episode_id", default=None)


@contextmanager
def restoring_id(stored: str) -> Iterator[None]:
    """Rebuild a stored episode under the id it was given at start. A store replays an
    episode in another process, whose run secret differs; the id must not."""
    token = _FORCED_ID.set(stored)
    try:
        yield
    finally:
        _FORCED_ID.reset(token)


def episode_id(seed: int, case_type: str | None, nonce: int = 0, salt: Salt | None = None) -> str:
    """`ep-` + 12 hex of HMAC(secret, seed, case type, nonce)."""
    forced = _FORCED_ID.get()
    if forced is not None:
        return forced
    return "ep-" + _mac(salt, {"seed": seed, "case_type": case_type, "nonce": nonce})[:12]


def stable_key(seed: int, case_type: str | None, nonce: int = 0, salt: Salt | None = None) -> str:
    """A per-episode key that is the same in every process (the pre-round-6 id for an
    unkeyed episode). Internal only, never shown to an agent: the scripted baselines,
    the zoo and the benches draw their per-episode randomness from it, so their runs
    stay reproducible across processes now that the visible id is secret."""
    if salt is not None:
        return "sk-" + _mac(salt, {"stable": [seed, case_type, nonce]})[:12]
    payload = {"seed": seed, "case_type": case_type, "nonce": nonce}
    return "ep-" + content_hash(payload)[:12]


def keyed_seed(seed: int, stream: str, salt: Salt | None) -> int:
    """The seed a keyed episode's `stream` uses; the seed itself when unkeyed."""
    if salt is None:
        return seed
    return int(_mac(salt, {"seed": seed, "stream": stream}), 16) >> (256 - SEED_BITS)


def keyed_tag(seed: int, stream: str, salt: Salt) -> str:
    """A 12-hex tag for ids that would otherwise show a seed."""
    return _mac(salt, {"seed": seed, "tag": stream})[:12]


__all__ = [
    "ENV_VAR",
    "episode_id",
    "keyed_seed",
    "keyed_tag",
    "new_salt",
    "restoring_id",
    "run_secret",
    "stable_key",
]
