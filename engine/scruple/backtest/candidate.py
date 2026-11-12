"""Candidates to backtest: a learned rule (or rule set) in JSON, or an agent module.

Rule JSON, any of:

- one `RuleSpec` (`{"description", "conditions", "action", "priority"}`),
- the `scruple propose` body (`{"rule_id", "spec", ...}`),
- `{"name": ..., "rules": [RuleSpec | {"rule_id", "spec"}], "default_action": ...}`.

`default_action` is `"historical"` unless given: where no rule matches, the case keeps
what actually happened, so a single rule is judged only where it fires. Give
`"accept"`, `"contest"` or `"escalate"` to backtest a whole policy.

Agent module: `path/to/agent.py`, `package.module` or `package.module:attr`. The
attribute (default `decide`) is called with a `DecisionView` and returns an `Action`,
its value (`"accept"`, `"contest"`, `"escalate"`), `None` to keep the historical
action, or `(action, ref)`. An object with a `decide` method works too. Loading a module
runs its code: only backtest modules you trust.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from scruple.backtest.view import DecisionView
from scruple.domain import Action, RuleSpec
from scruple.sim.policy import Policy, PolicyRule

HISTORICAL = "historical"
# How a decision was obtained. Rules and Python agents always give "ok"; an LLM agent's
# reply can also be malformed, fail or time out (scruple.backtest.llm). Only "ok"
# carries a decision; the backtest applies `BacktestConfig.on_agent_error` to the rest.
DecisionOutcome = Literal["ok", "malformed", "failed", "timeout"]


class CandidateDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action: Action | None  # None: keep the historical action
    ref: str  # which rule (or agent) decided
    outcome: DecisionOutcome = "ok"
    detail: str | None = None  # why there is no decision (outcome other than "ok")


class Candidate(Protocol):
    name: str

    def decide(self, view: DecisionView) -> CandidateDecision: ...


class RuleEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    rule_id: str = Field(min_length=1, max_length=128)
    spec: RuleSpec


class CandidateFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str | None = Field(default=None, max_length=200)
    rules: tuple[RuleSpec | RuleEntry, ...] = Field(min_length=1, max_length=200)
    default_action: Action | Literal["historical"] = "historical"


class RuleCandidate:
    def __init__(
        self, rules: tuple[RuleEntry, ...], default: Action | None = None, name: str | None = None
    ) -> None:
        if not rules:
            raise ValueError("a rule candidate needs at least one rule")
        self.policy = Policy(
            rules=tuple(PolicyRule(r.rule_id, f"{r.rule_id}@candidate", r.spec) for r in rules)
        )
        self.default = default
        self.name = name or ", ".join(r.rule_id for r in self.policy.rules)

    def decide(self, view: DecisionView) -> CandidateDecision:
        fields = view.rule_view()
        for rule in self.policy.rules:
            if rule.spec.matches(fields):
                return CandidateDecision(action=rule.spec.action, ref=rule.ref)
        if self.default is None:
            return CandidateDecision(action=None, ref=HISTORICAL)
        return CandidateDecision(action=self.default, ref="default")


def rule_candidate(doc: Any, name: str | None = None) -> RuleCandidate:
    if not isinstance(doc, dict):
        raise ValueError("a rule candidate file must hold a JSON object")
    if "rules" in doc:
        f = CandidateFile.model_validate(doc)
        entries = tuple(
            r if isinstance(r, RuleEntry) else RuleEntry(rule_id=f"rule-{i + 1}", spec=r)
            for i, r in enumerate(f.rules)
        )
        default = None if f.default_action == HISTORICAL else Action(f.default_action)
        return RuleCandidate(entries, default, f.name or name)
    if "spec" in doc:
        rid = doc.get("rule_id")
        if not isinstance(rid, str) or not rid:
            raise ValueError("a proposal body needs a rule_id")
        return RuleCandidate((RuleEntry(rule_id=rid, spec=RuleSpec.model_validate(doc["spec"])),))
    spec = RuleSpec.model_validate(doc)
    return RuleCandidate((RuleEntry(rule_id=name or "candidate", spec=spec),), name=name)


def _normalise(out: Any, ref: str) -> CandidateDecision:
    if isinstance(out, CandidateDecision):
        return out
    if isinstance(out, tuple) and len(out) == 2:
        action, ref = out
        return CandidateDecision(action=None if action is None else Action(action), ref=str(ref))
    if out is None:
        return CandidateDecision(action=None, ref=HISTORICAL)
    try:
        return CandidateDecision(action=Action(out), ref=ref)
    except ValueError:
        raise ValueError(f"agent returned {out!r}; expected an Action, its value or None") from None


class ModuleCandidate:
    def __init__(self, fn: Callable[[DecisionView], Any], name: str) -> None:
        self.fn = fn
        self.name = name

    def decide(self, view: DecisionView) -> CandidateDecision:
        return _normalise(self.fn(view), self.name)


def _module(spec: str) -> Any:
    if spec.endswith(".py"):
        path = Path(spec)
        if not path.is_file():
            raise FileNotFoundError(spec)
        loader = importlib.util.spec_from_file_location(f"scruple_candidate_{path.stem}", path)
        if loader is None or loader.loader is None:
            raise ValueError(f"cannot load {spec}")
        mod = importlib.util.module_from_spec(loader)
        loader.loader.exec_module(mod)
        return mod
    return importlib.import_module(spec)


def load_candidate(spec: str) -> Candidate:
    """A `.json` rule file, a `.py` file, or `module[:attr]`."""
    if spec.endswith(".json"):
        path = Path(spec)
        try:
            return rule_candidate(json.loads(path.read_text(encoding="utf-8")), path.stem)
        except ValidationError as e:
            raise ValueError(f"{spec}: not a rule candidate ({e.error_count()} errors)") from None
    mod_spec, _, attr = spec.partition(":")
    if spec.endswith(".py"):
        mod_spec, attr = spec, ""
    obj = getattr(_module(mod_spec), attr or "decide", None)
    if obj is None:
        raise ValueError(f"{spec}: no `{attr or 'decide'}` to call")
    fn = obj.decide if hasattr(obj, "decide") else obj
    if not callable(fn):
        raise ValueError(f"{spec}: `{attr or 'decide'}` is not callable")
    return ModuleCandidate(fn, Path(mod_spec).stem if spec.endswith(".py") else spec)
