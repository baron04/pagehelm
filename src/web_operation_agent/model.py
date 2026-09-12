"""Model client and spend accounting.

Every actual model call must correspond to exactly one line in the spend ledger, and every
ledger line must correspond to an actual call. The ledger file, price table and cost formula
therefore form one accounting contract rather than independent estimates.

The ledger path is overridable so tests never need to touch the real ledger; the test runner
also fingerprints the configured real file before and after the suite as a final guard.
"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import http.client
import ipaddress
import inspect
import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable

from .durable import SQLiteProviderQuota
from .egress import DEFAULT_PROVIDER_EGRESS_POLICY, ProviderEgressPolicy

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LEDGER = os.path.expanduser("~/.cache/web-operation-agent/model-spend.jsonl")

def load_env(path: str | Path | None = None) -> dict:
    """Read `.env` without echoing anything. Values are returned, never logged."""
    p = Path(path or REPO_ROOT / ".env")
    out: dict[str, str] = {}
    if not p.exists():
        return out
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


class ProviderError(Exception):
    """Callers map this to terminal reason `provider_error`."""

    def __init__(self, message: str, *, code: str = "provider_error", retryable: bool = True,
                 request_id: str = "", dispatched: bool | None = None):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.request_id = request_id
        if dispatched is not None and not isinstance(dispatched, bool):
            raise TypeError("provider dispatched state must be a boolean or None")
        self.dispatched = dispatched


class _ProviderHTTPStatus(Exception):
    def __init__(self, status: int):
        super().__init__(f"provider returned HTTP {status}")
        self.status = status


class ProviderOutcome(str, Enum):
    SUCCESS = "success"
    TRANSIENT_PROVIDER_FAILURE = "transient_provider_failure"
    PERMANENT_PROVIDER_FAILURE = "permanent_provider_failure"
    CANCELLED = "cancelled"
    DEADLINE = "deadline"


class ProviderRuntimeGuard:
    """Bound concurrent provider calls and stop a transient-failure stampede.

    The guard is intentionally in-process and content-free. A deployment can share one
    instance across multiple ``ChatModel`` objects that use the same endpoint. It never retries
    a paid request because an acknowledgement loss cannot prove that the provider did no work.
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


#: Per-model prices, per million tokens. Registered rather than assumed: a wrong price does
#: not fail, it quietly mis-states the running total. A model with no entry therefore raises
#: instead of guessing, and adding one requires an explicit dated price entry.
PRICES = json.loads((Path(__file__).parent / "policies" / "prices.json").read_text())



#: Extra request-body fields per model, keyed by the model's own name. See the file's own
#: `_why_thinking_budget` note for the rationale. Keyed by model rather than branched on in
#: code so changing model is a policy edit and an unknown model receives no unsupported fields.
MODEL_OPTIONS = json.loads((Path(__file__).parent / "policies" / "model_options.json").read_text())

#: Fields the caller owns; a policy may never reach in and rewrite the request itself.
_RESERVED_BODY = (
    "model", "messages", "tools", "tool_choice", "max_tokens", "temperature", "stream")
MAX_PROVIDER_REQUEST_BYTES = 2 * 1024 * 1024
MAX_PROVIDER_RESPONSE_BYTES = 4 * 1024 * 1024


def options_for(model: str) -> dict:
    """Extra body fields for a model. Unknown models get none, deliberately."""
    opt = MODEL_OPTIONS.get(model)
    return {k: v for k, v in opt.items() if k not in _RESERVED_BODY} \
        if isinstance(opt, dict) else {}


class UnpricedModel(ProviderError):
    """A model was used that has no registered price. Never silently mis-cost a run."""


def price_for(model: str) -> dict:
    """The price table for a model, or a refusal naming what to add."""
    if model in PRICES:
        return PRICES[model]
    raise UnpricedModel(
        f"no price registered for model {model!r}; add it to "
        f"src/web_operation_agent/policies/prices.json with the date it was taken, or the "
        f"ledger will under- or over-state what this run cost")


def cost_with(price: dict, i: int, o: int, c: int) -> float:
    """The frozen formula: cached prompt tokens at the cache rate, the rest at the input rate,
    completions at the output rate. An impossible figure is an accounting error, never clamped."""
    if c < 0 or c > i or i < 0 or o < 0:
        raise ValueError(f"impossible usage: in={i} out={o} cached={c}")
    return ((i - c) / 1e6 * price["input_per_m"] + c / 1e6 * price["cache_read_per_m"]
            + o / 1e6 * price["output_per_m"])


def _host_label(base_url: str) -> str:
    """A short, stable label from an endpoint's host, for the ledger only."""
    try:
        host = urllib.parse.urlsplit(base_url).hostname or ""
    except Exception:
        return "openai-compatible"
    parts = [p for p in host.split(".") if p not in ("www", "api", "com", "cn", "io", "ai", "co")]
    return parts[0] if parts else (host or "openai-compatible")


def _display_path(path) -> str:
    """A path fit to store in a shared record: relative to home where it lives there."""
    text = str(path)
    home = os.path.expanduser("~")
    return "~" + text[len(home):] if home and text.startswith(home) else text


