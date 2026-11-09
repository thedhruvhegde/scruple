"""LangGraph adapter (optional: `uv sync --extra langgraph`).

Two ways in:

- `build_graph(episode, model)`: a two-node `StateGraph` (model -> tools -> model ...)
  around any `model(messages) -> ModelStep` callable. Use it to wrap whatever model
  client you already have; the graph runs the episode's tools and stops on
  `submit_decision`, on a turn without tool calls, or at `max_turns`.
- `langchain_tools(episode)`: the episode's tools as LangChain `StructuredTool`s,
  for prebuilt agents such as `langgraph.prebuilt.create_react_agent`.

If langgraph is not installed, importing this module still works; `available()`
returns False and the builders raise ImportError with an install hint.
"""

from __future__ import annotations

import json
import operator
from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated, Any, TypedDict

from scruple.agents.episode import Episode, EpisodeError
from scruple.agents.models import TASK, EpisodeScore
from scruple.agents.tools import TOOLS, ToolSpec

if TYPE_CHECKING:
    from langchain_core.tools import StructuredTool

INSTALL_HINT = "langgraph is not installed: run `uv sync --extra langgraph`"


class ToolCall(TypedDict):
    id: str
    name: str
    args: Any


class ModelStep(TypedDict, total=False):
    text: str
    tool_calls: list[ToolCall]


ModelFn = Callable[[list[dict[str, Any]]], ModelStep]


class EpisodeState(TypedDict):
    messages: Annotated[list[dict[str, Any]], operator.add]
    turns: int


def available() -> bool:
    try:
        import langgraph.graph  # noqa: F401
    except ImportError:
        return False
    return True


def _run_tool(episode: Episode, name: str, args: Any) -> tuple[str, bool]:
    try:
        return json.dumps(episode.call(name, args), sort_keys=True), False
    except EpisodeError as exc:
        return json.dumps({"error": str(exc)}), True


def build_graph(episode: Episode, model: ModelFn, *, max_turns: int = 12) -> Any:
    """A compiled LangGraph graph that plays `episode` with `model`."""
    try:
        from langgraph.graph import END, START, StateGraph
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise ImportError(INSTALL_HINT) from exc

    def model_node(state: EpisodeState) -> dict[str, Any]:
        step = model(state["messages"])
        msg = {
            "role": "assistant",
            "content": step.get("text", ""),
            "tool_calls": list(step.get("tool_calls", [])),
        }
        return {"messages": [msg], "turns": state["turns"] + 1}

    def tools_node(state: EpisodeState) -> dict[str, Any]:
        out = []
        for call in state["messages"][-1]["tool_calls"]:
            content, is_error = _run_tool(episode, str(call.get("name")), call.get("args"))
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "name": call.get("name"),
                    "content": content,
                    "is_error": is_error,
                }
            )
        return {"messages": out}

    def after_model(state: EpisodeState) -> str:
        if not state["messages"][-1]["tool_calls"]:
            return END
        return "tools"

    def after_tools(state: EpisodeState) -> str:
        if episode.closed or state["turns"] >= max_turns:
            return END
        return "model"

    graph = StateGraph(EpisodeState)
    graph.add_node("model", model_node)
    graph.add_node("tools", tools_node)
    graph.add_edge(START, "model")
    graph.add_conditional_edges("model", after_model, ["tools", END])
    graph.add_conditional_edges("tools", after_tools, ["model", END])
    return graph.compile()


def run_episode(episode: Episode, model: ModelFn, *, max_turns: int = 12) -> EpisodeScore:
    graph = build_graph(episode, model, max_turns=max_turns)
    start: EpisodeState = {
        "messages": [
            {"role": "system", "content": TASK},
            {"role": "user", "content": "Observation:\n" + episode.observe().model_dump_json()},
        ],
        "turns": 0,
    }
    final = graph.invoke(start, {"recursion_limit": 2 * max_turns + 5})
    if episode.score is not None:
        return episode.score
    reason = "max_turns" if final["turns"] >= max_turns else "model_stopped"
    return episode.lapse(reason)


def _structured(spec: ToolSpec, episode: Episode) -> StructuredTool:
    from langchain_core.tools import StructuredTool

    def fn(**kwargs: Any) -> str:
        return _run_tool(episode, spec.name, kwargs)[0]

    return StructuredTool.from_function(
        func=fn, name=spec.name, description=spec.description, args_schema=spec.args
    )


def langchain_tools(episode: Episode) -> list[StructuredTool]:
    """The episode's tools as LangChain tools (errors come back as JSON text)."""
    try:
        import langchain_core.tools  # noqa: F401
    except ImportError as exc:  # pragma: no cover
        raise ImportError(INSTALL_HINT) from exc
    return [_structured(t, episode) for t in TOOLS]
