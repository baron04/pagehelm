"""Actor-bound admin reads and separately approved, once-only native dispatch.

Every request carries the tenant and the account's access token. A client made with an
account renews an expired token by logging in again once; one made from a person's own
token (`for_token`) never logs in: an expired token is refused, and the page's newer token
is installed with `renew`. Ordinary reads use GET. The explicit `write_once` entry never
retries or renews a token, and it never follows a URL a response names.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from web_operation_agent.goal.data import DataRefused

from ..host.accounts import load_account

#: yudao's "not logged in" code.
_UNAUTHORIZED = 401
_READ_CHECKPOINT = ContextVar("yudao_read_checkpoint", default=None)
_FENCED_GETS_GUARD: ContextVar[frozenset[str] | None] = ContextVar("yudao_fenced_gets_guard", default=None)


def _matches_fenced_get(path: str, fenced: frozenset[str]) -> bool:
    if not fenced:
        return False
    clean = path.split("?", 1)[0].lstrip("/")
    full = "/admin-api/" + clean
    if full in fenced or clean in fenced or ("/" + clean) in fenced:
        return True
    from ..host.page_guard import _writing_get_pattern
    folded = full.casefold()
    for entry in fenced:
        normalized = entry if entry.startswith("/admin-api/") else "/admin-api/" + entry.lstrip("/")
        try:
            if _writing_get_pattern(normalized).fullmatch(folded):
                return True
        except ValueError:
            continue
    return False


def _enforce_fenced_read(method: str, path: str) -> None:
    fenced = _FENCED_GETS_GUARD.get()
    if fenced is None:
        return
    if method != "GET":
        raise YudaoReadRefused(
            f"{path}: non-GET {method} refused in read-only fenced context",
            code=403,
            reason="fenced_non_get",
        )
    if _matches_fenced_get(path, fenced):
        raise YudaoReadRefused(
            f"{path}: writing GET refused in fenced context",
            code=403,
            reason="fenced_writing_get",
        )


class YudaoReadRefused(DataRefused):
    """Preserve the application's response code across the legacy and generic readers."""
    def __init__(self, message: str, *, code, reason: str = ""):
        super().__init__(message, reason=reason,
                         permission_denied=type(code) is int and code == 403,
                         read_failed=type(code) is int and 500 <= code <= 599)
        self.code = code


@dataclass(frozen=True)
class YudaoAccount:
    username: str
    password: str
    tenant: str = "1"

    @staticmethod
    def load(name: str, path) -> "YudaoAccount":
        """The account stored under `name` in a private accounts file (see `.accounts`)."""
        data = load_account(name, path)
        return YudaoAccount(str(data["username"]), str(data["password"]), str(data.get("tenant", "1")))


