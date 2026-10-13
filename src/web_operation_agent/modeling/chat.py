"""The model-independent chat port and admission decorators."""
from __future__ import annotations

import inspect
import threading
import time
from typing import Protocol, runtime_checkable

from .provider import ProviderError


class _ConcreteChatProvider:
    """Marker for provider clients whose constructor-owned state must be present."""


#: State a concrete provider client only has once its constructor ran. A double that
#: subclasses the concrete client to satisfy a type check inherits real methods without this
#: state, so the first call fails inside the turn instead of at composition time.
_PROVIDER_STATE = ("base", "_key", "meter", "max_completion_tokens", "temperature",
                   "timeout", "runtime_guard", "egress_policy")


@runtime_checkable
class ChatPort(Protocol):
    """The one boundary a turn uses to reach a model.

    Both entry points are mandatory. Cancellation is not an optional capability discovered at
    call time: an implementation that cannot abort a dispatched request must say so by not
    satisfying this port, never by silently completing a run the user already stopped.

    Per-call options may set tool_choice to auto (the default) or required. Required
    needs a non-empty tool list and a tool-call response. Decorators must forward it
    unchanged; provider configuration cannot select it for ordinary turns.
    """

    model: str

    def complete(self, messages: list[dict], tools: list[dict] | None, run_id: str,
                 max_tokens: int | None = None, options: dict | None = None,
                 timeout_s: float | None = None) -> dict: ...

    def complete_cancellable(self, messages: list[dict], tools: list[dict] | None, run_id: str,
                             *, cancelled, max_tokens: int | None = None,
                             options: dict | None = None,
                             timeout_s: float | None = None) -> dict: ...


def _accepts_keyword(signature: inspect.Signature, name: str, *, keyword_only: bool) -> bool:
    parameter = signature.parameters.get(name)
    if parameter is not None:
        if keyword_only:
            return parameter.kind is inspect.Parameter.KEYWORD_ONLY
        return parameter.kind in (inspect.Parameter.KEYWORD_ONLY,
                                  inspect.Parameter.POSITIONAL_OR_KEYWORD)
    if keyword_only:
        return any(p.kind is inspect.Parameter.VAR_KEYWORD
                   for p in signature.parameters.values())
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values())


def _accepts_positional(signature: inspect.Signature, count: int) -> bool:
    positional = [p for p in signature.parameters.values()
                  if p.kind in (inspect.Parameter.POSITIONAL_ONLY,
                                inspect.Parameter.POSITIONAL_OR_KEYWORD)]
    if len(positional) >= count:
        return True
    return any(p.kind is inspect.Parameter.VAR_POSITIONAL
               for p in signature.parameters.values())


def require_chat_port(candidate, *, role: str = "chat"):
    """Return ``candidate`` if it satisfies :class:`ChatPort`, else raise ``TypeError``.

    Composition calls this so a wrong shape fails where it is wired, not on the first model
    call several layers down. The checks are deliberately structural: presence of both entry
    points, a keyword-only cancellation hook, a usable model label, and no half-built
    concrete provider standing in for an implementation.
    """
    for name in ("complete", "complete_cancellable"):
        method = getattr(candidate, name, None)
        if not callable(method):
            raise TypeError(
                f"{role} must implement ChatPort.{name}; cancellation is not optional")
    if isinstance(candidate, _ConcreteChatProvider):
        missing = [name for name in _PROVIDER_STATE if not hasattr(candidate, name)]
        if missing:
            raise TypeError(
                f"{role} subclasses ChatModel but its __init__ never ran (missing "
                f"{missing[0]!r}); implement ChatPort instead of borrowing the concrete "
                "provider to satisfy a type check")
    try:
        plain = inspect.signature(candidate.complete)
        cancellable = inspect.signature(candidate.complete_cancellable)
    except (TypeError, ValueError) as exc:  # pragma: no cover - exotic callables
        raise TypeError(f"{role} exposes an uninspectable ChatPort signature") from exc
    for name, signature in (("complete", plain), ("complete_cancellable", cancellable)):
        if not _accepts_positional(signature, 3):
            raise TypeError(
                f"{role}.{name} must accept (messages, tools, run_id)")
        if not _accepts_keyword(signature, "timeout_s", keyword_only=False):
            raise TypeError(f"{role}.{name} must accept a timeout_s deadline")
    if not _accepts_keyword(cancellable, "cancelled", keyword_only=True):
        raise TypeError(
            f"{role}.complete_cancellable must take a keyword-only 'cancelled' hook so a "
            "caller cannot pass it by accident or omit it silently")
    label = getattr(candidate, "model", None)
    if not isinstance(label, str) or not label.strip() or label.strip() != label:
        raise TypeError(f"{role}.model must be a non-empty label without surrounding space")
    return candidate


