"""Baselines for the real-data tasks: majority class, keyword rules, TF-IDF + LR.

Every baseline is fitted on the train split only and answers through the same
strict JSON format a model must use, so its answers go through `Task.grade`
exactly like a model's. Choices (the LR regularisation strength) are made on dev.
The sealed test split is read once, for the reported numbers.

- `majority`: the most frequent train label; for outcome the train relief rate as
  a constant probability (so AUROC is 0.5 by construction).
- `keywords`: hand-written rules, not learned. Routing scores each allowed label
  by how many of its own content words appear in the narrative (ties and no-match
  fall back to train frequency). Outcome flags narratives that mention fees,
  refunds, reversals or double charges; the two probabilities are the train relief
  rates of the flagged and unflagged groups. Regime counts credit-card words
  against deposit/prepaid words.
- `tfidf_lr`: scikit-learn TfidfVectorizer (word 1-2 grams, sublinear tf, min_df 2,
  50k features) and LogisticRegression (lbfgs), C chosen on dev from (0.5, 2, 8) by
  macro-F1 (routing, regime) or Brier (outcome). Outcome adds product, issue,
  sub-issue and company as tokens. Needs the dev dependency `scikit-learn`.

For regime, every baseline cites the regime's general section paragraph
(`12 CFR 1005.11(a)(1)` or `12 CFR 1026.13(a)`), both real paragraph ids.
"""

from __future__ import annotations

import importlib
import json
import re
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Callable, Sequence
from typing import Any

from scruple.realdata import metrics as M  # noqa: N812
from scruple.realdata.labels import Regime
from scruple.realdata.tasks import Instance, OutcomeTask, RegimeTask, RoutingTask, Task

CITATION: dict[str, str] = {"reg_e": "12 CFR 1005.11(a)(1)", "reg_z": "12 CFR 1026.13(a)"}
C_GRID: tuple[float, ...] = (0.5, 2.0, 8.0)
STOP = frozenset(
    [
        "and",
        "the",
        "for",
        "with",
        "not",
        "other",
        "from",
        "about",
        "didn",
        "don",
        "into",
        "than",
        "when",
        "your",
        "was",
        "can",
        "company",
        "account",
        "problem",
        "problems",
        "issue",
        "issues",
    ]
)
RELIEF_WORDS: tuple[str, ...] = (
    "fee",
    "fees",
    "refund",
    "reverse",
    "reversed",
    "waive",
    "waived",
    "charged twice",
    "double charged",
    "duplicate",
    "overdraft",
)
REG_Z_WORDS: tuple[str, ...] = (
    "credit card",
    "statement",
    "apr",
    "interest",
    "credit limit",
    "minimum payment",
    "balance transfer",
    "card issuer",
    "billing",
)
REG_E_WORDS: tuple[str, ...] = (
    "debit",
    "atm",
    "checking",
    "savings",
    "prepaid",
    "direct deposit",
    "bank account",
    "withdrawal",
    "withdrew",
    "transfer",
    "deposit",
)
_WORD = re.compile(r"[a-z]+")


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def _count(text: str, phrases: Sequence[str]) -> int:
    low = f" {' '.join(_WORD.findall(text.lower()))} "
    return sum(1 for ph in phrases if f" {ph} " in low)


def _label(task: Task, inst: Instance) -> str:
    if isinstance(task, RoutingTask):
        if task.with_sub_issue:
            return f"{inst.gold['issue']} | {inst.gold['sub_issue']}"
        return str(inst.gold["issue"])
    if isinstance(task, RegimeTask):
        return str(inst.gold["regime"])
    raise TypeError(f"{task.id} is not a classification task")


def _answer_label(task: Task, label: str) -> str:
    if isinstance(task, RoutingTask):
        if task.with_sub_issue:
            issue, sub = label.split(" | ", 1)
            return json.dumps({"issue": issue, "sub_issue": sub})
        return json.dumps({"issue": label})
    return json.dumps({"regime": label, "citation": CITATION[label]})


def _answer_p(p: float) -> str:
    return json.dumps({"p_relief": round(min(max(p, 0.0), 1.0), 6)})


