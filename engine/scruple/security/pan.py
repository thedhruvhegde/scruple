"""Card numbers (PANs): Luhn check, keyed tokenisation, and scrubbing for logs.

The Luhn (mod 10) check is ISO/IEC 7812-1 Annex B. PAN length 13 to 19 digits is the
range ISO/IEC 7812 allows for an issuer identification number plus account number.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import unicodedata
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

from scruple.ingest.errors import IngestError

PAN_MIN_LEN = 13
PAN_MAX_LEN = 19

# Invisible format characters (zero-width, bidi controls, soft hyphen) that attackers
# insert to split a number; removed before matching.
FORMAT_CHARS = re.compile(
    "[\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180e\u200b-\u200f"
    "\u202a-\u202e\u2060-\u2064\u206a-\u206f\ufeff]"
)
_SEP = r"[ \t\-./\n]"
DIGIT_RUN = re.compile(rf"(?<![0-9])[0-9](?:{_SEP}?[0-9]){{11,22}}(?![0-9])")


def luhn_valid(digits: str) -> bool:
    """True if `digits` (ASCII digits only) passes the Luhn check."""
    if not digits or not digits.isascii() or not digits.isdigit():
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def luhn_check_digit(partial: str) -> str:
    """The digit that makes `partial + digit` Luhn-valid."""
    for d in "0123456789":
        if luhn_valid(partial + d):
            return d
    raise ValueError("no check digit")  # unreachable for digit strings


def looks_like_pan(digits: str) -> bool:
    return PAN_MIN_LEN <= len(digits) <= PAN_MAX_LEN and luhn_valid(digits)


# --- tokenisation -----------------------------------------------------------------


class InvalidPanError(IngestError):
    """The value is not a plausible card number. The message never contains the input."""


def normalise_pan(raw: str) -> str:
    """Digits only: NFKC (fullwidth digits), then drop spaces, dashes and format chars."""
    if len(raw) > 64:
        raise InvalidPanError("card number input is too long")
    s = FORMAT_CHARS.sub("", unicodedata.normalize("NFKC", raw))
    s = re.sub(r"[ \-.\t]", "", s)
    if not s.isascii() or not s.isdigit():
        raise InvalidPanError("card number must contain only digits and separators")
    if not PAN_MIN_LEN <= len(s) <= PAN_MAX_LEN:
        raise InvalidPanError(f"card number must have {PAN_MIN_LEN} to {PAN_MAX_LEN} digits")
    if not luhn_valid(s):
        raise InvalidPanError("card number fails the Luhn check")
    return s


class CardToken(BaseModel):
    """What scruple keeps instead of a PAN.

    ASSUMPTION: first six and last four digits are kept, the common truncation format;
    whether that is acceptable for a given deployment is a compliance decision.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    token: str = Field(pattern=r"^ctok_[0-9a-f]{64}$")
    key_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,32}$")
    bin6: str = Field(pattern=r"^[0-9]{6}$")
    last4: str = Field(pattern=r"^[0-9]{4}$")
    length: int = Field(ge=PAN_MIN_LEN, le=PAN_MAX_LEN)


class PanTokenizer:
    """Keyed HMAC-SHA256 tokenisation with key ids, for joining without storing PANs.

    The same PAN and key always give the same token (so disputes on one card can be
    linked); without the key a token cannot be reversed or brute-forced offline the
    way an unkeyed hash of a 16-digit PAN can. Keys are at least 32 bytes and never
    appear in `repr`, logs or errors.
    """

    def __init__(self, keys: Mapping[str, bytes], active: str) -> None:
        if active not in keys:
            raise ValueError("active key id is not among the keys")
        for kid, key in keys.items():
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", kid):
                raise ValueError("key ids are 1-32 letters, digits, '_' or '-'")
            if len(key) < 32:
                raise ValueError("tokenisation keys must be at least 32 bytes")
        self._keys = dict(keys)
        self._active = active

    def __repr__(self) -> str:
        return f"PanTokenizer(active={self._active!r}, key_ids={sorted(self._keys)!r})"

    def _mac(self, key_id: str, digits: str) -> str:
        return hmac.new(self._keys[key_id], digits.encode("ascii"), hashlib.sha256).hexdigest()

    def tokenize(self, raw: str, *, key_id: str | None = None) -> CardToken:
        kid = key_id or self._active
        if kid not in self._keys:
            raise InvalidPanError("unknown tokenisation key id")
        digits = normalise_pan(raw)
        tok = CardToken(
            token=f"ctok_{self._mac(kid, digits)}",
            key_id=kid,
            bin6=digits[:6],
            last4=digits[-4:],
            length=len(digits),
        )
        log.debug("tokenised card ****%s with key %s", tok.last4, kid)
        return tok

    def matches(self, token: CardToken, raw: str) -> bool:
        """Constant-time check that `raw` is the card behind `token`."""
        if token.key_id not in self._keys:
            return False
        try:
            digits = normalise_pan(raw)
        except InvalidPanError:
            return False
        expected = f"ctok_{self._mac(token.key_id, digits)}"
        return hmac.compare_digest(expected, token.token)

    def rotate(self, token: CardToken, raw: str) -> CardToken:
        """Re-issue a token under the active key; needs the PAN, so only at ingest time."""
        if not self.matches(token, raw):
            raise InvalidPanError("card number does not match the token")
        return self.tokenize(raw)


class PanScrubbingFilter(logging.Filter):
    """Logging filter that redacts card numbers from messages and arguments."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except (TypeError, ValueError):
            msg = str(record.msg)
        record.msg = scrub_pans(msg)
        record.args = None
        return True


def scrub_pans(text: str) -> str:
    """Replace every Luhn-valid 13-19 digit run (with separators) by [PAN]."""
    norm = FORMAT_CHARS.sub("", unicodedata.normalize("NFKC", text))

    def sub(m: re.Match[str]) -> str:
        digits = re.sub(r"[^0-9]", "", m.group())
        return "[PAN]" if looks_like_pan(digits) else m.group()

    return DIGIT_RUN.sub(sub, norm)


log = logging.getLogger("scruple.security.pan")
log.addFilter(PanScrubbingFilter())
