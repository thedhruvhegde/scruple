"""Deterministic PII redaction, applied before anything external is stored.

`redact_text` replaces what it finds with fixed placeholders ([EMAIL], [PAN], [IBAN],
[NUMBER], [PHONE], [IP], [ADDRESS], [NAME]); `redact_record` additionally blanks known
PII fields by key name and walks nested dicts and lists. Same input, same output; the
result is idempotent (placeholders contain nothing a detector matches).

Hostile input handling: text is NFKC-normalised first (fullwidth digits and the
fullwidth at sign become ASCII), zero-width and other format characters are removed,
and common obfuscations ("name [at] host [dot] com") are matched. The output is the
normalised text. Every regex quantifier is bounded, and input is capped at
`MAX_TEXT_CHARS`, so hostile text cannot cause catastrophic backtracking.

Limits (honest): names are only found by cue ("my name is", "Mr", "cardholder", ...)
or when the caller passes `known_names`; a bare name in free text is not detected.
Addresses are found by street-suffix, PO box and UK/US postcode patterns only.
"""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict

from scruple.ingest.errors import InputTooLargeError
from scruple.security.pan import looks_like_pan

MAX_TEXT_CHARS = 200_000
MAX_DEPTH = 32

_SEP = r"[ \t\-./\n]"
_AT = r"(?:@|\s{0,3}[\[(]\s{0,3}at\s{0,3}[\])]\s{0,3}|\s{1,3}at\s{1,3})"
_DOT = r"(?:\.|\s{0,3}[\[(]\s{0,3}dot\s{0,3}[\])]\s{0,3}|\s{1,3}dot\s{1,3})"
# Every quantifier is bounded so no pattern can backtrack quadratically (ReDoS tests).
_LOCAL = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]{1,64}(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]{1,64}){0,8}"
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
EMAIL = re.compile(
    rf"{_LOCAL}{_AT}{_LABEL}(?:{_DOT}{_LABEL}){{0,8}}{_DOT}[A-Za-z]{{2,24}}\b", re.IGNORECASE
)
IBAN = re.compile(r"\b[A-Z]{2}[0-9]{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,4})?\b")
DIGIT_RUN = re.compile(rf"(?<![0-9])[0-9](?:{_SEP}?[0-9]){{11,22}}(?![0-9])")
PHONE = re.compile(
    r"(?<![\w+])(?:\+[0-9][0-9 ().\-]{6,20}[0-9]"
    r"|\(?[0-9]{3}\)?[ .\-]?[0-9]{3}[ .\-][0-9]{4}"
    r"|0[0-9]{2,4}[ .\-]?[0-9]{3,4}[ .\-]?[0-9]{3,4})(?![\w])"
)
IPV4 = re.compile(r"(?<![0-9.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9.])")
IPV6 = re.compile(r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![0-9A-Fa-f:])")
_SUFFIX = (
    r"(?:street|st|avenue|ave|road|rd|boulevard|blvd|lane|ln|drive|dr|court|ct|way|place|pl"
    r"|terrace|ter|highway|hwy|parkway|pkwy|circle|cir|square|sq|close|crescent|cres)"
)
STREET = re.compile(
    rf"\b[0-9]{{1,6}}[A-Za-z]?(?:\s{{1,3}}[A-Za-z0-9'.\-]{{1,40}}){{1,5}}\s{{1,3}}{_SUFFIX}\b\.?"
    r"(?:,?\s{1,3}(?:apt|apartment|suite|ste|unit|flat|#)\s{0,3}[#]?[A-Za-z0-9\-]{1,10})?",
    re.IGNORECASE,
)
PO_BOX = re.compile(r"\bp\.?\s?o\.?\s*box\s+[0-9]+\b", re.IGNORECASE)
UK_POSTCODE = re.compile(r"\b[A-Z]{1,2}[0-9][A-Z0-9]?\s*[0-9][A-Z]{2}\b")
US_ZIP = re.compile(r"\b[A-Z]{2}\s+[0-9]{5}(?:-[0-9]{4})?\b")
_CAP = r"[A-Z][a-zA-Z'\-]{1,40}"
NAME_CUE = re.compile(
    r"\b(?i:(?:(?:my|his|her|their|customer|cardholder|account holder|payee|recipient)"
    r"(?:'s)?\s{1,3}name\s{1,3}(?:is|was|:)\s{0,3}|name\s{0,3}:\s{0,3}"
    r"|attn\s{0,3}:?\s{0,3}|signed by\s{1,3}"
    r"|(?:mr|mrs|ms|miss|mx|dr|prof|sir)\.?\s{1,3}|cardholder\s{1,3}|customer\s{1,3}))"
    rf"({_CAP}(?:\s{{1,3}}{_CAP}){{0,3}})"
)
FORMAT_CHARS = re.compile(
    "[\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180e\u200b-\u200f"
    "\u202a-\u202e\u2060-\u2064\u206a-\u206f\ufeff]"
)