class Baseline(ABC):
    name: str

    def __init__(self, task: Task) -> None:
        self.task = task
        self.info: dict[str, Any] = {}

    @abstractmethod
    def fit(self, train: Sequence[Instance], dev: Sequence[Instance]) -> None: ...

    @abstractmethod
    def answer(self, insts: Sequence[Instance]) -> list[str]: ...


class Majority(Baseline):
    name = "majority"

    def fit(self, train: Sequence[Instance], dev: Sequence[Instance]) -> None:
        if isinstance(self.task, OutcomeTask):
            self.p = sum(int(i.gold["relief"]) for i in train) / len(train)
            self.info = {"p_relief": round(self.p, 6)}
        else:
            counts = Counter(_label(self.task, i) for i in train)
            # most common, ties broken by label text for determinism
            self.label = min(counts, key=lambda k: (-counts[k], k))
            self.info = {"label": self.label}

    def answer(self, insts: Sequence[Instance]) -> list[str]:
        if isinstance(self.task, OutcomeTask):
            return [_answer_p(self.p) for _ in insts]
        return [_answer_label(self.task, self.label) for _ in insts]


class Keywords(Baseline):
    name = "keywords"

    def fit(self, train: Sequence[Instance], dev: Sequence[Instance]) -> None:
        if isinstance(self.task, OutcomeTask):
            groups: dict[bool, list[int]] = {True: [], False: []}
            for i in train:
                groups[_count(i.inputs["narrative"], RELIEF_WORDS) > 0].append(
                    int(i.gold["relief"])
                )
            self.p = {k: (sum(v) / len(v) if v else 0.0) for k, v in groups.items()}
            self.info = {
                "p_flagged": round(self.p[True], 6),
                "p_unflagged": round(self.p[False], 6),
                "flagged_share_train": round(len(groups[True]) / len(train), 6),
            }
            return
        self.prior = Counter(_label(self.task, i) for i in train)
        if isinstance(self.task, RoutingTask):
            self.vocab = {
                lab: {w for w in _words(lab) if w not in STOP and len(w) > 2}
                for lab in self.task.taxonomy
            }

    def _route(self, narrative: str) -> str:
        words = _words(narrative)
        assert isinstance(self.task, RoutingTask)
        best = max(
            self.task.taxonomy,
            key=lambda lab: (len(self.vocab[lab] & words), self.prior[lab], lab),
        )
        if not self.vocab[best] & words:
            best = min(self.prior, key=lambda k: (-self.prior[k], k))
        return best

    def _regime(self, narrative: str) -> Regime:
        z = _count(narrative, REG_Z_WORDS)
        e = _count(narrative, REG_E_WORDS)
        if z > e:
            return "reg_z"
        if e > z:
            return "reg_e"
        return "reg_z" if self.prior["reg_z"] >= self.prior["reg_e"] else "reg_e"

    def answer(self, insts: Sequence[Instance]) -> list[str]:
        if isinstance(self.task, OutcomeTask):
            return [
                _answer_p(self.p[_count(i.inputs["narrative"], RELIEF_WORDS) > 0]) for i in insts
            ]
        if isinstance(self.task, RoutingTask):
            return [_answer_label(self.task, self._route(i.inputs["narrative"])) for i in insts]
        return [_answer_label(self.task, self._regime(i.inputs["narrative"])) for i in insts]


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_") or "none"


def _text(task: Task, inst: Instance) -> str:
    if isinstance(task, OutcomeTask):
        f = inst.inputs
        tags = " ".join(
            f"zz{k}_{_slug(f[k])}"
            for k in ("product", "sub_product", "issue", "sub_issue", "company")
        )
        return f"{tags} {f['narrative']}"
    return inst.inputs["narrative"]