def _provider_base_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if (not parsed.hostname or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment):
        raise ProviderError("AGENT_OPENAI_BASE_URL must be an absolute provider base URL")
    loopback = parsed.hostname.casefold() == "localhost"
    try:
        loopback = loopback or ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        pass
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise ProviderError(
            "AGENT_OPENAI_BASE_URL must use HTTPS except for a loopback development endpoint")
    return value.rstrip("/")


class BudgetExhausted(Exception):
    def __init__(self, kind: str, detail: str):
        super().__init__(detail)
        self.kind = kind


class SpendMeter:
    """Cumulative usage, one ledger line per call, with the two stop lines."""

    def __init__(self, *, ledger: str | Path | None = None, batch_id: str = "",
                 run_input_token_abort: int = 220000, batch_cost_stop: float | None = None,
                 price: dict | None = None):
        # Precedence: an explicit argument, then the environment override, then the
        # project ledger. The environment is how a test run redirects everything at once;
        # a caller that names a file means that file.
        self.ledger = Path(ledger or os.environ.get("WOA_SPEND_LEDGER") or DEFAULT_LEDGER)
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.batch_id = batch_id
        self.run_input_token_abort = run_input_token_abort
        self.batch_cost_stop = batch_cost_stop
        self._lock = threading.Lock()
        self.calls = 0
        self.unmetered_calls = 0
        self.total_in = self.total_out = self.total_cached = self.total_reasoning = 0
        self.batch_in = self.batch_out = self.batch_cached = 0
        self.run_in: dict[str, int] = {}
        self._run_summaries: dict[str, dict] = {}
        # No default rate. A meter costs nothing until `ChatModel` sets the price of the
        # model actually in use; a module-level fallback would book a run at some other
        # model's rates and the ledger would look right while being wrong.
        self.price = price

    def cost_of(self, i: int, o: int, c: int) -> float:
        if self.price is None:
            # Nothing used cannot be mis-stated, so a meter that has not metered still
            # reports. The moment there are tokens, an unknown rate has to stop: a wrong
            # rate does not fail, it makes the running total look right while being wrong.
            if not (i or o or c):
                return 0.0
            raise UnpricedModel(
                "this meter has no price: construct it through ChatModel, which registers "
                "the price of the model in use, or pass price= explicitly")
        return cost_with(self.price, i, o, c)

    def batch_cost(self) -> float:
        return self.cost_of(self.batch_in, self.batch_out, self.batch_cached)

    @staticmethod
    def _reported_model(value: str | None) -> str:
        if value is None:
            return ""
        if (not isinstance(value, str) or not value.strip() or len(value) > 120
                or re.search(r"[\x00-\x1f\x7f]", value)):
            raise ValueError("provider reported_model must be a bounded safe string")
        return value.strip()

    @staticmethod
    def _empty_run_summary() -> dict:
        return {
            "model_calls": 0,
            "usage_unavailable_calls": 0,
            "known_input_tokens": 0,
            "known_output_tokens": 0,
            "known_cached_tokens": 0,
            "known_reasoning_tokens": 0,
            "known_cost_cny": 0.0,
            "reported_models": set(),
            "request_ids": [],
            "record_digests": [],
        }

    def _commit_run_record(self, *, run_id: str, rec: dict, reported_model: str,
                           request_id: str, unmetered: bool, known_cost: float = 0.0) -> None:
        summary = self._run_summaries.setdefault(run_id, self._empty_run_summary())
        summary["model_calls"] += 1
        summary["usage_unavailable_calls"] += int(unmetered)
        if not unmetered:
            summary["known_input_tokens"] += rec["in"]
            summary["known_output_tokens"] += rec["out"]
            summary["known_cached_tokens"] += rec["cached"]
            summary["known_reasoning_tokens"] += rec["reasoning"]
            summary["known_cost_cny"] += known_cost
        if reported_model:
            summary["reported_models"].add(reported_model)
        if request_id:
            summary["request_ids"].append(request_id)
        summary["record_digests"].append(hashlib.sha256(json.dumps(
            rec, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False).encode("utf-8")).hexdigest())

    def record(self, *, provider: str, model: str, run_id: str, usage: dict,
               request_id: str = "", reported_model: str | None = None) -> dict:
        if not isinstance(usage, dict):
            raise ValueError("provider usage must be an object")
        if request_id and not re.fullmatch(r"[0-9a-f]{32}", request_id):
            raise ValueError("provider request_id must be a 32-character lowercase hex value")
        selected_reported_model = self._reported_model(reported_model)
        prompt_details = usage.get("prompt_tokens_details")
        completion_details = usage.get("completion_tokens_details")
        prompt_details = {} if prompt_details is None else prompt_details
        completion_details = {} if completion_details is None else completion_details
        if not isinstance(prompt_details, dict) or not isinstance(completion_details, dict):
            raise ValueError("provider usage detail must be an object")

        def token_count(value, name: str) -> int:
            value = 0 if value is None else value
            if (isinstance(value, bool) or not isinstance(value, int)
                    or not 0 <= value <= 1_000_000_000):
                raise ValueError(f"{name} must be a non-negative bounded integer")
            return value

        i = token_count(usage.get("prompt_tokens"), "prompt_tokens")
        o = token_count(usage.get("completion_tokens"), "completion_tokens")
        c = token_count(prompt_details.get("cached_tokens"), "cached_tokens")
        # Reasoning is billed inside the completion count, so the cost is already right, but
        # it is worth its own column: on a reasoning model most of a planning call can be
        # thought rather than answer, and the source arm makes an extra planning call the
        # control arm does not. Absent from providers that do not report it.
        r = token_count(completion_details.get("reasoning_tokens"), "reasoning_tokens")
        if c > i or r > o:
            raise ValueError("provider usage detail exceeds its containing token count")
        call_cost = self.cost_of(i, o, c)
        with self._lock:
            next_calls = self.calls + 1
            next_total_in = self.total_in + i
            next_total_out = self.total_out + o
            next_total_cached = self.total_cached + c
            next_batch_in = self.batch_in + i
            next_batch_out = self.batch_out + o
            next_batch_cached = self.batch_cached + c
            next_run_in = self.run_in.get(run_id, 0) + i
            next_batch_cost = self.cost_of(
                next_batch_in, next_batch_out, next_batch_cached)
            rec = {"t": time.time(), "provider": provider, "model": model,
                   "batch_id": self.batch_id, "logical_run_id": run_id,
                   "accounting_status": "reported",
                   "in": i, "out": o, "cached": c, "reasoning": r,
                   "cum_in": next_total_in, "cum_out": next_total_out,
                   "call_cost_cny": round(call_cost, 6),
                   "batch_cost_cny": round(next_batch_cost, 4),
                   "run_in_tokens": next_run_in, "calls": next_calls}
            if request_id:
                rec["provider_request_id"] = request_id
            if selected_reported_model:
                rec["provider_reported_model"] = selected_reported_model
            self._append_record(rec, batch_recount=True)
            next_batch_cost = rec["batch_cost_cny"]
            self.calls = next_calls
            self.total_in = next_total_in
            self.total_out = next_total_out
            self.total_cached = next_total_cached
            self.total_reasoning += r
            self.batch_in = next_batch_in
            self.batch_out = next_batch_out
            self.batch_cached = next_batch_cached
            self.run_in[run_id] = next_run_in
            self._commit_run_record(
                run_id=run_id, rec=rec, reported_model=selected_reported_model,
                request_id=request_id, unmetered=False, known_cost=call_cost)
        if next_run_in > self.run_input_token_abort:
            raise BudgetExhausted("token_budget",
                                  f"run {run_id} consumed {next_run_in} input tokens, "
                                  f"over the {self.run_input_token_abort} ceiling")
        if self.batch_cost_stop is not None and next_batch_cost > self.batch_cost_stop:
            raise BudgetExhausted("batch_cost_stop",
                                  f"batch spend {next_batch_cost:.4f} passed the "
                                  f"{self.batch_cost_stop:.4f} stop line")
        return rec

    def record_unmetered(self, *, provider: str, model: str, run_id: str,
                         request_id: str, error_code: str,
                         reported_model: str | None = None) -> dict:
        """Record a dispatched call whose response supplied no trustworthy usage.

        A timeout or broken response can still be billed. Recording zero tokens or omitting
        the call would understate both traffic and uncertainty, so this row uses explicit null
        usage and cost fields for later provider-side reconciliation.
        """
        if not re.fullmatch(r"[0-9a-f]{32}", request_id):
            raise ValueError("provider request_id must be a 32-character lowercase hex value")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error_code):
            raise ValueError("provider error_code must be a bounded lowercase identifier")
        selected_reported_model = self._reported_model(reported_model)
        with self._lock:
            next_calls = self.calls + 1
            rec = {
                "t": time.time(), "provider": provider, "model": model,
                "batch_id": self.batch_id, "logical_run_id": run_id,
                "provider_request_id": request_id,
                "accounting_status": "usage_unavailable",
                "error_code": error_code,
                "in": None, "out": None, "cached": None, "reasoning": None,
                "call_cost_cny": None,
                "batch_cost_cny": round(self.batch_cost(), 4),
                "run_in_tokens": self.run_in.get(run_id, 0), "calls": next_calls,
            }
            if selected_reported_model:
                rec["provider_reported_model"] = selected_reported_model
            # A dispatched call with no usage adds no tokens, but its batch column must still
            # report the batch's spend rather than this process's share of it.
            self._append_record(rec, batch_recount=True)
            self.calls = next_calls
            self.unmetered_calls += 1
            self._commit_run_record(
                run_id=run_id, rec=rec, reported_model=selected_reported_model,
                request_id=request_id, unmetered=True)
        return rec

    def _batch_totals_from_ledger(self, fh) -> tuple[int, int, int] | None:
        """Sum this batch's reported tokens from the ledger. The caller holds the lock.

        Returns ``None`` when the batch has no rows yet, which a fresh ledger and a first
        call look alike, and lets the caller keep its own totals.
        """
        try:
            fh.seek(0)
            rows = fh.readlines()
        except OSError:
            return None
        totals, seen = [0, 0, 0], False
        for line in rows:
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if (not isinstance(row, dict) or row.get("batch_id") != self.batch_id
                    or row.get("accounting_status") != "reported"):
                continue
            seen = True
            for index, key in enumerate(("in", "out", "cached")):
                value = row.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    totals[index] += value
        return (totals[0], totals[1], totals[2]) if seen else None

    def _append_record(self, rec: dict, *, batch_recount: bool = False) -> dict:
        """Append one record, optionally recomputing its batch column under the lock.

        A batch sharded across processes accounts to one ledger, so process-local batch
        totals let every shard spend the stop line in full. With a stop line configured the
        batch column is recomputed from the ledger inside the same exclusive lock that
        serialises the write, which makes the figure the stop is checked against the batch's
        real spend rather than this process's share of it.
        """
        if not batch_recount or self.batch_cost_stop is None:
            self._locked_write(rec)
            return rec
        with open(self.ledger, "a+", encoding="utf-8") as fh:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            except OSError:
                pass
            try:
                totals = self._batch_totals_from_ledger(fh)
                if totals is not None:
                    def contribution(key: str) -> int:
                        value = rec.get(key)
                        return value if isinstance(value, int) and not isinstance(value, bool) else 0

                    rec["batch_cost_cny"] = round(self.cost_of(
                        totals[0] + contribution("in"),
                        totals[1] + contribution("out"),
                        totals[2] + contribution("cached")), 4)
                fh.seek(0, os.SEEK_END)
                fh.write(json.dumps(rec) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            finally:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
        return rec

    def _locked_write(self, rec: dict) -> None:
        with open(self.ledger, "a", encoding="utf-8") as fh:
            # The lock is between processes, not threads: sharding a batch across isolated
            # environments runs one harness process per worker and they all account to the one
            # ledger. A torn line there is a cost figure nobody can reconstruct.
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            except OSError:
                pass                      # no locking on this filesystem; single writer
            try:
                fh.write(json.dumps(rec) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            finally:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass

    def report(self) -> dict:
        return {"provider_calls": self.calls,
                "unmetered_provider_calls": self.unmetered_calls,
                "input_tokens": self.total_in,
                "output_tokens": self.total_out, "cached_tokens": self.total_cached,
                "reasoning_tokens": self.total_reasoning,
                "cost_cny": round(self.cost_of(self.total_in, self.total_out, self.total_cached), 4),
                # Home-relative, so a run record can be read and compared without carrying
                # whose machine produced it. The file used is unchanged; only its displayed
                # name is normalized.
                "ledger": _display_path(self.ledger), "batch_id": self.batch_id}

    def run_report(self, run_id: str) -> dict:
        """Return a content-free accounting slice for one logical run.

        Request IDs and spend rows remain private. Their ordered digests bind a trial record
        to the exact provider calls and ledger rows without exposing correlation IDs.
        """
        with self._lock:
            summary = self._run_summaries.get(run_id, self._empty_run_summary())
            request_ids = list(summary["request_ids"])
            record_digests = list(summary["record_digests"])
            calls = summary["model_calls"]
            request_digest = None
            if calls and len(request_ids) == calls:
                request_digest = "sha256:" + hashlib.sha256(json.dumps(
                    request_ids, separators=(",", ":")).encode("utf-8")).hexdigest()
            slice_digest = None
            if record_digests:
                slice_digest = "sha256:" + hashlib.sha256(json.dumps(
                    record_digests, separators=(",", ":")).encode("utf-8")).hexdigest()
            known_cost = round(summary["known_cost_cny"], 6)
            unavailable = summary["usage_unavailable_calls"]
            return {
                "model_calls": calls,
                "usage_unavailable_calls": unavailable,
                "provider_reported_models": sorted(summary["reported_models"]),
                "provider_request_ids_digest": request_digest,
                "known_input_tokens": summary["known_input_tokens"],
                "known_output_tokens": summary["known_output_tokens"],
                "known_cached_tokens": summary["known_cached_tokens"],
                "known_reasoning_tokens": summary["known_reasoning_tokens"],
                "known_cost_cny": known_cost,
                "total_cost_cny": None if unavailable else known_cost,
                "spend_ledger_slice_digest": slice_digest,
            }


#: The instance state a real :class:`ChatModel` only has once its constructor ran. A double
#: that subclasses the concrete provider to satisfy a type check inherits the real methods
#: without this state, so the first model call fails with ``AttributeError`` inside the turn
#: instead of failing as an implementation. ``require_chat_port`` refuses that shape.
_PROVIDER_STATE = ("base", "_key", "meter", "max_completion_tokens", "temperature",
                   "timeout", "runtime_guard", "egress_policy")


@runtime_checkable
class ChatPort(Protocol):
    """The one boundary a turn uses to reach a model.

    Both entry points are mandatory. Cancellation is not an optional capability discovered at
    call time: an implementation that cannot abort a dispatched request must say so by not
    satisfying this port, never by silently completing a run the user already stopped.
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
    """Return `candidate` if it satisfies :class:`ChatPort`, else raise ``TypeError``.

    Composition calls this so a wrong shape fails where it is wired, not on the first model
    call several layers down. The checks are deliberately structural: presence of both entry
    points, a keyword-only cancellation hook, a usable model label, and -- the case that made
    `evals/woa` report 22 of 36 failures at `61603fe` -- no half-built subclass of the
    concrete provider standing in for an implementation.
    """
    for name in ("complete", "complete_cancellable"):
        method = getattr(candidate, name, None)
        if not callable(method):
            raise TypeError(
                f"{role} must implement ChatPort.{name}; cancellation is not optional")
    if isinstance(candidate, ChatModel):
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


class ChatModel:
    """A single OpenAI-compatible chat endpoint with native tool calling."""

    def __init__(self, meter: SpendMeter, *, env: dict | None = None, model: str | None = None,
                 max_completion_tokens: int = 2048, temperature: float = 0.0,
                 timeout: int = 180, provider: str | None = None,
                 options: dict | None = None,
                 egress_policy: ProviderEgressPolicy | None = None,
                 runtime_guard: ProviderRuntimeGuard | None = None,
                 shared_quota: SQLiteProviderQuota | None = None):
        env = env if env is not None else load_env()
        raw_base = env.get("AGENT_OPENAI_BASE_URL") or ""
        self._key = env.get("AGENT_OPENAI_API_KEY") or ""
        self.model = model or env.get("AGENT_OPENAI_MODEL") or ""
        # A label for the ledger, nothing more. It has no behavioural meaning -- the code path
        # is the same for every OpenAI-compatible endpoint -- and defaults to the host in the
        # base URL so switching providers needs no code change, only .env.
        if not (raw_base and self._key and self.model):
            raise ProviderError(
                "AGENT_OPENAI_BASE_URL / _API_KEY / _MODEL are not all set. "
                "`AGENT_OPENAI_MODEL_PRO` was renamed to `AGENT_OPENAI_MODEL`: nothing "
                "ever used a second tier, and the old name made a flash model only "
                "settable through a key called PRO. It is not read as a fallback -- an "
                "unnoticed old name would silently pick a different model than intended.")
        if (not isinstance(raw_base, str) or not isinstance(self._key, str)
                or not isinstance(self.model, str)
                or any(re.search(r"[\x00-\x1f\x7f]", value)
                       for value in (raw_base, self._key, self.model))):
            raise ProviderError("provider endpoint, credential, and model must be safe strings")
        self.base = _provider_base_url(raw_base)
        provider = provider or env.get("AGENT_PROVIDER") or _host_label(self.base)
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError("provider timeout must be a positive finite number")
        if (isinstance(max_completion_tokens, bool)
                or not isinstance(max_completion_tokens, int)
                or not 1 <= max_completion_tokens <= 1_000_000):
            raise ValueError("max_completion_tokens must be an integer from 1 to 1000000")
        if (isinstance(temperature, bool) or not isinstance(temperature, (int, float))
                or not math.isfinite(temperature) or not 0 <= temperature <= 2):
            raise ValueError("temperature must be a finite number from 0 to 2")
        if shared_quota is not None and not isinstance(shared_quota, SQLiteProviderQuota):
            raise TypeError("shared_quota must be SQLiteProviderQuota or None")
        self.shared_quota = shared_quota
        self.meter = meter
        # Fail now, loudly, rather than book a run at another model's rates.
        meter.price = price_for(self.model)
        self.provider = provider
        self.max_completion_tokens = max_completion_tokens
        self.temperature = temperature
        self.timeout = timeout
        self.egress_policy = egress_policy or DEFAULT_PROVIDER_EGRESS_POLICY
        self.runtime_guard = (ProviderRuntimeGuard()
                              if runtime_guard is None else runtime_guard)
        if not isinstance(self.runtime_guard, ProviderRuntimeGuard):
            raise TypeError("runtime_guard must be ProviderRuntimeGuard or None")
        if options is not None and not isinstance(options, dict):
            raise ValueError("provider options must be an object or None")
        self.options = ({k: v for k, v in options.items() if k not in _RESERVED_BODY}
                        if options is not None else options_for(self.model))

    def complete(self, messages: list[dict], tools: list[dict] | None, run_id: str,
                 max_tokens: int | None = None, options: dict | None = None,
                 timeout_s: float | None = None) -> dict:
        return self._complete(
            messages, tools, run_id, max_tokens=max_tokens, options=options,
            timeout_s=timeout_s, cancelled=None)

    def complete_cancellable(
            self, messages: list[dict], tools: list[dict] | None, run_id: str,
            *, cancelled, max_tokens: int | None = None, options: dict | None = None,
            timeout_s: float | None = None) -> dict:
        """Complete one call while allowing the owning turn to abort its socket."""
        if not callable(cancelled):
            raise TypeError("cancelled must be callable")
        return self._complete(
            messages, tools, run_id, max_tokens=max_tokens, options=options,
            timeout_s=timeout_s, cancelled=cancelled)

    def _complete(self, messages: list[dict], tools: list[dict] | None, run_id: str,
                  *, max_tokens: int | None, options: dict | None,
                  timeout_s: float | None, cancelled) -> dict:
        """`max_tokens` overrides the per-call ceiling for this one request.

        The action loop's 2,048 is frozen: one structured tool call never needs more, and a
        model that spends thousands of reasoning tokens per click buys nothing. A contract
        document is a different shape of answer, and the shared ceiling truncated it mid-JSON
        in four of six source-arm runs, which measured the ceiling rather than the contract.

        `max_tokens` turns out to bound only the answer on at least one endpoint we use, so
        it cannot by itself stop a model thinking for two minutes about a click. `options`
        carries the per-model fields that can -- see `policies/model_options.json`; a caller
        passing its own merges over the model's, and neither may touch the fields above.
        """
        effective_max_tokens = self.max_completion_tokens if max_tokens is None else max_tokens
        if (isinstance(effective_max_tokens, bool)
                or not isinstance(effective_max_tokens, int)
                or not 1 <= effective_max_tokens <= 1_000_000):
            raise ValueError("max_tokens must be an integer from 1 to 1000000")
        if options is not None and not isinstance(options, dict):
            raise ValueError("provider options must be an object or None")
        body = {"model": self.model, "messages": messages, "temperature": self.temperature,
                "max_tokens": effective_max_tokens}
        body.update(self.options)
        if options:
            body.update({k: v for k, v in options.items() if k not in _RESERVED_BODY})
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        data, request_id = self._request_completion_with_receipt(
            body, run_id, timeout_s=timeout_s, cancelled=cancelled)
        choices = data.get("choices") or []
        if not isinstance(choices, list) or not choices:
            raise ProviderError("provider returned no choices", request_id=request_id)
        choice = choices[0]
        if not isinstance(choice, dict):
            raise ProviderError("provider choice must be an object", request_id=request_id)
        message = choice.get("message") or {}
        if not isinstance(message, dict):
            raise ProviderError("provider message must be an object", request_id=request_id)
        message["finish_reason"] = choice.get("finish_reason") or ""
        message.setdefault("reasoning",
                           message.get("reasoning_content") or message.get("reasoning") or "")
        return message

    def request_completion(self, body: dict, run_id: str,
                           *, timeout_s: float | None = None) -> dict:
        """Dispatch one caller-serialized chat completion through the protected provider path.

        This entry point exists for a pinned framework adapter such as PageAgent that owns its
        native message and tool-choice serialization. It neither injects nor rewrites request
        fields; it enforces the configured endpoint/model, data boundary, budgets and spend
        accounting, then returns the full OpenAI-compatible response.
        """
        data, _request_id = self._request_completion_with_receipt(
            body, run_id, timeout_s=timeout_s)
        return data

    def _request_completion_with_receipt(
            self, body: dict, run_id: str,
            *, timeout_s: float | None = None, cancelled=None) -> tuple[dict, str]:
        if timeout_s is not None and (
                isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float))
                or not math.isfinite(timeout_s) or timeout_s <= 0):
            raise ValueError("timeout_s must be a positive finite number")
        if not isinstance(body, dict):
            raise ProviderError("provider request body must be an object")
        if body.get("model") != self.model:
            raise ProviderError("provider request model must match the configured model")
        if body.get("stream") not in (None, False):
            raise ProviderError("streaming provider responses are unsupported")
        getattr(
            self, "egress_policy", DEFAULT_PROVIDER_EGRESS_POLICY
        ).inspect_completion_body(body)
        request_timeout = self.timeout if timeout_s is None else min(self.timeout, timeout_s)
        try:
            payload = json.dumps(
                body, ensure_ascii=False, separators=(",", ":"),
                allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, OverflowError) as exc:
            raise ProviderError("provider request body must be finite JSON") from exc
        if len(payload) > MAX_PROVIDER_REQUEST_BYTES:
            raise ProviderError("provider request body exceeds the 2 MiB limit")
        if cancelled is not None and cancelled():
            raise ProviderError(
                "provider call cancelled before dispatch", code="provider_cancelled",
                retryable=False, dispatched=False)
        guard = getattr(self, "runtime_guard", None)
        acquired = False
        request_id = ""
        request_dispatched = False
        try:
            waited_s = (
                guard.acquire(request_timeout, cancelled=cancelled)
                if guard is not None and cancelled is not None else
                guard.acquire(request_timeout) if guard is not None else 0.0)
            acquired = guard is not None
            socket_timeout = request_timeout - waited_s
            if socket_timeout <= 0:
                raise ProviderError(
                    "provider concurrency wait exhausted the request deadline",
                    code="provider_capacity",
                )
            quota = getattr(self, "shared_quota", None)
            if quota is not None and not quota.reserve():
                raise ProviderError("shared provider request quota exhausted",
                                    code="provider_capacity", dispatched=False)
            request_id = uuid.uuid4().hex
            url = f"{self.base}/chat/completions"
            headers = {"content-type": "application/json",
                       "authorization": f"Bearer {self._key}",
                       "x-woa-request-id": request_id}
            try:
                if cancelled is None:
                    req = urllib.request.Request(
                        url, data=payload, method="POST", headers=headers)
                    request_dispatched = True
                    with urllib.request.urlopen(req, timeout=socket_timeout) as r:
                        raw_response = r.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
                else:
                    raw_response = self._cancellable_post(
                        url, payload, headers, socket_timeout, cancelled)
                    request_dispatched = True
                if len(raw_response) > MAX_PROVIDER_RESPONSE_BYTES:
                    raise ProviderError(
                        "provider response exceeds the 4 MiB limit",
                        code="provider_response",
                        request_id=request_id,
                    )
                data = json.loads(raw_response)
            except urllib.error.HTTPError as e:
                transient = e.code in {408, 425, 429} or 500 <= e.code <= 599
                if guard is not None:
                    guard.record_outcome(
                        ProviderOutcome.TRANSIENT_PROVIDER_FAILURE if transient
                        else ProviderOutcome.PERMANENT_PROVIDER_FAILURE)
                e.close()
                self.meter.record_unmetered(
                    provider=self.provider, model=self.model, run_id=run_id,
                    request_id=request_id, error_code="provider_http")
                raise ProviderError(
                    f"provider returned HTTP {e.code}",
                    code="provider_http",
                    retryable=transient,
                    request_id=request_id,
                ) from e
            except _ProviderHTTPStatus as e:
                transient = e.status in {408, 425, 429} or 500 <= e.status <= 599
                if guard is not None:
                    guard.record_outcome(
                        ProviderOutcome.TRANSIENT_PROVIDER_FAILURE if transient
                        else ProviderOutcome.PERMANENT_PROVIDER_FAILURE)
                self.meter.record_unmetered(
                    provider=self.provider, model=self.model, run_id=run_id,
                    request_id=request_id, error_code="provider_http")
                raise ProviderError(
                    f"provider returned HTTP {e.status}", code="provider_http",
                    retryable=transient, request_id=request_id) from e
            except ProviderError as exc:
                if guard is not None:
                    guard.record_outcome(self._provider_outcome(exc))
                dispatched = (request_dispatched
                              if exc.dispatched is None else exc.dispatched)
                exc.dispatched = dispatched
                if dispatched:
                    exc.request_id = exc.request_id or request_id
                    self.meter.record_unmetered(
                        provider=self.provider, model=self.model, run_id=run_id,
                        request_id=request_id, error_code=exc.code)
                else:
                    exc.request_id = ""
                raise
            except Exception as e:
                deadline = self._is_deadline_error(e)
                if guard is not None:
                    guard.record_outcome(
                        ProviderOutcome.DEADLINE if deadline
                        else ProviderOutcome.TRANSIENT_PROVIDER_FAILURE)
                error_code = "provider_deadline" if deadline else "provider_transport"
                self.meter.record_unmetered(
                    provider=self.provider, model=self.model, run_id=run_id,
                    request_id=request_id, error_code=error_code)
                raise ProviderError(
                    f"provider call failed: {type(e).__name__}",
                    code=error_code, retryable=not deadline,
                    request_id=request_id, dispatched=True,
                ) from e
            else:
                if guard is not None:
                    guard.record_outcome(ProviderOutcome.SUCCESS)
        finally:
            if acquired:
                guard.release()
        if not isinstance(data, dict):
            self.meter.record_unmetered(
                provider=self.provider, model=self.model, run_id=run_id,
                request_id=request_id, error_code="provider_response")
            raise ProviderError(
                "provider response must be a JSON object",
                code="provider_response", request_id=request_id)
        usage = data.get("usage") or {}
        if (not isinstance(usage, dict)
                or "prompt_tokens" not in usage
                or "completion_tokens" not in usage):
            reported_model = data.get("model")
            try:
                reported_model = SpendMeter._reported_model(reported_model)
            except ValueError:
                reported_model = ""
            self.meter.record_unmetered(
                provider=self.provider, model=self.model, run_id=run_id,
                request_id=request_id, error_code="provider_usage",
                reported_model=reported_model or None)
            raise ProviderError(
                "provider usage must report prompt_tokens and completion_tokens",
                code="provider_usage", request_id=request_id)
        # Record before inspecting the answer: a malformed reply still cost money.
        try:
            reported_model = data.get("model")
            if reported_model is not None and not isinstance(reported_model, str):
                raise ValueError("provider reported model is invalid")
            self.meter.record(
                provider=self.provider, model=self.model, run_id=run_id, usage=usage,
                request_id=request_id, reported_model=reported_model)
        except ValueError as exc:
            self.meter.record_unmetered(
                provider=self.provider, model=self.model, run_id=run_id,
                request_id=request_id, error_code="provider_usage")
            raise ProviderError(
                "provider usage is invalid",
                code="provider_usage", request_id=request_id) from exc
        return data, request_id

    @staticmethod
    def _provider_outcome(error: ProviderError) -> ProviderOutcome:
        if error.code == "provider_cancelled":
            return ProviderOutcome.CANCELLED
        if error.code == "provider_deadline":
            return ProviderOutcome.DEADLINE
        return (ProviderOutcome.TRANSIENT_PROVIDER_FAILURE if error.retryable
                else ProviderOutcome.PERMANENT_PROVIDER_FAILURE)

    @staticmethod
    def _is_deadline_error(error: BaseException) -> bool:
        if isinstance(error, TimeoutError):
            return True
        return (isinstance(error, urllib.error.URLError)
                and isinstance(getattr(error, "reason", None), TimeoutError))

    @staticmethod
    def _cancellable_post(url: str, payload: bytes, headers: dict[str, str],
                          timeout_s: float, cancelled) -> bytes:
        """POST through a closeable stdlib connection and abort every blocking phase."""
        started = time.monotonic()
        parsed = urllib.parse.urlsplit(url)
        target_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        proxy_url = urllib.request.getproxies().get(parsed.scheme, "")
        use_proxy = bool(proxy_url and not urllib.request.proxy_bypass(parsed.hostname or ""))
        if use_proxy:
            proxy = urllib.parse.urlsplit(proxy_url)
            if proxy.scheme != "http" or not proxy.hostname:
                raise ProviderError(
                    "cancellable provider transport requires an HTTP CONNECT proxy",
                    code="provider_configuration", retryable=False)
            proxy_headers = {}
            if proxy.username is not None:
                credential = (urllib.parse.unquote(proxy.username) + ":"
                              + urllib.parse.unquote(proxy.password or ""))
                proxy_headers["Proxy-Authorization"] = "Basic " + base64.b64encode(
                    credential.encode()).decode()
            if parsed.scheme == "https":
                connection = http.client.HTTPSConnection(
                    proxy.hostname, proxy.port or 80, timeout=max(0.001, timeout_s))
                connection.set_tunnel(
                    parsed.hostname, target_port, headers=proxy_headers)
            else:
                connection = http.client.HTTPConnection(
                    proxy.hostname, proxy.port or 80, timeout=max(0.001, timeout_s))
                path = url
                headers = {**headers, **proxy_headers}
        else:
            connection_type = (http.client.HTTPSConnection if parsed.scheme == "https"
                               else http.client.HTTPConnection)
            connection = connection_type(
                parsed.hostname, parsed.port, timeout=max(0.001, timeout_s))
        connect_done = threading.Event()
        abandon_connect = threading.Event()
        connect_error: list[BaseException] = []

        def connect() -> None:
            try:
                connection.connect()
            except BaseException as exc:  # delivered to the owning call below
                connect_error.append(exc)
            finally:
                if abandon_connect.is_set():
                    connection.close()
                connect_done.set()

        if cancelled():
            raise ProviderError(
                "provider call cancelled before dispatch", code="provider_cancelled",
                retryable=False, dispatched=False)
        connector = threading.Thread(
            target=connect, name="woa-provider-connect", daemon=True)
        connector.start()
        while not connect_done.wait(.05):
            if cancelled():
                abandon_connect.set()
                connection.close()
                raise ProviderError(
                    "provider call cancelled while connecting", code="provider_cancelled",
                    retryable=False, dispatched=False)
            if time.monotonic() - started >= timeout_s:
                abandon_connect.set()
                connection.close()
                raise ProviderError(
                    "provider deadline elapsed while connecting", code="provider_deadline",
                    retryable=False, dispatched=False)
        if connect_error:
            if cancelled():
                raise ProviderError(
                    "provider call cancelled while connecting", code="provider_cancelled",
                    retryable=False, dispatched=False) from connect_error[0]
            if (time.monotonic() - started >= timeout_s
                    or ChatModel._is_deadline_error(connect_error[0])):
                raise ProviderError(
                    "provider deadline elapsed while connecting", code="provider_deadline",
                    retryable=False, dispatched=False) from connect_error[0]
            raise ProviderError(
                f"provider connection failed: {type(connect_error[0]).__name__}",
                code="provider_transport", retryable=True,
                dispatched=False) from connect_error[0]
        remaining = timeout_s - (time.monotonic() - started)
        if remaining <= 0:
            connection.close()
            raise ProviderError(
                "provider deadline elapsed before dispatch", code="provider_deadline",
                retryable=False, dispatched=False)
        connection.timeout = max(.001, remaining)
        if connection.sock is not None:
            connection.sock.settimeout(max(.001, remaining))
        finished = threading.Event()
        was_cancelled = threading.Event()

        def watch() -> None:
            while not finished.wait(.05):
                if cancelled():
                    was_cancelled.set()
                    sock = connection.sock
                    if sock is not None:
                        try:
                            sock.shutdown(2)
                        except OSError:
                            pass
                    connection.close()
                    return

        watcher = threading.Thread(target=watch, name="woa-provider-cancel", daemon=True)
        watcher.start()
        dispatched = False
        try:
            if cancelled():
                raise ProviderError(
                    "provider call cancelled before dispatch", code="provider_cancelled",
                    retryable=False, dispatched=False)
            dispatched = True
            connection.request("POST", path, body=payload, headers=headers)
            response = connection.getresponse()
            if not 200 <= response.status <= 299:
                raise _ProviderHTTPStatus(response.status)
            return response.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
        except ProviderError:
            raise
        except Exception as exc:
            if was_cancelled.is_set() or cancelled():
                raise ProviderError(
                    "provider call cancelled", code="provider_cancelled",
                    retryable=False, dispatched=dispatched) from exc
            if ChatModel._is_deadline_error(exc):
                raise ProviderError(
                    "provider deadline elapsed", code="provider_deadline",
                    retryable=False, dispatched=dispatched) from exc
            raise
        finally:
            finished.set()
            connection.close()
            watcher.join(timeout=1)
