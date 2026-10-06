"""OpenAI-compatible model client and the server-side chat port."""
from __future__ import annotations

import base64
import http.client
import ipaddress
import json
import math
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from .chat import (ChatPort, KeyedLimitedChat, KeyedProviderAdmission,
                   _ConcreteChatProvider, require_chat_port)
from ..state.quota import SQLiteProviderQuota
from .egress import DEFAULT_PROVIDER_EGRESS_POLICY, ProviderEgressPolicy
from .provider import ProviderError, ProviderOutcome, ProviderRuntimeGuard
from .reasoning import REASONING_FIELDS, request_options
from .spend import (DEFAULT_LEDGER, PRICES, BudgetExhausted, SpendMeter, UnpricedModel,
                    cost_with, price_for, reported_model_string)

REPO_ROOT = Path(__file__).resolve().parents[3]


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


class _ProviderHTTPStatus(Exception):
    def __init__(self, status: int):
        super().__init__(f"provider returned HTTP {status}")
        self.status = status


def _provider_http_error_code(status: int) -> str:
    """Classify HTTP failures without retaining provider response content."""
    if status == 429:
        return "provider_rate_limited"
    if status in {408, 425} or 500 <= status <= 599:
        return "provider_unavailable"
    if status in {401, 403}:
        return "provider_authentication"
    return "provider_http"


#: Compatibility options only. Reasoning mode belongs to the server call site.
MODEL_OPTIONS = json.loads(
    (Path(__file__).parents[1] / "policies" / "model_options.json").read_text())

#: Fields the caller owns; a policy may never reach in and rewrite the request itself.
_RESERVED_BODY = (
    "model", "messages", "tools", "tool_choice", "max_tokens", "temperature", "stream")
MAX_PROVIDER_REQUEST_BYTES = 2 * 1024 * 1024
MAX_PROVIDER_RESPONSE_BYTES = 4 * 1024 * 1024


def options_for(model: str) -> dict:
    """Extra body fields for a model. Unknown models get none, deliberately."""
    opt = MODEL_OPTIONS.get(model)
    return {k: v for k, v in opt.items() if k not in _RESERVED_BODY and k not in REASONING_FIELDS} \
        if isinstance(opt, dict) else {}


def _host_label(base_url: str) -> str:
    """A short, stable label from an endpoint's host, for the ledger only."""
    try:
        host = urllib.parse.urlsplit(base_url).hostname or ""
    except Exception:
        return "openai-compatible"
    parts = [p for p in host.split(".") if p not in ("www", "api", "com", "cn", "io", "ai", "co")]
    return parts[0] if parts else (host or "openai-compatible")


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


class _ConnectionCache:
    """A few idle provider connections, per destination, for the next call to reuse.

    Idle connections older than `IDLE_S` are closed rather than reused: providers and
    proxies drop idle keep-alive connections on their own schedule, and a stale one costs
    a failed request before the retry. Two per destination is enough for a turn's
    sequential calls and a planner call beside it.
    """

    MAX_IDLE = 2
    IDLE_S = 30.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._idle: dict[tuple, list[tuple[object, float]]] = {}

    def take(self, key: tuple):
        now = time.monotonic()
        stale = []
        chosen = None
        with self._lock:
            entries = self._idle.get(key, [])
            while entries:
                connection, since = entries.pop()
                if now - since <= self.IDLE_S and getattr(connection, "sock", None) is not None:
                    chosen = connection
                    break
                stale.append(connection)
        for connection in stale:
            connection.close()
        return chosen

    def give(self, key: tuple, connection) -> None:
        with self._lock:
            entries = self._idle.setdefault(key, [])
            if len(entries) < self.MAX_IDLE:
                entries.append((connection, time.monotonic()))
                return
        connection.close()

    def close_all(self) -> None:
        with self._lock:
            idle = [connection for entries in self._idle.values() for connection, _ in entries]
            self._idle.clear()
        for connection in idle:
            connection.close()