# Known PII field names (normalised: lower case, separators removed).
PII_FIELDS: Mapping[str, str] = {
    **dict.fromkeys(
        [
            "name",
            "firstname",
            "lastname",
            "fullname",
            "givenname",
            "familyname",
            "surname",
            "customername",
            "cardholdername",
            "billingname",
            "shippingname",
            "accountholdername",
            "individualname",
            "receivername",
            "payeename",
            "nameoncard",
        ],
        "NAME",
    ),
    **dict.fromkeys(
        [
            "email",
            "emailaddress",
            "customeremail",
            "customeremailaddress",
            "receiptemail",
            "shopperemail",
        ],
        "EMAIL",
    ),
    **dict.fromkeys(["phone", "phonenumber", "telephone", "mobile", "shopperphone"], "PHONE"),
    **dict.fromkeys(
        [
            "address",
            "billingaddress",
            "shippingaddress",
            "line1",
            "line2",
            "street",
            "streetaddress",
            "postalcode",
            "postcode",
            "zip",
            "zipcode",
            "housenumberorname",
        ],
        "ADDRESS",
    ),
    **dict.fromkeys(
        [
            "pan",
            "cardnumber",
            "number",
            "accountnumber",
            "dfiaccountnumber",
            "iban",
            "bankaccountnumber",
        ],
        "NUMBER",
    ),
    **dict.fromkeys(["ip", "ipaddress", "customerpurchaseip", "shopperip"], "IP"),
}


class Finding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str
    start: int  # offsets in the normalised text
    end: int


class Redaction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    findings: tuple[Finding, ...]

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in self.findings:
            out[f.kind] = out.get(f.kind, 0) + 1
        return out


def normalise(text: str) -> str:
    return FORMAT_CHARS.sub("", unicodedata.normalize("NFKC", text))


def _iban_ok(raw: str) -> bool:
    s = raw.replace(" ", "")
    if not 15 <= len(s) <= 34:
        return False
    moved = s[4:] + s[:4]
    digits = "".join(str(int(c, 36)) for c in moved)
    return int(digits) % 97 == 1


def _ip_ok(raw: str) -> bool:
    try:
        ipaddress.ip_address(raw)
    except ValueError:
        return False
    return True


