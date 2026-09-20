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

from . import ledger
from .population import digest_json

MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_ERROR_BYTES = 64 * 1024
SCRIPTED_MODEL = "p74-scripted/v1"
#: Every field a forwarded chat completion may carry besides the model's own option fields.
#: Anything else could change generation for one arm only, so the gateway refuses it.
SHARED_REQUEST_FIELDS = frozenset({"model", "messages", "tools", "tool_choice", "temperature",
                                   "max_tokens", "stream"})
#: Generation controls PageAgent adds on its own: ``top_p`` and ``max_completion_tokens`` from
#: its bridge request options, and ``enable_thinking`` from the pinned bundle's Qwen patch. Its
#: completion client removes exactly these and carries the output cap as ``max_tokens``; any
#: other field it does not recognise still reaches the gateway and is refused there.
PAGEAGENT_ENGINE_FIELDS = ("top_p", "max_completion_tokens", "enable_thinking")
#: The HTTP status of each refusal the gateway answers itself; every other refusal is a 400.
REFUSAL_STATUS = {"model_call_budget": 429, "accounting_failure": 503}


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

    def request_refusal(self, body: Any) -> str | None:
        """The typed reason a request may not be forwarded, or ``None`` when it may."""
        if not isinstance(body, dict) or body.get("model") != self.requested_model:
            return "request_contract"
        if set(body) - SHARED_REQUEST_FIELDS - set(self.model_options):
            return "request_contract"
        messages, tools, tool_choice = body.get("messages"), body.get("tools"), body.get(
            "tool_choice")
        if (body.get("stream") not in (None, False)
                or not isinstance(messages, list) or not messages
                or (tools is not None and not isinstance(tools, list))
                or (tool_choice is not None and not isinstance(tool_choice, (str, dict)))):
            return "request_contract"
        temperature = body.get("temperature")
        if (isinstance(temperature, bool) or not isinstance(temperature, (int, float))
                or temperature != self.temperature):
            return "request_contract"
        if any(name not in body or type(body[name]) is not type(value) or body[name] != value
               for name, value in self.model_options.items()):
            return "request_contract"
        output = body.get("max_tokens")
        if (isinstance(output, bool) or not isinstance(output, int)
                or not 1 <= output <= self.max_output_tokens):
            return "output_token_budget"
        return None

    def recorded_parameters(self, body: dict[str, Any]) -> dict[str, Any]:
        """The generation parameters a forwarded request actually carries, without content."""
        tool_choice = body.get("tool_choice")
        if isinstance(tool_choice, dict):
            tool_choice = "named_function" if tool_choice.get("function") else "object"
        return {"temperature": body["temperature"], "max_tokens": body["max_tokens"],
                "tool_choice": tool_choice, "tools": len(body.get("tools") or []),
                "options": {name: body[name] for name in sorted(self.model_options)}}


# -- the runtime provider binding ----------------------------------------------------------------
SCRIPTED_PROVIDER_ID = "p74-scripted"


def endpoint_digest(base_url: str) -> str:
    """A normalized endpoint's digest: case, a default port and a trailing slash do not change it."""
    parsed = urllib.parse.urlsplit(base_url)
    scheme, host, port = parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port
    netloc = host if port in (None, {"https": 443, "http": 80}.get(scheme)) else f"{host}:{port}"
    normalized = f"{scheme}://{netloc}{parsed.path.rstrip('/')}"
    return "sha256:" + hashlib.sha256(b"p74-provider-endpoint\0" + normalized.encode()).hexdigest()


def credential_identity(key: str) -> str:
    """A domain-separated SHA-256 of a high-entropy API key, which identifies it without holding it."""
    return "sha256:" + hashlib.sha256(b"p74-provider-credential\0" + key.encode()).hexdigest()


def read_evaluation_key(key_file: str | Path) -> str:
    key = Path(key_file).read_text(encoding="utf-8").strip()
    if not key or any(not 33 <= ord(character) <= 126 for character in key):
        raise ValueError("the evaluation key file must hold one printable token")
    return key


def upstream_identity(*, base_url: str, key: str, provider_id: str) -> dict[str, Any]:
    return {"provider_id": provider_id, "endpoint_digest": endpoint_digest(
        _provider_base_url(base_url)), "credential_identity": credential_identity(key)}


def request_contract_digest(budgets: GatewayBudgets) -> str:
    """What shapes every forwarded request; how many calls an attempt may make is not part of it."""
    fields = budgets.digest_fields()
    del fields["max_model_calls"]
    return digest_json(fields)


def provider_binding(identity: dict[str, Any], budgets: GatewayBudgets) -> dict[str, Any]:
    """The provider a batch runs against: who, where, under which credential and contract."""
    return {**identity, "requested_model": budgets.requested_model,
            "request_contract_digest": request_contract_digest(budgets)}


def digest_binding(binding: dict[str, Any]) -> str:
    return digest_json(binding)


def provider_binding_digest(identity: dict[str, Any], budgets: GatewayBudgets) -> str:
    return digest_binding(provider_binding(identity, budgets))


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


def _provider_base_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if (not parsed.hostname or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment):
        raise ValueError("the provider base URL must be absolute and carry no credentials")
    loopback = parsed.hostname.casefold() == "localhost"
    try:
        loopback = loopback or ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        pass
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise ValueError("the provider base URL must use HTTPS except on loopback")
    return value.rstrip("/")


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
