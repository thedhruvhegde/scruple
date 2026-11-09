"""`scruple ingest verify`: what a case store holds, as suppression-safe aggregates.

Everything per category goes through `scruple.calibration.aggregates.suppress_counts`
(primary and complementary small-cell suppression, default k = 10), so the summary can
be shared with someone who should not see row-level data. Totals are exact. The raw
PAN scan reads every text value in every table of the file and must find nothing.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict

from scruple.calibration.aggregates import CountTable, suppress_counts
from scruple.ingest.store import CaseStore, find_pans
from scruple.security.crypto import CryptoError, FieldCipher


class StoreSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    k: int
    runs: int
    rows_read: int
    rows_failed: int
    row_errors_listed: int
    cases: int
    cases_with_versions: int  # cases imported more than once with new content
    movements: int
    synthetic_runs: int
    by_source: CountTable
    by_state: CountTable
    by_stage: CountTable
    by_reason: CountTable
    by_currency: CountTable
    card_tokens: int
    token_key_ids: CountTable
    encrypted_fields: CountTable
    envelopes_checked: int | None  # None: no field key given
    envelopes_failed: int
    raw_pans_found: int  # Luhn-valid card numbers anywhere in the file's text: must be 0

    @property
    def ok(self) -> bool:
        return self.raw_pans_found == 0 and self.envelopes_failed == 0

    def lines(self) -> list[str]:
        def table(name: str, t: CountTable) -> str:
            if t.withheld:
                return f"  {name}: withheld (fewer than k={self.k} cases)"
            cells = ", ".join(f"{k} {v}" for k, v in t.cells.items())
            tail = f", suppressed {t.suppressed}" if t.suppressed else ""
            return f"  {name}: {cells or '-'}{tail}"

        out = [
            f"runs: {self.runs} ({self.synthetic_runs} synthetic), records read "
            f"{self.rows_read}, failed {self.rows_failed} ({self.row_errors_listed} errors listed)",
            f"cases: {self.cases} ({self.cases_with_versions} with later versions), "
            f"movements: {self.movements}",
            f"aggregates (cells under k={self.k} suppressed):",
            table("source", self.by_source),
            table("state", self.by_state),
            table("stage", self.by_stage),
            table("reason", self.by_reason),
            table("currency", self.by_currency),
            f"card tokens: {self.card_tokens}",
            table("token key ids", self.token_key_ids),
            table("encrypted fields", self.encrypted_fields),
        ]
        if self.envelopes_checked is None:
            out.append("envelopes: not checked (no SCRUPLE_FIELD_KEYS)")
        else:
            out.append(
                f"envelopes: {self.envelopes_checked} checked, {self.envelopes_failed} failed"
            )
        out.append(f"raw card numbers in the file: {self.raw_pans_found}")
        out.append("STORE OK" if self.ok else "STORE PROBLEM")
        return out


def _t(c: Mapping[str, int], k: int) -> CountTable:
    return suppress_counts(dict(c), k)


def summarise(store: CaseStore, *, k: int = 10, cipher: FieldCipher | None = None) -> StoreSummary:
    cases = store.cases()
    runs = store.runs()
    versions = store.versions()
    enc: Counter[str] = Counter()
    checked = failed = 0
    for sc in cases:
        for name, env in sc.encrypted.items():
            enc[name] += 1
            if cipher is not None:
                checked += 1
                try:
                    cipher.decrypt(env, sc.context(name))
                except CryptoError:
                    failed += 1
    return StoreSummary(
        k=k,
        runs=len(runs),
        rows_read=sum(int(r.get("rows_read", 0)) for r in runs),
        rows_failed=sum(int(r.get("rows_failed", 0)) for r in runs),
        row_errors_listed=len(store.row_errors()),
        cases=len(cases),
        cases_with_versions=sum(1 for v in versions.values() if v > 1),
        movements=len(store.movements()),
        synthetic_runs=sum(1 for r in runs if r.get("synthetic")),
        by_source=_t(Counter(sc.case.source for sc in cases), k),
        by_state=_t(Counter(sc.case.state.value for sc in cases), k),
        by_stage=_t(Counter(sc.case.stage.value for sc in cases), k),
        by_reason=_t(Counter(sc.case.source_reason for sc in cases), k),
        by_currency=_t(Counter(sc.case.currency for sc in cases), k),
        card_tokens=sum(1 for sc in cases if sc.card is not None),
        token_key_ids=_t(Counter(sc.card.key_id for sc in cases if sc.card), k),
        encrypted_fields=_t(enc, k),
        envelopes_checked=None if cipher is None else checked,
        envelopes_failed=failed,
        raw_pans_found=sum(_scan(v) for v in store.all_text()),
    )


_HASHES = re.compile(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])")
_ENVELOPES = re.compile(r"enc:v1:[A-Za-z0-9_-]{1,32}:[A-Za-z0-9_-]{1,64}:[A-Za-z0-9_-]+")


def _scan(text: str) -> int:
    """Card numbers in one stored text value. SHA-256 hex (token hashes, input file
    hashes) and encryption envelopes can hold digit runs by chance and are removed
    first; neither is a card number."""
    return find_pans(_HASHES.sub("", _ENVELOPES.sub("", text)))


__all__ = ["StoreSummary", "summarise"]
