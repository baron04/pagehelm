"""Odoo page-session identity and delegated API-key storage."""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from web_operation_agent.business.runtime import (BusinessToolAuthenticationError,
                                                  BusinessToolIdentityError)
from web_operation_agent.state.identity import IdentityAssertion

from .specs import (APPLICATION_ID, _MAX_RESPONSE_BYTES, _MAX_STORAGE_STATE_BYTES,
                    _SESSION_COOKIE, _validated_origin)


class OdooApiKeyStore:
    """A per-user Odoo API key held in one ignored 0600 file.

    Odoo mints these in Settings -> My Profile -> Account Security -> New API Key, bound to
    one user and one expiry. Provisioning is therefore a deployment step performed by the
    person whose records the Agent will act on, not something this adapter can do for them.
    """

    def __init__(self, key_path: str | Path):
        self.path = Path(key_path)
        self._lock = threading.Lock()
        self._cached: dict | None = None

    def _read(self) -> dict:
        with self._lock:
            if self._cached is not None:
                return self._cached
            try:
                raw = self.path.read_bytes()
            except OSError as exc:
                raise BusinessToolAuthenticationError(
                    "the Odoo API key file is unreadable") from exc
            if len(raw) > 64 * 1024:
                raise BusinessToolAuthenticationError("the Odoo API key file is oversized")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise BusinessToolAuthenticationError(
                    "the Odoo API key file is not valid JSON") from exc
            if not isinstance(payload, dict):
                raise BusinessToolAuthenticationError("the Odoo API key file is malformed")
            values = {}
            for field, pattern in (("database", r"[A-Za-z0-9_.-]{1,63}"),
                                   ("login", r"[^\x00-\x1f\x7f]{1,254}"),
                                   ("api_key", r"[A-Za-z0-9_-]{16,256}")):
                value = payload.get(field)
                if not isinstance(value, str) or not re.fullmatch(pattern, value):
                    raise BusinessToolAuthenticationError(
                        f"the Odoo API key file has no valid {field}")
                values[field] = value
            self._cached = values
            return values

    @property
    def database(self) -> str:
        return self._read()["database"]

    @property
    def login(self) -> str:
        return self._read()["login"]

    @property
    def api_key(self) -> str:
        return self._read()["api_key"]


class OdooPageSessionIdentityResolver:
    """Resolve who the browser is logged in as, without ever exposing its credential.

    The session cookie is read from the browser context's storage state, used once against
    ``/web/session/get_session_info``, and never returned: the assertion this produces
    carries identifiers only.
    """

    def __init__(self, *, base_url: str, storage_state_path: str | Path | None = None,
                 storage_state: dict | None = None, timeout_s: float = 15):
        if (storage_state_path is None) == (storage_state is None):
            raise ValueError("supply exactly one of storage_state_path or storage_state")
        self.base_url = _validated_origin(base_url)
        self.storage_state_path = storage_state_path
        self._storage_state = storage_state
        self.timeout_s = timeout_s

    def _load(self) -> dict:
        if self._storage_state is not None:
            return self._storage_state
        path = Path(self.storage_state_path)
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise BusinessToolIdentityError(
                "the Odoo browser storage state is unreadable") from exc
        if len(raw) > _MAX_STORAGE_STATE_BYTES:
            raise BusinessToolIdentityError("the Odoo browser storage state is oversized")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BusinessToolIdentityError(
                "the Odoo browser storage state is not valid JSON") from exc

    def _session_cookie(self) -> str:
        state = self._load()
        cookies = state.get("cookies") if isinstance(state, dict) else None
        if not isinstance(cookies, list):
            raise BusinessToolIdentityError(
                "the Odoo browser storage state has no cookies")
        host = urlsplit(self.base_url).hostname or ""
        for cookie in cookies:
            if not isinstance(cookie, dict) or cookie.get("name") != _SESSION_COOKIE:
                continue
            domain = str(cookie.get("domain") or "").lstrip(".").casefold()
            if domain and domain != host.casefold():
                continue
            value = cookie.get("value")
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{16,512}", value):
                return value
        raise BusinessToolIdentityError(
            "the Odoo browser storage state carries no usable session cookie")

    def binding_digest(self) -> str:
        """A stable digest of the live session cookie, for detecting a replaced session."""
        import hashlib
        return hashlib.sha256(
            f"odoo:{self.base_url}:{self._session_cookie()}".encode("utf-8")).hexdigest()

    def resolve(self, *, lifetime_s: int = 300,
                now_ms: int | None = None) -> IdentityAssertion:
        cookie = self._session_cookie()
        request = Request(self.base_url + "/web/session/get_session_info",
                          data=json.dumps({"jsonrpc": "2.0", "method": "call",
                                           "params": {}}).encode("utf-8"), method="POST")
        request.add_header("Content-Type", "application/json")
        request.add_header("Cookie", f"{_SESSION_COOKIE}={cookie}")
        try:
            with urlopen(request, timeout=self.timeout_s) as response:
                payload = json.loads(response.read(_MAX_RESPONSE_BYTES))
        except HTTPError as exc:
            raise BusinessToolIdentityError(
                f"Odoo rejected the page session ({exc.code})") from exc
        except (URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            raise BusinessToolIdentityError(
                "the Odoo page session could not be resolved") from exc
        info = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(info, dict) or info.get("uid") in (None, False):
            raise BusinessToolIdentityError(
                "the Odoo page session is no longer authenticated")
        companies = info.get("user_companies")
        company = (companies.get("current_company")
                   if isinstance(companies, dict) else None)
        database = info.get("db")
        uid = info.get("uid")
        if (not isinstance(database, str) or not database
                or isinstance(uid, bool) or not isinstance(uid, int)
                or isinstance(company, bool) or not isinstance(company, int)):
            raise BusinessToolIdentityError("the Odoo session info is incomplete")
        issued_at = int(time.time() * 1000) if now_ms is None else now_ms
        return IdentityAssertion(
            schema_version=1, source="page_session", application_id=APPLICATION_ID,
            tenant_id=database, workspace_id=str(company), actor_id=str(uid),
            issued_at=issued_at, expires_at=issued_at + lifetime_s * 1000)