class TfidfLR(Baseline):
    name = "tfidf_lr"

    def _make(self, c: float) -> Any:
        text = importlib.import_module("sklearn.feature_extraction.text")
        linear = importlib.import_module("sklearn.linear_model")
        pipeline = importlib.import_module("sklearn.pipeline")
        return pipeline.make_pipeline(
            text.TfidfVectorizer(
                ngram_range=(1, 2), sublinear_tf=True, min_df=2, max_features=50_000
            ),
            linear.LogisticRegression(C=c, max_iter=3000),
        )

    def _xy(self, insts: Sequence[Instance]) -> tuple[list[str], list[Any]]:
        x = [_text(self.task, i) for i in insts]
        if isinstance(self.task, OutcomeTask):
            return x, [int(i.gold["relief"]) for i in insts]
        return x, [_label(self.task, i) for i in insts]

    def _dev_score(self, model: Any, dev: Sequence[Instance]) -> float:
        x, y = self._xy(dev)
        if isinstance(self.task, OutcomeTask):
            p = [float(r[1]) for r in model.predict_proba(x)]
            b = M.brier(y, p)
            return -(b if b is not None else 1.0)
        f = M.macro_f1(y, [str(v) for v in model.predict(x)])
        return f if f is not None else 0.0

    def fit(self, train: Sequence[Instance], dev: Sequence[Instance]) -> None:
        x, y = self._xy(train)
        scores: dict[str, float] = {}
        best: tuple[float, float] | None = None
        for c in C_GRID:
            model = self._make(c).fit(x, y)
            s = self._dev_score(model, dev)
            scores[str(c)] = round(s, 6)
            if best is None or s > best[0]:
                best = (s, c)
        assert best is not None
        self.c = best[1]
        self.model = self._make(self.c).fit(x, y)
        metric = "-brier" if isinstance(self.task, OutcomeTask) else "macro_f1"
        self.info = {"C": self.c, "dev_selection": {"metric": metric, "by_C": scores}}

    def answer(self, insts: Sequence[Instance]) -> list[str]:
        x = [_text(self.task, i) for i in insts]
        if isinstance(self.task, OutcomeTask):
            classes = list(self.model.classes_)
            pos = classes.index(1)
            return [_answer_p(float(r[pos])) for r in self.model.predict_proba(x)]
        return [_answer_label(self.task, str(v)) for v in self.model.predict(x)]


BASELINES: dict[str, Callable[[Task], Baseline]] = {
    "majority": Majority,
    "keywords": Keywords,
    "tfidf_lr": TfidfLR,
}


def run_baseline(
    name: str,
    task: Task,
    train: Sequence[Instance],
    dev: Sequence[Instance],
    test: Sequence[Instance],
    *,
    n_boot: int = 1000,
    seed: int = 0,
) -> dict[str, Any]:
    b = BASELINES[name](task)
    b.fit(train, dev)
    answers = dict(zip([i.id for i in test], b.answer(test), strict=True))
    card = task.grade(test, answers, n_boot=n_boot, seed=seed)
    card["baseline"] = name
    card["fit"] = b.info
    return card


DATA_NOTE = (
    "Real data: CFPB Consumer Complaint Database narratives (public domain, 17 USC 105), "
    "frozen splits data/real/v1 (see docs/data/CFPB.md). Not synthetic."
)


def baseline_results(
    task_name: str,
    *,
    baselines: Sequence[str] = tuple(BASELINES),
    n_boot: int = 1000,
    seed: int = 0,
) -> dict[str, Any]:
    """Fit on train, choose on dev, score once on the sealed test split."""
    from scruple.realdata.splits import VERSION, load_manifest
    from scruple.realdata.tasks import get_task

    task = get_task(task_name)
    train = task.instances("train")
    dev = task.instances("dev")
    test = task.instances("test", unseal=True)
    manifest = load_manifest()["datasets"][task.dataset]["splits"]
    return {
        "task": task.id,
        "dataset": task.dataset,
        "data": DATA_NOTE,
        "splits": {
            "version": VERSION,
            "train": {"n": len(train), "sha256_jsonl": manifest["train"]["sha256_jsonl"]},
            "dev": {"n": len(dev), "sha256_jsonl": manifest["dev"]["sha256_jsonl"]},
            "test": {"n": len(test), "sha256_jsonl": manifest["test"]["sha256_jsonl"]},
        },
        "reproduce": f"uv run scruple realdata baselines --task {task.id}",
        "results": {
            b: run_baseline(b, task, train, dev, test, n_boot=n_boot, seed=seed) for b in baselines
        },
    }
