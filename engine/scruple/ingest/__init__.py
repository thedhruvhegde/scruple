"""Turn external records (processors, bank files, CSV exports) into scruple records.

Every adapter treats its input as hostile: bounded sizes, typed errors only
(`scruple.ingest.errors`), no eval, no XML entities. See docs/INTEGRATIONS.md.
"""

from scruple.ingest.errors import IngestError
from scruple.ingest.models import CaseState, ExternalCase, IngestBatch, LedgerMovement

__all__ = ["CaseState", "ExternalCase", "IngestBatch", "IngestError", "LedgerMovement"]
