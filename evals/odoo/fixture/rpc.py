"""Minimal Odoo JSON-RPC client for the independent evaluator and fixture guard.

Deliberately standard-library only and free of any product import: expected truth must come
from the application under the trial's own actor through code that shares nothing with the
adapter being measured. Credentials are read from the same per-user API-key files the
deployment registers, and never appear in errors or artifacts.
"""
from __future__ import annotations

import itertools
import json
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_LOOPBACK = re.compile(r"http://(127\.0\.0\.1|localhost|\[::1\]):[0-9]{2,5}\Z")


class OdooRpcError(RuntimeError):
    """An RPC failed; the message carries no credential and no record content."""


class OdooActorClient:
    """``execute_kw`` as one delegated Odoo user."""

    def __init__(self, *, base_url: str, api_key_file: str | Path, timeout_s: float = 30):
        if not _LOOPBACK.fullmatch(base_url.rstrip("/")):
            raise ValueError("the evaluator reaches Odoo only on a loopback origin")
        payload = json.loads(Path(api_key_file).read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or set(payload) != {"database", "login", "api_key"}:
            raise ValueError("API-key file has an unexpected shape")
        self.base_url = base_url.rstrip("/")
        self.database = payload["database"]
        self.login = payload["login"]
        self._key = payload["api_key"]
        self.timeout_s = timeout_s
        self._ids = itertools.count(1)
        self._uid: int | None = None

    def _call(self, service: str, method: str, *args: Any) -> Any:
        body = json.dumps({"jsonrpc": "2.0", "method": "call", "id": next(self._ids),
                           "params": {"service": service, "method": method,
                                      "args": list(args)}}).encode("utf-8")
        request = urllib.request.Request(self.base_url + "/jsonrpc", data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except (urllib.error.URLError, OSError) as exc:
            raise OdooRpcError(f"{service}.{method} transport failed") from exc
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise OdooRpcError(f"{service}.{method} response is oversized")
        reply = json.loads(raw)
        if reply.get("error"):
            data = reply["error"].get("data") or {}
            raise OdooRpcError(f"{service}.{method} failed: {data.get('name', 'error')}")
        return reply.get("result")

    @property
    def uid(self) -> int:
        if self._uid is None:
            uid = self._call("common", "authenticate", self.database, self.login, self._key, {})
            if isinstance(uid, bool) or not isinstance(uid, int) or uid <= 0:
                raise OdooRpcError("the API key did not authenticate its user")
            self._uid = uid
        return self._uid

    def execute(self, model: str, method: str, args: list[Any],
                kwargs: dict[str, Any] | None = None) -> Any:
        return self._call("object", "execute_kw", self.database, self.uid, self._key,
                          model, method, args, kwargs or {})

    def search_read(self, model: str, domain: list[Any], fields: list[str], *,
                    order: str = "id", limit: int | None = None,
                    context: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {"fields": fields, "order": order}
        if limit is not None:
            kwargs["limit"] = limit
        if context:
            kwargs["context"] = context
        rows = self.execute(model, "search_read", [domain], kwargs)
        if not isinstance(rows, list):
            raise OdooRpcError(f"{model}.search_read returned no list")
        return rows

    def search_count(self, model: str, domain: list[Any]) -> int:
        count = self.execute(model, "search_count", [domain])
        if isinstance(count, bool) or not isinstance(count, int):
            raise OdooRpcError(f"{model}.search_count returned no integer")
        return count
