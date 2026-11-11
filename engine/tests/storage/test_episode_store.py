"""Episodes on the storage layer: same replay semantics as the CLI's file store."""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client

from scruple.agents.episode import EpisodeClosedError, EpisodeError
from scruple.agents.store import EpisodeStore, UnknownEpisodeError
from scruple.agents.store_db import DbEpisodeStore, durable_table
from scruple.clock import SteppingClock
from scruple.mcp.sim_server import build_sim_server
from scruple.storage import ImmutableHistoryError, SQLiteStorage
from tests.storage.conftest import RawStorage
from tests.test_mcp_sim import data

SCRIPT: list[tuple[str, dict[str, Any]]] = [
    ("list_evidence", {}),
    ("represent", {"evidence_ids": ["ev-01"]}),
    ("refund", {}),  # refused: an action was already taken (counts as a tool error)
    ("submit_decision", {"explanation": "contested"}),
]


def _drive(store: Any, eid: str) -> list[Any]:
    out: list[Any] = []
    for tool, args in SCRIPT:
        try:
            out.append(store.call(eid, tool, args))
        except EpisodeError as exc:
            out.append({"error": str(exc)})
    return out


def _strip(results: list[Any]) -> list[Any]:
    return [{k: v for k, v in r.items() if k != "episode_id"} for r in results]


def test_db_store_matches_the_file_store(store: RawStorage, tmp_path: Path) -> None:
    db = DbEpisodeStore.on(store, clock=SteppingClock())
    files = EpisodeStore(tmp_path / "eps")
    a = db.start(1, "fraud")
    b = files.start(1, "fraud")
    assert a.episode_id == b.episode_id  # same seed, case type and nonce
    assert _drive(db, a.episode_id) == _drive(files, b.episode_id)
    assert db.log(a.episode_id) == files.log(b.episode_id)
    assert db.load(a.episode_id).score == files.load(b.episode_id).score
    with pytest.raises(EpisodeClosedError):
        db.call(a.episode_id, "list_evidence", {})
    assert len(db.log(a.episode_id)) == len(SCRIPT)  # the closed call is not logged


def test_a_fresh_process_replays_the_log(store: RawStorage) -> None:
    first = DbEpisodeStore.on(store)
    ep = first.start(7, None, "dispute")
    first.call(ep.episode_id, "get_case", {})
    first.call(ep.episode_id, "list_evidence", {})
    second = DbEpisodeStore.on(store)  # empty cache: everything comes from storage
    replayed = second.load(ep.episode_id)
    assert replayed.episode_id == ep.episode_id and not replayed.closed
    assert second.call(ep.episode_id, "get_case", {}) == first.call(ep.episode_id, "get_case", {})
    assert [c["tool"] for c in first.log(ep.episode_id)] == [
        "get_case",
        "list_evidence",
        "get_case",
        "get_case",
    ]


def test_restarting_a_seed_gives_new_ids_per_kind(store: RawStorage) -> None:
    s = DbEpisodeStore.on(store)
    ids = [s.start(3, "fraud").episode_id for _ in range(3)]
    assert len(set(ids)) == 3
    d = s.start(3, None, "dispute")
    assert d.episode_id not in ids
    with pytest.raises(ValueError, match="unknown episode kind"):
        s.start(3, None, "weird")
    with pytest.raises(UnknownEpisodeError):
        s.call("ep-000000000000", "get_case", {})


def test_tenants_do_not_see_each_others_episodes(store: RawStorage) -> None:
    a = DbEpisodeStore.on(store, tenant_id="a")
    ep = a.start(1, None)
    with pytest.raises(UnknownEpisodeError):
        DbEpisodeStore.on(store, tenant_id="b").log(ep.episode_id)


def test_calls_are_append_only(store: RawStorage) -> None:
    s = DbEpisodeStore.on(store)
    ep = s.start(1, None)
    s.call(ep.episode_id, "get_case", {})
    for sql in ("DELETE FROM episode_calls", "UPDATE episodes SET seed = 2"):
        with pytest.raises(ImmutableHistoryError):
            store.unsafe_execute(sql)


def test_two_processes_interleave_calls_on_one_episode(tmp_path: Path) -> None:
    path = str(tmp_path / "gate.db")
    s1, s2 = SQLiteStorage(path), SQLiteStorage(path)
    a, b = DbEpisodeStore.on(s1), DbEpisodeStore.on(s2)
    ep = a.start(1, "fraud")
    a.call(ep.episode_id, "list_evidence", {})
    b.call(ep.episode_id, "represent", {"evidence_ids": ["ev-01"]})  # b replays a's call
    with pytest.raises(EpisodeError, match="already taken"):
        a.call(ep.episode_id, "refund", {})  # a sees b's action before acting
    final = b.call(ep.episode_id, "submit_decision", {"explanation": "x"})
    assert final["tool_errors"] == 1 and final["action"] == "represent"
    assert [c["seq"] for c in s1.episodes.calls("default", ep.episode_id)] == [1, 2, 3, 4]
    s1.close()
    s2.close()


