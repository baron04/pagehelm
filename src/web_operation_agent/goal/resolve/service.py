"""The goal port every application gets: resolution by the core over the application's
data.

An adapter provides `GoalData` -- its index, its `DataPort` reads, and the optional
capabilities it offers -- and never resolves a declaration itself.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import re
import threading
from datetime import date
from typing import Protocol

from ...state.identity import OperationContext
from ..data import DataPort, DataRefused, Schema
from ..index import ApplicationIndex
from ..ports import Candidate, Resolution, capabilities_of
from ..spec import GoalSpec
from .resolver import Ambiguous, Resolver
from .words import norm


class GoalData(DataPort, Protocol):
    """What an adapter implements for goal-directed turns."""

    def index(self, *, context: OperationContext | None = None) -> ApplicationIndex: ...

    def capabilities(self) -> frozenset[str]: ...


class DataGoalPort:
    """`GoalPort` over an application's `GoalData`."""

    def __init__(self, data: GoalData, *, today: date | None = None,
                 reference_workers: int = 1):
        """`today` fixes the date relative periods count from, for an evaluation whose
        truth was written on a given day; otherwise it is the application's current date."""
        if isinstance(reference_workers, bool) or not isinstance(reference_workers, int) or not 1 <= reference_workers <= 8:
            raise ValueError("reference_workers must be an integer between 1 and 8")
        # Opt in only for data adapters whose actor-bound reads are safe concurrently.
        self.reference_workers = reference_workers
        self.data, self._today = data, today
        self._lock = threading.Lock()
        self._schema: Schema | None = None
        self._schema_for: ApplicationIndex | None = None

    def capabilities(self) -> frozenset[str]:
        return capabilities_of(self.data)

    def index(self, *, context: OperationContext | None = None) -> ApplicationIndex:
        return self.data.index(context=context)

    def today(self) -> str:
        """The current date where the application's users are, for the declaration."""
        return self._resolver(self.index()).today().isoformat()

    def resolve(self, spec: GoalSpec, *, context: OperationContext | None = None,
                current_path: str = "") -> Resolution:
        return self._resolver(self.index(context=context)).resolve(spec)

    def candidates(self, *, entities: tuple[str, ...] = (), known: tuple = (),
                   dates: tuple[str, ...] = (), context: OperationContext | None = None
                   ) -> tuple[Candidate, ...]:
        return self._resolver(self.index(context=context)).candidates(
            entities=entities, known=known, dates=dates)

    def locate_reference(self, text: str, *, context: OperationContext | None = None,
                         entities: tuple[str, ...] = ()):
        """The records a hard reference names, as (entity key, display name), looked for
        on every entity a page shows, or on those of `entities` a page shows; a number or a
        date names nothing here."""
        if not text.strip() or re.fullmatch(r"[\d.,/-]+", text.strip()):
            return ()
        index = self.index(context=context)
        shown = tuple(dict.fromkeys(p.entity for p in index.pages if p.entity))
        entities = tuple(e for e in shown if e in entities) if entities else shown
        serial_resolver = self._resolver(index) if self.reference_workers == 1 else None

        def find(entity):
            # A resolver is mutable; never share one between reader threads.
            resolver = serial_resolver if serial_resolver is not None else self._resolver(index)
            if resolver.view(entity) is None:
                return None
            try:
                found = resolver._find_record(entity, text)
            except DataRefused:
                # A missing parent/context on one list does not hide other entities.
                return None
            if not isinstance(found, tuple) or isinstance(found, Ambiguous):
                return None
            wanted = norm(text)
            shown = norm(resolver._own_name(found[2]))
            own = norm(found.personal)
            if (shown == wanted or shown.startswith(wanted) or norm(found[2]).startswith(wanted)
                    or own == wanted or own.startswith(wanted)):
                return entity, found[2]
            return None

        if self.reference_workers > 1 and len(entities) > 1:
            with ThreadPoolExecutor(max_workers=self.reference_workers) as pool:
                futures = [pool.submit(copy_context().run, find, entity) for entity in entities]
                hits = [future.result() for future in futures]  # Keep index order.
        else:
            hits = [find(entity) for entity in entities]
        return tuple(hit for hit in hits if hit is not None)[:3]

    def locate_names(self, names: tuple[str, ...], *, entities: tuple[str, ...] = (),
                     context: OperationContext | None = None):
        """(name, entity key, display name) of the one record each name picks out, looked
        for on `entities` and the entities their links point to: a record whose own name is
        the name, else the one record, on one entity only, whose name begins with it."""
        resolver = self._resolver(self.index(context=context))
        order: list[str] = []
        for entity in entities:
            view = resolver.view(entity)
            if view is None:
                continue
            order.append(entity)
            order += [f.relation for f in view.fields.values() if f.kind == "link" and f.relation]
        hits = []
        for name in names:
            wanted = norm(name)
            exact, prefixed = [], []
            for entity in dict.fromkeys(order):
                if not wanted or resolver.view(entity) is None:
                    continue
                try:
                    found = resolver._find_record(entity, name)
                except DataRefused:
                    continue
                if not isinstance(found, tuple) or isinstance(found, Ambiguous):
                    continue
                own = norm(resolver._own_name(found[2]))
                if own == wanted or norm(found.personal) == wanted:
                    exact.append((name, entity, found[2]))
                elif own.startswith(wanted):
                    prefixed.append((name, entity, found[2]))
            # A name that is some record's whole name is that record. One that only begins
            # records' names is taken when it begins one entity's (厦门海湾 begins a
            # customer's and a contract's name: neither is taken).
            if exact:
                hits.append(exact[0])
            elif len(prefixed) == 1:
                hits.append(prefixed[0])
        return tuple(hits)

    def _resolver(self, index: ApplicationIndex) -> Resolver:
        with self._lock:
            if self._schema_for is not index:
                self._schema, self._schema_for = self.data.schema(), index
            schema = self._schema
        return Resolver(self.data, index, schema, today=self._today)

    def invalidate(self) -> None:
        """Recompile the schema on next use (after the adapter's metadata changed)."""
        with self._lock:
            self._schema_for = None


__all__ = ["DataGoalPort", "GoalData"]