class YudaoClient:
    def __init__(self, base_url: str, account: YudaoAccount | None, *, timeout_s: float = 20,
                 token: str | None = None, tenant: str = ""):
        self.base_url = base_url.rstrip("/")
        self._account, self._timeout = account, timeout_s
        self._token: str | None = token
        self._tenant = str(account.tenant if account is not None else tenant or "1")
        self._lock = threading.RLock()
        self.calls: list[str] = []
        self._read_generation = 0

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
            if self._token != token:
                self._read_generation += 1
            self._token = token

    @property
    def read_generation(self) -> int:
        """Non-secret cache generation; renewed credentials invalidate brief data reads."""
        with self._lock:
            return self._read_generation

    @property
    def tenant(self) -> str:
        return self._tenant

    def get(self, path: str, **params):
        """GET /admin-api/<path>; the `data` of a successful response."""
        _enforce_fenced_read("GET", path)
        for attempt in range(2):
            check = _READ_CHECKPOINT.get()
            if check is not None:
                check()
            code, body = self._request("GET", path, params=params, token=self._login())
            if check is not None:
                check()
            if code == _UNAUTHORIZED and attempt == 0 and self._account is not None:
                with self._lock:
                    self._token = None
                    self._read_generation += 1
                continue
            return self._data(path, code, body)
        raise DataRefused(f"{path}: not authorised")

    def write_once(
        self,
        method: str,
        path: str,
        *,
        body: dict,
        params=None,
        operation: str = "",
        expect_result: str = "",
    ):
        """One approved native dispatch; never retry, relogin, or follow redirects.

        Business errors after native dispatch may have partial effects. Only an
        authentication/permission refusal at the application's outer gate proves
        zero effects; everything else retains the unresolved journal entry.
        """
        from web_operation_agent.business.errors import (BusinessMutationIndeterminate,
            BusinessMutationRefusedError, BusinessToolAuthenticationError, BusinessToolPermissionError)
        from ..writes.write_definitions import BACKEND_WRITE_ATOMICITY
        if method not in {"POST", "PUT"} or not re_safe_native_path(path):
            raise ValueError("native write requires a reviewed method and path")
        if _FENCED_GETS_GUARD.get() is not None:
            raise BusinessToolPermissionError("writes are forbidden in read-only fenced context")
        if operation and operation not in BACKEND_WRITE_ATOMICITY:
            raise ValueError("native write has no reviewed backend contract")
        atomicity = BACKEND_WRITE_ATOMICITY.get(operation)
        def evidence(response_class, **details):
            result = {"response_class": response_class, **details}
            if atomicity is not None:
                result["backend_transactional"] = atomicity[0]
                result["backend_service_method"] = atomicity[1]
            return result
        with self._lock:
            token = self._token
        if not token:
            raise BusinessToolAuthenticationError("authenticate before preparing the write")
        url = f"{self.base_url}/admin-api/{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        request = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False,
            allow_nan=False).encode(), method=method, headers={"tenant-id": self._tenant,
            "Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl): return None
        opener = urllib.request.build_opener(NoRedirect())
        with self._lock:
            self.calls.append(f"{method} {path}")
        from web_operation_agent.business.dispatch_evidence import mutation_transport
        with mutation_transport() as transport:
            try:
                with opener.open(request, timeout=self._timeout) as response:
                    payload = json.load(response)
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    transport.rejected()
                    raise BusinessToolPermissionError("the application denied this write") from exc
                raise BusinessMutationIndeterminate("native dispatch returned no safe outcome", phase="dispatch",
                    reconciliation=evidence("http_error", upstream_code=str(exc.code))) from exc
            except (OSError, ValueError, urllib.error.URLError) as exc:
                raise BusinessMutationIndeterminate("native dispatch returned no safe outcome", phase="dispatch") from exc
            if not isinstance(payload, dict) or payload.get("code") != 0:
                if isinstance(payload, dict) and payload.get("code") in (401, 403):
                    transport.rejected()
                    raise BusinessToolPermissionError("the application denied this write")
                code = payload.get("code") if isinstance(payload, dict) else None
                message = (payload.get("msg") or payload.get("message") or "") if isinstance(payload, dict) else ""
                # These two prefixes come only from Spring MVC argument binding
                # before entering the controller. The similar "请求参数不正确:" is
                # also used for ConstraintViolationException and is not proof
                # that an already-running service had no partial effect.
                if code == 400 and isinstance(message, str) and message.startswith(
                        ("请求参数类型错误:", "请求参数缺失:")):
                    transport.rejected()
                    raise BusinessMutationRefusedError("the application rejected the request parameter before the write",
                        reason="application_parameter_rejected")
                result_evidence = evidence("application_error" if isinstance(payload, dict)
                            else "invalid_response")
                if type(code) is int and 0 <= code <= 999999999:
                    result_evidence["upstream_code"] = str(code)
                if isinstance(message, str) and message.startswith("请求参数不正确"):
                    result_evidence["message_class"] = "validation_wording"
                elif type(code) is int and code >= 500:
                    result_evidence["message_class"] = "server_error"
                elif isinstance(message, str) and message:
                    result_evidence["message_class"] = "application_wording"
                else:
                    result_evidence["message_class"] = "unspecified"
                raise BusinessMutationIndeterminate("native operation may have partially completed",
                    phase="dispatch", reconciliation=result_evidence)
            value = payload.get("data")
            wanted = expect_result or ("bool" if method == "PUT" else "id")
            if wanted == "bool":
                acknowledged = value is True
            elif wanted == "id":
                acknowledged = type(value) is int and value > 0
            else:
                acknowledged = value is True or (type(value) is int and value > 0)
            if not acknowledged:
                raise BusinessMutationIndeterminate("native operation returned no effect acknowledgement", phase="acknowledgement")
            transport.applied()
            return value

    @contextmanager
    def read_window(self, checkpoint):
        """Bound every paginated/detail subrequest without changing another thread's client."""
        marker = _READ_CHECKPOINT.set(checkpoint)
        try:
            yield
        finally:
            _READ_CHECKPOINT.reset(marker)

    @contextmanager
    def fenced_read_only(self, writing_gets: frozenset[str] = frozenset()):
        """Enforce read-only GETs and refuse any endpoint in `writing_gets`."""
        existing = _FENCED_GETS_GUARD.get()
        merged = frozenset(writing_gets) if existing is None else (existing | frozenset(writing_gets))
        marker = _FENCED_GETS_GUARD.set(merged)
        try:
            yield
        finally:
            _FENCED_GETS_GUARD.reset(marker)

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
        _enforce_fenced_read(method, path)
        from web_operation_agent.execution.events import turn_interval
        with turn_interval("application"):
            return self._request_transport(method, path, params=params, body=body, token=token)

    def _request_transport(self, method: str, path: str, *, params=None, body=None, token=None):
        _enforce_fenced_read(method, path)
        url = f"{self.base_url}/admin-api/{path}"
        if params:
            url += "?" + urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None}, doseq=True)
        headers = {"tenant-id": self._tenant, "Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = json.dumps(body).encode() if body is not None else None
        with self._lock:
            self.calls.append(f"{method} {path}")
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            # The transport's authorization status must not turn into a retryable
            # availability failure. Do not echo an HTML error body or response headers.
            if exc.code in (401, 403):
                return exc.code, {"code": exc.code}
            raise DataRefused(f"{path}: HTTP {exc.code}", read_failed=500 <= exc.code <= 599) from exc
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise DataRefused(f"{path}: {type(exc).__name__}", read_failed=True) from exc
        return payload.get("code"), payload

    @staticmethod
    def _data(path: str, code, body):
        if code != 0:
            # The application's own sentence is what a person can act on ("请选择时间范围");
            # the endpoint and the code stay in the audit's fuller rendering.
            message = str(body.get("msg") or "")[:120]
            raise YudaoReadRefused(f"{path}: {code} {message}", code=code, reason=message)
        return body.get("data")


__all__ = ["YudaoAccount", "YudaoClient"]


def re_safe_native_path(path):
    import re
    return isinstance(path, str) and re.fullmatch(r"[a-z][a-z0-9/-]{1,120}", path) is not None
