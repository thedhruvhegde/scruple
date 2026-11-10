"""Claude Agent SDK adapter: the episode's tools as an in-process MCP server.

Install with `uv sync --extra claude` (package `claude-agent-sdk`). The server is
built with the SDK's `create_sdk_mcp_server` and `tool`, so Claude calls the tools
as `mcp__scruple__<name>`. `agent_options` turns off every built-in tool (files,
shell, web) so the agent can only touch the episode.

Verified offline: the server is driven through an in-memory MCP client in the tests.
Not verified: a live `query()` run, which needs the Claude Code CLI and an API key.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING, Any

from scruple.agents.episode import Episode, EpisodeError
from scruple.agents.models import TASK, EpisodeScore
from scruple.agents.tools import TOOLS, ToolSpec
from scruple.version import package_version

if TYPE_CHECKING:
    from claude_agent_sdk import ClaudeAgentOptions, McpSdkServerConfig, SdkMcpTool

SERVER_NAME = "scruple"


def available() -> bool:
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        return False
    return True


def tool_name(name: str) -> str:
    """The name Claude sees for a tool on this server."""
    return f"mcp__{SERVER_NAME}__{name}"


def _sdk_tool(spec: ToolSpec, episode: Episode) -> SdkMcpTool[Any]:
    from claude_agent_sdk import tool

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        try:
            out = episode.call(spec.name, args)
        except EpisodeError as exc:
            return {
                "content": [{"type": "text", "text": json.dumps({"error": str(exc)})}],
                "is_error": True,
            }
        return {"content": [{"type": "text", "text": json.dumps(out, sort_keys=True)}]}

    return tool(spec.name, spec.description, spec.json_schema())(handler)


def episode_server(episode: Episode) -> McpSdkServerConfig:
    """An in-process MCP server bound to one episode."""
    from claude_agent_sdk import create_sdk_mcp_server

    return create_sdk_mcp_server(
        name=SERVER_NAME, version=package_version(), tools=[_sdk_tool(t, episode) for t in TOOLS]
    )


def agent_options(
    episode: Episode, *, model: str | None = None, max_turns: int = 12
) -> ClaudeAgentOptions:
    from claude_agent_sdk import ClaudeAgentOptions

    return ClaudeAgentOptions(
        tools=[],  # no built-in tools: the episode is the whole world
        mcp_servers={SERVER_NAME: episode_server(episode)},
        allowed_tools=[tool_name(t.name) for t in TOOLS],
        system_prompt=TASK,
        max_turns=max_turns,
        model=model,
    )


QueryFn = Callable[..., AsyncIterator[Any]]


async def run_episode(
    episode: Episode,
    *,
    model: str | None = None,
    max_turns: int = 12,
    query_fn: QueryFn | None = None,
) -> EpisodeScore:
    """Let Claude (through the Agent SDK) play one episode. UNVERIFIED live."""
    if query_fn is None:
        from claude_agent_sdk import query

        query_fn = query
    options = agent_options(episode, model=model, max_turns=max_turns)
    prompt = "Observation:\n" + episode.observe().model_dump_json()
    async for _message in query_fn(prompt=prompt, options=options):
        if episode.closed:
            break
    return episode.score if episode.score is not None else episode.lapse("model_stopped")
