"""Host-side, OpenAI-compatible provider gateway that owns every model request of a batch.

Both arms reach their model only through this endpoint: WOA through its ordinary loopback
provider URL and the relay, PageAgent through the runner's server-held completion client.
The gateway binds each request to the one open attempt by a per-attempt bearer token,
appends ``model_request_dispatched`` to the attempt ledger *before* forwarding, and records
the response -- provider request id, returned model string, usage and cost, or the transport
failure -- afterwards. A request that belongs to no open attempt is written down as
``unowned_model_request`` and never forwarded, which forces the batch to seal.

Hard budgets and request parameters live here because they must be identical for comparable
arms: model calls per attempt, output tokens per call, request size, temperature, the model's
option fields and no sampling field either engine adds on its own. The product's request
already has that shape; PageAgent's completion client maps its request onto it before
sending. Refusals are recorded with their reason and answered with a typed error; nothing is
retried.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import ledger

MAX_REQUEST_BYTES = 2 * 1024 * 1024
SCRIPTED_MODEL = "p74-scripted/v1"
#: Fields only PageAgent's bridge sends. Neither is forwarded: ``top_p`` would give the arms
#: different sampling, and not every OpenAI-compatible provider honours
#: ``max_completion_tokens``, so the output cap always travels as ``max_tokens``.
UNSHARED_REQUEST_FIELDS = ("top_p", "max_completion_tokens")


@dataclass(frozen=True, slots=True)
class GatewayBudgets:
    requested_model: str
    max_model_calls: int
    max_output_tokens: int
    max_request_bytes: int = MAX_REQUEST_BYTES
    temperature: float = 0.0
    #: The option fields the product sends for the requested model; every forwarded request
    #: must carry exactly these values.
    model_options: dict[str, Any] = field(default_factory=dict)

    def digest_fields(self) -> dict[str, Any]:
        return {"requested_model": self.requested_model,
                "max_model_calls": self.max_model_calls,
                "max_output_tokens": self.max_output_tokens,
                "max_request_bytes": self.max_request_bytes,
                "temperature": self.temperature, "model_options": dict(self.model_options)}

    def parameters_match(self, body: dict[str, Any]) -> bool:
        temperature = body.get("temperature")
        return (not isinstance(temperature, bool) and isinstance(temperature, (int, float))
                and temperature == self.temperature
                and not any(name in body for name in UNSHARED_REQUEST_FIELDS)
                and all(name in body and type(body[name]) is type(value) and body[name] == value
                        for name, value in self.model_options.items()))


@dataclass(frozen=True, slots=True)
class BackendResult:
    status: str                     # ok | http_error | transport_error
    http_status: int | None
    payload: dict[str, Any] | None
    provider_request_id: str | None
    response_model: str | None
    usage: dict[str, int] | None
    cost_cny: float | None
    error_code: str | None


class Backend:
    """What sits behind the gateway: a scripted responder or an upstream provider."""

    def complete(self, body: dict[str, Any], *, attempt_id: str, call_index: int) -> BackendResult:
        raise NotImplementedError


def _usage(payload: dict[str, Any]) -> dict[str, int] | None:
    raw = payload.get("usage")
    if not isinstance(raw, dict):
        return None
    try:
        prompt = int(raw["prompt_tokens"])
        completion = int(raw["completion_tokens"])
    except (KeyError, TypeError, ValueError):
        return None
    details = raw.get("prompt_tokens_details") or {}
    reasoning = raw.get("completion_tokens_details") or {}
    return {"input_tokens": prompt, "output_tokens": completion,
            "cached_tokens": int(details.get("cached_tokens") or 0),
            "reasoning_tokens": int(reasoning.get("reasoning_tokens") or 0)}


class ScriptedBackend(Backend):
    """A fixed policy that never contacts a provider, with deterministic fault injection.

    ``policy(body, call_index)`` returns an OpenAI-compatible completion without ``model`` or
    ``usage``; this backend stamps the scripted model string and zero usage, so a scripted
    attempt is indistinguishable in shape from a metered one and costs exactly nothing.
    """

    def __init__(self) -> None:
        self._policies: dict[str, Callable[[dict[str, Any], int], dict[str, Any]]] = {}
        self._faults: dict[tuple[str, int], str] = {}
        self._models: dict[tuple[str, int], str] = {}
        self._lock = threading.Lock()

    def install(self, attempt_id: str, policy: Callable[[dict[str, Any], int], dict[str, Any]],
                *, faults: dict[int, str] | None = None,
                model_overrides: dict[int, str] | None = None) -> None:
        with self._lock:
            self._policies[attempt_id] = policy
            for index, fault in (faults or {}).items():
                if fault not in {"http_503", "connection_lost"}:
                    raise ValueError("unsupported scripted fault")
                self._faults[(attempt_id, index)] = fault
            for index, model in (model_overrides or {}).items():
                self._models[(attempt_id, index)] = model

    def complete(self, body, *, attempt_id, call_index):
        with self._lock:
            policy = self._policies.get(attempt_id)
            fault = self._faults.get((attempt_id, call_index))
            model = self._models.get((attempt_id, call_index), SCRIPTED_MODEL)
        request_id = f"scripted-{attempt_id}-{call_index}"
        if fault == "http_503":
            return BackendResult("http_error", 503, {"error": {"code": "scripted_outage"}},
                                 request_id, None, None, None, "provider_http_5xx")
        if fault == "connection_lost":
            return BackendResult("transport_error", None, None, request_id, None, None, None,
                                 "provider_connection_lost")
        if policy is None:
            return BackendResult("http_error", 500, {"error": {"code": "no_script"}},
                                 request_id, None, None, None, "scripted_policy_missing")
        completion = dict(policy(body, call_index))
        completion.setdefault("id", request_id)
        completion.setdefault("object", "chat.completion")
        completion.setdefault("created", 1_788_912_000 + call_index)
        completion["model"] = model
        completion["usage"] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        return BackendResult("ok", 200, completion, request_id, model, _usage(completion), 0.0,
                             None)


class _AttemptBinding:
    def __init__(self, attempt_id: str, token: str):
        self.attempt_id = attempt_id
        self.token = token
        self.calls = 0
        self.sequence = 0


class ProviderGateway:
    """Loopback HTTP endpoint bound to one ledger and one backend."""

    def __init__(self, *, ledger_path: str | Path, backend: Backend, budgets: GatewayBudgets,
                 host: str = "127.0.0.1", port: int = 0):
        self.ledger_path = Path(ledger_path)
        self.backend = backend
        self.budgets = budgets
        self._binding: _AttemptBinding | None = None
        self._retired_tokens: set[str] = set()
        self._lock = threading.Lock()
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "P74Gateway/1"
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                return

            def _reply(self, status: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):  # noqa: N802 - http.server naming
                gateway._handle(self)

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._server.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def start(self) -> "ProviderGateway":
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="p74-provider-gateway", daemon=True)
        self._thread.start()
        return self

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    # -- attempt binding -------------------------------------------------------------------
    def open_attempt(self, attempt_id: str, token: str) -> None:
        if len(token) < 24:
            raise ValueError("attempt token is too short")
        with self._lock:
            if self._binding is not None:
                raise RuntimeError("the gateway already serves an open attempt")
            if token in self._retired_tokens:
                raise RuntimeError("attempt tokens are single-use")
            self._binding = _AttemptBinding(attempt_id, token)

    def close_attempt(self, attempt_id: str) -> dict[str, int]:
        with self._lock:
            binding = self._binding
            if binding is None or binding.attempt_id != attempt_id:
                raise RuntimeError("that attempt is not open on the gateway")
            self._retired_tokens.add(binding.token)
            self._binding = None
            return {"model_calls": binding.calls, "requests": binding.sequence}

    # -- request handling --------------------------------------------------------------------
    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        try:
            length = int(handler.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        raw = handler.rfile.read(length) if 0 < length <= MAX_REQUEST_BYTES + 1 else b""
        body_digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        authorization = handler.headers.get("Authorization", "")
        token = authorization[7:] if authorization.startswith("Bearer ") else ""
        with self._lock:
            binding = self._binding
            owned = (binding is not None and token
                     and hmac.compare_digest(token.encode(), binding.token.encode()))
            if not owned or handler.path.rstrip("/") != "/v1/chat/completions":
                ledger.append(self.ledger_path, "unowned_model_request", {
                    "body_digest": body_digest,
                    "reason": "no_open_attempt" if binding is None else "token_or_path_mismatch"})
                handler._reply(401, {"error": {"code": "unowned_request"}})
                return
            binding.sequence += 1
            sequence = binding.sequence
            refusal = None
            body: dict[str, Any] | None = None
            if length <= 0 or length > self.budgets.max_request_bytes:
                refusal = "input_size_budget"
            else:
                try:
                    body = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    body = None
                output = body.get("max_tokens") if isinstance(body, dict) else None
                if (not isinstance(body, dict) or body.get("model") != self.budgets.requested_model
                        or body.get("stream") not in (None, False)
                        or not self.budgets.parameters_match(body)):
                    refusal = "request_contract"
                elif (isinstance(output, bool) or not isinstance(output, int)
                      or output > self.budgets.max_output_tokens):
                    refusal = "output_token_budget"
                elif binding.calls >= self.budgets.max_model_calls:
                    refusal = "model_call_budget"
            if refusal:
                ledger.append(self.ledger_path, "model_request_refused", {
                    "attempt_id": binding.attempt_id, "request_seq": sequence,
                    "reason": refusal})
                status = 429 if refusal == "model_call_budget" else 400
                handler._reply(status, {"error": {"code": refusal}})
                return
            binding.calls += 1
            attempt_id, call_index = binding.attempt_id, binding.calls
            ledger.append(self.ledger_path, "model_request_dispatched", {
                "attempt_id": attempt_id, "request_seq": sequence, "body_digest": body_digest,
                "requested_model": body["model"], "max_output_tokens": output})
        try:
            result = self.backend.complete(body, attempt_id=attempt_id, call_index=call_index)
        except Exception:
            result = BackendResult("transport_error", None, None, None, None, None, None,
                                   "backend_exception")
        ledger.append(self.ledger_path, "model_response_received", {
            "attempt_id": attempt_id, "request_seq": sequence, "status": result.status,
            "http_status": result.http_status, "provider_request_id": result.provider_request_id,
            "response_model": result.response_model, "usage": result.usage,
            "cost_cny": result.cost_cny, "error_code": result.error_code})
        if result.status == "transport_error":
            # The client sees a lost connection, exactly as it would from a provider.
            handler.close_connection = True
            try:
                handler.connection.shutdown(2)
            except OSError:
                pass
            return
        handler._reply(result.http_status or 502, result.payload or {})


class GatewayCompletionClient:
    """PageAgent's server-held completion client, routed through the same gateway.

    PageAgent's bridge sends ``top_p`` and ``max_completion_tokens`` and knows nothing of the
    product's model options. Before forwarding, this client drops the fields the product does
    not send, carries the output cap as ``max_tokens`` and adds the model's option fields, so
    both arms reach the provider with the same parameters; the gateway refuses any request
    that still differs.
    """

    class _Meter:
        def __init__(self, client: "GatewayCompletionClient"):
            self._client = client

        def run_report(self, _run_id: str) -> dict[str, Any]:
            return {"model_calls": self._client.calls}

    def __init__(self, *, base_url: str, token: str, model: str, timeout_s: float = 120,
                 model_options: dict[str, Any] | None = None):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.model = model
        self.timeout_s = timeout_s
        self.model_options = dict(model_options or {})
        self.calls = 0
        self.meter = self._Meter(self)

    def forwarded_body(self, body: dict[str, Any]) -> dict[str, Any]:
        forwarded = {key: value for key, value in body.items()
                     if key not in UNSHARED_REQUEST_FIELDS}
        if "max_tokens" not in forwarded and "max_completion_tokens" in body:
            forwarded["max_tokens"] = body["max_completion_tokens"]
        for key, value in self.model_options.items():
            forwarded.setdefault(key, value)
        return forwarded

    def request_completion(self, body: dict[str, Any], run_id: str, *,
                           timeout_s: float | None = None) -> dict[str, Any]:
        import urllib.error
        import urllib.request

        payload = json.dumps(self.forwarded_body(body), separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + "/chat/completions", data=payload, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.token}"})
        self.calls += 1
        started = time.monotonic()
        try:
            with urllib.request.urlopen(
                    request, timeout=min(self.timeout_s, timeout_s or self.timeout_s)) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            code = "provider_call"
            try:
                code = json.loads(exc.read()).get("error", {}).get("code") or code
            except Exception:
                pass
            error = RuntimeError(code)
            error.code = code  # type: ignore[attr-defined]
            raise error from None
        except OSError as exc:
            error = RuntimeError("provider_connection_lost")
            error.code = ("provider_deadline" if time.monotonic() - started
                          >= (timeout_s or self.timeout_s) - 0.5
                          else "provider_connection_lost")  # type: ignore[attr-defined]
            raise error from exc
