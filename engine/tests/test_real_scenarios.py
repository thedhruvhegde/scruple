"""Real-life scenarios: every file loads, validates and is grounded in a real complaint."""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st

from scruple.realdata import scenarios as rsc
from scruple.realdata.records import DATASETS
from scruple.realdata.regs import citation_exists
from scruple.realdata.splits import load_split
from scruple.sim.scenario import SCENARIO_DIR, ScenarioError, load_scenarios, run_scenario

SCENARIOS = rsc.load_real_scenarios()
IDS = [s.id for s in SCENARIOS]
FILES = sorted(rsc.REAL_DIR.glob("*.yaml"))


def test_there_are_at_least_sixty() -> None:
    assert len(SCENARIOS) >= 60
    assert len(FILES) == len(SCENARIOS)


def test_ids_are_unique_and_match_file_names() -> None:
    assert len(set(IDS)) == len(IDS)
    assert sorted(p.stem for p in FILES) == sorted(IDS)


def test_complaints_are_distinct() -> None:
    cited = [s.grounding.complaint for s in SCENARIOS]
    assert len(set(cited)) == len(cited)


def test_the_synthetic_suite_is_untouched() -> None:
    # the real files live in a subdirectory, so the synthetic loader never sees them
    assert len(load_scenarios(SCENARIO_DIR)) == len(list(SCENARIO_DIR.glob("*.yaml")))
    assert all(not s.id.startswith("real-") for s in load_scenarios(SCENARIO_DIR))


@pytest.mark.parametrize("rs", SCENARIOS, ids=IDS)
def test_grounding_resolves_to_committed_data(rs: rsc.RealScenario) -> None:
    assert rsc.check_grounding(rs) == []


@pytest.mark.parametrize("rs", SCENARIOS, ids=IDS)
def test_answer_key_passes(rs: rsc.RealScenario) -> None:
    run = run_scenario(rs.scenario)
    assert run.passed, run.problems
    assert run.outcome is not None and run.outcome.correct


@pytest.mark.parametrize("rs", SCENARIOS, ids=IDS)
def test_scenario_shape(rs: rsc.RealScenario) -> None:
    g, sc = rs.grounding, rs.scenario
    assert sc.id.startswith("real-")
    assert g.complaint.split("-")[1] in sc.id  # the complaint id is in the scenario id
    assert sc.expected.resolution is not None
    assert all(a.startswith("ASSUMPTION: ") for a in g.assumptions)
    # the family, the network and the masked dates are always declared as assumptions
    text = " ".join(g.assumptions)
    assert sc.case.family.value in text
    assert "dates" in text
    assert all(citation_exists(c) for c in g.regs)
    # the network rules behind the answer are cited
    assert any(s.startswith("S") for s in sc.sources)
    assert re.search(r"\bS\d+\b|A-\d+", sc.expected.why)


@pytest.mark.parametrize("rs", SCENARIOS, ids=IDS)
def test_case_amount_comes_from_the_narrative_or_is_an_assumption(rs: rsc.RealScenario) -> None:
    amount = rs.scenario.case.amount_minor / 100
    stated = {round(a, 2) for a in rs.grounding.amounts_usd}
    if round(amount, 2) not in stated:
        assert any("$" in a or "amount" in a or "case is" in a for a in rs.grounding.assumptions)


def test_no_scenario_cites_the_sealed_test_split() -> None:
    test_ids = {c.id for ds in DATASETS for c in load_split(ds, "test", unseal=True)}
    assert not test_ids & {s.grounding.complaint for s in SCENARIOS}


def test_cited_narratives_are_short_paraphrases_not_copies() -> None:
    pool = rsc.citable_complaints()
    for rs in SCENARIOS:
        narrative = pool[rs.grounding.complaint].narrative.lower()
        for fact in rs.grounding.facts:
            assert len(fact) <= 200, (rs.id, fact)
        # no 12-word run of the narrative is pasted into the facts
        words = narrative.split()
        grams = {" ".join(words[i : i + 12]) for i in range(max(0, len(words) - 11))}
        joined = " ".join(f.lower() for f in rs.grounding.facts)
        assert not any(g in joined for g in grams), rs.id


def test_both_regimes_and_all_actions_are_covered() -> None:
    regimes = {s.grounding.regime for s in SCENARIOS}
    actions = {s.scenario.expected.action for s in SCENARIOS}
    sources = {s.grounding.complaint.split("-")[0] for s in SCENARIOS}
    assert regimes == {"reg_e", "reg_z"}
    assert {"accept", "represent", "wait"} <= actions
    assert sources == {"cfpb2015", "cfpb2025", "cfpbcc2024"}


def test_loading_is_deterministic() -> None:
    again = rsc.load_real_scenarios(Path(str(rsc.REAL_DIR)))
    assert [s.model_dump_json() for s in again] == [s.model_dump_json() for s in SCENARIOS]


