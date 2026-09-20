"""Source-backed knowledge a deployment may arm sessions with, and how it is produced.

A compiler runs offline, once per release, against a version-bound source tree; a session
is only ever handed the ``SourceMap`` it produced. ``SourceCapability`` keeps the four
coupled session settings in the one value their shared owner -- this deployment's compiled
map -- decides.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..schemas import SourceMap


@runtime_checkable
class SourceCompiler(Protocol):
    """Read one application's own declarative configuration into a cited ``SourceMap``.

    Unlike the five ``ApplicationPorts``, a compiler has no place in a live session: it runs
    offline, once per deployment release, against a version-bound source tree, and its output
    -- a ``SourceMap`` of ``SourceRelation`` triples, each carrying a checkable citation -- is
    what a session is later handed as ``source_map``. Every application distribution that
    offers source-backed knowledge implements this against its own declarative shape (OFBiz's
    ``web_operation_agent_ofbiz.OFBizCompiler`` reads XML controller and widget descriptors);
    the core Agent never reads application source itself.
    """

    def compile(self) -> SourceMap: ...


@dataclass(frozen=True, slots=True)
class SourceCapability:
    """What one deployment's compiled source lets a session do, as one value.

    Four session settings have always moved together and have always had one real owner --
    whether this deployment has a compiled, version-bound ``SourceMap`` for the release it
    is pointed at. Leaving them as four independent constructor flags invited a composition
    to enable source-armed planning with no map, or to ship a map nothing consumes; it also
    left them looking like experiment knobs, which two of their neighbours genuinely are.

    A distribution that implements ``SourceCompiler`` builds this once per release and hands
    it to the composition; a distribution without one returns ``SourceCapability.none()``.
    """

    source_map: SourceMap | None = None
    source_contract: bool = False
    runtime_source: bool = False
    source_affordance_mode: str = "off"

    def __post_init__(self) -> None:
        if self.source_map is not None and not isinstance(self.source_map, SourceMap):
            raise TypeError("source_map must be a SourceMap or None")
        for name in ("source_contract", "runtime_source"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be a boolean")
        if self.source_affordance_mode not in {"off", "empty", "auto"}:
            raise ValueError("source_affordance_mode must be off, empty or auto")
        wants_source = (self.source_contract or self.runtime_source
                        or self.source_affordance_mode == "auto")
        if wants_source and self.source_map is None:
            raise ValueError("source-backed settings require a version-bound SourceMap")

    @staticmethod
    def none() -> "SourceCapability":
        """The capability of a deployment whose application ships no source compiler."""
        return SourceCapability()

    def session_kwargs(self) -> dict:
        """The exact keywords a session takes, so a composition cannot pick three of four."""
        return {
            "source_map": self.source_map,
            "source_contract": self.source_contract,
            "runtime_source": self.runtime_source,
            "source_affordance_mode": self.source_affordance_mode,
        }
