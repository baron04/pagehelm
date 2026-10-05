"""Optional bounded original-source reads over one deployment's admitted surface."""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from .source_facts import SourceHit


MAX_SOURCE_PATH_CHARS = 500
MAX_SOURCE_READ_LINES = 120
MAX_SOURCE_READ_CHARS = 3400


class SourceReadError(ValueError):
    """A typed refusal, never an empty file or an unrestricted filesystem fallback."""

    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(detail)


@runtime_checkable
class SourceReader(Protocol):
    release: str

    def read(self, path: str, *, start_line: int, end_line: int) -> SourceHit: ...
