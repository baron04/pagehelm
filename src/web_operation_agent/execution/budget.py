"""One turn's shared model-step and time budget; changing stages never resets it.

One model request, including a retry, consumes one step. Fixed browser and typed read
substeps check this same deadline and cancellation but have their own bounded execution.
"""
from __future__ import annotations

import threading
import time
import math

from ..modeling.spend import BudgetExhausted
from ..transport.browser import TurnCancelled


class TurnBudget:
    def __init__(self, *, steps, seconds, cancelled=lambda: False, clock=time.monotonic):
        if isinstance(steps, bool) or not isinstance(steps, int) or steps < 1:
            raise ValueError("model steps must be a positive integer")
        if (isinstance(seconds, bool) or not isinstance(seconds, (int, float))
                or not math.isfinite(seconds) or seconds <= 0):
            raise ValueError("turn seconds must be positive and finite")
        if not callable(cancelled) or not callable(clock):
            raise TypeError("turn clock and cancellation hook must be callable")
        self.steps, self.seconds = steps, seconds
        self.clock, self.started, self.cancelled = clock, clock(), cancelled
        self.used = 0
        self._failure = None
        self._lock = threading.Lock()

    def remaining(self):
        return max(0.0, self.seconds - (self.clock() - self.started))

    @property
    def stopped(self):
        with self._lock:
            return self._failure is not None

    def fail(self, exc):
        with self._lock:
            self._failure = self._failure or (exc.kind, str(exc))

    def check(self):
        if self.cancelled():
            raise TurnCancelled("the person stopped this turn")
        with self._lock:
            failed = self._failure
        if failed is not None:
            raise BudgetExhausted(*failed)
        remaining = self.remaining()
        if remaining <= 0:
            exc = BudgetExhausted("wall_budget", "the shared turn deadline elapsed")
            self.fail(exc)
            raise exc
        return remaining

    def reserve(self):
        self.check()
        with self._lock:
            if self._failure is not None:
                raise BudgetExhausted(*self._failure)
            if self.used >= self.steps:
                self._failure = self._failure or ("step_budget", "the shared model request limit was reached")
                raise BudgetExhausted(*self._failure)
            self.used += 1

    def projection(self):
        with self._lock:
            return {"model_requests_used": self.used, "model_request_limit": self.steps,
                    "terminal_reason": self._failure[0] if self._failure else ""}
