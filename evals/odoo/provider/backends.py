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
from evals.odoo.provider.contract import (
    MAX_ERROR_BYTES,
    MAX_RESPONSE_BYTES,
    SCRIPTED_MODEL,
    SCRIPTED_PROVIDER_ID,
    _provider_base_url,
    endpoint_digest,
    read_evaluation_key,
    upstream_identity,
)

# Provider contracts live in contract.py.

@dataclass(frozen=True, slots=True)
class BackendResult:
    status: str     # ok | http_error | transport_error | contract_error | accounting_error
    http_status: int | None
    payload: dict[str, Any] | None
    provider_request_id: str | None
    response_model: str | None
    usage: dict[str, int] | None
    cost_cny: float | None
    error_code: str | None


#: How long past its deadline an upstream request is still waited for. The client's own timeout
#: fires on a socket read, so the call ends a moment after the deadline, never before it.
UPSTREAM_SETTLE_MARGIN_S = 30.0
LATE_ANSWERS_SUFFIX = ".late-answers"


def late_answers_path(ledger_path: str | Path) -> Path:
    """Where the answers that arrived after their calls were settled as unresolved are kept."""
    return Path(ledger_path).with_suffix(LATE_ANSWERS_SUFFIX)


def append_late_answer(ledger_path: str | Path, record: dict[str, Any]) -> None:
    """Add one late answer beside the ledger, flushed to disk before the gateway goes on."""
    path = late_answers_path(ledger_path)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "ab") as handle:
        handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")).encode() + b"\n")
        handle.flush()
        os.fsync(handle.fileno())


def priced_late_answers(ledger_path: str | Path) -> dict[str, dict[str, Any]]:
    """Late answers that priced their call, by the name the ledger gives an unresolved call.

    Only an answer that carries usage and a cost counts; a timeout or an error that arrived late
    says nothing about what the provider charged. A line that cannot be read is skipped: this
    file only ever relieves a debt, so what it does not show stays owed.
    """
    path = late_answers_path(ledger_path)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    priced: dict[str, dict[str, Any]] = {}
    for line in lines:
        try:
            record = json.loads(line)
            name = f"{record['attempt_id']}#{int(record['request_seq'])}"
            cost = record["cost_cny"]
        except (ValueError, KeyError, TypeError):
            continue
        if (isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0
                and isinstance(record.get("usage"), dict)):
            priced.setdefault(name, record)
    return priced


class Backend:
    """What sits behind the gateway: a scripted responder or an upstream provider."""

    #: The frozen deadline of one forwarded request. With ``settle_margin_s`` it is how long an
    #: attempt waits for a call to settle before writing it down as unresolved: the lease a
    #: dispatch takes out expires with the request it belongs to, never on a timeout of the
    #: apparatus's own.
    request_deadline_s: float = 180.0
    #: How long after that deadline the request's own ending still arrives. An upstream call
    #: that times out reports it a moment after its deadline, so a lease that expired exactly at
    #: the deadline turned every timeout into a call nobody heard back about -- cost unknown,
    #: batch held for a bill -- although the gateway learned how it ended moments later.
    settle_margin_s: float = 0.0

    def complete(self, body: dict[str, Any], *, attempt_id: str, call_index: int) -> BackendResult:
        raise NotImplementedError

    def identity(self) -> dict[str, Any]:
        """The provider id, endpoint digest and credential identity this backend answers from."""
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


#: Scripted provider faults and the typed result each one stands for.
SCRIPTED_FAULTS = {
    "http_503": ("http_error", 503, "provider_unavailable"),
    "http_429": ("http_error", 429, "provider_rate_limited"),
    "http_401": ("http_error", 401, "provider_authentication"),
    "http_408": ("http_error", 408, "provider_deadline"),
    "context_length": ("http_error", 400, "provider_context_length"),
    "connection_lost": ("transport_error", None, "provider_connection_lost"),
    "malformed_response": ("contract_error", 200, "provider_response_contract"),
    "accounting_failure": ("accounting_error", 200, "accounting_failure"),
}


