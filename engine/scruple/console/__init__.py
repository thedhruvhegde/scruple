"""Web console (roadmap 181-184): runs and scores, case viewer, diffs, approvals inbox.

Server-rendered (FastAPI router + Jinja2 templates + one small script). See
docs/CONSOLE.md. All data shown is synthetic unless a page says otherwise.
"""

from scruple.console.routes import PREFIX, console_router
from scruple.console.sources import AgentRun, ConsoleSources

__all__ = ["PREFIX", "AgentRun", "ConsoleSources", "console_router"]
