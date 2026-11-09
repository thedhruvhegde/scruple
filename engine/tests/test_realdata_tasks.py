"""Label mappings, regulation paragraph ids, strict answer parsing and graders."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from scruple.realdata import labels as L  # noqa: N812
from scruple.realdata import regs
from scruple.realdata.records import Complaint
from scruple.realdata.tasks import (
    INVALID,
    TASKS,
    AnswerFormatError,
    Instance,
    OutcomeTask,
    RegimeTask,
    RoutingTask,
    get_task,
    sample,
    strict_json,
)

# --- label truth tables ------------------------------------------------------------


@pytest.mark.parametrize(
    ("response", "label"),
    [
        ("Closed with monetary relief", 1),
        ("Closed with non-monetary relief", 1),
        ("Closed with relief", 1),
        ("Closed with explanation", 0),
        ("Closed", None),
        ("Closed without relief", None),
        ("Untimely response", None),
        ("In progress", None),
        ("", None),
        ("closed with explanation", None),  # exact match only
    ],
)
def test_relief_truth_table(response: str, label: int | None) -> None:
    assert L.relief_label(response) == label


@pytest.mark.parametrize(
    ("product", "issue", "regime"),
    [
        ("Credit card", "Billing disputes", "reg_z"),
        ("Credit card", "Identity theft / Fraud / Embezzlement", "reg_z"),
        ("Credit card", "Transaction issue", "reg_z"),
        ("Credit card", "Late fee", None),
        ("Credit card", "Rewards", None),
        ("Bank account or service", "Using a debit or ATM card", "reg_e"),
        ("Bank account or service", "Making/receiving payments, sending money", "reg_e"),
        ("Bank account or service", "Account opening, closing, or management", None),
        ("Bank account or service", "Deposits and withdrawals", None),
        ("Prepaid card", "Unauthorized transactions/trans. issues", "reg_e"),
        ("Prepaid card", "Fraud or scam", "reg_e"),
        ("Prepaid card", "Fees", None),
        ("Money transfers", "Fraud or scam", None),
        ("Money transfers", "Wrong amount charged or received", None),
        ("Checking or savings account", "Managing an account", None),
    ],
)
def test_regime_truth_table(product: str, issue: str, regime: str | None) -> None:
    assert L.regime_label(product, issue) == regime


def test_regime_sections_exist_in_regs() -> None:
    ids = regs.paragraph_ids()
    for r in L.REGIMES:
        assert L.REGIME_SECTION[r] in ids
        assert L.REGIME_SECTION[r].startswith(L.REGIME_PART[r] + ".")


# --- regulation paragraph ids ---------------------------------------------------------


@pytest.mark.parametrize(
    "pid",
    [
        "1005.11",
        "1005.11(a)(1)(i)",
        "1005.11(a)(1)(vii)",
        "1005.11(b)(1)",
        "1005.11(c)(2)(i)(A)",
        "1005.11(c)(4)",
        "1005.6(b)(1)",
        "1005.6(b)(5)(i)",
        "1026.12(b)(1)(ii)",
        "1026.12(c)(3)(ii)(F)",
        "1026.13(a)(1)",
        "1026.13(c)(1)",
        "1026.13(g)(4)(i)",
        "1026.13(i)",
        "1026.13(i)(2)",
    ],
)
def test_known_paragraphs_exist(pid: str) -> None:
    assert pid in regs.paragraph_ids()


@pytest.mark.parametrize(
    "pid",
    ["1005.11(f)", "1026.13(j)", "1026.13(g)(i)", "1005.11(c)(3)(ii)(C)(4)", "1005.33", "1026.14"],
)
def test_bogus_paragraphs_absent(pid: str) -> None:
    assert pid not in regs.paragraph_ids()


def test_parser_on_synthetic_text() -> None:
    text = "\n".join(
        [
            "§ 9.1 Title.",
            "(a) Head —(1)(i) Deep.",
            "(ii) Next roman.",
            "(2) Two. See paragraph (h)(9) of this section.",
            "(h) Letter h.",
            "(i) Letter i, since no digit paragraph is open.",
            "(1) One. (i) Inline roman after a full stop.",
            "(A) Upper.",
            "(1) Italic one.",
            "(2) Back to digit two because italic two is expected.",
        ]
    )
    got = regs.parse_paragraph_ids(text, "9.1")
    assert got == {
        "9.1",
        "9.1(a)",
        "9.1(a)(1)",
        "9.1(a)(1)(i)",
        "9.1(a)(1)(ii)",
        "9.1(a)(2)",
        "9.1(h)",
        "9.1(i)",
        "9.1(i)(1)",
        "9.1(i)(1)(i)",
        "9.1(i)(1)(i)(A)",
        "9.1(i)(1)(i)(A)(1)",
        "9.1(i)(1)(i)(A)(2)",
    }


def test_roman_helpers() -> None:
    assert regs._roman_value("iv") == 4
    assert regs._roman_value("xiv") == 14
    assert regs._roman_value("iiii") is None
    assert regs._roman_value("b") is None
    assert regs._roman_value("") is None


@pytest.mark.parametrize(
    ("text", "ok"),
    [
        ("12 CFR 1005.11", True),
        ("12 CFR 1005.11(b)(1)", True),
        ("12 CFR 1026.13(i)", True),
        ("12 CFR 1026.13(z)", False),  # well formed, not a real paragraph
        ("12 CFR 1005.33(a)", False),
        ("1005.11(b)(1)", False),
        ("12 C.F.R. 1005.11(b)", False),
        ("12 CFR § 1005.11(b)", False),
        ("12 CFR 1005.11 (b)", False),
        (" 12 CFR 1005.11(b)", False),
    ],
)
def test_citation_exists(text: str, ok: bool) -> None:
    assert regs.citation_exists(text) is ok


# --- strict JSON --------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "",
        "issue: Late fee",
        '```json\n{"issue": "Late fee"}\n```',
        'Sure! {"issue": "Late fee"}',
        '{"issue": "Late fee"} thanks',
        '{"issue": "Late fee", "issue": "Rewards"}',
        '{"issue": "Late fee", "why": "x"}',
        "{}",
        '["Late fee"]',
        "{'issue': 'Late fee'}",
        '{"issue": "Late fee"',
    ],
)
def test_strict_json_rejects(text: str) -> None:
    with pytest.raises(AnswerFormatError):
        strict_json(text, ("issue",))


def test_strict_json_accepts_whitespace() -> None:
    assert strict_json('  \n{"issue": "x"}\n', ("issue",)) == {"issue": "x"}
    with pytest.raises(AnswerFormatError):
        strict_json(123, ("issue",))  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity", "true", '"0.5"', "1.5", "-0.1"])
def test_outcome_rejects_bad_probabilities(bad: str) -> None:
    with pytest.raises(AnswerFormatError):
        OutcomeTask("cfpb2015").parse_answer('{"p_relief": ' + bad + "}")


@pytest.mark.parametrize("good", ["0", "1", "0.25", "1e-3", "1.0"])
def test_outcome_accepts_probabilities(good: str) -> None:
    got = OutcomeTask("cfpb2015").parse_answer('{"p_relief": ' + good + "}")
    assert got == {"p_relief": float(json.loads(good))}


# --- real-label instance builders -------------------------------------------------------


def _c(i: int, **kw: Any) -> Complaint:
    base: dict[str, Any] = {
        "id": f"cfpb2015-{i}",
        "dataset": "cfpb2015",
        "complaint_id": str(i),
        "date_received": date(2015, 4, 1),
        "product": "Credit card",
        "sub_product": "",
        "issue": "Billing disputes",
        "sub_issue": "",
        "narrative": f"narrative {i}",
        "company": "Example Bank",
        "state": "",
        "submitted_via": "Web",
        "company_response": "Closed with explanation",
        "timely_response": "Yes",
        "consumer_disputed": "No",
    }
    base.update(kw)
    return Complaint(**base)


def test_outcome_build_drops_unmapped() -> None:
    rows = [
        _c(1, company_response="Closed with monetary relief"),
        _c(2, company_response="Closed"),
        _c(3),
        _c(4, company_response="Untimely response"),
    ]
    got = OutcomeTask("cfpb2015").build(rows)
    assert [(i.id, i.gold["relief"]) for i in got] == [("cfpb2015-1", 1), ("cfpb2015-3", 0)]
    assert got[0].inputs["company"] == "Example Bank"


def test_regime_build_uses_product_mapping_and_hides_product() -> None:
    rows = [
        _c(1),
        _c(2, product="Bank account or service", issue="Using a debit or ATM card"),
        _c(3, product="Money transfers", issue="Fraud or scam"),
        _c(4, issue="Rewards"),
    ]
    got = RegimeTask("cfpb2015").build(rows)
    assert [(i.id, i.gold["regime"]) for i in got] == [
        ("cfpb2015-1", "reg_z"),
        ("cfpb2015-2", "reg_e"),
    ]
    assert set(got[0].inputs) == {"narrative"}


def test_routing_build_inputs_are_narrative_only() -> None:
    got = RoutingTask("cfpb2015").build([_c(1)])
    assert got[0].inputs == {"narrative": "narrative 1"}
    assert got[0].gold == {"issue": "Billing disputes"}


# --- graders on hand cases ---------------------------------------------------------------


def _inst(task: str, i: int, gold: dict[str, str | int]) -> Instance:
    return Instance(id=f"x{i}", task=task, inputs={"narrative": "n"}, gold=gold)


def test_routing_grade_hand_case() -> None:
    t = RoutingTask("cfpb2015")
    issues = ["Billing disputes", "Billing disputes", "Late fee", "Rewards"]
    inst = [_inst(t.id, i, {"issue": g}) for i, g in enumerate(issues)]
    answers = {
        "x0": '{"issue": "Billing disputes"}',
        "x1": '{"issue": "Late fee"}',
        "x2": '```{"issue": "Late fee"}```',  # malformed -> wrong
        # x3 missing -> wrong
    }
    card = t.grade(inst, answers, n_boot=50, seed=1)
    assert card["n"] == 4
    assert card["n_invalid"] == 2
    assert card["n_missing"] == 1
    iss = card["issue"]
    assert iss["accuracy"]["value"] == 0.25
    # f1: billing tp1 gold2 pred1 -> 2/3; late tp0 gold1 pred1 -> 0; rewards 0
    assert iss["macro_f1"]["value"] == pytest.approx(round((2 / 3) / 3, 6))
    assert iss["per_class"]["Billing disputes"] == {"n": 2, "recall": 0.5}
    assert iss["confusion"]["Late fee"] == {INVALID: 1}
    assert iss["confusion"]["Billing disputes"] == {"Billing disputes": 1, "Late fee": 1}


def test_routing_rejects_unknown_label_and_unknown_ids() -> None:
    t = RoutingTask("cfpb2015")
    with pytest.raises(AnswerFormatError):
        t.parse_answer('{"issue": "Not a CFPB issue"}')
    with pytest.raises(KeyError):
        t.grade([_inst(t.id, 0, {"issue": "Late fee"})], {"nope": "{}"}, n_boot=0)


def test_routing_2025_requires_allowed_pair() -> None:
    t = RoutingTask("cfpb2025")
    issue, sub = t.taxonomy[0].split(" | ")
    assert t.parse_answer(json.dumps({"issue": issue, "sub_issue": sub})) == {
        "issue": issue,
        "sub_issue": sub,
    }
    with pytest.raises(AnswerFormatError):
        t.parse_answer(json.dumps({"issue": issue}))
    other = next(p for p in t.taxonomy if not p.startswith(issue + " | "))
    with pytest.raises(AnswerFormatError):
        t.parse_answer(json.dumps({"issue": issue, "sub_issue": other.split(" | ")[1]}))


def test_routing_2025_grades_issue_and_pair() -> None:
    t = RoutingTask("cfpb2025")
    issues = [p.split(" | ")[0] for p in t.taxonomy]
    a = t.taxonomy[0]
    b = next(p for p in t.taxonomy if issues.count(p.split(" | ")[0]) > 1)
    ia, sa = a.split(" | ")
    ib, sb = b.split(" | ")
    inst = [
        _inst(t.id, 0, {"issue": ia, "sub_issue": sa}),
        _inst(t.id, 1, {"issue": ib, "sub_issue": sb}),
    ]
    same_issue_other_sub = next(p for p in t.taxonomy if p.startswith(ib + " | ") and p != b).split(
        " | "
    )[1]
    answers = {
        "x0": json.dumps({"issue": ia, "sub_issue": sa}),
        "x1": json.dumps({"issue": ib, "sub_issue": same_issue_other_sub}),
    }
    card = t.grade(inst, answers, n_boot=0)
    assert card["issue"]["accuracy"]["value"] == 1.0
    assert card["issue_sub_issue"]["accuracy"]["value"] == 0.5


def test_outcome_grade_hand_case() -> None:
    t = OutcomeTask("cfpb2015")
    inst = [_inst(t.id, i, {"relief": y}) for i, y in enumerate([1, 0, 1, 0])]
    answers = {
        "x0": '{"p_relief": 0.9}',
        "x1": '{"p_relief": 0.2}',
        "x2": '{"p_relief": 0.4}',
        "x3": "0.1",  # malformed -> p = 1 - y = 1.0
    }
    card = t.grade(inst, answers, n_boot=0)
    # scores [.9, .2, .4, 1.0]; pos {.9, .4} vs neg {.2, 1.0}: wins .9>.2, .4>.2 -> 2/4
    assert card["auroc"]["value"] == 0.5
    assert card["brier"]["value"] == pytest.approx(round((0.01 + 0.04 + 0.36 + 1.0) / 4, 6))
    # hard [1, 0, 0, 1]: recall pos 1/2, neg 1/2
    assert card["balanced_accuracy"]["value"] == 0.5
    assert card["base_rate"] == 0.5
    assert card["n_invalid"] == 1


def test_outcome_all_malformed_is_worst_case() -> None:
    t = OutcomeTask("cfpb2015")
    inst = [_inst(t.id, i, {"relief": y}) for i, y in enumerate([1, 0, 1, 0])]
    card = t.grade(inst, {}, n_boot=0)
    assert card["auroc"]["value"] == 0.0
    assert card["brier"]["value"] == 1.0
    assert card["ece"]["value"] == 1.0


def test_regime_grade_hand_case() -> None:
    t = RegimeTask("cfpb2015")
    golds = ["reg_z", "reg_z", "reg_e", "reg_e", "reg_e"]
    inst = [_inst(t.id, i, {"regime": g}) for i, g in enumerate(golds)]
    answers = {
        "x0": '{"regime": "reg_z", "citation": "12 CFR 1026.13(a)(1)"}',  # strict ok
        "x1": '{"regime": "reg_z", "citation": "12 CFR 1005.11(a)"}',  # wrong part
        "x2": '{"regime": "reg_e", "citation": "12 CFR 1005.11(z)"}',  # not real
        "x3": '{"regime": "reg_z", "citation": "12 CFR 1026.12(b)(1)"}',  # wrong regime
        "x4": '{"regime": "reg_e", "citation": "Reg E"}',  # malformed
    }
    card = t.grade(inst, answers, n_boot=0)
    assert card["n_invalid"] == 1
    assert card["accuracy"]["value"] == pytest.approx(0.6)
    assert card["citation_exists"]["value"] == pytest.approx(0.6)
    assert card["citation_consistent"]["value"] == pytest.approx(0.4)
    assert card["strict_accuracy"]["value"] == pytest.approx(0.2)
    assert card["per_class"]["reg_e"] == {"n": 3, "recall": pytest.approx(round(1 / 3, 6))}


def test_regime_parse_requires_regime_and_citation_format() -> None:
    t = RegimeTask("cfpb2015")
    with pytest.raises(AnswerFormatError):
        t.parse_answer('{"regime": "reg_x", "citation": "12 CFR 1005.11"}')
    with pytest.raises(AnswerFormatError):
        t.parse_answer('{"regime": "reg_e", "citation": 1005.11}')
    with pytest.raises(AnswerFormatError):
        t.parse_answer('{"regime": "reg_e"}')
    # well formed but non-existent paragraphs parse; the grader marks them wrong
    assert t.parse_answer('{"regime": "reg_e", "citation": "12 CFR 1005.11(q)"}')


# --- fuzzing ----------------------------------------------------------------------------

ALL = [get_task(n) for n in sorted(TASKS)]


@settings(max_examples=200, deadline=None)
@given(st.text(max_size=300), st.sampled_from(ALL))
def test_parse_answer_never_crashes(text: str, task: Any) -> None:
    try:
        out = task.parse_answer(text)
    except AnswerFormatError:
        return
    assert set(out) == set(task.answer_keys)


json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(max_size=20),
    lambda kids: (
        st.lists(kids, max_size=3) | st.dictionaries(st.text(max_size=8), kids, max_size=3)
    ),
    max_leaves=8,
)


@settings(max_examples=200, deadline=None)
@given(
    st.dictionaries(
        st.sampled_from(["issue", "sub_issue", "p_relief", "regime", "citation", "x"]),
        json_values,
        max_size=4,
    ),
    st.sampled_from(ALL),
)
def test_parse_answer_on_random_json_objects(obj: dict[str, Any], task: Any) -> None:
    try:
        out = task.parse_answer(json.dumps(obj))
    except AnswerFormatError:
        return
    assert set(obj) == set(task.answer_keys)
    assert out == {k: (float(v) if k == "p_relief" else v) for k, v in obj.items()}


@settings(max_examples=50, deadline=None)
@given(st.data())
def test_valid_routing_answers_round_trip_and_wrapping_breaks_them(data: st.DataObject) -> None:
    t = RoutingTask("cfpb2015")
    label = data.draw(st.sampled_from(t.taxonomy))
    text = json.dumps({"issue": label})
    assert t.parse_answer(text) == {"issue": label}
    junk = data.draw(st.text(min_size=1, max_size=10).filter(lambda s: s.strip() != ""))
    with pytest.raises(AnswerFormatError):
        t.parse_answer(text + junk if not junk.strip().startswith("}") else junk + text)


# --- prompts and sampling -------------------------------------------------------------------


def test_prompts_render_inputs_and_format() -> None:
    for task in ALL:
        inst = task.instances("dev")[0]
        p = task.render_prompt(inst)
        assert inst.inputs["narrative"] in p
        assert "Reply with only this JSON object" in p
        for key in task.answer_keys:
            assert f'"{key}"' in p
        assert p == task.render_prompt(inst)
    r = RoutingTask("cfpb2015")
    p = r.render_prompt(r.instances("dev")[0])
    assert all(f"- {lab}" in p for lab in r.taxonomy)
    o = get_task("outcome-2015")
    inst = o.instances("dev")[0]
    assert f"Company: {inst.inputs['company']}" in o.render_prompt(inst)


def test_regime_prompt_does_not_leak_product() -> None:
    t = get_task("regime-2015")
    inst = t.instances("dev")[0]
    p = t.render_prompt(inst).replace(inst.inputs["narrative"], "")
    assert "Product" not in p
    assert "Bank account or service" not in p


def test_sample_is_deterministic_prefix() -> None:
    t = get_task("regime-2015")
    inst = t.instances("dev")
    a = sample(inst, 10, seed=3)
    assert a == sample(inst, 10, seed=3)
    assert sample(inst, 20, seed=3)[:10] == a
    assert a != sample(inst, 10, seed=4)
    assert len(sample(inst, None, seed=3)) == len(inst)


def test_get_task_unknown() -> None:
    with pytest.raises(KeyError, match="unknown task"):
        get_task("routing-1999")


def test_instances_respect_seal() -> None:
    from scruple.realdata.splits import SealedSplitError

    with pytest.raises(SealedSplitError):
        get_task("outcome-2025").instances("test")


def test_task_ids() -> None:
    assert [get_task(n).id for n in sorted(TASKS)] == sorted(TASKS)