# --- refusals -----------------------------------------------------------------------------
BASE = FILES[0].read_text(encoding="utf-8")


def _mutate(edit: dict[str, object], where: str | None = None) -> str:
    data = yaml.safe_load(BASE)
    target = data if where is None else data[where]
    target.update(edit)
    return yaml.safe_dump(data, sort_keys=False)


@pytest.mark.parametrize(
    ("edit", "where", "needle"),
    [
        ({"complaint": "cfpb2015-x"}, "grounding", "complaint"),
        ({"assumptions": ["the network is visa"]}, "grounding", "ASSUMPTION"),
        ({"regs": ["CFR 1026.13"]}, "grounding", "malformed citation"),
        ({"regs": ["12 CFR 1005.11(a)"]}, "grounding", "not in the reg_z part"),
        ({"facts": ["only one"]}, "grounding", "facts"),
        ({"surprise": 1}, "grounding", "surprise"),
        ({"consumer_says_refunded": "maybe"}, "grounding", "consumer_says_refunded"),
        ({"amounts_usd": [-1.0]}, "grounding", "positive"),
    ],
)
def test_bad_grounding_is_refused(edit: dict[str, object], where: str, needle: str) -> None:
    rs = rsc.parse_real_scenario(BASE)
    data = yaml.safe_load(BASE)
    if rs.grounding.regime == "reg_e" and needle.startswith("not in"):
        edit = {"regs": ["12 CFR 1026.13(a)"]}
        needle = "not in the reg_e part"
    data[where].update(edit)
    with pytest.raises(ScenarioError, match=needle):
        rsc.parse_real_scenario(yaml.safe_dump(data, sort_keys=False), "bad.yaml")


def test_missing_grounding_or_resolution_or_prefix_is_refused() -> None:
    data = yaml.safe_load(BASE)
    no_g = {k: v for k, v in data.items() if k != "grounding"}
    with pytest.raises(ScenarioError, match="grounding block"):
        rsc.parse_real_scenario(yaml.safe_dump(no_g), "x.yaml")
    no_res = yaml.safe_load(BASE)
    del no_res["expected"]["resolution"]
    with pytest.raises(ScenarioError, match="resolution is required"):
        rsc.parse_real_scenario(yaml.safe_dump(no_res), "x.yaml")
    bad_id = yaml.safe_load(BASE)
    bad_id["id"] = "fake-" + bad_id["id"]
    with pytest.raises(ScenarioError, match="start with 'real-'"):
        rsc.parse_real_scenario(yaml.safe_dump(bad_id), "x.yaml")


def test_duplicate_keys_and_bad_cases_are_refused() -> None:
    with pytest.raises(ScenarioError, match="duplicate key"):
        rsc.parse_real_scenario(BASE + "\nid: again\n", "dup.yaml")
    data = yaml.safe_load(BASE)
    data["case"]["amount_minor"] = -5
    with pytest.raises(ScenarioError, match="amount_minor"):
        rsc.parse_real_scenario(yaml.safe_dump(data, sort_keys=False), "neg.yaml")
    with pytest.raises(ScenarioError, match="mapping"):
        rsc.parse_real_scenario("- a\n- b\n", "list.yaml")


def test_grounding_checks_catch_wrong_fields_and_amounts() -> None:
    rs = SCENARIOS[0]
    g = rs.grounding.model_copy(
        update={"product": "Mortgage", "amounts_usd": (123456.78,), "company_response": "Closed"}
    )
    bad = rs.model_copy(update={"grounding": g})
    problems = " ".join(rsc.check_grounding(bad))
    assert "product" in problems and "123456.78" in problems and "company_response" in problems
    unknown = rs.model_copy(
        update={"grounding": rs.grounding.model_copy(update={"complaint": "cfpb2015-1"})}
    )
    assert "not a citable" in rsc.check_grounding(unknown)[0]


def test_find_and_duplicates(tmp_path: Path) -> None:
    assert rsc.find_real_scenario(IDS[0]).id == IDS[0]
    with pytest.raises(KeyError):
        rsc.find_real_scenario("real-nope")
    (tmp_path / "a.yaml").write_text(BASE)
    (tmp_path / "b.yaml").write_text(BASE)
    with pytest.raises(ScenarioError, match="duplicate scenario ids"):
        rsc.load_real_scenarios(tmp_path)


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("paid {$45.00} twice", {45.0}),
        ("{$1,400.00} and {$.00} and {$9.5}", {1400.0, 9.5}),
        ("no amounts, $ XXXX masked", set()),
    ],
)
def test_stated_amounts(text: str, want: set[float]) -> None:
    assert rsc.stated_amounts(text) == want


@given(st.decimals(min_value=Decimal("0.01"), max_value=Decimal("99999"), places=2))
def test_stated_amounts_round_trip(x: object) -> None:
    assert rsc.stated_amounts(f"charged {{${x}}} once") == {round(float(str(x)), 2)}
