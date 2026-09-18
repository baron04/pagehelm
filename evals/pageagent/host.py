"""Server-held provider boundary for the pinned PageAgent browser bridge."""
from __future__ import annotations

import hmac
import json
import math
import re
import threading
import time
from dataclasses import dataclass
from typing import Any


_PROVIDER_URL = "https://provider.invalid/v1/chat/completions"
_MAX_REQUEST_BYTES = 2 * 1024 * 1024
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_SAFE_MODEL = re.compile(r"[^\x00-\x1f\x7f]{1,120}\Z")


#: Where a host failure came from. ``provider`` is a failed call the completion client typed --
#: the gateway recorded it and can explain it. ``limit`` is one of the run's own limits, a
#: scorable ending. ``host`` is this proxy or the bridge failing on its own: nothing the provider
#: did, and nothing the gateway can explain.
ORIGINS = ("provider", "limit", "host")


class PageAgentHostError(RuntimeError):
    """A content-free baseline-host failure safe to cross the page binding."""

    def __init__(self, code: str, *, origin: str = "host"):
        if origin not in ORIGINS:
            raise ValueError("origin is invalid")
        super().__init__(code)
        self.code = code
        self.origin = origin


def _number(value: Any, name: str, minimum: float, maximum: float) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not minimum <= value <= maximum):
        raise ValueError(f"{name} is invalid")
    return float(value)


