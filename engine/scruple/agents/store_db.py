"""Episodes in the gate database: the store the API and MCP server use.

`scruple.agents.store.EpisodeStore` (JSON files) stays the CLI's store: one terminal,
one writer. This store keeps the same record in two append-only tables
(`scruple.storage.episodes`) and has the same replay semantics:

- an episode is rebuilt by replaying its call log, which is exact (episodes are
  deterministic given kind, seed, case type and nonce);
- every call is persisted, refused calls included (they count as tool errors), except
  calls on a closed episode, which change nothing and are not logged.

Differences from the file store, all for many writers: each call runs inside the
backend's write transaction (SQLite `BEGIN IMMEDIATE`, the Postgres advisory lock), so
two processes calling the same episode are serialised and every call gets the next
`seq`; a process caches live episodes and replays only when another process has
appended calls since (the stored call count differs from the cached one).

`DurableEpisodeTable` adapts this store to the MCP sim server's table interface, so
`scruple mcp sim --db ...` keeps episodes across server restarts.
"""

from __future__ import annotations

import json
import threading
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

from scruple.agents.dispute_episode import DisputeEpisode
from scruple.agents.episode import Episode, EpisodeError
from scruple.agents.store import (
    KINDS,
    AnyEpisode,
    UnknownEpisodeError,
    new_episode,
    replay_episode,
)
from scruple.clock import Clock, SystemClock
from scruple.storage.base import DEFAULT_TENANT, DuplicateKeyError
from scruple.storage.episodes import EpisodeRow, SQLEpisodes

if TYPE_CHECKING:
    from scruple.mcp.sim_server import EpisodeTable

CACHE = 256


def episodes_repo(storage: object) -> SQLEpisodes:
    repo = getattr(storage, "episodes", None)
    if not isinstance(repo, SQLEpisodes):
        raise TypeError(f"{type(storage).__name__} has no episode tables")
    return repo


