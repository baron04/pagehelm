"""Model client and spend accounting.

Every actual model call must correspond to exactly one line in the spend ledger, and every
ledger line must correspond to an actual call. The ledger file, price table and cost formula
therefore form one accounting contract rather than independent estimates.

The ledger path is overridable so tests never need to touch the real ledger; the test runner
also fingerprints the configured real file before and after the suite as a final guard.
"""
from __future__ import annotations

import fcntl
import ipaddress
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
from pathlib import Path

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
                 request_id: str = ""):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.request_id = request_id


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

    def acquire(self, timeout_s: float) -> float:
        """Acquire one request slot and return seconds spent waiting for it."""
        if (isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float))
                or not math.isfinite(timeout_s) or timeout_s <= 0):
            raise ValueError("provider guard timeout must be a positive finite number")
        started = self._clock()
        with self._lock:
            self._require_closed_circuit()
        if not self._slots.acquire(timeout=float(timeout_s)):
            raise ProviderError(
                "provider concurrency limit was not available before the request deadline",
                code="provider_capacity",
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

    def record_success(self) -> None:
        with self._lock:
            self._consecutive_transient_failures = 0
            self._open_until = 0.0

    def record_failure(self, *, transient: bool) -> None:
        with self._lock:
            if not transient:
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

#: The model used by existing ledger entries. Keeping it as the module-level default preserves
#: a comparable cumulative figure; the accounting tests verify the shared price contract.
DEFAULT_PRICE_MODEL = "MiniMax-M3"
PRICE = PRICES[DEFAULT_PRICE_MODEL]


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
        self.price = price or PRICE

    def cost_of(self, i: int, o: int, c: int) -> float:
        return cost_with(self.price, i, o, c)

    def batch_cost(self) -> float:
        return self.cost_of(self.batch_in, self.batch_out, self.batch_cached)

    def record(self, *, provider: str, model: str, run_id: str, usage: dict,
               request_id: str = "") -> dict:
        if not isinstance(usage, dict):
            raise ValueError("provider usage must be an object")
        if request_id and not re.fullmatch(r"[0-9a-f]{32}", request_id):
            raise ValueError("provider request_id must be a 32-character lowercase hex value")
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
            self.calls += 1
            self.total_in += i
            self.total_out += o
            self.total_cached += c
            self.total_reasoning += r
            self.batch_in += i
            self.batch_out += o
            self.batch_cached += c
            self.run_in[run_id] = self.run_in.get(run_id, 0) + i
            rec = {"t": time.time(), "provider": provider, "model": model,
                   "batch_id": self.batch_id, "logical_run_id": run_id,
                   "accounting_status": "reported",
                   "in": i, "out": o, "cached": c, "reasoning": r,
                   "cum_in": self.total_in, "cum_out": self.total_out,
                   "call_cost_cny": round(call_cost, 6),
                   "batch_cost_cny": round(self.batch_cost(), 4),
                   "run_in_tokens": self.run_in[run_id], "calls": self.calls}
            if request_id:
                rec["provider_request_id"] = request_id
            self._append_record(rec)
        if self.run_in[run_id] > self.run_input_token_abort:
            raise BudgetExhausted("token_budget",
                                  f"run {run_id} consumed {self.run_in[run_id]} input tokens, "
                                  f"over the {self.run_input_token_abort} ceiling")
        if self.batch_cost_stop is not None and self.batch_cost() > self.batch_cost_stop:
            raise BudgetExhausted("batch_cost_stop",
                                  f"batch spend {self.batch_cost():.4f} passed the "
                                  f"{self.batch_cost_stop:.4f} stop line")
        return rec

    def record_unmetered(self, *, provider: str, model: str, run_id: str,
                         request_id: str, error_code: str) -> dict:
        """Record a dispatched call whose response supplied no trustworthy usage.

        A timeout or broken response can still be billed. Recording zero tokens or omitting
        the call would understate both traffic and uncertainty, so this row uses explicit null
        usage and cost fields for later provider-side reconciliation.
        """
        if not re.fullmatch(r"[0-9a-f]{32}", request_id):
            raise ValueError("provider request_id must be a 32-character lowercase hex value")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error_code):
            raise ValueError("provider error_code must be a bounded lowercase identifier")
        with self._lock:
            self.calls += 1
            self.unmetered_calls += 1
            rec = {
                "t": time.time(), "provider": provider, "model": model,
                "batch_id": self.batch_id, "logical_run_id": run_id,
                "provider_request_id": request_id,
                "accounting_status": "usage_unavailable",
                "error_code": error_code,
                "in": None, "out": None, "cached": None, "reasoning": None,
                "call_cost_cny": None,
                "batch_cost_cny": round(self.batch_cost(), 4),
                "run_in_tokens": self.run_in.get(run_id, 0), "calls": self.calls,
            }
            self._append_record(rec)
        return rec

    def _append_record(self, rec: dict) -> None:
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