@dataclass(frozen=True, slots=True)
class PageAgentProviderContract:
    model: str
    temperature: float
    top_p: float
    max_completion_tokens: int
    max_model_calls: int
    wall_time_s: float
    parallel_tool_calls: bool = False

    def __post_init__(self) -> None:
        if (not isinstance(self.model, str) or self.model != self.model.strip()
                or not _SAFE_MODEL.fullmatch(self.model)):
            raise ValueError("model is invalid")
        object.__setattr__(
            self, "temperature", _number(self.temperature, "temperature", 0, 2))
        object.__setattr__(self, "top_p", _number(self.top_p, "top_p", 0, 1))
        for name, maximum in (("max_completion_tokens", 131_072),
                              ("max_model_calls", 100)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise ValueError(f"{name} is invalid")
        object.__setattr__(
            self, "wall_time_s", _number(self.wall_time_s, "wall_time_s", 1, 3_600))
        if self.parallel_tool_calls is not False:
            raise ValueError("parallel tool calls must be disabled")

    def validate_body(self, body: Any) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise PageAgentHostError("transport_contract")
        expected = {
            "model": self.model,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_completion_tokens": self.max_completion_tokens,
            "parallel_tool_calls": False,
        }
        if any(body.get(key) != value for key, value in expected.items()):
            raise PageAgentHostError("transport_contract")
        if body.get("stream") not in (None, False):
            raise PageAgentHostError("transport_contract")
        return body

    def bridge_request_options(self) -> dict[str, Any]:
        return {
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_completion_tokens": self.max_completion_tokens,
            "parallel_tool_calls": False,
        }


def require_completion_client(candidate: Any, *, error: type[Exception] = TypeError,
                              role: str = "client") -> Any:
    """Return `candidate` if it satisfies the PageAgent completion port.

    PageAgent reaches the provider through `request_completion`, not through the product's
    `ChatPort`, so this is a separate and narrower contract: one dispatch method, a frozen
    model label and a meter that can project one run. It is checked structurally on purpose.
    An `isinstance(candidate, ChatModel)` gate here would accept any subclass of the concrete
    provider whose constructor never ran, which is the shape that made `evals/woa` report
    apparatus failures at `61603fe`.
    """
    if not callable(getattr(candidate, "request_completion", None)):
        raise error(f"{role} must provide request_completion")
    label = getattr(candidate, "model", None)
    if not isinstance(label, str) or not label.strip() or label.strip() != label:
        raise error(f"{role}.model must be a non-empty label without surrounding space")
    if not callable(getattr(getattr(candidate, "meter", None), "run_report", None)):
        raise error(f"{role} meter must provide run_report")
    return candidate


class PageAgentProviderProxy:
    """Bind one opaque browser run to one guarded server-side completion client."""

    def __init__(self, *, run_token: str, run_id: str, client: Any,
                 contract: PageAgentProviderContract, cancelled: threading.Event | None = None,
                 clock: Any = None):
        if (not isinstance(run_token, str) or not 16 <= len(run_token) <= 128
                or re.search(r"[\x00-\x1f\x7f]", run_token)):
            raise ValueError("run_token is invalid")
        if (not isinstance(run_id, str) or not 1 <= len(run_id) <= 160
                or re.search(r"[\x00-\x1f\x7f]", run_id)):
            raise ValueError("run_id is invalid")
        require_completion_client(client)
        if getattr(client, "model", None) != contract.model:
            raise ValueError("client model does not match the frozen contract")
        if cancelled is not None and not isinstance(cancelled, threading.Event):
            raise TypeError("cancelled must be a threading.Event or None")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable or None")
        self._run_token = run_token
        self.run_id = run_id
        self.client = client
        self.contract = contract
        self.cancelled = cancelled or threading.Event()
        self._clock = clock or time.monotonic
        self._deadline = self._clock() + contract.wall_time_s
        self._lock = threading.Lock()
        self._attempts = 0
        self._completed = 0
        self._last_error_code = ""

    def _remaining(self) -> float:
        remaining = self._deadline - self._clock()
        if remaining <= 0:
            raise PageAgentHostError("provider_deadline", origin="limit")
        return remaining

    def handle(self, payload: Any) -> dict[str, Any]:
        if self.cancelled.is_set():
            raise PageAgentHostError("cancelled")
        if not isinstance(payload, dict) or set(payload) != {"run_token", "url", "method", "body"}:
            raise PageAgentHostError("transport_contract")
        token = payload.get("run_token")
        if not isinstance(token, str) or not hmac.compare_digest(token, self._run_token):
            raise PageAgentHostError("transport_contract")
        if payload.get("url") != _PROVIDER_URL or payload.get("method") != "POST":
            raise PageAgentHostError("transport_contract")
        encoded = payload.get("body")
        try:
            encoded_bytes = encoded.encode("utf-8") if isinstance(encoded, str) else b""
        except UnicodeEncodeError as exc:
            raise PageAgentHostError("transport_contract") from exc
        if not encoded_bytes or len(encoded_bytes) > _MAX_REQUEST_BYTES:
            raise PageAgentHostError("transport_contract")
        try:
            body = json.loads(encoded)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise PageAgentHostError("transport_contract") from exc
        self.contract.validate_body(body)
        with self._lock:
            if self._attempts >= self.contract.max_model_calls:
                raise PageAgentHostError("model_call_budget", origin="limit")
            self._attempts += 1
        try:
            response = self.client.request_completion(
                body, self.run_id, timeout_s=self._remaining())
            if self.cancelled.is_set():
                raise PageAgentHostError("cancelled")
            if (not isinstance(response, dict)
                    or not isinstance(response.get("model"), str)
                    or response["model"] != response["model"].strip()
                    or not _SAFE_MODEL.fullmatch(response["model"])):
                raise PageAgentHostError("provider_response")
            try:
                response_body = json.dumps(
                    response, ensure_ascii=False, separators=(",", ":"),
                    allow_nan=False)
            except (TypeError, ValueError, OverflowError) as exc:
                raise PageAgentHostError("provider_response") from exc
            try:
                response_bytes = response_body.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise PageAgentHostError("provider_response") from exc
            if len(response_bytes) > _MAX_RESPONSE_BYTES:
                raise PageAgentHostError("provider_response")
        except PageAgentHostError as exc:
            self._last_error_code = exc.code
            raise
        except Exception as exc:
            # Only a failure the completion client typed is the provider's: the gateway recorded
            # it and can explain the run's ending. Any other exception on this path is the host's
            # own fault. It used to be reported as a failed provider call too, which no gateway
            # record could explain, so one proxy hiccup was scored as classification drift and
            # sealed the batch.
            code = getattr(exc, "code", None)
            if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
                self._last_error_code = code
                raise PageAgentHostError("provider_call", origin="provider") from None
            self._last_error_code = "host_fault"
            raise PageAgentHostError("host_fault") from None
        with self._lock:
            self._completed += 1
        return {"status": 200, "body": response_body}

    def report(self) -> dict[str, Any]:
        accounting = self.client.meter.run_report(self.run_id)
        if not isinstance(accounting, dict):
            raise PageAgentHostError("ledger_failure")
        transport = self.transport_report()
        attempts = transport["provider_attempts"]
        completed = transport["provider_responses"]
        calls = accounting.get("model_calls")
        if (isinstance(calls, bool) or not isinstance(calls, int)
                or calls < completed or calls > attempts):
            raise PageAgentHostError("ledger_failure")
        return {**transport, "accounting": accounting}

    def transport_report(self) -> dict[str, Any]:
        """Return content-free transport counts even when accounting is unavailable."""
        with self._lock:
            return {
                "provider_attempts": self._attempts,
                "provider_responses": self._completed,
                "last_error_code": self._last_error_code or None,
            }