class ScriptedBackend(Backend):
    """A fixed policy that never contacts a provider, with deterministic fault injection.

    ``policy(body, call_index)`` returns an OpenAI-compatible completion without ``model`` or
    ``usage``; this backend stamps the scripted model string and zero usage, so a scripted
    attempt is indistinguishable in shape from a metered one and costs exactly nothing.
    """

    def __init__(self, *, slow_answer_s: float = 20.0) -> None:
        self._policies: dict[str, Callable[[dict[str, Any], int], dict[str, Any]]] = {}
        self._faults: dict[tuple[str, int], str] = {}
        self._models: dict[tuple[str, int], str] = {}
        self._lock = threading.Lock()
        #: How long a ``slow_answer`` keeps the client waiting before its scripted answer.
        self.slow_answer_s = slow_answer_s

    def identity(self) -> dict[str, Any]:
        return {"provider_id": SCRIPTED_PROVIDER_ID,
                "endpoint_digest": endpoint_digest("scripted://p74-scripted-backend"),
                "credential_identity": None}

    def install(self, attempt_id: str, policy: Callable[[dict[str, Any], int], dict[str, Any]],
                *, faults: dict[int, str] | None = None,
                model_overrides: dict[int, str] | None = None) -> None:
        with self._lock:
            self._policies[attempt_id] = policy
            for index, fault in (faults or {}).items():
                if fault not in SCRIPTED_FAULTS and fault != "slow_answer":
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
        if fault == "slow_answer":
            time.sleep(self.slow_answer_s)
        elif fault is not None:
            status, http_status, code = SCRIPTED_FAULTS[fault]
            payload = {"error": {"code": code}} if status == "http_error" else None
            return BackendResult(status, http_status, payload, request_id, None, None, None,
                                 code)
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


def _http_error_code(status: int) -> str:
    """The content-free type of a provider HTTP failure.

    A 408 is the provider's own request deadline, so it gets the type a timeout on this side
    records: the same fact scores the same whichever form it arrives in.
    """
    if status == 429:
        return "provider_rate_limited"
    if status == 408:
        return "provider_deadline"
    if status == 425 or 500 <= status <= 599:
        return "provider_unavailable"
    if status in {401, 403}:
        return "provider_authentication"
    return "provider_http"


#: How OpenAI-compatible providers name an input longer than the model's context. Only the parsed
#: error object is read, and only this classification is kept.
CONTEXT_LENGTH_CODES = frozenset({"context_length_exceeded", "string_above_max_length"})
CONTEXT_LENGTH_MESSAGE = re.compile(
    r"context length|context window|maximum context|input length|too many (?:input )?tokens"
    r"|prompt is too long|reduce the length", re.IGNORECASE)


def _context_rejection(detail: bytes) -> bool:
    try:
        value = json.loads(detail)
    except (ValueError, UnicodeDecodeError):
        return False
    if not isinstance(value, dict):
        return False
    for item in (value, value.get("error")):
        if not isinstance(item, dict):
            continue
        code = item.get("code")
        if isinstance(code, str) and code.lower() in CONTEXT_LENGTH_CODES:
            return True
        message = item.get("message")
        if isinstance(message, str) and CONTEXT_LENGTH_MESSAGE.search(message):
            return True
    return False


def provider_error_code(status: int, detail: bytes = b"") -> str:
    """The typed code of a provider HTTP failure; a rejection naming the context length is one."""
    code = _http_error_code(status)
    if code == "provider_http" and status in {400, 413, 422} and _context_rejection(detail):
        return "provider_context_length"
    return code


def _timed_out(exc: BaseException) -> bool:
    reason = getattr(exc, "reason", None)
    return (isinstance(exc, (TimeoutError, socket.timeout))
            or isinstance(reason, (TimeoutError, socket.timeout)))


def _reported_model(value: Any) -> str | None:
    """A provider's model string as this apparatus compares it, or null if it is not one.

    The definition lives in the product the apparatus measures, and this reads it rather than
    restating it: a second definition here once recorded as good a padded answer the product
    could not spend against.
    """
    from web_operation_agent.modeling.client import reported_model_string

    return reported_model_string(value)


def _response_contract(status: int, request_id: str, reported: str | None = None) -> BackendResult:
    """An answer outside the evaluation provider's response contract; no client receives it."""
    return BackendResult("contract_error", status, None, request_id, reported, None, None,
                         "provider_response_contract")


def _accounting_failure(result: BackendResult) -> BackendResult:
    """A dispatched call the spend ledger could not record, so its cost is not known."""
    return BackendResult("accounting_error", result.http_status, None,
                         result.provider_request_id, result.response_model, result.usage, None,
                         "accounting_failure")


