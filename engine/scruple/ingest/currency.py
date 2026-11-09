"""ISO 4217 currency codes and their minor units (decimal exponent).

Source: datasets/currency-codes, data/codes-all.csv at 31cfa1380316 (fetched 2026-11-09),
https://github.com/datasets/currency-codes (ODC-PDDL-1.0 for that selection; the list
itself is published by SIX Group, the ISO 4217 maintenance agency). Only currencies with
no withdrawal date and a numeric minor unit are kept.

Note: processors may define "smallest unit" differently from ISO for a few currencies.
Adapters that receive minor units from a processor keep that processor's integers as-is;
this table is only used to parse decimal strings (bank files, CSV) and to validate codes.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Annotated

from pydantic import AfterValidator

from scruple.ingest.errors import SchemaMismatchError, UnmappedValueError

# fmt: off
MINOR_UNITS: dict[str, int] = {
    "AED": 2, "AFN": 2, "ALL": 2, "AMD": 2, "AOA": 2, "ARS": 2, "AUD": 2, "AWG": 2, "AZN": 2,
    "BAM": 2, "BBD": 2, "BDT": 2, "BHD": 3, "BIF": 0, "BMD": 2, "BND": 2, "BOB": 2, "BOV": 2,
    "BRL": 2, "BSD": 2, "BTN": 2, "BWP": 2, "BYN": 2, "BZD": 2, "CAD": 2, "CDF": 2, "CHE": 2,
    "CHF": 2, "CHW": 2, "CLF": 4, "CLP": 0, "CNY": 2, "COP": 2, "COU": 2, "CRC": 2, "CUP": 2,
    "CVE": 2, "CZK": 2, "DJF": 0, "DKK": 2, "DOP": 2, "DZD": 2, "EGP": 2, "ERN": 2, "ETB": 2,
    "EUR": 2, "FJD": 2, "FKP": 2, "GBP": 2, "GEL": 2, "GHS": 2, "GIP": 2, "GMD": 2, "GNF": 0,
    "GTQ": 2, "GYD": 2, "HKD": 2, "HNL": 2, "HTG": 2, "HUF": 2, "IDR": 2, "ILS": 2, "INR": 2,
    "IQD": 3, "IRR": 2, "ISK": 0, "JMD": 2, "JOD": 3, "JPY": 0, "KES": 2, "KGS": 2, "KHR": 2,
    "KMF": 0, "KPW": 2, "KRW": 0, "KWD": 3, "KYD": 2, "KZT": 2, "LAK": 2, "LBP": 2, "LKR": 2,
    "LRD": 2, "LSL": 2, "LYD": 3, "MAD": 2, "MDL": 2, "MGA": 2, "MKD": 2, "MMK": 2, "MNT": 2,
    "MOP": 2, "MRU": 2, "MUR": 2, "MVR": 2, "MWK": 2, "MXN": 2, "MXV": 2, "MYR": 2, "MZN": 2,
    "NAD": 2, "NGN": 2, "NIO": 2, "NOK": 2, "NPR": 2, "NZD": 2, "OMR": 3, "PAB": 2, "PEN": 2,
    "PGK": 2, "PHP": 2, "PKR": 2, "PLN": 2, "PYG": 0, "QAR": 2, "RON": 2, "RSD": 2, "RUB": 2,
    "RWF": 0, "SAR": 2, "SBD": 2, "SCR": 2, "SDG": 2, "SEK": 2, "SGD": 2, "SHP": 2, "SLE": 2,
    "SOS": 2, "SRD": 2, "SSP": 2, "STN": 2, "SVC": 2, "SYP": 2, "SZL": 2, "THB": 2, "TJS": 2,
    "TMT": 2, "TND": 3, "TOP": 2, "TRY": 2, "TTD": 2, "TWD": 2, "TZS": 2, "UAH": 2, "UGX": 0,
    "USD": 2, "USN": 2, "UYI": 0, "UYU": 2, "UYW": 4, "UZS": 2, "VED": 2, "VES": 2, "VND": 0,
    "VUV": 0, "WST": 2, "XAD": 2, "XAF": 0, "XCD": 2, "XCG": 2, "XOF": 0, "XPF": 0, "YER": 2,
    "ZAR": 2, "ZMW": 2, "ZWG": 2,
}
# fmt: on

_AMOUNT_CHARS = frozenset("+-0123456789.")


def minor_units(currency: str) -> int:
    """The ISO 4217 exponent for `currency` (upper case). Unknown codes are rejected."""
    try:
        return MINOR_UNITS[currency]
    except KeyError:
        raise UnmappedValueError(f"unknown ISO 4217 currency {currency[:8]!r}") from None


def normalise_currency(raw: str) -> str:
    """Upper-case and validate a three-letter code ('usd' -> 'USD')."""
    code = raw.strip().upper()
    if len(code) != 3 or not code.isascii() or not code.isalpha():
        raise SchemaMismatchError(f"currency {raw[:16]!r} is not a three-letter code")
    minor_units(code)
    return code


def _check(v: str) -> str:
    if v not in MINOR_UNITS:
        raise ValueError(f"currency {v[:8]!r} must be an upper-case ISO 4217 code")
    return v


Currency = Annotated[str, AfterValidator(_check)]


def decimal_to_minor(text: str, currency: str, *, max_digits: int = 18) -> int:
    """Parse a plain decimal string ('12.34', '-5') into minor units of `currency`.

    Exact: never goes through float. Thousands separators, exponents and more
    fractional digits than the currency allows are errors, not silent rounding.
    """
    s = text.strip()
    if not s or len(s) > max_digits + 2 or not set(s) <= _AMOUNT_CHARS:
        raise SchemaMismatchError(f"amount {text[:32]!r} is not a plain decimal")
    try:
        d = Decimal(s)
    except InvalidOperation:
        raise SchemaMismatchError(f"amount {text[:32]!r} is not a decimal") from None
    exp = minor_units(currency)
    scaled = d.scaleb(exp)
    if scaled != scaled.to_integral_value():
        raise SchemaMismatchError(f"amount {s!r} has more than {exp} decimal places for {currency}")
    return int(scaled)


def minor_to_decimal(amount_minor: int, currency: str) -> str:
    """Inverse of `decimal_to_minor`, canonical form ('-12.30' for -1230 USD)."""
    exp = minor_units(currency)
    q = Decimal(amount_minor).scaleb(-exp)
    return f"{q:.{exp}f}"
