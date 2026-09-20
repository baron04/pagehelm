"""Provider-call failures and the in-process runtime guard.

This module owns the provider health state that is shared by model clients.  It is
deliberately content-free: it bounds concurrent calls and records only coarse outcomes,
never prompts, responses, credentials, or retryable request bodies.
"""
from __future__ import annotations

import math
import threading
import time
from enum import Enum


class ProviderError(Exception):
    """Callers map this to terminal reason ``provider_error``."""

    def __init__(self, message: str, *, code: str = "provider_error", retryable: bool = True,
                 request_id: str = "", dispatched: bool | None = None):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.request_id = request_id
        if dispatched is not None and not isinstance(dispatched, bool):
            raise TypeError("provider dispatched state must be a boolean or None")
        self.dispatched = dispatched


class ProviderOutcome(str, Enum):
    SUCCESS = "success"
    TRANSIENT_PROVIDER_FAILURE = "transient_provider_failure"
    PERMANENT_PROVIDER_FAILURE = "permanent_provider_failure"
    CANCELLED = "cancelled"
    DEADLINE = "deadline"


class ProviderRuntimeGuard:
    """Bound concurrent provider calls and stop a transient-failure stampede.

    The guard is intentionally in-process and content-free. A deployment can share one
    instance across multiple model clients that use the same endpoint. It never retries a
    paid request because an acknowledgement loss cannot prove that the provider did no work.
    """

    def __init__(self, *, max_concurrent_requests: int = 4,
                 failure_threshold: int = 3, cooldown_s: float = 30,
                 clock=None):
        integer_limits = {
            "max_concurrent_requests": (max_concurrent_requests, 1, 1_000),
            "failure_threshold": (failure_threshold, 1, 100),
        }
        for name, (value, minimum, maximum) in integer_limits.items():
            if (isinstance(value, bool) or not isinstance(value, int)
                    or not minimum <= value <= maximum):
                raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
        if (isinstance(cooldown_s, bool) or not isinstance(cooldown_s, (int, float))
                or not math.isfinite(cooldown_s) or not 0 < cooldown_s <= 3_600):
            raise ValueError("cooldown_s must be a finite number from 0 to 3600")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable or None")
        self.max_concurrent_requests = max_concurrent_requests
        self.failure_threshold = failure_threshold
        self.cooldown_s = float(cooldown_s)
        self._clock = clock or time.monotonic
        self._slots = threading.BoundedSemaphore(max_concurrent_requests)
        self._lock = threading.Lock()
        self._in_flight = 0
        self._consecutive_transient_failures = 0
        self._open_until = 0.0

    def _require_closed_circuit(self) -> None:
        now = self._clock()
        if now < self._open_until:
            raise ProviderError(
                "provider circuit is temporarily open after repeated transient failures",
                code="provider_circuit_open",
            )
        if self._open_until:
            self._open_until = 0.0
            self._consecutive_transient_failures = 0

    def acquire(self, timeout_s: float, *, cancelled=None) -> float:
        """Acquire one request slot and return seconds spent waiting for it."""
        if (isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float))
                or not math.isfinite(timeout_s) or timeout_s <= 0):
            raise ValueError("provider guard timeout must be a positive finite number")
        if cancelled is not None and not callable(cancelled):
            raise TypeError("provider guard cancelled hook must be callable or None")
        started = self._clock()
        with self._lock:
            self._require_closed_circuit()
        if cancelled is not None and cancelled():
            raise ProviderError(
                "provider call cancelled before dispatch", code="provider_cancelled",
                retryable=False, dispatched=False)
        if cancelled is None:
            acquired = self._slots.acquire(timeout=float(timeout_s))
        else:
            deadline = time.monotonic() + float(timeout_s)
            acquired = False
            while not acquired:
                if cancelled():
                    raise ProviderError(
                        "provider call cancelled while waiting for capacity",
                        code="provider_cancelled", retryable=False, dispatched=False)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                acquired = self._slots.acquire(timeout=min(.05, remaining))
        if not acquired:
            raise ProviderError(
                "provider concurrency limit was not available before the request deadline",
                code="provider_capacity", dispatched=False,
            )
        try:
            with self._lock:
                self._require_closed_circuit()
                self._in_flight += 1
        except BaseException:
            self._slots.release()
            raise
        return max(0.0, self._clock() - started)

    def release(self) -> None:
        with self._lock:
            if self._in_flight <= 0:
                raise RuntimeError("provider request slot was released without an acquisition")
            self._in_flight -= 1
        self._slots.release()

    def record_outcome(self, outcome: ProviderOutcome) -> None:
        """Update provider health only from provider health evidence.

        Cancellation and a caller-owned deadline say nothing about provider health, so they
        neither add to nor erase an existing transient-failure sequence.
        """
        if not isinstance(outcome, ProviderOutcome):
            raise TypeError("provider outcome must be a ProviderOutcome")
        with self._lock:
            if outcome in {ProviderOutcome.CANCELLED, ProviderOutcome.DEADLINE}:
                return
            if outcome in {ProviderOutcome.SUCCESS,
                           ProviderOutcome.PERMANENT_PROVIDER_FAILURE}:
                self._consecutive_transient_failures = 0
                self._open_until = 0.0
                return
            self._consecutive_transient_failures += 1
            if self._consecutive_transient_failures >= self.failure_threshold:
                self._open_until = self._clock() + self.cooldown_s

    def status(self) -> dict:
        with self._lock:
            now = self._clock()
            return {
                "max_concurrent_requests": self.max_concurrent_requests,
                "in_flight": self._in_flight,
                "consecutive_transient_failures": self._consecutive_transient_failures,
                "circuit_open": now < self._open_until,
                "retry_after_s": round(max(0.0, self._open_until - now), 3),
            }
