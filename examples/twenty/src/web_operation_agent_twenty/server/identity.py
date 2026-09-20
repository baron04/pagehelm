"""Browser-session identity and delegated OAuth token handling for Twenty."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from http.client import HTTPConnection, HTTPSConnection, HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from web_operation_agent.business.runtime import (BusinessToolAuthenticationError,
                                                  BusinessToolError,
                                                  BusinessToolIdentityError)
from web_operation_agent.state.identity import IdentityAssertion

from .specs import (_COOKIE_NAME, _IDENTITY_QUERY, _MAX_IDENTITY_RESPONSE_BYTES,
                    _MAX_STORAGE_STATE_BYTES, _SESSION_COOKIE_NAMES, _validated_origin)

try:
    import fcntl
except ImportError:  # pragma: no cover - the public runtime requires POSIX.
    fcntl = None


class TwentyPageSessionIdentityResolver:
    """Resolve the browser actor by presenting its exact session cookie to Twenty.

    The storage-state file is trusted host input and never crosses the Runtime or model
    boundary. Only Twenty's HttpOnly session cookie is sent, directly to the configured
    origin with no redirect support; analytics and local-storage values are ignored.
    """

    def __init__(self, *, base_url: str,
                 storage_state_path: str | Path | None = None,
                 storage_state: dict | None = None,
                 timeout_s: float = 15):
        self.base_url = _validated_origin(base_url)
        if (storage_state_path is None) == (storage_state is None):
            raise ValueError("supply exactly one browser storage state source")
        self.storage_state_path = (
            Path(storage_state_path) if storage_state_path is not None else None)
        self._storage_state = storage_state
        if (isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float))
                or not math.isfinite(timeout_s) or timeout_s <= 0 or timeout_s > 60):
            raise ValueError("page identity timeout_s must be greater than 0 and at most 60 seconds")
        self.timeout_s = float(timeout_s)

    def _load_storage_state(self) -> dict:
        if self.storage_state_path is not None:
            descriptor = -1
            try:
                flags = os.O_RDONLY
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                descriptor = os.open(self.storage_state_path, flags)
                metadata = os.fstat(descriptor)
                if metadata.st_mode & 0o077:
                    raise BusinessToolIdentityError(
                        "Twenty browser storage state must not be group/world accessible")
                with os.fdopen(descriptor, "rb") as handle:
                    descriptor = -1
                    raw = handle.read(_MAX_STORAGE_STATE_BYTES + 1)
                if len(raw) > _MAX_STORAGE_STATE_BYTES:
                    raise BusinessToolIdentityError(
                        "Twenty browser storage state exceeds 2 MiB")
                payload = json.loads(raw)
            except BusinessToolError:
                raise
            except (OSError, json.JSONDecodeError) as exc:
                raise BusinessToolIdentityError(
                    "Twenty browser storage state is unavailable") from exc
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
        else:
            try:
                raw = json.dumps(
                    self._storage_state, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"), allow_nan=False).encode("utf-8")
                if len(raw) > _MAX_STORAGE_STATE_BYTES:
                    raise BusinessToolIdentityError(
                        "Twenty browser storage state exceeds 2 MiB")
                payload = json.loads(raw)
            except BusinessToolError:
                raise
            except (TypeError, ValueError, OverflowError) as exc:
                raise BusinessToolIdentityError(
                    "Twenty browser storage state is invalid") from exc
        if not isinstance(payload, dict):
            raise BusinessToolIdentityError("Twenty browser storage state is invalid")
        return payload

    def _session_cookie(self, *, now_s: float) -> tuple[str, int | None]:
        cookies = self._load_storage_state().get("cookies")
        if not isinstance(cookies, list):
            raise BusinessToolIdentityError("Twenty browser storage state is invalid")
        parsed = urlsplit(self.base_url)
        host = (parsed.hostname or "").casefold()
        secure_request = parsed.scheme == "https"
        matches: list[tuple[str, str, int | None]] = []
        for cookie in cookies:
            if not isinstance(cookie, dict) or cookie.get("name") not in _SESSION_COOKIE_NAMES:
                continue
            name = cookie.get("name")
            value = cookie.get("value")
            domain = cookie.get("domain")
            path = cookie.get("path", "/")
            secure = cookie.get("secure", False)
            http_only = cookie.get("httpOnly")
            expires = cookie.get("expires", -1)
            if (not isinstance(name, str) or not _COOKIE_NAME.fullmatch(name)
                    or not isinstance(value, str) or not value
                    or re.search(r"[\x00-\x20\x7f;,]", value)
                    or not isinstance(domain, str) or not domain
                    or not isinstance(path, str) or not path.startswith("/")
                    or not isinstance(secure, bool) or http_only is not True
                    or isinstance(expires, bool)
                    or not isinstance(expires, (int, float))
                    or not math.isfinite(expires)):
                raise BusinessToolIdentityError(
                    "Twenty browser session cookie is invalid")
            cookie_domain = domain.lstrip(".").casefold()
            if host != cookie_domain:
                continue
            if secure and not secure_request:
                continue
            if path != "/":
                continue
            expires_at = int(expires * 1000) if expires > 0 else None
            if expires_at is not None and expires <= now_s:
                continue
            matches.append((name, value, expires_at))
        if len(matches) != 1:
            raise BusinessToolIdentityError(
                "Twenty browser storage state must contain one live session cookie")
        name, value, expires_at = matches[0]
        return f"{name}={value}", expires_at

    def binding_digest(self, *, now_ms: int | None = None) -> str:
        """Return a non-secret binding for detecting browser session replacement."""
        checked_at = int(time.time() * 1000) if now_ms is None else now_ms
        if isinstance(checked_at, bool) or not isinstance(checked_at, int) or checked_at < 0:
            raise ValueError("identity now_ms must be a non-negative integer")
        cookie, _expires_at = self._session_cookie(now_s=checked_at / 1000)
        return hashlib.sha256(cookie.encode("utf-8")).hexdigest()

    def resolve(self, *, lifetime_s: int = 300,
                now_ms: int | None = None) -> IdentityAssertion:
        if (isinstance(lifetime_s, bool) or not isinstance(lifetime_s, int)
                or not 1 <= lifetime_s <= 3_600):
            raise ValueError("identity lifetime_s must be an integer from 1 to 3600")
        issued_at = int(time.time() * 1000) if now_ms is None else now_ms
        if isinstance(issued_at, bool) or not isinstance(issued_at, int) or issued_at < 0:
            raise ValueError("identity now_ms must be a non-negative integer")
        cookie, cookie_expires_at = self._session_cookie(now_s=issued_at / 1000)
        parsed = urlsplit(self.base_url)
        connection_type = HTTPSConnection if parsed.scheme == "https" else HTTPConnection
        connection = connection_type(
            parsed.hostname, parsed.port, timeout=self.timeout_s)
        body = json.dumps({"query": _IDENTITY_QUERY}, separators=(",", ":")).encode()
        try:
            connection.request("POST", "/metadata", body=body, headers={
                "Cookie": cookie,
                "Origin": self.base_url,
                "Accept": "application/json",
                "Content-Type": "application/json",
            })
            response = connection.getresponse()
            raw = response.read(_MAX_IDENTITY_RESPONSE_BYTES + 1)
            if len(raw) > _MAX_IDENTITY_RESPONSE_BYTES:
                raise BusinessToolIdentityError(
                    "Twenty page identity response exceeds 1 MiB")
            payload = json.loads(raw)
        except BusinessToolError:
            raise
        except (OSError, TimeoutError, HTTPException, json.JSONDecodeError) as exc:
            raise BusinessToolIdentityError(
                "Twenty page session identity read failed") from exc
        finally:
            connection.close()
        if response.status != 200 or not isinstance(payload, dict) or payload.get("errors"):
            raise BusinessToolIdentityError(
                "Twenty page session identity response is invalid")
        data = payload.get("data")
        user = data.get("currentUser") if isinstance(data, dict) else None
        workspace = data.get("currentWorkspace") if isinstance(data, dict) else None
        actor_id = user.get("id") if isinstance(user, dict) else None
        workspace_id = workspace.get("id") if isinstance(workspace, dict) else None
        if not isinstance(actor_id, str) or not isinstance(workspace_id, str):
            raise BusinessToolIdentityError(
                "Twenty page session identity response is incomplete")
        expires_at = issued_at + lifetime_s * 1000
        if cookie_expires_at is not None:
            expires_at = min(expires_at, cookie_expires_at)
        if expires_at <= issued_at:
            raise BusinessToolIdentityError(
                "Twenty browser session cookie has expired")
        return IdentityAssertion(
            schema_version=1,
            source="page_session",
            application_id="twenty",
            tenant_id=workspace_id,
            workspace_id=workspace_id,
            actor_id=actor_id,
            issued_at=issued_at,
            expires_at=expires_at,
        )


class TwentyOAuthTokenStore:
    _locks_guard = threading.Lock()
    _thread_locks: dict[str, threading.Lock] = {}

    def __init__(self, token_path: str | Path, registration_path: str | Path):
        self.token_path = Path(token_path)
        self.registration_path = Path(registration_path)

    @staticmethod
    def _read(path: Path) -> dict:
        try:
            value = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise BusinessToolAuthenticationError(
                "Twenty OAuth state is unavailable") from exc
        if not isinstance(value, dict):
            raise BusinessToolAuthenticationError("Twenty OAuth state is invalid")
        return value

    def access_token(self) -> str:
        value = self._read(self.token_path).get("access_token")
        if not isinstance(value, str) or not value:
            raise BusinessToolAuthenticationError(
                "Twenty OAuth access token is unavailable")
        return value

    @contextmanager
    def _refresh_lock(self):
        if fcntl is None:
            raise BusinessToolAuthenticationError(
                "Twenty OAuth refresh requires a POSIX file lock")
        key = str(self.token_path.resolve(strict=False))
        with self._locks_guard:
            thread_lock = self._thread_locks.setdefault(key, threading.Lock())
        with thread_lock:
            lock_path = self.token_path.with_name(f".{self.token_path.name}.refresh.lock")
            flags = os.O_CREAT | os.O_RDWR
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                descriptor = os.open(lock_path, flags, 0o600)
            except OSError as exc:
                raise BusinessToolAuthenticationError(
                    "Twenty OAuth refresh lock is unavailable") from exc
            try:
                os.fchmod(descriptor, 0o600)
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            finally:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)

    def refresh(self, base_url: str, timeout_s: float, *,
                rejected_access_token: str | None = None) -> str:
        """Refresh once across threads/processes, or reuse a newer stored token."""
        with self._refresh_lock():
            tokens = self._read(self.token_path)
            current = tokens.get("access_token")
            if (rejected_access_token is not None and isinstance(current, str)
                    and current and current != rejected_access_token):
                return current
            registration = self._read(self.registration_path)
            refresh = tokens.get("refresh_token")
            client_id = registration.get("client_id")
            if not isinstance(refresh, str) or not refresh or not isinstance(client_id, str) \
                    or not client_id:
                raise BusinessToolAuthenticationError(
                    "Twenty OAuth refresh state is unavailable")
            body = urlencode({
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": client_id,
            }).encode()
            request = Request(
                base_url + "/oauth/token", data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
            try:
                with urlopen(request, timeout=timeout_s) as response:
                    updated = json.load(response)
            except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
                raise BusinessToolAuthenticationError(
                    "Twenty OAuth refresh failed") from exc
            access_token = updated.get("access_token") if isinstance(updated, dict) else None
            if not isinstance(access_token, str) or not access_token:
                raise BusinessToolAuthenticationError(
                    "Twenty OAuth refresh returned no access token")
            if not updated.get("refresh_token"):
                updated["refresh_token"] = refresh
            updated["obtained_at"] = int(time.time())
            self.token_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self.token_path.name}.", suffix=".tmp",
                dir=self.token_path.parent)
            temporary = Path(temporary_name)
            try:
                os.fchmod(descriptor, 0o600)
                handle = os.fdopen(descriptor, "w", encoding="utf-8")
                descriptor = -1
                with handle:
                    json.dump(updated, handle, ensure_ascii=False, sort_keys=True, indent=2)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.token_path)
                directory_fd = os.open(self.token_path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                temporary.unlink(missing_ok=True)
            return access_token