class ChatModel:
    """A single OpenAI-compatible chat endpoint with native tool calling."""

    def __init__(self, meter: SpendMeter, *, env: dict | None = None, model: str | None = None,
                 max_completion_tokens: int = 2048, temperature: float = 0.0,
                 timeout: int = 180, provider: str | None = None,
                 options: dict | None = None,
                 egress_policy: ProviderEgressPolicy | None = None,
                 runtime_guard: ProviderRuntimeGuard | None = None):
        env = env if env is not None else load_env()
        raw_base = env.get("AGENT_OPENAI_BASE_URL") or ""
        self._key = env.get("AGENT_OPENAI_API_KEY") or ""
        self.model = model or env.get("AGENT_OPENAI_MODEL_PRO") or ""
        # A label for the ledger, nothing more. It has no behavioural meaning -- the code path
        # is the same for every OpenAI-compatible endpoint -- and defaults to the host in the
        # base URL so switching providers needs no code change, only .env.
        if not (raw_base and self._key and self.model):
            raise ProviderError("AGENT_OPENAI_BASE_URL / _API_KEY / _MODEL_PRO are not all set")
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
        if timeout_s is not None and (
                isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float))
                or not math.isfinite(timeout_s) or timeout_s <= 0):
            raise ValueError("timeout_s must be a positive finite number")
        getattr(self, "egress_policy", DEFAULT_PROVIDER_EGRESS_POLICY).inspect(
            messages, tools)
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
        request_timeout = self.timeout if timeout_s is None else min(self.timeout, timeout_s)
        try:
            payload = json.dumps(
                body, ensure_ascii=False, separators=(",", ":"),
                allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, OverflowError) as exc:
            raise ProviderError("provider request body must be finite JSON") from exc
        if len(payload) > MAX_PROVIDER_REQUEST_BYTES:
            raise ProviderError("provider request body exceeds the 2 MiB limit")
        guard = getattr(self, "runtime_guard", None)
        acquired = False
        request_id = ""
        try:
            waited_s = guard.acquire(request_timeout) if guard is not None else 0.0
            acquired = guard is not None
            socket_timeout = request_timeout - waited_s
            if socket_timeout <= 0:
                raise ProviderError(
                    "provider concurrency wait exhausted the request deadline",
                    code="provider_capacity",
                )
            request_id = uuid.uuid4().hex
            req = urllib.request.Request(
                f"{self.base}/chat/completions", data=payload, method="POST",
                headers={"content-type": "application/json",
                         "authorization": f"Bearer {self._key}",
                         "x-woa-request-id": request_id})
            try:
                with urllib.request.urlopen(req, timeout=socket_timeout) as r:
                    raw_response = r.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
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
                    guard.record_failure(transient=transient)
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
            except ProviderError as exc:
                if guard is not None:
                    guard.record_failure(transient=True)
                exc.request_id = exc.request_id or request_id
                self.meter.record_unmetered(
                    provider=self.provider, model=self.model, run_id=run_id,
                    request_id=request_id, error_code=exc.code)
                raise
            except Exception as e:
                if guard is not None:
                    guard.record_failure(transient=True)
                self.meter.record_unmetered(
                    provider=self.provider, model=self.model, run_id=run_id,
                    request_id=request_id, error_code="provider_transport")
                raise ProviderError(
                    f"provider call failed: {type(e).__name__}",
                    code="provider_transport",
                    request_id=request_id,
                ) from e
            else:
                if guard is not None:
                    guard.record_success()
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
            self.meter.record_unmetered(
                provider=self.provider, model=self.model, run_id=run_id,
                request_id=request_id, error_code="provider_usage")
            raise ProviderError(
                "provider usage must report prompt_tokens and completion_tokens",
                code="provider_usage", request_id=request_id)
        # Record before inspecting the answer: a malformed reply still cost money.
        try:
            self.meter.record(
                provider=self.provider, model=self.model, run_id=run_id, usage=usage,
                request_id=request_id)
        except ValueError as exc:
            self.meter.record_unmetered(
                provider=self.provider, model=self.model, run_id=run_id,
                request_id=request_id, error_code="provider_usage")
            raise ProviderError(
                "provider usage is invalid",
                code="provider_usage", request_id=request_id) from exc
        choices = data.get("choices") or []
        if not isinstance(choices, list) or not choices:
            raise ProviderError("provider returned no choices", request_id=request_id)
        choice = choices[0]
        if not isinstance(choice, dict):
            raise ProviderError("provider choice must be an object", request_id=request_id)
        message = choice.get("message") or {}
        if not isinstance(message, dict):
            raise ProviderError("provider message must be an object", request_id=request_id)
        # Two provider-neutral signals lifted to the top of the message so callers never have
        # to know which vendor produced it:
        #  * finish_reason distinguishes "the answer was cut off" (a budget problem the caller
        #    can fix) from "the model stopped on its own" (an answer problem it cannot);
        #  * reasoning is whatever the provider returned in a separate reasoning channel, under
        #    any of the several field names vendors use for it. Inline <think> text is handled
        #    later, in split_reasoning; this only catches the out-of-band form.
        message["finish_reason"] = choice.get("finish_reason") or ""
        message.setdefault("reasoning",
                           message.get("reasoning_content") or message.get("reasoning") or "")
        return message
