"""Claude Code plugin layout, manifests, and the `scruple mcp` commands they wire up."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client, StdioServerParameters

from scruple.agents.cli import cmd_mcp
from scruple.cli.main import build_parser

ROOT = Path(__file__).resolve().parent.parent
PLUGINS = ROOT / "plugins"
PLUGIN = PLUGINS / "claude-code"
KEBAB = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def load(path: Path) -> Any:
    return json.loads(path.read_text())


def test_layout() -> None:
    assert (PLUGIN / ".claude-plugin" / "plugin.json").is_file()
    assert (PLUGIN / ".mcp.json").is_file()
    assert (PLUGIN / "commands" / "scruple-gate.md").is_file()
    assert (PLUGIN / "commands" / "scruple-episode.md").is_file()
    # Only the manifest lives inside .claude-plugin/.
    assert [p.name for p in (PLUGIN / ".claude-plugin").iterdir()] == ["plugin.json"]


@pytest.mark.parametrize("path", sorted(PLUGINS.rglob("*.json")), ids=lambda p: p.name)
def test_every_json_file_parses(path: Path) -> None:
    assert isinstance(load(path), dict)


def test_plugin_manifest() -> None:
    m = load(PLUGIN / ".claude-plugin" / "plugin.json")
    assert KEBAB.match(m["name"])
    assert re.match(r"^\d+\.\d+\.\d+$", m["version"])
    assert m["description"]
    assert set(m["author"]) <= {"name", "email", "url"}
    assert m["author"]["name"] == "thedhruvhegde"
    assert set(m) <= {
        "name",
        "version",
        "description",
        "author",
        "keywords",
        "homepage",
        "repository",
        "license",
        "commands",
        "agents",
        "hooks",
        "mcpServers",
    }


def test_marketplace_points_at_the_plugin() -> None:
    mk = load(PLUGINS / ".claude-plugin" / "marketplace.json")
    assert KEBAB.match(mk["name"])
    assert mk["owner"]["name"]
    (entry,) = mk["plugins"]
    assert (PLUGINS / entry["source"]).resolve() == PLUGIN
    assert entry["name"] == load(PLUGIN / ".claude-plugin" / "plugin.json")["name"]


def scruple_argv(server: dict[str, Any]) -> list[str]:
    args: list[str] = server["args"]
    return args[args.index("scruple") + 1 :]


def test_mcp_servers_wire_real_cli_commands() -> None:
    servers = load(PLUGIN / ".mcp.json")["mcpServers"]
    assert set(servers) == {"scruple-sim", "scruple-gate"}
    for name, server in servers.items():
        assert server["command"] == "uv"
        assert server["args"][:3] == ["run", "--project", "${CLAUDE_PLUGIN_ROOT}/../.."]
        ns = build_parser().parse_args(scruple_argv(server))
        assert ns.fn is cmd_mcp
        assert ns.server == name.removeprefix("scruple-")
    # The project the plugin points at is this repository.
    assert (PLUGIN / ".." / ".." / "pyproject.toml").resolve() == ROOT / "pyproject.toml"


@pytest.mark.parametrize("path", sorted((PLUGIN / "commands").glob("*.md")), ids=lambda p: p.stem)
def test_commands_have_frontmatter(path: Path) -> None:
    text = path.read_text()
    assert text.startswith("---\n")
    front, body = text[4:].split("\n---\n", 1)
    fields = dict(line.split(": ", 1) for line in front.splitlines())
    assert set(fields) <= {"description", "argument-hint", "allowed-tools", "model"}
    assert fields["description"]
    assert "$ARGUMENTS" in body
    assert "synthetic" in body
    assert KEBAB.match(path.stem)


def test_gate_command_never_approves() -> None:
    body = (PLUGIN / "commands" / "scruple-gate.md").read_text()
    assert "Do not try to approve" in body


@pytest.mark.parametrize(("server", "extra"), [("sim", []), ("gate", ["--db", ":memory:"])])
def test_mcp_cli_parses(server: str, extra: list[str]) -> None:
    ns = build_parser().parse_args(["mcp", server, *extra])
    assert ns.fn is cmd_mcp
    assert ns.server == server


def test_mcp_cli_rejects_unknown_server() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["mcp", "bank"])


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["mcp", "sim"], "start_episode"),
        (["mcp", "gate", "--db", ":memory:"], "submit_rule"),
    ],
)
def test_stdio_servers_start(argv: list[str], expected: str) -> None:
    """Launch the real stdio server as a subprocess and list its tools."""
    params = StdioServerParameters(command=sys.executable, args=["-m", "scruple.cli.main", *argv])

    async def main() -> set[str]:
        with anyio.fail_after(60):
            async with Client(params) as c:
                return {t.name for t in (await c.list_tools()).tools}

    names = anyio.run(main)
    assert expected in names
