"""Baselines, the LLM runner path (fake transports only) and the realdata CLI."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from scruple.cli.main import main
from scruple.realdata import llm
from scruple.realdata.baselines import (
    CITATION,
    Keywords,
    Majority,
    TfidfLR,
    run_baseline,
)
from scruple.realdata.regs import citation_exists
from scruple.realdata.tasks import Instance, OutcomeTask, RegimeTask, RoutingTask, get_task, sample

HAS_SKLEARN = importlib.util.find_spec("sklearn") is not None


def _inst(task: str, i: int, narrative: str, gold: dict[str, str | int], **inputs: str) -> Instance:
    return Instance(id=f"x{i}", task=task, inputs={"narrative": narrative, **inputs}, gold=gold)


# --- baselines --------------------------------------------------------------------------


def test_baseline_citations_are_real_paragraphs() -> None:
    for c in CITATION.values():
        assert citation_exists(c)


def test_majority_routing_and_outcome() -> None:
    t = RoutingTask("cfpb2015")
    train = [
        _inst(t.id, i, "n", {"issue": g}) for i, g in enumerate(["Late fee"] * 3 + ["Rewards"])
    ]
    m = Majority(t)
    m.fit(train, [])
    assert m.answer(train[:2]) == ['{"issue": "Late fee"}'] * 2
    o = OutcomeTask("cfpb2015")
    otrain = [_inst(o.id, i, "n", {"relief": y}) for i, y in enumerate([1, 0, 0, 0])]
    mo = Majority(o)
    mo.fit(otrain, [])
    assert mo.answer(otrain[:1]) == ['{"p_relief": 0.25}']
    card = o.grade(
        otrain, dict(zip(["x0", "x1", "x2", "x3"], mo.answer(otrain), strict=True)), n_boot=0
    )
    assert card["auroc"]["value"] == 0.5


def test_majority_ties_break_by_label() -> None:
    t = RegimeTask("cfpb2015")
    train = [_inst(t.id, 0, "n", {"regime": "reg_z"}), _inst(t.id, 1, "n", {"regime": "reg_e"})]
    m = Majority(t)
    m.fit(train, [])
    assert m.info == {"label": "reg_e"}
    assert t.parse_answer(m.answer(train)[0]) == {"regime": "reg_e", "citation": CITATION["reg_e"]}


def test_keywords_regime_rules() -> None:
    t = RegimeTask("cfpb2015")
    train = [_inst(t.id, 0, "n", {"regime": "reg_z"})] * 2 + [
        _inst(t.id, 1, "n", {"regime": "reg_e"})
    ]
    k = Keywords(t)
    k.fit(train, [])
    test = [
        _inst(t.id, 0, "My debit card was used at an ATM", {"regime": "reg_e"}),
        _inst(t.id, 1, "My credit card statement shows interest", {"regime": "reg_z"}),
        _inst(t.id, 2, "Nothing relevant here", {"regime": "reg_e"}),
    ]
    got = [json.loads(a)["regime"] for a in k.answer(test)]
    assert got == ["reg_e", "reg_z", "reg_z"]  # tie -> train majority


def test_keywords_routing_uses_label_words() -> None:
    t = RoutingTask("cfpb2015")
    train = [_inst(t.id, 0, "n", {"issue": "Billing disputes"})]
    k = Keywords(t)
    k.fit(train, [])
    a = k.answer([_inst(t.id, 1, "they charged a late fee again", {"issue": "Late fee"})])
    assert json.loads(a[0]) == {"issue": "Late fee"}
    b = k.answer([_inst(t.id, 2, "zzz qqq", {"issue": "Late fee"})])
    assert json.loads(b[0]) == {"issue": "Billing disputes"}  # no match -> majority


def test_keywords_outcome_rates_from_train() -> None:
    o = OutcomeTask("cfpb2015")
    train = [
        _inst(o.id, 0, "they charged a fee", {"relief": 1}),
        _inst(o.id, 1, "please refund me", {"relief": 0}),
        _inst(o.id, 2, "rude staff", {"relief": 0}),
        _inst(o.id, 3, "rude again", {"relief": 0}),
    ]
    k = Keywords(o)
    k.fit(train, [])
    assert k.info["p_flagged"] == 0.5
    assert k.info["p_unflagged"] == 0.0
    assert [json.loads(a)["p_relief"] for a in k.answer(train)] == [0.5, 0.5, 0.0, 0.0]


@pytest.mark.skipif(not HAS_SKLEARN, reason="scikit-learn not installed")
def test_tfidf_lr_on_real_dev_sample_is_deterministic() -> None:
    task = get_task("regime-2015")
    dev = task.instances("dev")
    train, held = sample(dev, 250, seed=1), sample(dev, None, seed=1)[250:]
    a = run_baseline("tfidf_lr", task, train, held[:60], held[60:], n_boot=0)
    b = run_baseline("tfidf_lr", task, train, held[:60], held[60:], n_boot=0)
    assert a == b
    assert a["n_invalid"] == 0
    assert a["fit"]["C"] in (0.5, 2.0, 8.0)
    assert a["accuracy"]["value"] > 0.6


@pytest.mark.skipif(not HAS_SKLEARN, reason="scikit-learn not installed")
def test_tfidf_lr_outcome_gives_probabilities() -> None:
    task = get_task("outcome-2025")
    dev = task.instances("dev")
    m = TfidfLR(task)
    m.fit(dev[:300], dev[300:400])
    ps = [json.loads(a)["p_relief"] for a in m.answer(dev[400:450])]
    assert all(0.0 <= p <= 1.0 for p in ps)
    assert m.info["dev_selection"]["metric"] == "-brier"


# --- LLM runner path (fake transports) ------------------------------------------------------


class Script:
    def __init__(self, replies: list[Any]) -> None:
        self.replies = list(replies)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        if isinstance(r, httpx.Response):
            return r
        return httpx.Response(200, json=r)

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests]


def _prompts(n: int = 2) -> list[llm.PromptRecord]:
    return [
        llm.PromptRecord(id=f"p{i}", task="regime-2015", split="dev", system="sys", prompt=f"q{i}")
        for i in range(n)
    ]


def _anthropic(text: str) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 11, "output_tokens": 3},
    }


def test_anthropic_path_sends_plain_completion() -> None:
    s = Script([_anthropic('{"regime": "reg_e", "citation": "12 CFR 1005.11"}'), _anthropic("x")])
    r = llm.make_runner(
        "anthropic",
        model="m-test",
        api_key="k",
        transport=httpx.MockTransport(s),
        sleep=lambda _: None,
    )
    out = llm.answer_prompts(r, _prompts())
    assert [a.answer for a in out] == ['{"regime": "reg_e", "citation": "12 CFR 1005.11"}', "x"]
    assert out[0].input_tokens == 11 and out[0].model == "m-test"
    body = s.bodies()[0]
    assert body["system"] == "sys"
    assert body["temperature"] == 0
    assert "tools" not in body
    assert body["messages"][0]["content"][0]["text"] == "q0"
    assert s.requests[0].url.path == "/v1/messages"
    assert s.requests[0].headers["x-api-key"] == "k"


def test_anthropic_path_retries_then_records_errors() -> None:
    s = Script(
        [
            httpx.Response(529, text="overloaded"),
            _anthropic("ok"),
            httpx.Response(400, text="bad"),
            {"content": "not a list"},
        ]
    )
    r = llm.make_runner(
        "anthropic", model="m", api_key="k", transport=httpx.MockTransport(s), sleep=lambda _: None
    )
    out = llm.answer_prompts(r, _prompts(3))
    assert out[0].answer == "ok" and out[0].error is None
    assert out[1].answer == "" and out[1].error is not None and "400" in out[1].error
    assert out[2].answer == "" and out[2].error is not None


def test_openai_path() -> None:
    s = Script(
        [
            {
                "choices": [{"message": {"content": '{"p_relief": 0.3}'}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            },
            httpx.ConnectError("down"),
        ]
    )
    r = llm.make_runner("openai", model="gpt-test", api_key="k", transport=httpx.MockTransport(s))
    out = llm.answer_prompts(r, _prompts())
    assert out[0].answer == '{"p_relief": 0.3}' and out[0].output_tokens == 2
    assert out[1].error is not None
    body = s.bodies()[0]
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert body["max_completion_tokens"] == 256
    assert s.requests[0].headers["authorization"] == "Bearer k"


def test_runner_config_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ValueError, match="API key"):
        llm.make_runner("anthropic")
    with pytest.raises(ValueError, match="no default model"):
        llm.make_runner("openai", api_key="k")
    with pytest.raises(ValueError):
        llm.make_runner("other", api_key="k")  # type: ignore[arg-type]


# --- CLI -------------------------------------------------------------------------------------


def test_cli_prompt_grade_round_trip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    prompts = tmp_path / "p.jsonl"
    assert (
        main(
            [
                "realdata",
                "prompt",
                "--task",
                "regime-2015",
                "--split",
                "dev",
                "--n",
                "5",
                "--seed",
                "2",
                "--out",
                str(prompts),
            ]
        )
        == 0
    )
    rows = [json.loads(line) for line in prompts.read_text().splitlines()]
    assert len(rows) == 5
    assert {r["task"] for r in rows} == {"regime-2015"}
    assert all("Complaint narrative" in r["prompt"] for r in rows)
    task = get_task("regime-2015")
    gold = {i.id: i.gold["regime"] for i in task.instances("dev")}
    answers = tmp_path / "a.jsonl"
    lines = [
        json.dumps(
            {
                "id": r["id"],
                "answer": json.dumps(
                    {"regime": gold[r["id"]], "citation": CITATION[str(gold[r["id"]])]}
                ),
            }
        )
        for r in rows[:4]
    ]
    answers.write_text("\n".join(lines) + "\n")
    card_path = tmp_path / "card.json"
    assert (
        main(
            [
                "realdata",
                "grade",
                "--task",
                "regime-2015",
                "--split",
                "dev",
                "--answers",
                str(answers),
                "--prompts",
                str(prompts),
                "--n-boot",
                "20",
                "--out",
                str(card_path),
            ]
        )
        == 0
    )
    card = json.loads(card_path.read_text())
    assert card["n"] == 5
    assert card["n_missing"] == 1
    assert card["accuracy"]["value"] == 0.8
    assert card["strict_accuracy"]["value"] == 0.8
    # without --prompts only the answered ids are graded, to stdout
    capsys.readouterr()
    assert (
        main(
            [
                "realdata",
                "grade",
                "--task",
                "regime-2015",
                "--split",
                "dev",
                "--answers",
                str(answers),
                "--n-boot",
                "0",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["accuracy"]["value"] == 1.0


def test_cli_grade_rejects_bad_answer_files(tmp_path: Path) -> None:
    a = tmp_path / "a.jsonl"
    a.write_text('{"id": "nope", "answer": "x"}\n')
    assert (
        main(["realdata", "grade", "--task", "routing-2015", "--split", "dev", "--answers", str(a)])
        == 2
    )
    a.write_text('{"id": "x", "answer": 1}\n')
    assert (
        main(["realdata", "grade", "--task", "routing-2015", "--split", "dev", "--answers", str(a)])
        == 2
    )
    first = get_task("routing-2015").instances("dev")[0].id
    a.write_text(f'{{"id": "{first}", "answer": "a"}}\n{{"id": "{first}", "answer": "b"}}\n')
    assert (
        main(["realdata", "grade", "--task", "routing-2015", "--split", "dev", "--answers", str(a)])
        == 2
    )
    a.write_text("[1]\n")
    assert (
        main(["realdata", "grade", "--task", "routing-2015", "--split", "dev", "--answers", str(a)])
        == 2
    )


def test_cli_run_with_fake_transport(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = Script([_anthropic('{"p_relief": 0.5}')])
    real = llm.make_runner

    def fake(provider: Any, **kw: Any) -> Any:
        return real(provider, api_key="k", transport=httpx.MockTransport(s), **kw)

    monkeypatch.setattr(llm, "make_runner", fake)
    p = tmp_path / "p.jsonl"
    p.write_text(_prompts(1)[0].model_dump_json() + "\n")
    out = tmp_path / "a.jsonl"
    assert main(["realdata", "run", "--prompts", str(p), "--model", "m", "--out", str(out)]) == 0
    row = json.loads(out.read_text())
    assert row["answer"] == '{"p_relief": 0.5}'
    assert row["id"] == "p0"


def test_cli_verify_and_baselines(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["realdata", "verify"]) == 0
    assert "regs: 142 paragraph ids" in capsys.readouterr().out
    assert (
        main(
            [
                "realdata",
                "baselines",
                "--task",
                "regime-2015",
                "--baseline",
                "majority",
                "--n-boot",
                "0",
                "--out",
                str(tmp_path),
            ]
        )
        == 0
    )
    res = json.loads((tmp_path / "regime-2015.json").read_text())
    assert set(res["results"]) == {"majority"}
    assert res["results"]["majority"]["n"] == res["splits"]["test"]["n"] == 764


def test_cli_build_splits_needs_pinned_sources(tmp_path: Path) -> None:
    assert (
        main(["realdata", "build-splits", "--src", str(tmp_path), "--out", str(tmp_path / "o")])
        == 2
    )
