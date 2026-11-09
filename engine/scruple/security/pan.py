"""Card number (PAN) helpers: Luhn check and digit-run extraction.

The Luhn (mod 10) check is ISO/IEC 7812-1 Annex B. PAN length 13 to 19 digits is the
range ISO/IEC 7812 allows for an issuer identification number plus account number.
"""

from __future__ import annotations

PAN_MIN_LEN = 13
PAN_MAX_LEN = 19


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
