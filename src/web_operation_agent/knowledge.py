"""Application-neutral contract for optional server-side navigation knowledge."""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ApplicationKnowledgePort(Protocol):
    """Select bounded cards and resolve opaque application-owned navigation targets."""

    def relevant(self, task: str, limit: int = 3) -> tuple[object, ...]: ...

    def resolve(self, skill_id: object, bindings: object, current_url: str,
                allowed_ids: set[str]) -> tuple[str, str, object | None]: ...

    def app_link(self, skill_id: object, observation: object,
                 allowed_ids: set[str]) -> tuple[str, str, object | None]: ...

    def gap_evidence(self, skill: object, observation: object) -> dict: ...
