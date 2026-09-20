"""Authenticated JSON-RPC transport and delegated identity for Odoo."""
from __future__ import annotations

import json
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from web_operation_agent.business.runtime import (BusinessToolAuthenticationError,
                                                  BusinessToolContractError,
                                                  BusinessToolDispatchError,
                                                  BusinessToolError,
                                                  BusinessToolIdentityError,
                                                  BusinessToolPermissionError,
                                                  BusinessToolUnavailableError)
from web_operation_agent.state.identity import IdentityAssertion, PrincipalError

from .specs import APPLICATION_ID, _MAX_RESPONSE_BYTES


class OdooRpcClient:
    """Application-bounded RPC behavior used by the typed Odoo executor."""

    def _rpc(self, params: dict, *, mutation: bool = False) -> object:
        body = json.dumps({"jsonrpc": "2.0", "method": "call", "params": params}
                          ).encode("utf-8")
        request = Request(self.base_url + "/jsonrpc", data=body, method="POST")
        request.add_header("Content-Type", "application/json")
        try:
            with urlopen(request, timeout=self.timeout_s) as response:
                payload = json.loads(response.read(_MAX_RESPONSE_BYTES))
        except HTTPError as exc:
            if exc.code in (401, 403):
                raise BusinessToolAuthenticationError(
                    "Odoo rejected the delegated API key") from exc
            error = (BusinessToolDispatchError if mutation
                     else BusinessToolUnavailableError)
            raise error(f"the Odoo API returned HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            error = (BusinessToolDispatchError if mutation
                     else BusinessToolUnavailableError)
            raise error("the Odoo API is unreachable") from exc
        except json.JSONDecodeError as exc:
            if mutation:
                raise BusinessToolDispatchError(
                    "the Odoo API returned an unreadable mutation response") from exc
            raise BusinessToolContractError("the Odoo API returned invalid JSON") from exc
        if not isinstance(payload, dict):
            if mutation:
                raise BusinessToolDispatchError(
                    "the Odoo API returned an invalid mutation response")
            raise BusinessToolContractError("the Odoo API response is not an object")
        error = payload.get("error")
        if error is not None:
            name = ""
            if isinstance(error, dict) and isinstance(error.get("data"), dict):
                name = str(error["data"].get("name") or "")
            # Two different answers that used to share one class. `AccessDenied` is Odoo
            # refusing the credential; `AccessError` is Odoo's access rules refusing an
            # authenticated user this model or record, which is an authorization result.
            if "AccessDenied" in name:
                raise BusinessToolAuthenticationError(
                    "Odoo rejected the delegated credential")
            if "AccessError" in name:
                raise BusinessToolPermissionError(
                    "Odoo's access rules do not permit the delegated user this operation",
                    layer="application")
            # The message may quote record content, so only the exception class crosses.
            raise BusinessToolError(f"Odoo refused the call ({name or 'unknown'})")
        return payload.get("result")

    def _authenticate(self) -> int:
        with self._lock:
            if self._uid is not None:
                return self._uid
        uid = self._rpc({"service": "common", "method": "authenticate",
                         "args": [self.keys.database, self.keys.login,
                                  self.keys.api_key, {}]})
        if isinstance(uid, bool) or not isinstance(uid, int) or uid <= 0:
            raise BusinessToolAuthenticationError(
                "the Odoo API key did not authenticate a user")
        with self._lock:
            self._uid = uid
        return uid

    def time_zone(self) -> str:
        """The signed-in user's own time zone, as Odoo holds it.

        Odoo stores datetimes in UTC and every one of its own screens shows them in this
        zone, so an assistant that reports UTC disagrees with the page the person is looking
        at, and "today" means a different day for the first hours of theirs (P7.5 CR-18). A
        user without a zone is UTC in Odoo too, so that is what this answers.
        """
        with self._lock:
            if self._time_zone is not None:
                return self._time_zone
        zone = ""
        try:
            rows = self.call("res.users", "read", [[self._authenticate()]], {"fields": ["tz"]})
            if isinstance(rows, list) and rows and isinstance(rows[0], dict):
                value = rows[0].get("tz")
                if isinstance(value, str) and len(value) <= 64:
                    ZoneInfo(value)
                    zone = value
        except (BusinessToolError, ZoneInfoNotFoundError, ValueError, KeyError, TypeError):
            zone = ""
        with self._lock:
            self._time_zone = zone
        return zone

    def call(self, model: str, method: str, args: list, kwargs: dict | None = None,
             *, mutation: bool = False):
        uid = self._authenticate()
        return self._rpc({"service": "object", "method": "execute_kw",
                          "args": [self.keys.database, uid, self.keys.api_key,
                                   model, method, args, kwargs or {}]}, mutation=mutation)

    # -- identity -----------------------------------------------------------------------
    def resolve_delegated_identity(self, *, lifetime_s: int = 300,
                                   now_ms: int | None = None) -> IdentityAssertion:
        if isinstance(lifetime_s, bool) or not isinstance(lifetime_s, int) \
                or not 1 <= lifetime_s <= 3_600:
            raise ValueError("identity lifetime_s must be an integer from 1 to 3600")
        uid = self._authenticate()
        rows = self.call("res.users", "read", [[uid]], {"fields": ["login", "company_id"]})
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
            raise BusinessToolIdentityError("Odoo returned no delegated user record")
        company = rows[0].get("company_id")
        company_id = company[0] if isinstance(company, list) and company else None
        if isinstance(company_id, bool) or not isinstance(company_id, int):
            raise BusinessToolIdentityError("the Odoo delegated user has no company")
        issued_at = int(time.time() * 1000) if now_ms is None else now_ms
        return IdentityAssertion(
            schema_version=1, source="delegated_api", application_id=APPLICATION_ID,
            tenant_id=self.keys.database, workspace_id=str(company_id),
            actor_id=str(uid), issued_at=issued_at,
            expires_at=issued_at + lifetime_s * 1000)
