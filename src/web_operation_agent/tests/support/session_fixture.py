"""Build a real `ServerAgentSession` for tests that only need one method of it.

Several tests used to reach a method through `object.__new__(ServerAgentSession)`, which
left every attribute the method touched undefined. `session.py` compensated with sixteen
`getattr(self, "...", default)` reads whose only reason to exist was those tests, and the
defaults silently disagreed with the constructor's. Building the real object instead keeps
one definition of what a session is, and a method that grows a new dependency now fails
loudly here rather than reading a default nobody chose.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from web_operation_agent.state.ledger import SessionLedger
from web_operation_agent.execution.session import ServerAgentSession, TaskSpec
from web_operation_agent.tests.support.execution import _Browser, _Chat, _Control, _obs


class SessionFixture:
    """Own one temporary ledger directory and hand out real sessions against it."""

    def __init__(self):
        self._directory = tempfile.TemporaryDirectory()
        self._sessions = 0

    @property
    def root(self) -> Path:
        return Path(self._directory.name)

    def session(self, **overrides) -> ServerAgentSession:
        self._sessions += 1
        arguments = {
            "task": TaskSpec("T", "inspect"),
            "chat": _Chat([]),
            "browser": _Browser([_obs(1)]),
            "control": _Control([[], []]),
            "ledger": SessionLedger(self.root / f"audit-{self._sessions}.jsonl", "fixture"),
        }
        arguments.update(overrides)
        return ServerAgentSession(**arguments)

    def close(self) -> None:
        self._directory.cleanup()