class DbEpisodeStore:
    """Same interface as `EpisodeStore` (start, load, call, log), on the database."""

    def __init__(
        self,
        repo: SQLEpisodes,
        *,
        tenant_id: str = DEFAULT_TENANT,
        clock: Clock | None = None,
        cache: int = CACHE,
    ) -> None:
        self._repo = repo
        self.tenant_id = tenant_id
        self._clock = clock or SystemClock()
        self._cache: OrderedDict[str, tuple[AnyEpisode, int]] = OrderedDict()
        self._size = cache
        self._lock = threading.RLock()

    @classmethod
    def on(cls, storage: object, **kw: Any) -> DbEpisodeStore:
        return cls(episodes_repo(storage), **kw)

    def _remember(self, ep: AnyEpisode, n_calls: int) -> None:
        self._cache[ep.episode_id] = (ep, n_calls)
        self._cache.move_to_end(ep.episode_id)
        while len(self._cache) > self._size:
            self._cache.popitem(last=False)

    def start(self, seed: int, case_type: str | None, kind: str = "simple") -> AnyEpisode:
        if kind not in KINDS:
            raise ValueError(f"unknown episode kind {kind!r}; choose from {', '.join(KINDS)}")
        with self._lock:
            for _ in range(1_000):
                with self._repo.transaction():
                    nonce = self._repo.count_starts(self.tenant_id, kind, seed, case_type)
                    while True:
                        ep = new_episode(kind, seed, case_type, nonce)
                        if self._repo.get_episode(self.tenant_id, ep.episode_id) is None:
                            break
                        nonce += 1
                    try:
                        self._repo.insert_episode(
                            EpisodeRow(
                                tenant_id=self.tenant_id,
                                episode_id=ep.episode_id,
                                kind=kind,
                                seed=seed,
                                case_type=case_type,
                                nonce=nonce,
                                created_at=self._clock.now(),
                            )
                        )
                    except DuplicateKeyError:  # pragma: no cover - a concurrent start won
                        continue
                self._remember(ep, 0)
                return ep
        raise RuntimeError("could not allocate an episode id")  # pragma: no cover

    def _row(self, episode_id: str) -> EpisodeRow:
        row = self._repo.get_episode(self.tenant_id, episode_id)
        if row is None:
            raise UnknownEpisodeError(f"unknown episode {episode_id!r}")
        return row

    def _replay(self, episode_id: str) -> tuple[AnyEpisode, int]:
        r = self._row(episode_id)
        calls = [
            (c["tool"], json.loads(c["args_json"]))
            for c in self._repo.calls(self.tenant_id, episode_id)
        ]
        return replay_episode(r.kind, r.seed, r.case_type, calls, r.nonce), len(calls)

    def _current(self, episode_id: str) -> tuple[AnyEpisode, int]:
        """The live episode, replayed only if other processes added calls."""
        stored = len(self._repo.calls(self.tenant_id, episode_id))
        hit = self._cache.get(episode_id)
        if hit is not None and hit[1] == stored:
            self._cache.move_to_end(episode_id)
            return hit
        ep, n = self._replay(episode_id)
        self._remember(ep, n)
        return ep, n

    def load(self, episode_id: str) -> AnyEpisode:
        """A fresh replay (never the cached object: callers may mutate what they get)."""
        return self._replay(episode_id)[0]

    def call(self, episode_id: str, tool: str, args: Any) -> dict[str, Any]:
        """Run one tool and persist it, as `EpisodeStore.call` does."""
        args_json = json.dumps(args, sort_keys=True)
        refused: EpisodeError | None = None
        result: dict[str, Any] = {}
        with self._lock, self._repo.transaction():
            ep, n = self._current(episode_id)
            if ep.closed:
                return ep.call(tool, args)  # raises EpisodeClosedError; nothing logged
            try:
                result = ep.call(tool, json.loads(args_json))  # as a replay will see it
            except EpisodeError as exc:
                refused = exc  # logged too, then raised once the log is committed
            try:
                self._repo.append_call(
                    self.tenant_id, episode_id, n + 1, tool, args_json, self._clock.now()
                )
            except BaseException:
                self._cache.pop(episode_id, None)  # memory ran ahead: replay next time
                raise
            self._remember(ep, n + 1)
        if refused is not None:
            raise refused
        return result

    def log(self, episode_id: str) -> list[dict[str, Any]]:
        self._row(episode_id)
        return [
            {"tool": c["tool"], "args": json.loads(c["args_json"])}
            for c in self._repo.calls(self.tenant_id, episode_id)
        ]


def durable_table(store: DbEpisodeStore) -> EpisodeTable:
    """An MCP `EpisodeTable` whose episodes live in `store` (built lazily: mcp is an
    optional import)."""
    from mcp.server.mcpserver.exceptions import ToolError

    from scruple.mcp.sim_server import EpisodeTable

    class DurableEpisodeTable(EpisodeTable):
        def start(self, seed: int, case_type: str | None) -> Episode:
            try:
                ep = store.start(seed, case_type, "simple")
            except ValueError as exc:
                raise ToolError(str(exc)) from exc
            assert isinstance(ep, Episode)
            return ep

        def start_dispute(self, seed: int, family: str | None) -> DisputeEpisode:
            try:
                ep = store.start(seed, family, "dispute")
            except ValueError as exc:
                raise ToolError(str(exc)) from exc
            assert isinstance(ep, DisputeEpisode)
            return ep

        def call(self, episode_id: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
            try:
                return store.call(episode_id, tool, args)
            except UnknownEpisodeError:
                raise ToolError(
                    f"unknown episode {episode_id!r}; call start_episode first"
                ) from None
            except EpisodeError as exc:
                raise ToolError(str(exc)) from exc

    return DurableEpisodeTable()


__all__ = ["DbEpisodeStore", "durable_table", "episodes_repo"]