def _spans(text: str, known_names: Sequence[str]) -> list[tuple[int, int, str]]:
    found: list[tuple[int, int, str]] = []
    for m in EMAIL.finditer(text):
        found.append((m.start(), m.end(), "EMAIL"))
    for m in IBAN.finditer(text):
        if _iban_ok(m.group()):
            found.append((m.start(), m.end(), "IBAN"))
    for m in DIGIT_RUN.finditer(text):
        digits = re.sub(r"[^0-9]", "", m.group())
        found.append((m.start(), m.end(), "PAN" if looks_like_pan(digits) else "NUMBER"))
    for m in PHONE.finditer(text):
        if sum(c.isdigit() for c in m.group()) >= 7:
            found.append((m.start(), m.end(), "PHONE"))
    for rx in (IPV4, IPV6):
        for m in rx.finditer(text):
            if _ip_ok(m.group()):
                found.append((m.start(), m.end(), "IP"))
    for rx in (STREET, PO_BOX, UK_POSTCODE, US_ZIP):
        for m in rx.finditer(text):
            found.append((m.start(), m.end(), "ADDRESS"))
    for m in NAME_CUE.finditer(text):
        found.append((m.start(1), m.end(1), "NAME"))
    for name in known_names:
        n = normalise(name).strip()
        if len(n) < 2:
            continue
        parts = [re.escape(p) for p in n.split()]
        rx = re.compile(r"(?<!\w)" + r"[\s\W]{0,3}".join(parts) + r"(?!\w)", re.IGNORECASE)
        for m in rx.finditer(text):
            found.append((m.start(), m.end(), "NAME"))
        for p in n.split():
            if len(p) >= 3:
                for m in re.finditer(r"(?<!\w)" + re.escape(p) + r"(?!\w)", text, re.IGNORECASE):
                    found.append((m.start(), m.end(), "NAME"))
    return found


def redact_text(text: str, *, known_names: Iterable[str] = ()) -> Redaction:
    """Redact PII in free text. Deterministic; raises `InputTooLargeError` on huge input."""
    if len(text) > MAX_TEXT_CHARS:
        raise InputTooLargeError(f"text longer than {MAX_TEXT_CHARS} characters")
    norm = normalise(text)
    spans = _spans(norm, tuple(known_names))
    # Longest match wins; ties go to the earlier start, then the detector order above.
    kinds = ["EMAIL", "IBAN", "PAN", "PHONE", "NUMBER", "IP", "ADDRESS", "NAME"]
    order = {k: i for i, k in enumerate(kinds)}
    spans.sort(key=lambda s: (-(s[1] - s[0]), s[0], order[s[2]]))
    taken: list[tuple[int, int, str]] = []
    for s in spans:
        if all(s[1] <= t[0] or s[0] >= t[1] for t in taken):
            taken.append(s)
    taken.sort()
    out: list[str] = []
    pos = 0
    for start, end, kind in taken:
        out.append(norm[pos:start])
        out.append(f"[{kind}]")
        pos = end
    out.append(norm[pos:])
    return Redaction(
        text="".join(out),
        findings=tuple(Finding(kind=k, start=a, end=b) for a, b, k in taken),
    )


def _field_kind(key: str) -> str | None:
    return PII_FIELDS.get(re.sub(r"[^a-z0-9]", "", key.lower()))


def redact_record(record: Any, *, known_names: Iterable[str] = (), _depth: int = 0) -> Any:
    """Redact a JSON-like value: known PII keys are blanked, other strings are scanned.

    Integers that look like card numbers are replaced too. Booleans, floats and None are
    kept. Depth is bounded.
    """
    if _depth > MAX_DEPTH:
        raise InputTooLargeError("record nested too deeply to redact")
    names = tuple(known_names)
    if isinstance(record, Mapping):
        out: dict[str, Any] = {}
        for k, v in record.items():
            key = str(k)
            kind = _field_kind(key)
            if kind is not None and v not in (None, "", [], {}):
                out[key] = f"[{kind}]"
            else:
                out[key] = redact_record(v, known_names=names, _depth=_depth + 1)
        return out
    if isinstance(record, list | tuple):
        return [redact_record(v, known_names=names, _depth=_depth + 1) for v in record]
    if isinstance(record, str):
        return redact_text(record, known_names=names).text
    if (
        isinstance(record, int)
        and not isinstance(record, bool)
        and looks_like_pan(str(abs(record)))
    ):
        return "[PAN]"
    return record
