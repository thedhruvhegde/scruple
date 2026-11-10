"""Real-life dispute scenarios: the scenario DSL, grounded in real CFPB complaints.

Each file in `scruple/sim/scenarios/real/` is an ordinary scenario (`scruple.sim.scenario`)
plus a `grounding` block that ties it to one public CFPB complaint narrative:

    grounding:
      complaint: cfpbcc2024-15328101          # a committed complaint id (v1 train/dev or
                                              # the v2 card-dispute sample, never v1 test)
      product: Credit card                    # must equal the CFPB field
      issue: Problem with a purchase shown on your statement
      company_response: Closed with monetary relief
      amounts_usd: [45.0, 45.0]               # dollar amounts the narrative states ({$45.00})
      consumer_says_refunded: "no"            # yes | no | partly | not stated
      merchant_response: the station confirmed in writing it processed one sale
      evidence_mentioned: [station's written confirmation]
      facts:                                  # paraphrased, stated in the narrative
        - two $45 charges, same station id, seconds apart; one fill-up
      assumptions:                            # everything the narrative does not say
        - "ASSUMPTION: network is Visa (the narrative names the issuer, not the network)"
      regime: reg_z                           # reg_z (credit card) or reg_e (debit, prepaid)
      regs: ["12 CFR 1026.13(a)(1)"]          # paragraphs of data/real/regs (checked)
      reg_note: why those paragraphs apply (our reading of the text; not legal advice)

The engine half is the merchant's side of the same dispute. The case family (who is
right under the network rules) is the narrative's account taken at face value unless
the record says otherwise, and it is always listed as an ASSUMPTION; amounts the
narrative states are used as stated; dates are masked by the CFPB, so the timeline is
an ASSUMPTION too. The cardholder's side is real; the merchant's evidence is not.

`load_real_scenarios()` loads and validates every file: the scenario half through
`parse_scenario` (strict, builds the case), the grounding half against the committed
data (`check_grounding`).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from scruple.realdata.records import DATASETS, Complaint
from scruple.realdata.regs import citation_exists, parse_citation
from scruple.realdata.splits import load_split
from scruple.realdata.v2 import CardComplaint, load_card_narratives
from scruple.sim.scenario import SCENARIO_DIR, Scenario, ScenarioError, parse_scenario

REAL_DIR = SCENARIO_DIR / "real"
COMPLAINT_ID = r"^(cfpb2015|cfpb2025|cfpbcc2024)-\d+$"
_STRICT = ConfigDict(frozen=True, extra="forbid")
_AMOUNT = re.compile(r"\{\$([0-9,]*\.?[0-9]*)\}")

Regime = Literal["reg_e", "reg_z"]
Refunded = Literal["yes", "no", "partly", "not stated"]


class Grounding(BaseModel):
    model_config = _STRICT

    complaint: str = Field(pattern=COMPLAINT_ID)
    product: str
    issue: str
    company_response: str
    amounts_usd: tuple[float, ...] = ()
    consumer_says_refunded: Refunded
    merchant_response: str = Field(min_length=3)
    evidence_mentioned: tuple[str, ...] = ()
    facts: tuple[str, ...] = Field(min_length=2)
    assumptions: tuple[str, ...] = Field(min_length=1)
    regime: Regime
    regs: tuple[str, ...] = ()
    reg_note: str = Field(min_length=20)

    @model_validator(mode="after")
    def _shape(self) -> Grounding:
        bad = [a for a in self.assumptions if not a.startswith("ASSUMPTION: ")]
        if bad:
            raise ValueError(f"every assumption starts with 'ASSUMPTION: ', got {bad}")
        for c in self.regs:
            parsed = parse_citation(c)
            if parsed is None:
                raise ValueError(f"malformed citation {c!r} (write '12 CFR 1026.13(a)(3)')")
            part = parsed[0].split(".")[0]
            if (part == "1005") != (self.regime == "reg_e"):
                raise ValueError(f"{c} is not in the {self.regime} part")
        if any(a <= 0 for a in self.amounts_usd):
            raise ValueError("amounts_usd must be positive")
        return self


class RealScenario(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    grounding: Grounding
    scenario: Scenario

    @property
    def id(self) -> str:
        return self.scenario.id


def _strict_load(text: str, name: str) -> dict[str, object]:
    from scruple.sim.scenario import _StrictLoader

    try:
        data = yaml.load(text, Loader=_StrictLoader)  # noqa: S506 - SafeLoader subclass
    except yaml.YAMLError as exc:
        raise ScenarioError(f"{name}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ScenarioError(f"{name}: a scenario must be a mapping at top level")
    return data


def parse_real_scenario(text: str, name: str = "<real scenario>") -> RealScenario:
    """Parse one grounded scenario. Raises `ScenarioError` naming the file and field."""
    data = _strict_load(text, name)
    if "grounding" not in data:
        raise ScenarioError(f"{name}: a real scenario needs a grounding block")
    raw = data.pop("grounding")
    try:
        g = Grounding.model_validate(raw)
    except ValidationError as exc:
        msgs = "; ".join(
            f"grounding.{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise ScenarioError(f"{name}: {msgs}") from exc
    sc = parse_scenario(yaml.safe_dump(data, sort_keys=False), name)
    if sc.expected.resolution is None:
        raise ScenarioError(f"{name}: expected.resolution is required for a real scenario")
    if not sc.id.startswith("real-"):
        raise ScenarioError(f"{name}: real scenario ids start with 'real-'")
    return RealScenario(path=name, grounding=g, scenario=sc)


def load_real_scenario(path: str | Path) -> RealScenario:
    p = Path(path)
    return parse_real_scenario(p.read_text(encoding="utf-8"), str(p))


@cache
def _load_dir(directory: str) -> tuple[RealScenario, ...]:
    out = [load_real_scenario(p) for p in sorted(Path(directory).glob("*.yaml"))]
    ids = [s.id for s in out]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise ScenarioError(f"{directory}: duplicate scenario ids {dupes}")
    return tuple(sorted(out, key=lambda s: s.id))


def load_real_scenarios(directory: str | Path = REAL_DIR) -> list[RealScenario]:
    """Every grounded scenario in `directory`, sorted by id."""
    return list(_load_dir(str(directory)))


def find_real_scenario(scenario_id: str, directory: str | Path = REAL_DIR) -> RealScenario:
    for s in load_real_scenarios(directory):
        if s.id == scenario_id:
            return s
    raise KeyError(f"unknown real scenario {scenario_id!r}")


# --- the complaints scenarios may cite --------------------------------------------------
class ComplaintRef(BaseModel):
    """The CFPB fields a grounding block is checked against."""

    model_config = _STRICT

    id: str
    product: str
    issue: str
    sub_issue: str
    company: str
    company_response: str
    narrative: str


@cache
def citable_complaints() -> dict[str, ComplaintRef]:
    """Committed complaints a scenario may cite: v1 train and dev (never the sealed
    test split) and the v2 card-dispute narrative sample."""
    rows: list[ComplaintRef] = []
    for ds in DATASETS:
        for split in ("train", "dev"):
            rows += [_ref(c) for c in load_split(ds, split)]
    rows += [_ref(c) for c in load_card_narratives()]
    return {r.id: r for r in rows}


def _ref(c: Complaint | CardComplaint) -> ComplaintRef:
    return ComplaintRef(
        id=c.id,
        product=c.product,
        issue=c.issue,
        sub_issue=c.sub_issue,
        company=c.company,
        company_response=c.company_response,
        narrative=c.narrative,
    )


def stated_amounts(narrative: str) -> set[float]:
    """Dollar amounts the CFPB kept in a narrative (it writes them as {$45.00})."""
    out: set[float] = set()
    for m in _AMOUNT.finditer(narrative):
        raw = m.group(1).replace(",", "")
        if raw and raw != "." and float(raw) > 0:  # "{$.00}": a masked amount
            out.add(round(float(raw), 2))
    return out


def check_grounding(
    rs: RealScenario, complaints: dict[str, ComplaintRef] | None = None
) -> list[str]:
    """Problems with a grounding block against the committed data (empty when fine)."""
    pool = citable_complaints() if complaints is None else complaints
    g = rs.grounding
    c = pool.get(g.complaint)
    if c is None:
        return [f"{rs.id}: complaint {g.complaint} is not a citable committed complaint"]
    problems = []
    for field in ("product", "issue", "company_response"):
        if getattr(g, field) != getattr(c, field):
            problems.append(f"{rs.id}: {field} {getattr(g, field)!r} != CFPB {getattr(c, field)!r}")
    have = stated_amounts(c.narrative)
    for a in g.amounts_usd:
        if round(a, 2) not in have:
            problems.append(f"{rs.id}: ${a:.2f} is not stated in {g.complaint}")
    for cite in g.regs:
        if not citation_exists(cite):
            problems.append(f"{rs.id}: {cite} is not a paragraph of data/real/regs")
    want_regime: Regime = "reg_z" if c.product == "Credit card" else "reg_e"
    if g.regime != want_regime:
        problems.append(f"{rs.id}: regime {g.regime} but the product is {c.product}")
    return problems


def check_all(scenarios: Iterable[RealScenario] | None = None) -> list[str]:
    out: list[str] = []
    for rs in scenarios if scenarios is not None else load_real_scenarios():
        out += check_grounding(rs)
    return out


__all__ = [
    "REAL_DIR",
    "ComplaintRef",
    "Grounding",
    "RealScenario",
    "check_all",
    "check_grounding",
    "citable_complaints",
    "find_real_scenario",
    "load_real_scenario",
    "load_real_scenarios",
    "parse_real_scenario",
    "stated_amounts",
]
