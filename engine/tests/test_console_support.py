"""Shared builders for the console tests (no tests here). Synthetic data only."""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Callable
from html.parser import HTMLParser
from pathlib import Path

from fastapi.testclient import TestClient

from scruple.agents.dispute_baselines import BASELINES, read_all, run_policy
from scruple.agents.dispute_episode import DisputeEpisode
from scruple.agents.errors import EpisodeError
from scruple.backtest import BacktestConfig, load_stripe, rule_candidate, run_backtest, to_json
from scruple.calibration import DEFAULT_PROFILE, stripe_disputes
from scruple.clock import SteppingClock
from scruple.console.app import create_console_app
from scruple.console.security import COOKIE, Csrf
from scruple.console.sources import AgentRun, ConsoleSources, policy_run, report_entries
from scruple.gate.approval import ApprovalPolicy, NamedApprover
from scruple.gate.config import GateConfig
from scruple.service import AgentGate
from scruple.sim.library import BAD_RULE, BAD_RULE_ID, GOOD_RULE, GOOD_RULE_ID
from scruple.storage import SQLiteStorage
from scruple.verifiers.record import EpisodeRecord, SelfReport
from tests.ci_support import PROV

SECRET = b"console-test-secret-0123456789abcdef"
EVAL_SEED = 918_273_645  # distinctive, so a test can prove it never reaches a page
CANARY_SEED = 827_364_519
SEEDS = range(4)
SCRIPT = "<script>alert('xss')</script>"
IMG = '<img src=x onerror="alert(1)">'
ALICE = NamedApprover(name="alice@demo.bank", roles=("model-risk",))
BOB = NamedApprover(name="bob@demo.bank", roles=("ops-lead",))
CAROL = NamedApprover(name="carol@demo.bank", roles=("model-risk",))
POLICY = ApprovalPolicy(approvers=(ALICE, BOB, CAROL), required=2, required_role="model-risk")
OPERATOR = "model-risk@demo.bank"  # GateConfig.approvers default
PROPOSER = "proposer@demo.bank"


def config(**kw: object) -> GateConfig:
    base: dict[str, object] = {
        "n_cases": 800,
        "bootstrap_resamples": 100,
        "canary_n_cases": 2_000,
        "eval_seed": EVAL_SEED,
        "canary_seed": CANARY_SEED,
    }
    return GateConfig.model_validate({**base, **kw})


def ready_gate(*, require_canary: bool = True, **kw: object) -> AgentGate:
    """Baseline active, the good rule waiting for approval, the bad rule blocked."""
    g = AgentGate(SQLiteStorage(":memory:"), SteppingClock(), config(**kw))
    g.bootstrap_baseline()
    g.propose(GOOD_RULE_ID, GOOD_RULE, "ops-team", PROV)
    recs = g.evaluate(GOOD_RULE_ID, 1)
    assert not require_canary or recs[-1].decision.outcome.value == "canary_passed"
    g.propose(BAD_RULE_ID, BAD_RULE, "ops-team", PROV)
    g.evaluate(BAD_RULE_ID, 1)
    return g


def xss_policy(ep: DisputeEpisode) -> None:
    """Reads the case, calls a tool whose name is markup, then concedes with markup."""
    got, _ = read_all(ep)
    with contextlib.suppress(EpisodeError):
        ep.call(SCRIPT)
    allowed = got["status"]["allowed_actions"]
    if "accept" in allowed:
        ep.call("accept", {"reason": IMG})
    elif "refund" in allowed:
        ep.call("refund", {"amount_minor": got["case"]["amount_minor"], "reason": IMG})


def xss_report(_ep: DisputeEpisode) -> SelfReport:
    return SelfReport(explanation=f"I won. {SCRIPT} {IMG}", confidence=0.5)


def xss_run(seeds: range = SEEDS) -> AgentRun:
    def load() -> list[EpisodeRecord]:
        return [run_policy(xss_policy, s, report=xss_report).record() for s in seeds]

    return AgentRun("xss-agent", f"markup in every free-text field {SCRIPT}", load)


def write_report(root: Path, dataset: str = "synthetic test history") -> Path:
    objs = stripe_disputes(DEFAULT_PROFILE, 300, 1)
    history = load_stripe(json.dumps(objs), label=dataset)
    cand = rule_candidate(
        {
            "name": "contest-3ds",
            "rules": [
                {
                    "description": "contest 3-D Secure disputes",
                    "conditions": [{"field": "three_ds", "op": "eq", "value": True}],
                    "action": "contest",
                    "priority": 10,
                }
            ],
            "default_action": "accept",
        }
    )
    report = run_backtest(history, cand, config=BacktestConfig(n_synthetic=500))
    out = root / "contest-3ds" / "report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(to_json(report))
    return out


def sources(
    reports: Path | None = None, extra: dict[str, AgentRun] | None = None
) -> ConsoleSources:
    agents = {
        name.replace("_", "-"): policy_run(name.replace("_", "-"), p, SEEDS)
        for name, p in BASELINES.items()
    }
    agents.update(extra or {})
    return ConsoleSources(agents, report_entries(reports), replay_seed=11, replay_cases=300)


def client(gate: AgentGate, src: ConsoleSources | None = None) -> TestClient:
    return TestClient(create_console_app(gate, src or sources(), secret=SECRET))


_TOKEN = re.compile(r'name="csrf" value="([0-9a-f]{64})"')


def csrf_token(c: TestClient, path: str = "/console/approvals") -> str:
    """The token a form on `path` carries (computed from the cookie if no form shows)."""
    r = c.get(path)
    assert r.status_code == 200, r.text
    m = _TOKEN.search(r.text)
    if m is not None:
        return m.group(1)
    sid = c.cookies.get(COOKIE)
    assert sid, "the page set no CSRF cookie"
    return Csrf(SECRET).token_for(sid)


class Structure(HTMLParser):
    """Collects what the structural checks need from one page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.inline_scripts = 0
        self._in_script = False
        self.forms: list[dict[str, object]] = []
        self.h1 = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        self.tags.append((tag, a))
        if tag == "h1":
            self.h1 += 1
        if tag == "script":
            self._in_script = True
            if not a.get("src"):
                self.inline_scripts += 1
        if tag == "form":
            self.forms.append({"method": (a.get("method") or "get").lower(), "csrf": False})
        if tag == "input" and a.get("name") == "csrf" and self.forms:
            self.forms[-1]["csrf"] = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._in_script = False


def structure(html: str) -> Structure:
    s = Structure()
    s.feed(html)
    return s


Builder = Callable[[], AgentGate]