class UpstreamBackend(Backend):
    """The dedicated evaluation provider behind the gateway.

    One POST per forwarded request, never retried, with the evaluation key held only here.
    Every dispatched call is accounted as the product's own client accounts it: a 32-character
    request id sent as ``x-woa-request-id``, reported usage priced at the registered rate into
    the named spend ledger, and an explicit usage-unavailable row when a dispatched call returns
    no trustworthy usage. The attempt ledger receives the same request id, the HTTP status, the
    returned model string, usage and cost. A dropped connection is ``provider_connection_lost``,
    an apparatus fault; a timeout is ``provider_deadline``, which the protocol scores as a
    product result. An answer that is not a JSON object, names no model or reports no usage is
    outside the response contract and is never passed on, and a call the spend ledger could not
    record is an accounting failure.
    """

    def __init__(self, *, base_url: str, key_file: str | Path, requested_model: str,
                 spend_ledger: str | Path, batch_id: str, provider: str = "p74-evaluation",
                 timeout_s: float = 180.0):
        from web_operation_agent.modeling.client import SpendMeter, price_for

        self.base_url = _provider_base_url(base_url)
        key = read_evaluation_key(key_file)
        self._key = key
        self._identity = upstream_identity(base_url=self.base_url, key=key, provider_id=provider)
        self.requested_model = requested_model
        self.provider = provider
        self.timeout_s = float(timeout_s)
        self.request_deadline_s = self.timeout_s
        self.settle_margin_s = UPSTREAM_SETTLE_MARGIN_S
        # The evaluation's budgets are the gateway's; this meter only accounts.
        self.meter = SpendMeter(ledger=spend_ledger, batch_id=batch_id,
                                run_input_token_abort=10 ** 12,
                                price=price_for(requested_model))

    def identity(self) -> dict[str, Any]:
        return dict(self._identity)

    def _unmetered(self, result: BackendResult, attempt_id: str,
                   error_code: str) -> BackendResult:
        """Record a dispatched call without trustworthy usage; a call that cannot be recorded
        is an accounting failure instead."""
        try:
            self.meter.record_unmetered(provider=self.provider, model=self.requested_model,
                                        run_id=attempt_id, request_id=result.provider_request_id,
                                        error_code=error_code,
                                        reported_model=result.response_model)
        except Exception:
            return _accounting_failure(result)
        return result

    def complete(self, body, *, attempt_id, call_index):
        request_id = uuid.uuid4().hex
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + "/chat/completions", data=data, method="POST",
            headers={"content-type": "application/json",
                     "authorization": f"Bearer {self._key}", "x-woa-request-id": request_id})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                status = response.status
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                detail = exc.read(MAX_ERROR_BYTES)
            except OSError:
                detail = b""
            finally:
                exc.close()
            code = provider_error_code(status, detail)
            return self._unmetered(BackendResult("http_error", status, {"error": {"code": code}},
                                                 request_id, None, None, None, code),
                                   attempt_id, code)
        except OSError as exc:
            if _timed_out(exc):
                return self._unmetered(BackendResult("transport_error", None, None, request_id,
                                                     None, None, None, "provider_deadline"),
                                       attempt_id, "provider_deadline")
            return self._unmetered(BackendResult("transport_error", None, None, request_id, None,
                                                 None, None, "provider_connection_lost"),
                                   attempt_id, "provider_transport")
        payload = None
        if len(raw) <= MAX_RESPONSE_BYTES:
            try:
                payload = json.loads(raw)
            except ValueError:
                payload = None
        if not isinstance(payload, dict):
            return self._unmetered(_response_contract(status, request_id), attempt_id,
                                   "provider_response")
        reported = _reported_model(payload.get("model"))
        usage = payload.get("usage")
        if not (isinstance(usage, dict) and "prompt_tokens" in usage
                and "completion_tokens" in usage):
            return self._unmetered(_response_contract(status, request_id, reported), attempt_id,
                                   "provider_usage")
        try:
            row = self.meter.record(provider=self.provider, model=self.requested_model,
                                    run_id=attempt_id, usage=usage, request_id=request_id,
                                    reported_model=reported)
        except ValueError:
            return self._unmetered(_response_contract(status, request_id, reported), attempt_id,
                                   "provider_usage")
        except Exception:
            return _accounting_failure(BackendResult("ok", status, None, request_id, reported,
                                                     _usage(payload), None, None))
        usage_record, cost = _usage(payload), row["call_cost_cny"]
        if reported is None:
            # A completion that names no model cannot be attributed; whatever it cost is
            # already accounted above.
            return BackendResult("contract_error", status, None, request_id, None, usage_record,
                                 cost, "provider_response_contract")
        return BackendResult("ok", status, payload, request_id, reported, usage_record, cost,
                             None)