def test_mcp_episodes_survive_a_server_restart(tmp_path: Path) -> None:
    path = str(tmp_path / "gate.db")

    def server() -> Any:
        return build_sim_server(durable_table(DbEpisodeStore.on(SQLiteStorage(path))))

    async def start(c: Client) -> str:
        r = await c.call_tool("start_episode", {"seed": 1, "case_type": "fraud"})
        eid = data(r)["episode_id"]
        await c.call_tool("represent", {"episode_id": eid, "evidence_ids": ["ev-01"]})
        return str(eid)

    async def finish(eid: str) -> Any:
        async with Client(server()) as c:
            r = await c.call_tool("submit_decision", {"episode_id": eid, "explanation": "ok"})
            missing = await c.call_tool("get_case", {"episode_id": "ep-000000000000"})
            return data(r), missing.is_error

    async def main() -> Any:
        async with Client(server()) as c:
            eid = await start(c)
        return await finish(eid)  # a new server process, same database

    score, missing_is_error = anyio.run(main)
    assert score["action"] == "represent" and missing_is_error


def test_a_failed_append_does_not_leave_the_cache_ahead() -> None:
    storage = SQLiteStorage(":memory:")
    s = DbEpisodeStore.on(storage)
    ep = s.start(1, None)
    real = storage.episodes.append_call

    def boom(*a: Any, **k: Any) -> None:
        raise OSError("disk full")

    storage.episodes.append_call = boom  # type: ignore[method-assign]
    with pytest.raises(OSError), contextlib.suppress(EpisodeError):
        s.call(ep.episode_id, "represent", {"evidence_ids": ["ev-01"]})
    storage.episodes.append_call = real  # type: ignore[method-assign]
    assert s.log(ep.episode_id) == []
    s.call(ep.episode_id, "represent", {"evidence_ids": ["ev-01"]})  # not "already taken"


@pytest.mark.parametrize(
    ("kind", "case_type"),
    [
        ("simple", None),
        ("dispute", None),
        ("ach", None),
        ("recon", None),
        ("aml", None),
        ("payables", None),
        ("scenario", "real-cc-15328101-gas-charged-twice"),
    ],
)
def test_every_episode_kind_round_trips_like_the_file_store(
    store: RawStorage, tmp_path: Path, kind: str, case_type: str | None
) -> None:
    """Every kind the file store knows (`KINDS`) is stored and replayed the same way."""
    db = DbEpisodeStore.on(store, clock=SteppingClock())
    files = EpisodeStore(tmp_path / "eps")
    a, b = db.start(3, case_type, kind), files.start(3, case_type, kind)
    assert type(a) is type(b) and a.episode_id == b.episode_id
    for target, eid in ((db, a.episode_id), (files, b.episode_id)):
        with contextlib.suppress(EpisodeError):
            target.call(eid, "no_such_tool", {})  # refused, and logged as a tool error
    assert db.log(a.episode_id) == files.log(b.episode_id)
    again = DbEpisodeStore.on(store).load(a.episode_id)  # a fresh process replays it
    assert type(again) is type(b)
    loaded = files.load(b.episode_id)
    assert (again.closed, again.score) == (loaded.closed, loaded.score)


@pytest.mark.parametrize(
    ("kind", "start_args", "read_tool"),
    [
        ("ach", {"seed": 2}, "ach_get_case"),
        ("recon", {"seed": 2}, "recon_get_task"),
        ("aml", {"seed": 2}, "aml_get_alert"),
        ("payables", {"seed": 2}, "payables_get_batch"),
    ],
)
def test_mcp_workflow_episodes_survive_a_server_restart(
    tmp_path: Path, kind: str, start_args: dict[str, Any], read_tool: str
) -> None:
    path = str(tmp_path / "gate.db")

    def server() -> Any:
        return build_sim_server(durable_table(DbEpisodeStore.on(SQLiteStorage(path))))

    async def main() -> Any:
        async with Client(server()) as c:
            eid = data(await c.call_tool(f"start_{kind}_episode", start_args))["episode_id"]
            first = data(await c.call_tool(read_tool, {"episode_id": eid}))
            wrong = await c.call_tool("get_case", {"episode_id": eid})
        async with Client(server()) as c:  # a new server process, same database
            again = data(await c.call_tool(read_tool, {"episode_id": eid}))
            done = data(
                await c.call_tool(
                    f"{kind}_submit_decision", {"episode_id": eid, "explanation": "x"}
                )
            )
        return first, again, wrong, done

    first, again, wrong, done = anyio.run(main)
    assert first == again
    assert wrong.is_error and f"use the {kind}_* tools" in wrong.content[0].text
    assert done["tool_calls"] == 3  # both reads and the submit, across the restart


def test_mcp_scenario_episode_is_durable(tmp_path: Path) -> None:
    from scruple.realdata.scenarios import load_real_scenarios

    path = str(tmp_path / "gate.db")
    sid = load_real_scenarios()[0].id

    def server() -> Any:
        return build_sim_server(durable_table(DbEpisodeStore.on(SQLiteStorage(path))))

    async def main() -> Any:
        async with Client(server()) as c:
            r = await c.call_tool("start_scenario_episode", {"scenario_id": sid})
            eid = data(r)["episode_id"]
            await c.call_tool("accept", {"episode_id": eid})
        async with Client(server()) as c:
            return data(
                await c.call_tool("submit_dispute", {"episode_id": eid, "explanation": "ok"})
            )

    assert anyio.run(main)["episode_id"].startswith("ep-")
