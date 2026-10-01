"""Source-backed knowledge a deployment may arm sessions with, and how it is produced.

A deployment can provide a cited repository lookup directly. Compiled maps remain optional
for contracts and source-derived navigation; ordinary source questions require neither.
``SourceCapability`` validates the release and the settings of that one knowledge surface.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..schemas import SourceMap
from ..planning.source_facts import CompiledSourceLookup, SourceLookup
from ..planning.source_history import SourceHistoryLookup


@runtime_checkable
class SourceCompiler(Protocol):
    """Read one application's own declarative configuration into a cited ``SourceMap``.

    Unlike the five ``ApplicationPorts``, a compiler has no place in a live session: it runs
    offline, once per deployment release, against a version-bound source tree, and its output
    -- a ``SourceMap`` of ``SourceRelation`` triples, each carrying a checkable citation -- is
    what a session is later handed as ``source_map`` when compiled navigation is needed.
    A distribution may instead provide a repository lookup without a compiler. The core
    Agent consumes only the configured knowledge port, never an unrestricted filesystem.
    """

    def compile(self) -> SourceMap: ...


@dataclass(frozen=True, slots=True)
class SourceCapability:
    """One deployment's release-bound lookup, with optional compiled navigation knowledge.

    A repository lookup answers from cited code rather than an enumerated set of compiled
    relations. It grants no route, tool or permission. Contracts and source affordances
    still require an explicit compiled map while their existing callers use that path.
    """

    source_map: SourceMap | None = None
    source_contract: bool = False
    runtime_source: bool = False
    source_affordance_mode: str = "off"
    #: Direct local search over an allowlisted release. It needs no compiled map. When
    #: absent, an explicitly provided map supplies its deterministic rule index.
    lookup: SourceLookup | None = None
    #: Optional local Git metadata bound to the same deployment release.  Its absence is
    #: explicit because an extracted container tree commonly has no ``.git`` directory.
    history: SourceHistoryLookup | None = None
    history_unavailable_reason: str = "repository history is not configured"

    def __post_init__(self) -> None:
        if self.source_map is not None and not isinstance(self.source_map, SourceMap):
            raise TypeError("source_map must be a SourceMap or None")
        for name in ("source_contract", "runtime_source"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be a boolean")
        if self.source_affordance_mode not in {"off", "empty", "auto"}:
            raise ValueError("source_affordance_mode must be off, empty or auto")
        if (self.source_contract or self.source_affordance_mode == "auto") \
                and self.source_map is None:
            raise ValueError("source contracts and affordances require a version-bound SourceMap")
        if self.lookup is not None:
            if not isinstance(self.lookup, SourceLookup):
                raise TypeError("lookup must implement SourceLookup")
            if not self.runtime_source:
                raise ValueError("a source lookup is used only by runtime source lookup")
            if not isinstance(self.lookup.release, str) or not self.lookup.release.strip():
                raise ValueError("a source lookup must declare its release")
            if self.source_map is not None and self.lookup.release != self.source_map.release:
                raise ValueError("a source lookup must search the map's own release")
        if self.runtime_source and self.source_map is None and self.lookup is None:
            raise ValueError("runtime source requires a version-bound SourceMap or source lookup")
        if not isinstance(self.history_unavailable_reason, str):
            raise TypeError("history_unavailable_reason must be a string")
        if self.history is not None:
            if not isinstance(self.history, SourceHistoryLookup):
                raise TypeError("history must implement SourceHistoryLookup")
            if not self.runtime_source:
                raise ValueError("source history is used only by runtime source lookup")
            if self.history.release != self.release:
                raise ValueError("source history must describe the source lookup's own release")
            if self.history_unavailable_reason:
                object.__setattr__(self, "history_unavailable_reason", "")

    @property
    def release(self) -> str:
        if self.source_map is not None:
            return self.source_map.release
        return self.lookup.release if self.lookup is not None else ""

    @property
    def content_digest(self) -> str:
        if self.source_map is not None:
            return self.source_map.content_digest
        return str(getattr(self.lookup, "surface_digest", ""))

    @staticmethod
    def none() -> "SourceCapability":
        """The capability of a deployment that provides no source knowledge."""
        return SourceCapability()

    def fact_lookup(self) -> SourceLookup | None:
        """What runtime lookup asks about application rules."""
        if self.lookup is not None:
            return self.lookup
        return CompiledSourceLookup(self.source_map) if self.source_map is not None else None

    def history_lookup(self) -> SourceHistoryLookup | None:
        """The deployment-bound Git provider, when the source extraction includes one."""
        return self.history

    def control(self, *, lookup: SourceLookup | None = None) -> "SourceCapability":
        """Keep the interface and release, replacing compiled facts with an empty map."""
        if self.source_map is None:
            raise ValueError("a capability without a map has no control to derive")
        empty = SourceMap(
            schema_version=self.source_map.schema_version,
            attestation_id=self.source_map.attestation_id,
            repository_id=self.source_map.repository_id,
            release=self.source_map.release,
            relations=[],
        )
        return SourceCapability(
            source_map=empty,
            source_contract=self.source_contract,
            runtime_source=self.runtime_source,
            source_affordance_mode=self.source_affordance_mode,
            lookup=lookup,
        )

    def session_kwargs(self) -> dict:
        """The exact keywords a session takes, so a composition cannot pick three of four."""
        return {
            "source_map": self.source_map,
            "source_contract": self.source_contract,
            "runtime_source": self.runtime_source,
            "source_affordance_mode": self.source_affordance_mode,
        }
