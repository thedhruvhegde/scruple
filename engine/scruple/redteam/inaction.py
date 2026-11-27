"""Inaction policies against the honest one, per environment and verifier version (R6).

    uv run python -m scruple.redteam.inaction [--n 500] [--seed0 20000]

Every policy reports *truthfully*: its structured claims are computed from the engine
(the honest report of its environment), so the comparison prices the actions, not the
self-report. Policies:

- `honest`: the environment's honest reference policy;
- `do_nothing`: reads the task, takes no action, submits;
- `never_decide`: aml escalates every alert (with indicators that hold); recon
  escalates every line; payables holds every invoice; ach accepts every return;
- `buy_nothing` (commerce): reads the task and the offers, buys nothing;
- `always_accept` (dispute): concedes every chargeback.

Rewards are means with normal 95% intervals over held-out seeds (`seed0..seed0+n-1`,
curriculum case types for the finance-ops envs).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
from collections.abc import Callable
from datetime import date
from typing import Any

from scruple.agents.errors import EpisodeError

Play = Callable[[Any], None]


def mean_ci(xs: list[float]) -> tuple[float, float, float]:
    n = len(xs)
    m = sum(xs) / n
    v = sum((x - m) ** 2 for x in xs) / (n - 1) if n > 1 else 0.0
    h = 1.96 * math.sqrt(v / n)
    return round(m, 4), round(m - h, 4), round(m + h, 4)


def _finops_report(env: str, ep: Any) -> Any:
    from scruple.agents import ach_baselines, aml_baselines, payables_baselines
    from scruple.finops.envs import get_env

    if env == "recon":
        return get_env("recon").policies()["honest"].report(ep)
    return {"aml": aml_baselines, "ach": ach_baselines, "payables": payables_baselines}[
        env
    ].honest_report(ep)


def _recon_escalate_all(ep: Any) -> None:
    st = ep.call("get_statement")["lines"]
    bk = ep.call("get_ledger")["lines"]
    for i in range(0, len(st), 20):
        ep.call("escalate", {"statement_line_ids": [x["line_id"] for x in st[i : i + 20]],
                             "reason": "review"})  # fmt: skip
    for i in range(0, len(bk), 20):
        ep.call("escalate", {"ledger_line_ids": [x["line_id"] for x in bk[i : i + 20]],
                             "reason": "review"})  # fmt: skip


def _aml_escalate_all(ep: Any) -> None:
    from scruple.agents.aml_baselines import review
    from scruple.sim.aml.indicators import INDICATORS, holds

    alert, subject, accounts, linked = review(ep)
    on = date.fromisoformat(alert["raised_on"])
    ind = [i.value for i in INDICATORS if holds(i, subject, accounts, linked, on)]
    ep.call("escalate", {"indicators": ind or ["fan_in"],
                         "txn_ids": [t.txn_id for t in linked], "reason": "L2"})  # fmt: skip


def _first_read(env: str) -> str:
    return {"recon": "get_task", "aml": "get_alert", "ach": "get_case", "payables": "get_batch"}[
        env
    ]


def finops_plays(env: str) -> dict[str, Play]:
    from scruple.agents import ach_baselines, payables_baselines
    from scruple.finops.envs import get_env

    honest = get_env(env).policies()["honest"]
    out: dict[str, Play] = {
        "honest": honest.act,
        "do_nothing": lambda ep: ep.call(_first_read(env)),
    }
    if env == "recon":
        out["never_decide"] = _recon_escalate_all
    elif env == "aml":
        out["never_decide"] = _aml_escalate_all
    elif env == "payables":
        out["never_decide"] = payables_baselines.BASELINES["hold_everything"]
    elif env == "ach":

        def accept(ep: Any) -> None:
            ep.call("get_case")
            ep.call("accept", {"reason": "valid return"})

        out["never_decide"] = accept
        _ = ach_baselines
    return out


def run_finops(env: str, verifier: str, play: Play, seeds: range) -> list[float]:
    from scruple.finops.difficulty import curriculum
    from scruple.finops.envs import get_env

    e = get_env(env)
    ver = e.verifier(verifier)
    out = []
    n = len(seeds)
    for i, s in enumerate(seeds):
        ep = e.new_episode(s, curriculum(env, round(i / max(1, n - 1), 4)))
        with contextlib.suppress(EpisodeError):
            play(ep)
        if not ep.closed:
            ep.call("submit_decision", _finops_report(env, ep).model_dump(mode="json"))
        out.append(ver.score(ep.record()).reward)
    return out


def run_commerce(verifier: str, play: Play, seeds: range) -> list[float]:
    from scruple.agents.commerce_baselines import wallet_report
    from scruple.agents.commerce_episode import CommerceEpisode
    from scruple.finops.verifiers import get_verifier

    ver = get_verifier(verifier)
    out = []
    for s in seeds:
        ep = CommerceEpisode(s)
        with contextlib.suppress(EpisodeError):
            play(ep)
        if not ep.closed:
            ep.submit(wallet_report(ep))
        out.append(ver.score(ep.record()).reward)
    return out


def run_dispute(verifier: str, play: Play, seeds: range) -> list[float]:
    from scruple.agents.dispute_baselines import honest_report
    from scruple.agents.dispute_episode import DisputeEpisode
    from scruple.finops.verifiers import get_verifier

    ver = get_verifier(verifier)
    out = []
    for s in seeds:
        ep = DisputeEpisode(s)
        with contextlib.suppress(EpisodeError):
            play(ep)
        if not ep.closed:
            ep.submit(honest_report(ep))
        out.append(ver.score(ep.record()).reward)
    return out


def table(n: int = 500, seed0: int = 20_000) -> list[dict[str, Any]]:
    from scruple.agents.commerce_baselines import careful_policy
    from scruple.agents.dispute_baselines import always_accept, evidence_policy

    seeds = range(seed0, seed0 + n)
    rows: list[dict[str, Any]] = []
    for env in ("recon", "aml", "ach", "payables"):
        for name, play in finops_plays(env).items():
            row: dict[str, Any] = {"env": env, "policy": name}
            for v in (f"{env}@2", f"{env}@3"):
                row[v] = mean_ci(run_finops(env, v, play, seeds))
            rows.append(row)

    def buy_nothing(ep: Any) -> None:
        ep.call("get_task")
        ep.call("list_offers")

    for name, play in (("honest", careful_policy), ("buy_nothing", buy_nothing)):
        rows.append({"env": "commerce", "policy": name,
                     **{v: mean_ci(run_commerce(v, play, seeds)) for v in
                        ("commerce@2", "commerce@3")}})  # fmt: skip

    def nothing(ep: Any) -> None:
        ep.call("get_case")

    for name, play in (("honest", evidence_policy), ("do_nothing", nothing),
                       ("always_accept", always_accept)):  # fmt: skip
        rows.append({"env": "dispute", "policy": name,
                     **{v: mean_ci(run_dispute(v, play, seeds)) for v in
                        ("dispute@2", "dispute@3")}})  # fmt: skip
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=500)
    ap.add_argument("--seed0", type=int, default=20_000)
    a = ap.parse_args()
    for row in table(a.n, a.seed0):
        print(json.dumps(row), flush=True)


if __name__ == "__main__":  # pragma: no cover
    main()
