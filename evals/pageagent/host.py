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


class PageAgentHostError(RuntimeError):
    """A content-free baseline-host failure safe to cross the page binding."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


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
        if not callable(getattr(client, "request_completion", None)):
            raise TypeError("client must provide request_completion")
        if getattr(client, "model", None) != contract.model:
            raise ValueError("client model does not match the frozen contract")
        if not callable(getattr(getattr(client, "meter", None), "run_report", None)):
            raise TypeError("client meter must provide run_report")
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
            raise PageAgentHostError("provider_deadline")
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
                raise PageAgentHostError("model_call_budget")
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
            code = getattr(exc, "code", "provider_call")
            self._last_error_code = (
                code if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code)
                else "provider_call")
            raise PageAgentHostError("provider_call") from None
        with self._lock:
            self._completed += 1
        return {"status": 200, "body": response_body}

    def report(self) -> dict[str, Any]:
        accounting = self.client.meter.run_report(self.run_id)
        if not isinstance(accounting, dict):
            raise PageAgentHostError("ledger_failure")
        with self._lock:
            attempts = self._attempts
            completed = self._completed
        calls = accounting.get("model_calls")
        if (isinstance(calls, bool) or not isinstance(calls, int)
                or calls < completed or calls > attempts):
            raise PageAgentHostError("ledger_failure")
        return {
            "provider_attempts": attempts,
            "provider_responses": completed,
            "last_error_code": self._last_error_code or None,
            "accounting": accounting,
        }
