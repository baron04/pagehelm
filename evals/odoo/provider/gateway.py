"""Host-side, OpenAI-compatible provider gateway that owns every model request of a batch.

Both arms reach their model only through this endpoint: WOA through its ordinary loopback
provider URL and the relay, PageAgent through the runner's server-held completion client.
The gateway binds each request to the one open attempt by a per-attempt bearer token,
appends ``model_request_dispatched`` to the attempt ledger *before* forwarding, and records
the response -- provider request id, returned model string, usage and cost, or the transport
failure -- afterwards. A request that belongs to no open attempt is written down as
``unowned_model_request`` and never forwarded, which forces the batch to seal.

Hard budgets and request parameters live here because they must be identical for comparable
arms. The gateway fails closed: a request may carry only the shared chat-completion fields and
the model's own option fields, with the frozen temperature, the frozen option values and an
output cap from one to the frozen maximum. The input is bounded by request bytes, not by a
token count, and every forwarded request's actual parameters are recorded. The product's
request already has that shape; PageAgent's completion client maps its request onto it before
sending. Refusals are recorded with their reason and answered with a typed error; nothing is
retried.

Behind the gateway sits either :class:`ScriptedBackend`, which never contacts a provider, or
:class:`UpstreamBackend`, the dedicated evaluation provider. Every result is typed: an answer,
an HTTP failure with a content-free code (outage, rate limit, refused credential, context-length
rejection or another rejection), a transport failure, an answer outside the evaluation
provider's response contract, or a call the spend ledger could not record. Every client
receives the same typed error for the same fact, ``outcomes.gateway_facts`` classifies attempts
from these records alone, and once a call could not be accounted the gateway forwards nothing
more.

Every backend names what it answers from: a provider id, a normalized endpoint digest and a
credential identity digest. Together with the requested alias and the request contract, that is
the runtime provider binding a batch header and every attempt must share, so an open batch
never resumes under a changed endpoint or key. No key text leaves the backend.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from evals.odoo.reporting import ledger
from evals.odoo.provider.backends import (
    UPSTREAM_SETTLE_MARGIN_S,
    Backend,
    BackendResult,
    ScriptedBackend,
    UpstreamBackend,
    _reported_model,
    append_late_answer,
    late_answers_path,
    priced_late_answers,
    provider_error_code,
)
from evals.odoo.provider.contract import (
    MAX_REQUEST_BYTES,
    PAGEAGENT_ENGINE_FIELDS,
    REFUSAL_STATUS,
    SCRIPTED_MODEL,
    SCRIPTED_PROVIDER_ID,
    SHARED_REQUEST_FIELDS,
    GatewayBudgets,
    credential_identity,
    digest_binding,
    endpoint_digest,
    provider_binding,
    provider_binding_digest,
    read_evaluation_key,
    request_contract_digest,
    upstream_identity,
)

# Provider contracts and backends live in focused modules.

class _AttemptBinding:
    def __init__(self, attempt_id: str, token: str):
        self.attempt_id = attempt_id
        self.token = token
        self.calls = 0
        self.sequence = 0
        #: Closing: the attempt still owns the calls it made, and takes no new ones.
        self.closing = False
        #: One lease per forwarded call -- its request sequence and when the provider's own
        #: deadline for it expires. Each settles exactly once, as an answer or as unresolved.
        self.leases: dict[int, float] = {}


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
        self._recorded = threading.Condition(self._lock)
        #: Set once a call's cost is unknown -- the spend ledger could not record it, or the
        #: provider never answered it. Either way this gateway forwards nothing more: the batch
        #: owes a bill, and the ledger refuses the request as well.
        self._cost_unknown = False
        #: Provider answers that arrived after their call had already been settled as unresolved.
        self.late_responses = 0
        #: Late answers that could not be written beside the ledger. Their calls stay owed.
        self.late_answer_write_failures = 0
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "P74Gateway/1"
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                return

            def _reply(self, status: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    # The client stopped waiting, as a product does when its user stops a turn;
                    # what the call did is already in the ledger.
                    pass

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

    def binding(self) -> dict[str, Any]:
        """The runtime provider binding of this gateway's backend and request contract."""
        return provider_binding(self.backend.identity(), self.budgets)

    def binding_digest(self) -> str:
        return digest_binding(self.binding())

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
            if self._cost_unknown or self._ledger_holds():
                # A restarted driver reads the hold from the ledger rather than from a flag its
                # predecessor held in memory.
                raise RuntimeError("this batch holds for a call whose cost is unknown; "
                                   "reconcile it before opening another attempt")
            self._binding = _AttemptBinding(attempt_id, token)

    def _ledger_holds(self) -> bool:
        try:
            return bool(ledger.read(self.ledger_path)[1]["cost_unknown"])
        except (ledger.LedgerError, OSError, KeyError):
            # A ledger this gateway cannot read is not a ledger that owes nothing.
            return True

    def close_attempt(self, attempt_id: str) -> dict[str, int]:
        """Stop the attempt's calls, wait out the leases it still holds, and record what never
        settled while the attempt is still open on the ledger.

        The attempt takes no new call from here, and every call it did make settles exactly
        once: recorded when the provider answers within its own deadline, and written down as
        ``model_response_unresolved`` when that deadline passes. Nothing waits on a timeout of
        the apparatus's own, and no answer is left to arrive after the ledger would take it.
        """
        with self._lock:
            binding = self._binding
            if binding is None or binding.attempt_id != attempt_id:
                raise RuntimeError("that attempt is not open on the gateway")
            binding.closing = True
            while binding.leases:
                remaining = max(binding.leases.values()) - time.monotonic()
                if remaining <= 0:
                    break
                self._recorded.wait(min(remaining, 0.2))
            unresolved = sorted(binding.leases)
            binding.leases.clear()
            self._retired_tokens.add(binding.token)
            self._binding = None
            settled = {"model_calls": binding.calls, "requests": binding.sequence,
                       "unresolved": len(unresolved)}
        for sequence in unresolved:
            # Written after the lock is released and before the attempt's own outcome: the cost
            # of this call is unknown, and the bill reconciliation must clear it.
            ledger.append(self.ledger_path, "model_response_unresolved", {
                "attempt_id": attempt_id, "request_seq": sequence,
                "reason": "provider_deadline_elapsed"})
        if unresolved:
            # This gateway forwards nothing more until that bill is reconciled.
            with self._lock:
                self._cost_unknown = True
        return settled

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
            # A closing attempt owns the calls it already made and takes no new one: a request
            # that arrives while it settles belongs to no open attempt, exactly as one that
            # arrives after it closed.
            open_binding = None if binding is None or binding.closing else binding
            owned = (open_binding is not None and token
                     and hmac.compare_digest(token.encode(), open_binding.token.encode()))
            if not owned or handler.path.rstrip("/") != "/v1/chat/completions":
                ledger.append(self.ledger_path, "unowned_model_request", {
                    "body_digest": body_digest,
                    "reason": ("no_open_attempt" if open_binding is None
                               else "token_or_path_mismatch")})
                handler._reply(401, {"error": {"code": "unowned_request"}})
                return
            binding.sequence += 1
            sequence = binding.sequence
            refusal = None
            body: dict[str, Any] | None = None
            if self._cost_unknown:
                refusal = "accounting_failure"
            elif length <= 0 or length > self.budgets.max_request_bytes:
                refusal = "input_size_budget"
            else:
                try:
                    body = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    body = None
                refusal = self.budgets.request_refusal(body)
                if refusal is None and binding.calls >= self.budgets.max_model_calls:
                    refusal = "model_call_budget"
                # What the ledger will not record is not forwarded. This gateway accepted any
                # string as a tool choice and any option value the policy file carried, while
                # the ledger accepts a bounded name and no null, dict or list -- and the lease
                # below is taken before the dispatch is recorded, so the difference stranded a
                # lease nothing could settle. The ledger's rule is asked here, before the lease.
                if refusal is None and not ledger.records_request_parameters(
                        self.budgets.recorded_parameters(body), body["max_tokens"]):
                    refusal = "request_contract"
            if refusal:
                ledger.append(self.ledger_path, "model_request_refused", {
                    "attempt_id": binding.attempt_id, "request_seq": sequence,
                    "reason": refusal})
                handler._reply(REFUSAL_STATUS.get(refusal, 400), {"error": {"code": refusal}})
                return
            binding.calls += 1
            binding.leases[sequence] = (time.monotonic() + self.backend.request_deadline_s
                                        + self.backend.settle_margin_s)
            attempt_id, call_index = binding.attempt_id, binding.calls
            ledger.append(self.ledger_path, "model_request_dispatched", {
                "attempt_id": attempt_id, "request_seq": sequence, "body_digest": body_digest,
                "requested_model": body["model"], "max_output_tokens": body["max_tokens"],
                "parameters": self.budgets.recorded_parameters(body)})
        try:
            result = self.backend.complete(body, attempt_id=attempt_id, call_index=call_index)
        except Exception:
            result = BackendResult("transport_error", None, None, None, None, None, None,
                                   "backend_exception")
        if result.status == "accounting_error":
            with self._lock:
                # Nothing more may be sent once a call could not be accounted.
                self._cost_unknown = True
        self._record_response(attempt_id, sequence, result)
        if result.status == "transport_error":
            # The client sees a lost connection, exactly as it would from a provider.
            handler.close_connection = True
            try:
                handler.connection.shutdown(2)
            except OSError:
                pass
            return
        if result.status == "ok":
            handler._reply(result.http_status or 200, result.payload or {})
            return
        # Every client receives the same typed error for the same fact. An answer outside the
        # response contract, or one that could not be accounted, is never passed on.
        status = (result.http_status if result.status == "http_error" and result.http_status
                  else 502)
        handler._reply(status, {"error": {"code": result.error_code}})

    def _record_response(self, attempt_id: str, sequence: int, result: BackendResult) -> None:
        with self._recorded:
            try:
                self._record_locked(attempt_id, sequence, result)
            finally:
                self._recorded.notify_all()

    def _record_locked(self, attempt_id: str, sequence: int, result: BackendResult) -> None:
        binding = self._binding
        if binding is None or binding.attempt_id != attempt_id or sequence not in binding.leases:
            # The call outlived its lease and was settled as unresolved when its attempt closed.
            # It settles once: this answer does not rewrite the attempt ledger. It is written
            # beside it, where the formal entry reads it: an answer that carries its usage has
            # priced the call, and no bill is needed to say what it cost.
            self.late_responses += 1
            record = {"attempt_id": attempt_id, "request_seq": sequence,
                      "status": result.status, "http_status": result.http_status,
                      "provider_request_id": result.provider_request_id,
                      "response_model": result.response_model, "usage": result.usage,
                      "cost_cny": result.cost_cny, "error_code": result.error_code}
            try:
                append_late_answer(self.ledger_path, record)
            except OSError:
                self.late_answer_write_failures += 1
            return
        binding.leases.pop(sequence, None)
        if result.status == "accounting_error":
            self._cost_unknown = True
        ledger.append(self.ledger_path, "model_response_received", {
            "attempt_id": attempt_id, "request_seq": sequence, "status": result.status,
            "http_status": result.http_status,
            "provider_request_id": result.provider_request_id,
            "response_model": result.response_model, "usage": result.usage,
            "cost_cny": result.cost_cny, "error_code": result.error_code})


class GatewayCompletionClient:
    """PageAgent's server-held completion client, routed through the same gateway.

    PageAgent's bridge sends ``top_p`` and ``max_completion_tokens``, its pinned bundle adds
    ``enable_thinking`` for Qwen models, and it knows nothing of the product's model options.
    Before forwarding, this client removes exactly those engine fields, carries the output cap
    as ``max_tokens`` and adds the model's option fields, so both arms reach the provider with
    the same parameters. It passes any other field through untouched; the gateway refuses it.
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
                     if key not in PAGEAGENT_ENGINE_FIELDS}
        if "max_tokens" not in forwarded and "max_completion_tokens" in body:
            forwarded["max_tokens"] = body["max_completion_tokens"]
        for key, value in self.model_options.items():
            forwarded.setdefault(key, value)
        return forwarded

    def request_completion(self, body: dict[str, Any], run_id: str, *,
                           timeout_s: float | None = None) -> dict[str, Any]:
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
