"""A local file store so an episode can span several processes (the CLI).

Each episode is one JSON file holding its kind (`simple`, the default, `dispute`, `ach`,
`recon`, `aml`, `payables`, or `scenario` for a real-life scenario whose case type is
the scenario id), seed, case type (the case family for `dispute`), nonce and call log.
An episode is rebuilt by replaying the log, which is exact because episodes are
deterministic. Files are written atomically. One writer at a time per episode is
assumed (a terminal session); concurrent writers to the same episode are not locked.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from scruple.agents.ach_episode import AchEpisode
from scruple.agents.aml_episode import AmlEpisode
from scruple.agents.dispute_episode import DisputeEpisode
from scruple.agents.episode import Episode, EpisodeError
from scruple.agents.payables_episode import PayablesEpisode
from scruple.agents.recon_episode import ReconEpisode

DEFAULT_STORE = ".scruple/episodes"
_ID = re.compile(r"^ep-[0-9a-f]{12}$")


def default_store() -> Path:
    return Path(os.environ.get("SCRUPLE_EPISODE_STORE", DEFAULT_STORE))


class UnknownEpisodeError(KeyError):
    pass


KINDS = ("simple", "dispute", "scenario", "ach", "recon", "aml", "payables")
AnyEpisode = Episode | DisputeEpisode | AchEpisode | ReconEpisode | AmlEpisode | PayablesEpisode
WORKFLOW_KINDS: dict[
    str, type[AchEpisode] | type[ReconEpisode] | type[AmlEpisode] | type[PayablesEpisode]
] = {
    "ach": AchEpisode,
    "recon": ReconEpisode,
    "aml": AmlEpisode,
    "payables": PayablesEpisode,
}


def new_episode(kind: str, seed: int, case_type: str | None, nonce: int) -> AnyEpisode:
    if kind in WORKFLOW_KINDS:
        return WORKFLOW_KINDS[kind](seed, case_type, nonce=nonce)
    if kind == "dispute":
        return DisputeEpisode(seed, case_type, nonce=nonce)
    if kind == "scenario":  # a real-life scenario; case_type is its id (scruple.realdata)
        from scruple.realdata.situations import ScenarioEpisode

        return ScenarioEpisode(seed, case_type, nonce=nonce)
    return Episode(seed, case_type, nonce=nonce)


def replay_episode(
    kind: str, seed: int, case_type: str | None, calls: list[tuple[str, Any]], nonce: int
) -> AnyEpisode:
    """Rebuild an episode of any kind from its call log (exact: episodes are
    deterministic). Shared by the file store and the database store."""
    if kind in WORKFLOW_KINDS:
        return WORKFLOW_KINDS[kind].replay(seed, case_type, calls, nonce=nonce)
    if kind == "dispute":
        return DisputeEpisode.replay(seed, case_type, calls, nonce=nonce)
    if kind == "scenario":
        from scruple.realdata.situations import ScenarioEpisode

        return ScenarioEpisode.replay(seed, case_type, calls, nonce=nonce)
    return Episode.replay(seed, case_type, calls, nonce=nonce)


class EpisodeStore:
    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root is not None else default_store()

    def _path(self, episode_id: str) -> Path:
        if not _ID.match(episode_id):
            raise UnknownEpisodeError(f"malformed episode id {episode_id!r}")
        return self.root / f"{episode_id}.json"

    def _write(self, episode_id: str, record: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(episode_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, sort_keys=True, indent=1))
        os.replace(tmp, path)

    def _read(self, episode_id: str) -> dict[str, Any]:
        path = self._path(episode_id)
        if not path.exists():
            raise UnknownEpisodeError(f"unknown episode {episode_id!r} in {self.root}")
        record: dict[str, Any] = json.loads(path.read_text())
        return record

    def start(self, seed: int, case_type: str | None, kind: str = "simple") -> AnyEpisode:
        if kind not in KINDS:
            raise ValueError(f"unknown episode kind {kind!r}; choose from {', '.join(KINDS)}")
        nonce = len(list(self.root.glob("ep-*.json"))) if self.root.exists() else 0
        ep = new_episode(kind, seed, case_type, nonce)
        while self._path(ep.episode_id).exists():  # pragma: no cover - only on races
            nonce += 1
            ep = new_episode(kind, seed, case_type, nonce)
        record: dict[str, Any] = {"seed": seed, "case_type": case_type, "nonce": nonce, "calls": []}
        if kind != "simple":
            record["kind"] = kind
        self._write(ep.episode_id, record)
        return ep

    def load(self, episode_id: str) -> AnyEpisode:
        r = self._read(episode_id)
        calls = [(c["tool"], c["args"]) for c in r["calls"]]
        return replay_episode(r.get("kind", "simple"), r["seed"], r["case_type"], calls, r["nonce"])

    def call(self, episode_id: str, tool: str, args: Any) -> dict[str, Any]:
        """Run one tool and persist it. Refused calls are persisted too (they count
        as tool errors), except calls on a closed episode, which change nothing."""
        record = self._read(episode_id)
        ep = self.load(episode_id)
        if ep.closed:
            return ep.call(tool, args)  # raises EpisodeClosedError
        try:
            return ep.call(tool, args)
        finally:
            record["calls"].append({"tool": tool, "args": args})
            self._write(episode_id, record)

    def log(self, episode_id: str) -> list[dict[str, Any]]:
        calls: list[dict[str, Any]] = self._read(episode_id)["calls"]
        return calls


__all__ = [
    "KINDS",
    "AnyEpisode",
    "EpisodeError",
    "EpisodeStore",
    "UnknownEpisodeError",
    "default_store",
    "new_episode",
    "replay_episode",
]
