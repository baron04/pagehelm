"""A read-only client of yudao's admin API under one account.

Every request carries the tenant and the account's access token. A client made with an
account renews an expired token by logging in again once; one made from a person's own
token (`for_token`) never logs in: an expired token is refused, and the page's newer token
is installed with `renew`. The client never sends anything but GET requests after login,
and it never follows a URL a response names.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from web_operation_agent.goal.data import DataRefused

#: yudao's "not logged in" code.
_UNAUTHORIZED = 401


@dataclass(frozen=True)
class YudaoAccount:
    username: str
    password: str
    tenant: str = "1"

    @staticmethod
    def from_file(path) -> "YudaoAccount":
        """A JSON file {"username", "password", "tenant"} kept outside Git."""
        data = json.loads(open(path, encoding="utf-8").read())
        return YudaoAccount(str(data["username"]), str(data["password"]), str(data.get("tenant", "1")))


class YudaoClient:
    def __init__(self, base_url: str, account: YudaoAccount | None, *, timeout_s: float = 20,
                 token: str | None = None, tenant: str = ""):
        self.base_url = base_url.rstrip("/")
        self._account, self._timeout = account, timeout_s
        self._token: str | None = token
        self._tenant = str(account.tenant if account is not None else tenant or "1")
        self._lock = threading.Lock()
        self.calls: list[str] = []

    @staticmethod
    def for_token(base_url: str, token: str, tenant: str, *, timeout_s: float = 20) -> "YudaoClient":
        """Reads as the person whose access token this is, never logging in."""
        if not token:
            raise ValueError("an access token is required")
        return YudaoClient(base_url, None, timeout_s=timeout_s, token=token, tenant=tenant)

    def renew(self, token: str) -> None:
        """Install the person's newer access token (the page renews its own)."""
        if self._account is not None or not token:
            raise ValueError("only a client made from a token takes a newer one")
        with self._lock:
            self._token = token

    @property
    def tenant(self) -> str:
        return self._tenant

    def get(self, path: str, **params):
        """GET /admin-api/<path>; the `data` of a successful response."""
        for attempt in range(2):
            code, body = self._request("GET", path, params=params, token=self._login())
            if code == _UNAUTHORIZED and attempt == 0 and self._account is not None:
                with self._lock:
                    self._token = None
                continue
            return self._data(path, code, body)
        raise DataRefused(f"{path}: not authorised")

    def _login(self) -> str:
        with self._lock:
            if self._token is None:
                if self._account is None:
                    raise DataRefused("the access token is no longer valid")
                code, body = self._request("POST", "system/auth/login", body={
                    "username": self._account.username, "password": self._account.password})
                self._token = self._data("system/auth/login", code, body)["accessToken"]
            return self._token

    def _request(self, method: str, path: str, *, params=None, body=None, token=None):
        url = f"{self.base_url}/admin-api/{path}"
        if params:
            url += "?" + urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None}, doseq=True)
        headers = {"tenant-id": self._tenant, "Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = json.dumps(body).encode() if body is not None else None
        self.calls.append(f"{method} {path}")
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                payload = json.load(response)
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise DataRefused(f"{path}: {type(exc).__name__}") from exc
        return payload.get("code"), payload

    @staticmethod
    def _data(path: str, code, body):
        if code != 0:
            # The application's own sentence is what a person can act on ("请选择时间范围");
            # the endpoint and the code stay in the audit's fuller rendering.
            message = str(body.get("msg") or "")[:120]
            raise DataRefused(f"{path}: {code} {message}", reason=message)
        return body.get("data")


__all__ = ["YudaoAccount", "YudaoClient"]
