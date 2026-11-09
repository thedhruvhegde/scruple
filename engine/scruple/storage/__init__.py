"""Persistence for rules, audit entries and decisions.

`Storage` is the facade every caller depends on; it is composed of one repository
protocol per aggregate (see `scruple.storage.base`).
"""

from __future__ import annotations

from scruple.storage.base import (
    AuditRepository,
    DecisionRepository,
    RuleVersionRepository,
    Storage,
    Transactional,
)
from scruple.storage.sqlite import SCHEMA, SQLiteStorage

__all__ = [
    "SCHEMA",
    "AuditRepository",
    "DecisionRepository",
    "RuleVersionRepository",
    "SQLiteStorage",
    "Storage",
    "Transactional",
]
