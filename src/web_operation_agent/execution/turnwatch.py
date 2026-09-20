"""Two small things a turn needs while it runs: a feed, and a guard.

Both were fields and methods of the session class, which is how a 3400-line class comes
about: they have nothing to do with a page, an answer or a write, and nothing else in the
session has anything to do with them.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable

from ..schemas import PageObservation


class ProgressFeed:
    """Where a turn says what it is doing, for whoever is watching it happen.

    Facts only: the sentence a person reads is written by the panel view. A listener that
    raises is a broken panel, not a broken turn, so the failure is recorded and the turn
    goes on.
    """

    def __init__(self, publish: Callable[[dict], None] | None,
                 on_failure: Callable[..., object] | None = None):
        self._publish = publish
        self._on_failure = on_failure

    def note(self, **facts) -> None:
        if self._publish is None:
            return
        try:
            self._publish(facts)
        except Exception as exc:  # noqa: BLE001 -- see the docstring
            if self._on_failure is not None:
                self._on_failure("progress_publish_failed", error_type=type(exc).__name__)


class RepeatGuard:
    """Whether this turn is deciding the same thing over and over on the same page.

    A model that repeats a decision is not making progress, and the step budget is a poor
    way to find that out: it spends every remaining step first. The same tool with the same
    arguments on the same page (the epoch aside, which changes with every observation) is
    the repeat this refuses -- once with a warning, and then by ending the turn.
    """

    def __init__(self, limit: int | None):
        self._limit = limit
        self._fingerprint = ""
        self._repeats = 0
        self._warned = False

    @property
    def repeats(self) -> int:
        """How many times running the current decision has been made."""
        return self._repeats

    def reset(self) -> None:
        self._fingerprint = ""
        self._repeats = 0
        self._warned = False

    def decide(self, name: str, args: object, observation: PageObservation) -> str:
        """`""` to carry on, `"warn"` to say so once, `"end"` to stop the turn."""
        if self._limit is None or name in {"finish", "ask_user"}:
            self.reset()
            return ""
        fingerprint = self._fingerprint_of(name, args, observation)
        if fingerprint != self._fingerprint:
            self._fingerprint = fingerprint
            self._repeats = 1
            self._warned = False
            return ""
        self._repeats += 1
        if self._warned:
            return "end"
        if self._repeats >= self._limit:
            self._warned = True
            return "warn"
        return ""

    @staticmethod
    def _fingerprint_of(name: str, args: object, observation: PageObservation) -> str:
        page = observation.to_json()
        page.pop("epoch", None)
        for element in page.get("elements", ()):
            element.pop("epoch", None)
        try:
            encoded = json.dumps(
                {"tool": name, "args": args, "page": page}, ensure_ascii=False,
                sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (TypeError, ValueError):
            encoded = json.dumps(
                {"tool": name, "args_type": type(args).__name__, "page": page},
                ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
