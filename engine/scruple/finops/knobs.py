"""Difficulty knobs for the finance-operations environments, carried in the case type.

A workflow case type is `<tokens>@<difficulty>` (see `scruple.agents.ach_episode`).
Knobs ride after a `;` as `key=value` pairs, so every existing entry point (the
episode CLI, the MCP sim server, the file and database stores, replay) carries them
without a new argument::

    fx+fee_net@0.6;n=40;breaks=8;noise=0.3;adv=0.5;h=120

- `n` (int 0-400, recon): clean statement/book pairs (default: seeded, 6-20)
- `breaks` (int 1-40, recon): breaks injected when no kinds are forced (default seeded)
- `noise` (float [0, 1], recon and aml): recon, the share of statement references the
  bank mangled; aml, background card spend added (40 x noise transfers)
- `adv` (float [0, 1], recon and aml): probability that each adversarial artefact
  candidate is planted (`scruple.finops.artefacts`)
- `h` (int 1-5000, every workflow kind): horizon, the tool calls allowed before the
  episode lapses (submitting is always allowed)

With no knobs a case is byte-identical to the one the kind generated before knobs
existed (the new randomness uses separate seeded streams). This module imports
nothing from the rest of scruple, so the episode modules can use it.
"""

from __future__ import annotations

from dataclasses import dataclass, fields

SEP = ";"
KNOB_NAMES = ("n", "breaks", "noise", "adv", "h")
KIND_KNOBS: dict[str, frozenset[str]] = {
    "recon": frozenset({"n", "breaks", "noise", "adv", "h"}),
    "aml": frozenset({"noise", "adv", "h"}),
    "ach": frozenset({"h"}),
    "payables": frozenset({"h"}),
}
_INT_RANGE = {"n": (0, 400), "breaks": (1, 40), "h": (1, 5_000)}


@dataclass(frozen=True)
class Knobs:
    n: int | None = None
    breaks: int | None = None
    noise: float = 0.0
    adv: float = 0.0
    h: int | None = None

    def is_default(self) -> bool:
        return self == Knobs()

    def as_dict(self) -> dict[str, int | float | None]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    def suffix(self) -> str:
        """Canonical `;k=v` text (empty for the default), in `KNOB_NAMES` order."""
        parts = []
        for name in KNOB_NAMES:
            v = getattr(self, name)
            if v is None or (v == 0.0 and name in ("noise", "adv")):
                continue
            parts.append(f"{name}={_fmt(v)}")
        return (SEP + SEP.join(parts)) if parts else ""


def _fmt(v: int | float) -> str:
    if isinstance(v, int):
        return str(v)
    return f"{v:.6g}"


def split_case_type(case_type: str | None) -> tuple[str | None, str]:
    """`base;k=v;...` -> (base or None, the knob text)."""
    if not case_type:
        return None, ""
    base, _, rest = case_type.partition(SEP)
    return (base or None), rest


def parse_knobs(case_type: str | None, kind: str | None = None) -> Knobs:
    """The knobs in a case type. Unknown names, bad values and knobs the kind does not
    take raise ValueError."""
    _, text = split_case_type(case_type)
    if not text:
        return Knobs()
    allowed = KIND_KNOBS.get(kind or "", frozenset(KNOB_NAMES))
    vals: dict[str, int | float] = {}
    for part in text.split(SEP):
        if not part.strip():
            continue
        key, eq, raw = part.partition("=")
        key = key.strip()
        if not eq or key not in KNOB_NAMES:
            raise ValueError(f"unknown knob {part!r}; knobs are {', '.join(KNOB_NAMES)}")
        if key not in allowed:
            raise ValueError(f"knob {key!r} does not apply to {kind} episodes")
        if key in vals:
            raise ValueError(f"knob {key!r} given twice")
        try:
            if key in _INT_RANGE:
                iv = int(raw)
                lo, hi = _INT_RANGE[key]
                if not lo <= iv <= hi:
                    raise ValueError
                vals[key] = iv
            else:
                fv = float(raw)
                if not 0.0 <= fv <= 1.0:
                    raise ValueError
                vals[key] = fv
        except ValueError:
            raise ValueError(f"bad value for knob {key!r}: {raw!r}") from None
    return Knobs(**vals)  # type: ignore[arg-type]


def with_knobs(base: str | None, knobs: Knobs) -> str | None:
    """A case type from a base (`tokens@difficulty`) and knobs; None when both empty."""
    text = (base or "") + knobs.suffix()
    return text or None


__all__ = [
    "KIND_KNOBS",
    "KNOB_NAMES",
    "SEP",
    "Knobs",
    "parse_knobs",
    "split_case_type",
    "with_knobs",
]
