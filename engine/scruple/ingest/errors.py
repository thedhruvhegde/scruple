"""Typed errors for everything that reads external data.

Every parser in `scruple.ingest` either returns a value or raises a subclass of
`IngestError`. Nothing else may escape (the hypothesis fuzz tests check this).
"""

from __future__ import annotations


class IngestError(ValueError):
    """Base class: the input was rejected."""


class InputTooLargeError(IngestError):
    """The input exceeds a configured size, depth or count limit."""


class MalformedInputError(IngestError):
    """The bytes are not valid for the declared format (bad JSON, XML, layout)."""


class SchemaMismatchError(IngestError):
    """Well-formed input that does not match the expected schema."""


class UnmappedValueError(IngestError):
    """A value that scruple has no mapping for (for example a new status)."""


class IntegrityError(IngestError):
    """Control totals, hashes or record counts do not agree."""


class SignatureError(IngestError):
    """A signature is missing, malformed, stale or does not match."""


class LiveModeRefusedError(IngestError):
    """Live-mode data or keys were supplied while only test mode is allowed."""
