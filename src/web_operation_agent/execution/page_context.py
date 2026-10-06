"""Server-owned snapshot spans inside otherwise ordinary ChatPort text.

Only the observation renderer marks a span. Page text, tool output and user text cannot
mark themselves by copying delimiters. JSON serialization exposes just the string; the
span bookkeeping stays in the server's conversation before provider dispatch.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

DEFAULT_KEEP_OBSERVATIONS = 1


@dataclass(frozen=True)
class SnapshotSpan:
    start: int
    end: int
    epoch: int
    digest: str
    url: str

    @property
    def identity(self):
        return self.epoch, self.digest


class PageContextText(str):
    """A string whose immutable, server-generated spans survive text composition."""

    __slots__ = ("snapshots",)

    def __new__(cls, value, snapshots=()):
        text = super().__new__(cls, value)
        spans = tuple(snapshots)
        edge = 0
        for span in spans:
            if (not isinstance(span, SnapshotSpan) or span.start < edge
                    or span.end <= span.start or span.end > len(text)):
                raise ValueError("snapshot spans must be ordered, disjoint text ranges")
            edge = span.end
        object.__setattr__(text, "snapshots", spans)
        return text

    def __setattr__(self, name, value):
        raise AttributeError("page context text is immutable")

    def __delattr__(self, name):
        raise AttributeError("page context text is immutable")

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self

    def __add__(self, other):
        if not isinstance(other, str):
            return NotImplemented
        extra = (tuple(replace(s, start=s.start + len(self), end=s.end + len(self))
                       for s in other.snapshots) if isinstance(other, PageContextText) else ())
        return PageContextText(str(self) + str(other), self.snapshots + extra)

    def __radd__(self, other):
        if not isinstance(other, str):
            return NotImplemented
        shifted = tuple(replace(s, start=s.start + len(other), end=s.end + len(other))
                        for s in self.snapshots)
        return PageContextText(str(other) + str(self), shifted)


def snapshot_text(value: str, *, epoch: int, digest: str, url: str) -> PageContextText:
    return PageContextText(value, (SnapshotSpan(0, len(value), epoch, digest, url),))


def join_context(parts, separator="\n\n") -> str:
    """Join server text without losing its snapshot ownership metadata."""
    result = ""
    for i, part in enumerate(parts):
        result = result + (separator if i else "") + part
    return result


def compact_snapshots(messages: list[dict], keep: int) -> list[dict]:
    """Replace superseded snapshots, preserving all text outside their exact spans.

    Retain the latest `keep` distinct observations, one copy of each. Conflicting views
    at the latest epoch remain intact: an equal epoch cannot prove either superseded the
    other. Message roles, call IDs, facts, citations and policy feedback are never cut.
    """
    if isinstance(keep, bool) or not isinstance(keep, int) or keep < 1:
        raise ValueError("retained observations must be a positive integer")
    occurrences = [(i, j, span) for i, message in enumerate(messages)
                   if isinstance(message.get("content"), PageContextText)
                   for j, span in enumerate(message["content"].snapshots)]
    if not occurrences:
        return messages
    last = {span.identity: (i, j, span) for i, j, span in occurrences}
    ordered = sorted(last.values(), key=lambda item: (item[2].epoch, item[0], item[1]))
    newest_epoch = ordered[-1][2].epoch
    retained = {(i, j) for i, j, span in ordered[-keep:]}
    retained.update((i, j) for i, j, span in ordered if span.epoch == newest_epoch)
    for i, message in enumerate(messages):
        content = message.get("content")
        if not isinstance(content, PageContextText) or not content.snapshots:
            continue
        parts, edge, changed = [], 0, False
        for j, span in enumerate(content.snapshots):
            parts.append(str(content[edge:span.start]))
            if (i, j) in retained:
                parts.append(snapshot_text(str(content[span.start:span.end]),
                    epoch=span.epoch, digest=span.digest, url=span.url))
            else:
                parts.append(f"(earlier page snapshot omitted; epoch {span.epoch}; "
                             f"url: {span.url}. Use the latest observation for controls.)")
                changed = True
            edge = span.end
        if changed:
            parts.append(str(content[edge:]))
            # Even when all its spans were replaced, this message belongs to structured
            # context. It must not fall back to legacy whole-tool-result shortening.
            joined = join_context(parts, "")
            messages[i] = {**message, "content": PageContextText(joined,
                getattr(joined, "snapshots", ()))}
    return messages
