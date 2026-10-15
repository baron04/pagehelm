"""Content-free timing and accounting for one turn, including parallel stages."""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
import json
import math
import re
import threading
import time

from ..modeling.chat import require_chat_port
from ..modeling.provider import ProviderError
from ..modeling.spend import BudgetExhausted
from .events import current_events

STAGES = frozenset({"scope", "interpret", "vote", "reconcile", "repair", "source",
                    "business", "ui", "wait", "model", "action_approval",
                    "action_admission", "action_dispatch", "action_verification"})
_ACCOUNTING = ("model_calls", "usage_unavailable_calls", "known_input_tokens",
               "known_output_tokens", "known_cached_tokens", "known_reasoning_tokens",
               "known_cost_cny", "total_cost_cny")
_OPTIONS = frozenset({"enable_thinking", "thinking_budget", "parallel_tool_calls",
                      "reasoning_effort"})


def occupied(intervals) -> float:
    """Seconds covered by the union, rather than the sum, of monotonic intervals."""
    total, edge = 0.0, None
    for start, end in sorted(intervals):
        if edge is None or start >= edge:
            total += max(0.0, end - start)
        elif end > edge:
            total += end - edge
        edge = max(edge if edge is not None else end, end)
    return total


class TurnReceipt:
    """Retain only stage intervals and counters; prompts/results never enter this object."""

    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.events = current_events()
        self.started = clock()
        self._stage = ContextVar("turn_receipt_stage", default="model")
        self._lock = threading.Lock()
        self._intervals: dict[str, list[tuple[float, float]]] = {}
        self._models: list[tuple[float, float]] = []
        self._counts = Counter()
        self._operations = Counter()
        self._inputs = Counter()
        self._outcomes = Counter()
        self._inflight = 0

    @contextmanager
    def stage(self, name):
        if name not in STAGES:
            raise ValueError("unknown receipt stage")
        token, start = self._stage.set(name), self.clock()
        with self._lock:
            self._operations[name] += 1
        try:
            yield
        finally:
            end = self.clock()
            with self._lock:
                self._intervals.setdefault(name, []).append((start, end))
            self._stage.reset(token)

    def start_model(self, messages, tools):
        size = len(json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False,
                              separators=(",", ":"), allow_nan=False).encode())
        name, start = self._stage.get(), self.clock()
        interval = (self.events[0].interval("model", turn_id=self.events[1]) if self.events else None)
        if interval:
            interval.__enter__()
        with self._lock:
            self._counts[name] += 1
            self._inputs[name] += size
            self._inflight += 1
        return name, start, interval

    def end_model(self, handle, *, succeeded):
        name, start, interval = handle
        if interval:
            interval.__exit__(None, None, None)
        end = self.clock()
        with self._lock:
            self._models.append((start, end))
            self._intervals.setdefault(name, []).append((start, end))
            self._outcomes["succeeded" if succeeded else "failed"] += 1
            self._inflight -= 1

    def report(self, chat, run_id):
        meter = getattr(chat, "meter", None)
        accounting = {key: None for key in _ACCOUNTING}
        if callable(getattr(meter, "run_report", None)):
            try:
                raw = meter.run_report(run_id)
                for key in _ACCOUNTING:
                    value = raw.get(key)
                    if (not isinstance(value, bool) and isinstance(value, (int, float))
                            and math.isfinite(value) and value >= 0):
                        accounting[key] = value
            except Exception:
                # Missing accounting is unknown, never a zero-cost request.
                pass
        options = getattr(chat, "options", None)
        options = options if isinstance(options, dict) else {}
        safe_options = {k: v for k, v in options.items() if k in _OPTIONS
                        and (isinstance(v, (bool, int)) or v in ("none", "low", "medium", "high"))}
        with self._lock:
            names = set(self._intervals) | set(self._counts)
            labels = {key: value if isinstance(value, str) and re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", value) else None
                for key, value in (("provider", getattr(chat, "provider", None)),
                                   ("model", getattr(chat, "model", None)))}
            return {"schema_version": 1, **labels,
                "wall_seconds": round(max(0.0, self.clock() - self.started), 6),
                "model_occupied_seconds": round(occupied(self._models), 6),
                "model_requests": sum(self._counts.values()),
                "model_inflight": self._inflight,
                "accounting_complete": self._inflight == 0 and accounting["model_calls"] is not None
                    and accounting["usage_unavailable_calls"] == 0,
                "accounting": accounting,
                "model_options": safe_options,
                "stages": {name: {"occupied_seconds": round(occupied(self._intervals.get(name, ())), 6),
                    "model_requests": self._counts[name], "operations": self._operations[name],
                    "input_bytes": self._inputs[name]}
                    for name in sorted(names)},
                "model_outcomes": dict(self._outcomes), "contains_raw_payload": False,
                "stage_intervals_may_overlap": True}


class MeasuredChat:
    """Same ChatPort; each request uses the turn's remaining deadline and accounting."""

    def __init__(self, delegate, receipt, *, remaining_s, budget=None, stopped=lambda: False):
        self._delegate = require_chat_port(delegate, role="measured chat delegate")
        self._receipt, self._remaining = receipt, remaining_s
        self._budget = budget
        self._stopped = stopped
        self.model = delegate.model
        self.meter = getattr(delegate, "meter", None)
        self.options = getattr(delegate, "options", None)

    def _call(self, messages, tools, run_id, *, cancelled=None, **kwargs):
        upstream = cancelled
        if upstream is not None or self._budget is not None:
            cancelled = lambda: bool((upstream is not None and upstream()) or self._stopped()
                                     or (self._budget is not None and
                                         (self._budget.stopped or self._budget.cancelled())))
        if self._budget is not None:
            self._budget.reserve()
        left = self._remaining()
        if left <= 0:
            raise ProviderError("the turn deadline elapsed", code="provider_deadline",
                                retryable=False, dispatched=False)
        if cancelled is not None and cancelled():
            raise ProviderError("the turn was cancelled", code="provider_cancelled",
                                retryable=False, dispatched=False)
        ceiling = kwargs.get("timeout_s")
        kwargs["timeout_s"] = min(left, ceiling) if ceiling is not None else left
        handle, succeeded = self._receipt.start_model(messages, tools), False
        try:
            if cancelled is None:
                reply = self._delegate.complete(messages, tools, run_id, **kwargs)
            else:
                reply = self._delegate.complete_cancellable(messages, tools, run_id,
                                                            cancelled=cancelled, **kwargs)
            if self._budget is not None:
                self._budget.check()
            succeeded = True
            return reply
        except BudgetExhausted as exc:
            if self._budget is not None:
                self._budget.fail(exc)
            raise
        finally:
            self._receipt.end_model(handle, succeeded=succeeded)

    def complete(self, messages, tools, run_id, max_tokens=None, options=None, timeout_s=None):
        return self._call(messages, tools, run_id, max_tokens=max_tokens,
                          options=options, timeout_s=timeout_s)

    def complete_cancellable(self, messages, tools, run_id, *, cancelled,
                             max_tokens=None, options=None, timeout_s=None):
        return self._call(messages, tools, run_id, cancelled=cancelled,
                          max_tokens=max_tokens, options=options, timeout_s=timeout_s)
