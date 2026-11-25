"""Difficulty: a nominal score from the knobs, a measured score from a probe panel,
and the one-parameter curriculum that maps a target difficulty to knobs.

Nominal difficulty `nominal(env, case_type)` in [0, 1] is the mean of the knobs'
normalised settings (each in [0, 1]):

- recon: base difficulty, (n - 6) / 54, (breaks - 1) / 11, noise / 0.5, adv, and the
  horizon tightness `1 - h / 400` when `h` is set (each clipped to [0, 1]);
- aml: base difficulty, noise, adv, horizon tightness `1 - h / 60`;
- ach, payables: base difficulty, horizon tightness `1 - h / 40`.

Knobs left at their seeded default count at the base difficulty (the seeded counts
scale with it). `curriculum(env, d)` is the inverse along the diagonal: every knob at
the setting whose normalised value is `d`, so `nominal(env, curriculum(env, d)) == d`
up to rounding of the integer knobs.

Measured difficulty `measured(env, seed, case_type)` is `1 - mean reward` of the
environment's probe panel (`PANELS`: imperfect, honest policies; the expert honest
policy is excluded because it is near-perfect by construction) on that exact task
under the env's default verifier. It is deterministic and costs a few episodes.
"""

from __future__ import annotations

from collections.abc import Sequence

from scruple.agents.ach_episode import parse_case_type
from scruple.finops.knobs import Knobs, parse_knobs, with_knobs

PANELS: dict[str, tuple[str, ...]] = {
    "recon": ("novice", "credulous", "honest_exact_refs"),
    "aml": ("novice", "credulous", "defensive"),
    "ach": ("always_write_off", "two_day_dishonor"),
    "payables": ("match_only", "pay_everything", "trust_the_email"),
}
H_REF = {"recon": 400, "aml": 60, "ach": 40, "payables": 40}


def _clip(x: float) -> float:
    return min(1.0, max(0.0, x))


def nominal(env: str, case_type: str | None) -> float:
    _, base = parse_case_type(case_type)
    k = parse_knobs(case_type, env)
    parts: list[float] = [base]
    if env == "recon":
        parts += [
            base if k.n is None else _clip((k.n - 6) / 54),
            base if k.breaks is None else _clip((k.breaks - 1) / 11),
            _clip(k.noise / 0.5),
            k.adv,
        ]
    elif env == "aml":
        parts += [k.noise, k.adv]
    if k.h is not None:
        parts.append(_clip(1 - k.h / H_REF[env]))
    return round(sum(parts) / len(parts), 6)


def curriculum(env: str, d: float, *, horizon: bool = False) -> str:
    """The case type at nominal difficulty `d` (seeded families / break kinds)."""
    if not 0.0 <= d <= 1.0:
        raise ValueError("difficulty must be in [0, 1]")
    base = f"@{d:.4g}"
    h = round(H_REF[env] * (1 - d)) if horizon else None
    h = None if h is None else max(1, h)
    match env:
        case "recon":
            k = Knobs(n=6 + round(54 * d), breaks=1 + round(11 * d), noise=0.5 * d, adv=d, h=h)
        case "aml":
            k = Knobs(noise=d, adv=d, h=h)
        case _:
            k = Knobs(h=h)
    out = with_knobs(base, k)
    assert out is not None
    return out


def measured(
    env: str, seed: int, case_type: str | None, *, panel: Sequence[str] | None = None
) -> tuple[float, dict[str, float]]:
    from scruple.finops.envs import get_env
    from scruple.finops.policies import run_policy

    e = get_env(env)
    pols = e.policies()
    ver = e.verifier()
    rewards: dict[str, float] = {}
    for name in panel or PANELS[env]:
        ep = run_policy(e.episode_cls, pols[name], seed, case_type)
        rewards[name] = round(ver.score(ep.record()).reward, 6)
    return round(1 - sum(rewards.values()) / len(rewards), 6), rewards


__all__ = ["H_REF", "PANELS", "curriculum", "measured", "nominal"]
