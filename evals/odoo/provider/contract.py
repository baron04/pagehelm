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
from evals.odoo.domain.population import digest_json

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

