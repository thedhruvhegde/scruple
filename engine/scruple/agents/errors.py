"""Errors an episode raises. Messages are safe to show to the agent."""

from __future__ import annotations


class EpisodeError(Exception):
    """A call the episode refuses."""


class EpisodeClosedError(EpisodeError):
    """The decision was already submitted; nothing else is accepted."""


class ToolArgumentError(EpisodeError):
    """Arguments did not match the tool's schema."""
