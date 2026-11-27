"""Train reader C (`ngram`) and write `data/ngram.npz`.

    uv run python -m scruple.verifiers.claimtext.train [--public DIR]

Training data, per claim:

- the corpus's *train* split for that claim (assert / deny / none, with perturbed
  variants), and every other claim's train sentences as "none" (a sentence about a SAR
  says nothing about a reconciliation);
- with `--public DIR`, "none" distractors from permissively licensed public corpora
  (their own train splits only), so negated
  and hedged text that makes no claim reads as "none". Only corpora whose licence
  allows training a shipped model are read: FEVER-Symmetric (MIT; Wikipedia text
  CC BY-SA, attribution: Schuster et al. 2019) and CondaQA (Apache-2.0; Ravichander
  et al. 2022). Licence-restricted sets (XNLI, SFU-SP, *SEM 2012, BioScope, ...) are
  used only by the internal evaluation (`scruple.redteam.textbench`), never here.

The regularisation strength is picked on the corpus's *dev* split. The test split is
never read here. Needs scikit-learn (a dev dependency); inference needs only numpy.
"""

from __future__ import annotations

import argparse
import json
import random
import re
from pathlib import Path

import numpy as np

from scruple.verifiers.claimtext.cores import core_sentences
from scruple.verifiers.claimtext.corpus import sentences
from scruple.verifiers.claimtext.model import Claim, Polarity
from scruple.verifiers.claimtext.ngram import CLASSES, DIM, features
from scruple.verifiers.claimtext.textnorm import normalise

OUT = Path(__file__).parent / "data" / "ngram.npz"
_LABEL = {Polarity.ASSERT: 0, Polarity.DENY: 1, Polarity.NONE: 2}


def public_distractors(root: Path, split: str, limit: int, seed: int = 0) -> list[str]:
    out: list[str] = []
    fv = root / "fever_symmetric" / "clean" / f"{split}.jsonl"
    if fv.exists():
        for x in fv.read_text().splitlines():
            if x.strip():
                r = json.loads(x)
                out += [r.get("claim", ""), r.get("evidence", "")]
    cq = root / "condaqa" / "clean" / f"{split}.jsonl"
    if cq.exists():
        seen: set[str] = set()
        for x in cq.read_text().splitlines():
            if not x.strip():
                continue
            p = json.loads(x).get("passage", "")
            for s in re.split(r"(?<=[.!?])\s+", p):
                if 20 < len(s) < 300 and s not in seen:
                    seen.add(s)
                    out.append(s)
    rng = random.Random(seed)
    rng.shuffle(out)
    return out[:limit]


def _matrix(texts: list[str]):  # type: ignore[no-untyped-def]  # scipy is dev-only
    from scipy.sparse import csr_matrix  # type: ignore[import-untyped]

    rows, cols = [], []
    for i, t in enumerate(texts):
        f = features(normalise(t))
        rows += [i] * len(f)
        cols += f
    data = np.ones(len(rows), dtype=np.float32)
    return csr_matrix((data, (rows, cols)), shape=(len(texts), DIM))


def dataset(claim: Claim, split: str, distractors: list[str]) -> tuple[list[str], list[int]]:
    xs, ys = [], []
    for ex in [*sentences(split, variants=3), *core_sentences(split, n=6)]:
        xs.append(ex.text)
        ys.append(_LABEL[ex.label] if ex.claim == claim else 2)
    xs += distractors
    ys += [2] * len(distractors)
    return xs, ys


def train(public: Path | None = None) -> dict[str, dict[str, float]]:
    from sklearn.linear_model import LogisticRegression  # type: ignore[import-untyped]

    dis_train = public_distractors(public, "train", 4000) if public else []
    dis_dev = public_distractors(public, "dev", 1000) if public else []
    arrays: dict[str, np.ndarray] = {}
    report: dict[str, dict[str, float]] = {}
    for claim in Claim:
        xtr, ytr = dataset(claim, "train", dis_train)
        xdv, ydv = dataset(claim, "dev", dis_dev)
        mtr, mdv = _matrix(xtr), _matrix(xdv)
        best = None
        for c in (0.3, 1.0, 3.0, 10.0):
            m = LogisticRegression(C=c, max_iter=2000, class_weight="balanced")
            m.fit(mtr, ytr)
            acc = float((m.predict(mdv) == np.array(ydv)).mean())
            if best is None or acc > best[0]:
                best = (acc, c, m)
        assert best is not None
        acc, c, m = best
        weights = np.zeros((3, DIM), dtype=np.float32)
        b = np.zeros(3, dtype=np.float32)
        for k, cls in enumerate(m.classes_):
            weights[int(cls)] = m.coef_[k]
            b[int(cls)] = m.intercept_[k]
        arrays[f"{claim.value}_W"] = weights.astype(np.float16)
        arrays[f"{claim.value}_b"] = b
        report[claim.value] = {"dev_accuracy": round(acc, 4), "C": c, "n_train": len(xtr)}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    arrays["classes"] = np.array(CLASSES)
    np.savez_compressed(OUT, **arrays)  # type: ignore[arg-type]
    return report


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--public", default=None, help="public_data directory (optional)")
    a = ap.parse_args()
    print(json.dumps(train(Path(a.public) if a.public else None), indent=1))


if __name__ == "__main__":  # pragma: no cover
    main()