class KeyedProviderAdmission:
    """Bound concurrent provider work for one trusted principal or tenant key."""

    def __init__(self, maximum: int):
        if (isinstance(maximum, bool) or not isinstance(maximum, int)
                or not 1 <= maximum <= 10_000):
            raise ValueError("keyed provider maximum must be an integer from 1 to 10000")
        self.maximum = maximum
        self._condition = threading.Condition()
        self._active: dict[str, int] = {}

    def acquire(self, key: str, timeout_s: float, *, cancelled=None) -> float:
        if not isinstance(key, str) or not key:
            raise ValueError("provider admission key must be a non-empty string")
        started = time.monotonic()
        deadline = started + max(0.001, float(timeout_s))
        with self._condition:
            while self._active.get(key, 0) >= self.maximum:
                if cancelled is not None and cancelled():
                    raise ProviderError(
                        "provider call cancelled while waiting for principal capacity",
                        code="provider_cancelled", retryable=False, dispatched=False)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ProviderError(
                        "principal provider capacity was unavailable before the deadline",
                        code="provider_capacity", retryable=True, dispatched=False)
                self._condition.wait(min(.05, remaining))
            self._active[key] = self._active.get(key, 0) + 1
            return time.monotonic() - started

    def release(self, key: str) -> None:
        with self._condition:
            count = self._active.get(key, 0)
            if count <= 0:
                raise RuntimeError("principal provider slot released without acquisition")
            if count == 1:
                self._active.pop(key, None)
            else:
                self._active[key] = count - 1
            self._condition.notify_all()

    def active(self, key: str) -> int:
        with self._condition:
            return self._active.get(key, 0)


class KeyedLimitedChat:
    """ChatPort decorator that applies keyed admission before the provider guard."""

    def __init__(self, delegate, admission: KeyedProviderAdmission, key: str,
                 *, wait_observer=None):
        self._delegate = require_chat_port(delegate, role="keyed chat delegate")
        if not isinstance(admission, KeyedProviderAdmission):
            raise TypeError("admission must be a KeyedProviderAdmission")
        if not isinstance(key, str) or not key:
            raise ValueError("provider admission key must be a non-empty string")
        self._admission = admission
        self._key = key
        if wait_observer is not None and not callable(wait_observer):
            raise TypeError("wait_observer must be callable or None")
        self._wait_observer = wait_observer
        self.model = delegate.model
        self.meter = getattr(delegate, "meter", None)
        self.options = getattr(delegate, "options", None)
        self.egress_policy = getattr(delegate, "egress_policy", None)

    def complete(self, messages, tools, run_id, max_tokens=None, options=None,
                 timeout_s=None):
        timeout = float(timeout_s if timeout_s is not None else 180)
        waited = self._admission.acquire(self._key, timeout)
        if self._wait_observer is not None:
            try:
                self._wait_observer(run_id, waited)
            except Exception:
                pass
        try:
            return self._delegate.complete(
                messages, tools, run_id, max_tokens=max_tokens, options=options,
                timeout_s=timeout_s)
        finally:
            self._admission.release(self._key)

    def complete_cancellable(self, messages, tools, run_id, *, cancelled,
                             max_tokens=None, options=None, timeout_s=None):
        timeout = float(timeout_s if timeout_s is not None else 180)
        waited = self._admission.acquire(self._key, timeout, cancelled=cancelled)
        if self._wait_observer is not None:
            try:
                self._wait_observer(run_id, waited)
            except Exception:
                pass
        try:
            return self._delegate.complete_cancellable(
                messages, tools, run_id, cancelled=cancelled,
                max_tokens=max_tokens, options=options, timeout_s=timeout_s)
        finally:
            self._admission.release(self._key)
