"""Visa Compelling Evidence 3.0 eligibility, as publicly described.

Rule as reported (docs/SOURCES.md S2-S5; primary rule text not read, see the note
there):

- Only Visa dispute condition 10.4, card-absent fraud (S3).
- At least two prior transactions on the same card with no dispute against them,
  processed more than 120 and fewer than 365 days before the disputed transaction
  (S3, S4).
- Each qualifying prior transaction matches the disputed one on at least two of four
  data elements: user/account id, IP address, shipping address, device id or
  fingerprint; one of the two must be the IP address or the device id (S2).
- A device id must be at least 15 characters to count (S5).

Not modelled (A-28): the October 2026 change reported in S6 (prior transactions
across merchants), fraud-report exclusions, IP-is-public checks, and how issuers
and acquirers exchange the data.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field

from scruple.sim.networks import Network

_FROZEN = ConfigDict(frozen=True, extra="forbid")

CE3_CODE = "10.4"
MIN_AGE_DAYS = 120  # exclusive (S4: "more than 120")
MAX_AGE_DAYS = 365  # exclusive (S4: "fewer than 365")
MIN_PRIORS = 2
MIN_MATCHES = 2
MIN_DEVICE_ID_LEN = 15
ELEMENTS: tuple[str, ...] = ("account_id", "ip", "shipping_address", "device_id")
STRONG: frozenset[str] = frozenset({"ip", "device_id"})


class Fingerprint(BaseModel):
    """The CE 3.0 data elements captured with a transaction."""

    model_config = _FROZEN

    account_id: str | None = None
    ip: str | None = None
    shipping_address: str | None = None
    device_id: str | None = None

    def value(self, element: str) -> str | None:
        v: str | None = getattr(self, element)
        if v is None or v == "":
            return None
        if element == "device_id" and len(v) < MIN_DEVICE_ID_LEN:
            return None
        return v


class PriorTransaction(BaseModel):
    model_config = _FROZEN

    txn_id: str
    processed_on: date
    disputed: bool
    fingerprint: Fingerprint


class PriorCheck(BaseModel):
    model_config = _FROZEN

    txn_id: str
    age_days: int
    matched: tuple[str, ...]
    qualifies: bool
    reason: str


class CE3Result(BaseModel):
    model_config = _FROZEN

    eligible: bool
    reason: str
    qualifying: tuple[str, ...] = ()
    checks: tuple[PriorCheck, ...] = Field(default_factory=tuple)


def matched_elements(a: Fingerprint, b: Fingerprint) -> tuple[str, ...]:
    return tuple(e for e in ELEMENTS if a.value(e) is not None and a.value(e) == b.value(e))


def check_prior(disputed_on: date, current: Fingerprint, prior: PriorTransaction) -> PriorCheck:
    age = (disputed_on - prior.processed_on).days
    matched = matched_elements(current, prior.fingerprint)

    def out(ok: bool, why: str) -> PriorCheck:
        return PriorCheck(
            txn_id=prior.txn_id, age_days=age, matched=matched, qualifies=ok, reason=why
        )

    if prior.disputed:
        return out(False, "prior transaction was disputed")
    if not MIN_AGE_DAYS < age < MAX_AGE_DAYS:
        return out(False, f"processed {age} days before; needs 121-364")
    if len(matched) < MIN_MATCHES:
        return out(False, f"matches {len(matched)} element(s); needs {MIN_MATCHES}")
    if not STRONG & set(matched):
        return out(False, "no IP address or device id match")
    return out(True, "qualifies")


def ce3_eligibility(
    network: Network,
    reason_code: str,
    disputed_on: date,
    current: Fingerprint | None,
    priors: tuple[PriorTransaction, ...],
) -> CE3Result:
    """Whether a dispute meets the CE 3.0 criteria above. `disputed_on` is the disputed
    transaction's processing date."""
    if network is not Network.VISA or reason_code != CE3_CODE:
        return CE3Result(eligible=False, reason="CE 3.0 applies to Visa 10.4 only")
    if current is None:
        return CE3Result(eligible=False, reason="no data elements on the disputed transaction")
    checks = tuple(check_prior(disputed_on, current, p) for p in priors)
    ok = tuple(c.txn_id for c in checks if c.qualifies)
    if len(ok) < MIN_PRIORS:
        return CE3Result(
            eligible=False,
            reason=f"{len(ok)} qualifying prior transaction(s); needs {MIN_PRIORS}",
            qualifying=ok,
            checks=checks,
        )
    return CE3Result(eligible=True, reason="eligible", qualifying=ok, checks=checks)
