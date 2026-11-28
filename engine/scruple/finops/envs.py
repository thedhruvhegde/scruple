"""The finance-operations environment registry.

One `FinopsEnv` per environment: the episode class, its tools (Anthropic and OpenAI
schemas), the verifiers that grade it (versioned; `scruple.finops.verifiers`), the
scripted policies, the knobs it takes and how a nominal difficulty maps to a case
type. `ENVS` is the registry `scruple tasks`, the HTTP grader and the experiments use.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cache
from typing import Any

from scruple.agents.ach_episode import AchEpisode
from scruple.agents.ach_tools import ACH_TOOLS
from scruple.agents.aml_episode import AmlEpisode
from scruple.agents.aml_tools import AML_TOOLS
from scruple.agents.dispute_tools import DisputeToolSpec
from scruple.agents.payables_episode import PayablesEpisode
from scruple.agents.payables_tools import PAYABLES_TOOLS
from scruple.agents.recon_episode import ReconEpisode
from scruple.agents.recon_tools import RECON_TOOLS
from scruple.finops.knobs import KIND_KNOBS
from scruple.finops.policies import (
    Policy,
    aml_policies,
    kind_policies,
    recon_policies,
)
from scruple.finops.verifiers import VERIFIERS, Verifier, default_verifier_id

SUITE_VERSION = "1.0.0"


def anthropic_schema(specs: tuple[DisputeToolSpec, ...]) -> list[dict[str, Any]]:
    return [
        {"name": t.name, "description": t.description, "input_schema": t.json_schema()}
        for t in specs
    ]


def openai_schema(specs: tuple[DisputeToolSpec, ...]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.json_schema(),
            },
        }
        for t in specs
    ]


@dataclass(frozen=True)
class FinopsEnv:
    name: str
    title: str
    episode_cls: Callable[..., Any]
    tools: tuple[DisputeToolSpec, ...]
    policy_factory: Callable[[], dict[str, Policy]]
    parity: bool  # brought to dispute parity (knobs, artefacts, hacker family, oracle)
    families: tuple[str, ...] = field(default=())

    @property
    def default_verifier(self) -> str:
        """The verifier id every surface grades with (`scruple.finops.verifiers.
        default_verifier`, round 7: one source of truth)."""
        return default_verifier_id(self.name)

    @property
    def knobs(self) -> frozenset[str]:
        return KIND_KNOBS[self.name]

    @property
    def task(self) -> str:
        return str(getattr(self.episode_cls, "task", ""))

    def policies(self) -> dict[str, Policy]:
        return _policies(self.name)

    def verifier(self, vid: str | None = None) -> Verifier:
        v = VERIFIERS[vid or self.default_verifier]
        if v.env != self.name:
            raise ValueError(f"verifier {v.id} grades {v.env}, not {self.name}")
        return v

    def verifiers(self) -> tuple[Verifier, ...]:
        return tuple(v for v in VERIFIERS.values() if v.env == self.name)

    def anthropic_tools(self) -> list[dict[str, Any]]:
        return anthropic_schema(self.tools)

    def openai_tools(self) -> list[dict[str, Any]]:
        return openai_schema(self.tools)

    def new_episode(
        self,
        seed: int,
        case_type: str | None,
        *,
        reference: bool = False,
        salt: str | bytes | None = None,
    ) -> Any:
        """`salt` keys the episode: the case is generated from HMAC(salt, seed)."""
        return self.episode_cls(seed, case_type, reference=reference, salt=salt)


def _families(enum_path: str) -> tuple[str, ...]:
    mod, _, name = enum_path.rpartition(".")
    m = __import__(mod, fromlist=[name])
    return tuple(x.value for x in getattr(m, name))


ENVS: dict[str, FinopsEnv] = {
    "recon": FinopsEnv(
        "recon",
        "Bank reconciliation / month-end close",
        ReconEpisode,
        RECON_TOOLS,
        recon_policies,
        parity=True,
        families=_families("scruple.sim.recon.models.BreakKind"),
    ),
    "aml": FinopsEnv(
        "aml",
        "AML transaction-monitoring alert investigation",
        AmlEpisode,
        AML_TOOLS,
        aml_policies,
        parity=True,
        families=_families("scruple.sim.aml.models.AmlFamily"),
    ),
    "ach": FinopsEnv(
        "ach",
        "ACH returns (originator side)",
        AchEpisode,
        ACH_TOOLS,
        lambda: kind_policies("ach", "retry_everything"),
        parity=False,
        families=_families("scruple.sim.ach.models.AchFamily"),
    ),
    "payables": FinopsEnv(
        "payables",
        "Accounts payable pay run",
        PayablesEpisode,
        PAYABLES_TOOLS,
        lambda: kind_policies("payables", "trust_the_email"),
        parity=False,
    ),
}


@cache
def _policies(name: str) -> dict[str, Policy]:
    return ENVS[name].policy_factory()


def get_env(name: str) -> FinopsEnv:
    try:
        return ENVS[name]
    except KeyError:
        raise ValueError(f"unknown environment {name!r}; choose from {', '.join(ENVS)}") from None


__all__ = ["ENVS", "SUITE_VERSION", "FinopsEnv", "anthropic_schema", "get_env", "openai_schema"]