class ChatModel(_ConcreteChatProvider):
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
        selected = ({k: v for k, v in options.items()
                     if k not in _RESERVED_BODY and k not in REASONING_FIELDS}
                    if options is not None else options_for(self.model))
        self.options = request_options(self.model, self.base, selected)

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

        Ordinary calls explicitly disable reasoning for supported endpoints. A server call
        site can select a complex-plan scenario through options; answer-budget growth does
        not increase reasoning effort. Model configuration cannot select the scenario.
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
        selected = dict(self.options)
        if options:
            selected.update({k: v for k, v in options.items() if k not in _RESERVED_BODY})
        body.update(request_options(self.model, self.base, selected))
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
        within_allowance = getattr(self.meter, "within_allowance", None)
        if within_allowance is not None and not within_allowance():
            raise ProviderError("the daily model allowance is spent",
                                code="provider_allowance_spent", retryable=False,
                                dispatched=False)
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
                error_code = _provider_http_error_code(e.code)
                if guard is not None:
                    guard.record_outcome(
                        ProviderOutcome.TRANSIENT_PROVIDER_FAILURE if transient
                        else ProviderOutcome.PERMANENT_PROVIDER_FAILURE)
                e.close()
                self.meter.record_unmetered(
                    provider=self.provider, model=self.model, run_id=run_id,
                    request_id=request_id, error_code=error_code)
                raise ProviderError(
                    f"provider returned HTTP {e.code}",
                    code=error_code,
                    retryable=transient,
                    request_id=request_id,
                ) from e
            except _ProviderHTTPStatus as e:
                transient = e.status in {408, 425, 429} or 500 <= e.status <= 599
                error_code = _provider_http_error_code(e.status)
                if guard is not None:
                    guard.record_outcome(
                        ProviderOutcome.TRANSIENT_PROVIDER_FAILURE if transient
                        else ProviderOutcome.PERMANENT_PROVIDER_FAILURE)
                self.meter.record_unmetered(
                    provider=self.provider, model=self.model, run_id=run_id,
                    request_id=request_id, error_code=error_code)
                raise ProviderError(
                    f"provider returned HTTP {e.status}", code=error_code,
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

    def _cancellable_post(self, url: str, payload: bytes, headers: dict[str, str],
                          timeout_s: float, cancelled) -> bytes:
        """POST through a closeable, reusable stdlib connection; abort every blocking phase.

        Each call used to open a new TCP and TLS connection and close it afterwards. Measured
        against the provider on 2026-09-22, a request on a fresh connection took a median
        1.87 s (worst 6.65 s) and one on a kept connection 0.37 s, and the extra handshakes
        were where most transport failures happened. A connection that ends a call cleanly
        -- whole body read, no `Connection: close` -- now goes back to a small per-model
        cache; a cancelled, failed or deadline-bound call still closes its own connection,
        so the cancellation contract is unchanged.

        Two retries, each at most once, and only where no request can have been processed:
        a new connection that fails before anything was sent, and a kept connection the
        provider had already closed (it fails before a status line arrives). Nothing that
        received a response is ever sent twice.
        """
        started = time.monotonic()
        parsed = urllib.parse.urlsplit(url)
        target_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        proxy_url = urllib.request.getproxies().get(parsed.scheme, "")
        use_proxy = bool(proxy_url and not urllib.request.proxy_bypass(parsed.hostname or ""))
        proxy_headers: dict[str, str] = {}
        proxy = None
        if use_proxy:
            proxy = urllib.parse.urlsplit(proxy_url)
            if proxy.scheme != "http" or not proxy.hostname:
                raise ProviderError(
                    "cancellable provider transport requires an HTTP CONNECT proxy",
                    code="provider_configuration", retryable=False)
            if proxy.username is not None:
                credential = (urllib.parse.unquote(proxy.username) + ":"
                              + urllib.parse.unquote(proxy.password or ""))
                proxy_headers["Proxy-Authorization"] = "Basic " + base64.b64encode(
                    credential.encode()).decode()
            if parsed.scheme != "https":
                path = url
                headers = {**headers, **proxy_headers}
        key = (parsed.scheme, parsed.hostname, target_port,
               (proxy.hostname, proxy.port) if proxy is not None else None,
               tuple(sorted(proxy_headers.items())))
        cache = self._connection_cache()

        def fresh() -> http.client.HTTPConnection:
            if proxy is not None and parsed.scheme == "https":
                connection = http.client.HTTPSConnection(
                    proxy.hostname, proxy.port or 80, timeout=max(0.001, timeout_s))
                connection.set_tunnel(parsed.hostname, target_port, headers=proxy_headers)
                return connection
            if proxy is not None:
                return http.client.HTTPConnection(
                    proxy.hostname, proxy.port or 80, timeout=max(0.001, timeout_s))
            connection_type = (http.client.HTTPSConnection if parsed.scheme == "https"
                               else http.client.HTTPConnection)
            return connection_type(parsed.hostname, parsed.port,
                                   timeout=max(0.001, timeout_s))

        def connect(connection: http.client.HTTPConnection) -> None:
            connect_done = threading.Event()
            abandon_connect = threading.Event()
            connect_error: list[BaseException] = []

            def run() -> None:
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
                target=run, name="woa-provider-connect", daemon=True)
            connector.start()
            while not connect_done.wait(.05):
                if cancelled():
                    abandon_connect.set()
                    connection.close()
                    raise ProviderError(
                        "provider call cancelled while connecting",
                        code="provider_cancelled", retryable=False, dispatched=False)
                if time.monotonic() - started >= timeout_s:
                    abandon_connect.set()
                    connection.close()
                    raise ProviderError(
                        "provider deadline elapsed while connecting",
                        code="provider_deadline", retryable=False, dispatched=False)
            if connect_error:
                connection.close()
                if cancelled():
                    raise ProviderError(
                        "provider call cancelled while connecting",
                        code="provider_cancelled", retryable=False,
                        dispatched=False) from connect_error[0]
                if (time.monotonic() - started >= timeout_s
                        or ChatModel._is_deadline_error(connect_error[0])):
                    raise ProviderError(
                        "provider deadline elapsed while connecting",
                        code="provider_deadline", retryable=False,
                        dispatched=False) from connect_error[0]
                raise ProviderError(
                    f"provider connection failed: {type(connect_error[0]).__name__}",
                    code="provider_transport", retryable=True,
                    dispatched=False) from connect_error[0]

        def open_new() -> http.client.HTTPConnection:
            """A connected fresh connection; one more attempt if nothing was sent."""
            for attempt in (1, 2):
                connection = fresh()
                try:
                    connect(connection)
                    return connection
                except ProviderError as exc:
                    if (attempt == 2 or exc.code != "provider_transport"
                            or time.monotonic() - started >= timeout_s):
                        raise
                    self._transport_retries += 1
            raise AssertionError("unreachable")

        connection = cache.take(key)
        reused = connection is not None
        if connection is None:
            connection = open_new()
        while True:
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

            def watch(connection=connection, finished=finished,
                      was_cancelled=was_cancelled) -> None:
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
            keep = False
            stale = False
            try:
                if cancelled():
                    raise ProviderError(
                        "provider call cancelled before dispatch", code="provider_cancelled",
                        retryable=False, dispatched=False)
                dispatched = True
                try:
                    connection.request("POST", path, body=payload, headers=headers)
                    response = connection.getresponse()
                except (http.client.RemoteDisconnected, ConnectionResetError,
                        BrokenPipeError) as exc:
                    # A kept connection the provider closed while it sat idle fails here,
                    # before any status line: the request was never processed. Anything
                    # else is the provider's answer and is not retried.
                    if (reused and not was_cancelled.is_set() and not cancelled()
                            and time.monotonic() - started < timeout_s):
                        stale = True
                    raise
                if not 200 <= response.status <= 299:
                    raise _ProviderHTTPStatus(response.status)
                body = response.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
                keep = (not response.will_close and response.isclosed()
                        and not was_cancelled.is_set() and not cancelled())
                return body
            except ProviderError:
                raise
            except Exception as exc:
                if stale:
                    pass
                elif was_cancelled.is_set() or cancelled():
                    raise ProviderError(
                        "provider call cancelled", code="provider_cancelled",
                        retryable=False, dispatched=dispatched) from exc
                elif ChatModel._is_deadline_error(exc):
                    raise ProviderError(
                        "provider deadline elapsed", code="provider_deadline",
                        retryable=False, dispatched=dispatched) from exc
                else:
                    raise
            finally:
                finished.set()
                watcher.join(timeout=1)
                if keep:
                    cache.give(key, connection)
                else:
                    connection.close()
            # Only a stale kept connection reaches here: retry once on a new connection.
            self._stale_retries += 1
            reused = False
            connection = open_new()

    def close_connections(self) -> None:
        """Close the idle provider connections this model kept for reuse."""
        cache = self.__dict__.get("_connections")
        if cache is not None:
            cache.close_all()

    def _connection_cache(self) -> "_ConnectionCache":
        cache = self.__dict__.get("_connections")
        if cache is None:
            cache = self.__dict__.setdefault("_connections", _ConnectionCache())
            self.__dict__.setdefault("_transport_retries", 0)
            self.__dict__.setdefault("_stale_retries", 0)
        return cache
