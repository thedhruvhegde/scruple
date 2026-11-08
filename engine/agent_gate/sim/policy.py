"""A policy is the ordered set of rules an agent applies, plus a default action."""

from __future__ import annotations

from dataclasses import dataclass

from agent_gate.domain import Action, RuleSpec


@dataclass(frozen=True)
class PolicyRule:
    rule_id: str
    ref: str  # e.g. "inr-contest@v2"
    spec: RuleSpec


@dataclass(frozen=True)
class Policy:
    rules: tuple[PolicyRule, ...]
    default_action: Action = Action.ACCEPT

    def __post_init__(self) -> None:
        ordered = tuple(sorted(self.rules, key=lambda r: (r.spec.priority, r.rule_id)))
        object.__setattr__(self, "rules", ordered)

    def decide(self, view: dict[str, bool | int | str]) -> tuple[Action, str]:
        for rule in self.rules:
            if rule.spec.matches(view):
                return rule.spec.action, rule.ref
        return self.default_action, "default"

    def with_rule(self, rule: PolicyRule) -> Policy:
        """This policy with `rule` added, replacing any rule with the same rule_id."""
        kept = tuple(r for r in self.rules if r.rule_id != rule.rule_id)
        return Policy(rules=(*kept, rule), default_action=self.default_action)

    def refs(self) -> list[str]:
        return [r.ref for r in self.rules]
