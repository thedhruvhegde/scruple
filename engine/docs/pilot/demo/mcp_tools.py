"""Demo: start the scruple MCP servers over stdio and list their tools (synthetic data).

Run from the engine directory:  uv run python docs/pilot/demo/mcp_tools.py

This is what Claude Code does when the plugin in plugins/claude-code is installed:
it launches `scruple mcp sim` and `scruple mcp gate --db ...` as stdio servers.
"""

from __future__ import annotations

import asyncio
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVERS = {
    "scruple-sim": ["-m", "scruple.cli.main", "mcp", "sim"],
    "scruple-gate": ["-m", "scruple.cli.main", "mcp", "--db", ":memory:", "gate"],
}


async def tools(args: list[str]) -> list[str]:
    params = StdioServerParameters(command=sys.executable, args=args)
    async with stdio_client(params) as (read, write), ClientSession(read, write) as s:
        await s.initialize()
        return sorted(t.name for t in (await s.list_tools()).tools)


def main() -> None:
    for name, args in SERVERS.items():
        names = asyncio.run(tools(args))
        print(f"{name}: {len(names)} tools")
        print("  " + ", ".join(names))


if __name__ == "__main__":
    main()
