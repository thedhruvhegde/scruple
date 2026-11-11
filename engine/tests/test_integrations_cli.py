"""`scruple integrations ...` as the local admin (no network: an IP-literal endpoint)."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from scruple.cli.main import main
from scruple.integrations.store import IntegrationStore
from scruple.service import AgentGate
from scruple.storage import SQLiteStorage

KEY = base64.urlsafe_b64encode(b"\x03" * 32).decode()


def test_integrations_commands(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SCRUPLE_DATABASE_URL", raising=False)
    monkeypatch.delenv("SCRUPLE_TENANT", raising=False)
    monkeypatch.setenv("SCRUPLE_FIELD_KEYS", f"k1:{KEY}")
    db = str(tmp_path / "g.db")
    g = AgentGate(SQLiteStorage(db))
    g.bootstrap_baseline()
    settings = tmp_path / "integrations.yaml"
    settings.write_text(
        "jira:\n  base_url: https://acme.atlassian.net\n  email: bot@acme.example\n"
        "  api_token_env: SCRUPLE_SECRET_DEFAULT_JIRA\n  project_key: RISK\n"
    )
    assert main(["integrations", "settings", "set", str(settings), "--db", db]) == 0
    assert "#1 in force" in capsys.readouterr().out
    assert main(["integrations", "settings", "show", "--db", db]) == 0
    assert json.loads(capsys.readouterr().out)["jira"]["project_key"] == "RISK"
    foreign = tmp_path / "foreign.yaml"
    foreign.write_text(settings.read_text().replace("_DEFAULT_", "_BANK_B_"))
    assert main(["integrations", "settings", "set", str(foreign), "--db", db]) == 1
    assert "SCRUPLE_SECRET_DEFAULT_" in capsys.readouterr().err

    url = "https://93.184.215.14/hook"
    assert (
        main(["integrations", "webhooks", "add", url, "--db", db, "--event", "decision.recorded"])
        == 0
    )
    out = capsys.readouterr()
    assert out.out.strip().startswith("whsec_") and "shown only once" in out.err
    assert main(["integrations", "webhooks", "add", "https://10.0.0.1/", "--db", db]) == 1
    assert "refused" in capsys.readouterr().err
    assert main(["integrations", "webhooks", "list", "--db", db]) == 0
    (ep,) = json.loads(capsys.readouterr().out)
    assert ep["url"] == url and ep["events"] == ["decision.recorded"]
    eid = ep["endpoint_id"]
    assert main(["integrations", "webhooks", "rotate", eid, "--db", db]) == 0
    assert capsys.readouterr().out.startswith("whsec_")
    assert main(["integrations", "webhooks", "disable", eid, "--db", db]) == 0
    assert json.loads(capsys.readouterr().out)["active"] is False
    assert main(["integrations", "webhooks", "enable", eid, "--db", db]) == 0
    capsys.readouterr()

    g.rollback(g.registry.active_rules()[0].rule_id, "model-risk@demo.bank", "cli test")
    assert main(["integrations", "dispatch", "--db", db]) == 0
    res = json.loads(capsys.readouterr().out)
    assert res["events"] == 1 and res["jobs"] == 2  # one webhook, one jira ticket
    assert main(["integrations", "deliveries", "--db", db, "--status", "pending"]) == 0
    (d,) = json.loads(capsys.readouterr().out)
    assert main(["integrations", "replay", d["delivery_id"], "--db", db]) == 0
    assert "replay 1" in capsys.readouterr().out
    assert main(["integrations", "replay", "whd_" + "0" * 24, "--db", db]) == 1
    capsys.readouterr()
    assert IntegrationStore(SQLiteStorage(db)).delivery(d["delivery_id"]) is not None
