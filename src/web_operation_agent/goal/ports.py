"""What an application provides to a goal-directed turn, and what it hands back.

The adapter owns every application fact: how a reference becomes a record, which page
shows an entity, what a filter means, how a value is displayed. The core owns the turn:
asking once, confirming page switches, dispatching generic page commands, checking the
outcome against the resolution, and writing the reply.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..state.identity import OperationContext
from .index import ApplicationIndex
from .spec import GoalSpec


@dataclass(frozen=True)
class Fact:
    subject: str
    label: str
    value: str


@dataclass(frozen=True)
class Aggregate:
    subject: str
    count: int
    #: (label, displayed total) for each summed field.
    sums: tuple[tuple[str, str], ...] = ()
    #: (group name, count) for the groups reported.
    groups: tuple[tuple[str, int], ...] = ()
    #: Record display names when the person asked for a list.
    records: tuple[str, ...] = ()
    #: True when `count` is the whole scope, not a bounded page of it.
    complete: bool = True
    #: A qualification the reply must carry, e.g. the wider count when the scope is empty.
    note: str = ""


@dataclass(frozen=True)
class PagePlan:
    """A page state to reach, in terms the runtime can act on and the core can check."""
    #: Same-origin path to open. Empty when the current page is meant.
    url: str
    #: How the application labels the destination, for the reply.
    label: str
    #: Path the page is expected to show once open; the URL may differ after redirects.
    expected_path: str = ""
    #: Expected rendered view name, when a view was asked for.
    view: str = ""
    facets_on: tuple[str, ...] = ()
    facets_off: tuple[str, ...] = ()
    #: Tab to select on the record's page, by its visible name.
    section: str = ""
    #: Control to highlight and leave to the person, by its visible name.
    highlight: str = ""
    #: What pressing that control does, when the adapter knows.
    highlight_effect: str = ""


@dataclass(frozen=True)
class Resolved:
    """One deliverable after resolution. Exactly one of the payloads is present."""
    kind: str
    facts: tuple[Fact, ...] = ()
    aggregate: Aggregate | None = None
    page: PagePlan | None = None
    #: Why this deliverable could not be resolved: not found, ambiguous, unsupported.
    problem: str = ""
    #: Candidate names when a reference was ambiguous, so the reply can ask.
    candidates: tuple[str, ...] = ()


@dataclass(frozen=True)
class Resolution:
    deliverables: tuple[Resolved, ...]
    #: Reads performed, for the audit record: (operation, target) pairs, no values.
    reads: tuple[tuple[str, str], ...] = ()


class GoalPort(Protocol):
    """Compiled by the adapter under the acting user; read-only by construction."""

    def index(self, *, context: OperationContext | None = None) -> ApplicationIndex: ...

    def resolve(self, spec: GoalSpec, *, context: OperationContext | None = None,
                current_path: str = "") -> Resolution: ...


__all__ = ["Aggregate", "Fact", "GoalPort", "PagePlan", "Resolution", "Resolved"]
