"""Tenant admin operations shared by the API and the CLI."""

from __future__ import annotations

from scruple.integrations.config import IntegrationSettings
from scruple.integrations.store import IntegrationStore
from scruple.integrations.webhooks import start_cursor
from scruple.service import AgentGate


def set_settings(gate: AgentGate, body: IntegrationSettings, actor: str) -> int:
    """Put integration settings in force (a new version, audited). Raises ValueError
    when a named secret is not the tenant's own."""
    body.check_secret_names(gate.storage.tenant)
    store = IntegrationStore(gate.storage).ensure()
    with gate.storage.transaction():
        start_cursor(gate, store)
        n = store.put_settings(body, actor, gate.clock.now())
        gate.record_runtime_event(
            "integration.settings_set",
            actor,
            {
                "number": n,
                "slack": body.slack is not None,
                "jira": body.jira is not None,
                "linear": body.linear is not None,
            },
        )
    return n


__all__ = ["set_settings"]
