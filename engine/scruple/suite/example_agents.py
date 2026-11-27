"""Example local agents for `scruple suite run --agent python:MODULE:FUNCTION`.

A local agent is a function that takes a `scruple.suite.agents.Session` and plays
the episode through `session.call(tool, args)`. These two are deliberately naive;
they show the interface and give a suite something to fail.
"""

from __future__ import annotations

from scruple.suite.agents import Session

READS = ("get_case", "get_task", "get_alert", "list_evidence", "list_offers", "get_inbox")


def read_then_submit(session: Session) -> None:
    """Read whatever the environment offers, then submit an honest, empty report."""
    names = {t["name"] for t in session.tools}
    for r in READS:
        if r in names:
            session.call(r, {})
    session.call("submit_decision", {"explanation": "Read the case; took no action."})


def gives_up(session: Session) -> None:
    """Reads one thing and stops without submitting (the episode lapses)."""
    names = [t["name"] for t in session.tools]
    session.call(names[0], {})


__all__ = ["gives_up", "read_then_submit"]
