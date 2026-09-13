"""Narrow Twenty v2.39.0 adapter for company data and record handoff.

Application credentials remain in ignored 0600 files.  The adapter projects company
name/domain fields plus bounded record routes, and its audit record never contains bearer
tokens, browser cookies, or raw result rows.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import threading
import time
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from http.client import HTTPConnection, HTTPSConnection, HTTPException
from importlib import resources
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from web_operation_agent.answering import StructuredQueryAnswer
from web_operation_agent.business import (BusinessMutationIndeterminate,
                                          BusinessMutationSpec, BusinessSkill,
                                          BusinessToolAuthenticationError,
                                          BusinessToolContractError,
                                          BusinessToolError,
                                          BusinessToolIdentityError,
                                          BusinessToolPermissionError,
                                          BusinessToolRequestError,
                                          BusinessToolResult,
                                          BusinessToolRuntime,
                                          BusinessToolUnavailableError,
                                          business_text_key, normalize_business_text)
from web_operation_agent.application import (
    APPLICATION_ADAPTER_VERSION,
    ApplicationDescriptor,
    ApplicationPorts,
    DeclaredRoutePort,
    MutationReconciliation,
    DeclaredDataClassifier,
    ResultPostconditionVerifier,
)
from web_operation_agent.identity import (IdentityAssertion, OperationContext,
                                          PrincipalContext, bind_matching_identity)
from web_operation_agent.mutation import MutationJournal

try:
    import fcntl
except ImportError:  # pragma: no cover - the public runtime requires POSIX.
    fcntl = None


QUERY_COMPANIES_SCHEMA = {
    "type": "function",
    "function": {
        "name": "query_companies",
        "description": "Read companies visible to the currently delegated Twenty user.",
        "parameters": {
            "type": "object",
            "properties": {
                "name_contains": {
                    "type": "string",
                    "maxLength": 100,
                    "description": "Optional case-insensitive company name substring.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "description": "Maximum rows to return; defaults to 20.",
                },
            },
            "additionalProperties": False,
        },
    },
}


def _query_record_schema(name: str, description: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {
                    "name_contains": {
                        "type": "string", "maxLength": 100,
                        "description": "Optional case-insensitive record name substring.",
                    },
                    "limit": {
                        "type": "integer", "minimum": 1, "maximum": 50,
                        "description": "Maximum rows to return; defaults to 20.",
                    },
                },
                "additionalProperties": False,
            },
        },
    }


QUERY_PEOPLE_SCHEMA = _query_record_schema(
    "query_people", "Read people visible to the currently delegated Twenty user.")
QUERY_OPPORTUNITIES_SCHEMA = _query_record_schema(
    "query_opportunities",
    "Read opportunities visible to the currently delegated Twenty user.")

CREATE_COMPANY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "create_company",
        "description": (
            "Propose creation of exactly one company for explicit user confirmation. "
            "The server checks duplicates again before writing and returns the created route."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 200,
                    "description": "Exact company name supplied by the user.",
                },
                "domain": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 253,
                    "description": "Company domain without a path or credentials.",
                },
            },
            "required": ["name", "domain"],
            "additionalProperties": False,
        },
    },
}

_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}", re.I)
_DOMAIN = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?", re.I)
_SKILL_RESOURCE_ROOT = ("skills",)
_QUERY_SKILL_RESOURCE = "companies-query.json"
_PEOPLE_SKILL_RESOURCE = "people-query.json"
_OPPORTUNITIES_SKILL_RESOURCE = "opportunities-query.json"
_MUTATION_SKILL_RESOURCE = "companies-create.json"
_IDENTITY_QUERY = "query WoaIdentity { currentUser { id } currentWorkspace { id } }"
_SESSION_COOKIE_NAMES = frozenset({"twenty-session", "__Host-twenty-session"})
_COOKIE_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_MAX_STORAGE_STATE_BYTES = 2 * 1024 * 1024
_MAX_IDENTITY_RESPONSE_BYTES = 1024 * 1024
_COMPANY_PAGE_SIZE = 100
_MAX_COMPANY_PAGES = 20
_MAX_COMPANY_RECORDS = 2_000


def _packaged_skill(filename: str) -> BusinessSkill:
    artifact = resources.files("web_operation_agent_twenty")
    for part in (*_SKILL_RESOURCE_ROOT, filename):
        artifact = artifact.joinpath(part)
    artifact_uri = "package://web_operation_agent_twenty/" + "/".join(
        (*_SKILL_RESOURCE_ROOT, filename))
    try:
        raw = artifact.read_bytes()
    except OSError as exc:
        raise RuntimeError(f"packaged Twenty Skill is unavailable: {filename}") from exc
    return BusinessSkill.from_bytes(raw, artifact_uri=artifact_uri)


def normalize_twenty_domain(value: object) -> str:
    return re.sub(r"^https?://", "", str(value or ""), flags=re.I).rstrip("/").casefold()


def _validated_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise ValueError("Twenty base_url must be an http(s) origin")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("plain HTTP is allowed only for a loopback Twenty instance")
    return f"{parsed.scheme}://{parsed.netloc}"


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


class TwentyBusinessToolExecutor:
    def __init__(self, *, base_url: str, tokens: TwentyOAuthTokenStore,
                 timeout_s: float = 15, allow_mutations: bool = False):
        if not isinstance(allow_mutations, bool):
            raise ValueError("allow_mutations must be a boolean")
        self.base_url = _validated_origin(base_url)
        self.tokens = tokens
        self.timeout_s = timeout_s
        self.allow_mutations = allow_mutations

    def tool_schemas(self) -> tuple[dict, ...]:
        reads = (QUERY_COMPANIES_SCHEMA, QUERY_PEOPLE_SCHEMA, QUERY_OPPORTUNITIES_SCHEMA)
        return reads + ((CREATE_COMPANY_SCHEMA,) if self.allow_mutations else ())

    def tool_effects(self) -> dict[str, str]:
        effects = {name: "read" for name in
                   ("query_companies", "query_people", "query_opportunities")}
        if self.allow_mutations:
            effects["create_company"] = "write"
        return effects

    def resolve_delegated_identity(self, *, lifetime_s: int = 300,
                                   now_ms: int | None = None) -> IdentityAssertion:
        """Resolve the OAuth actor and workspace from Twenty's Metadata API."""
        if isinstance(lifetime_s, bool) or not isinstance(lifetime_s, int) \
                or not 1 <= lifetime_s <= 3_600:
            raise ValueError("identity lifetime_s must be an integer from 1 to 3600")
        token = self.tokens.access_token()
        try:
            payload = self._authorized_metadata_json(token)
        except BusinessToolAuthenticationError:
            token = self.tokens.refresh(
                self.base_url, self.timeout_s, rejected_access_token=token)
            payload = self._authorized_metadata_json(token)
        data = payload.get("data") if isinstance(payload, dict) else None
        user = data.get("currentUser") if isinstance(data, dict) else None
        workspace = data.get("currentWorkspace") if isinstance(data, dict) else None
        actor_id = user.get("id") if isinstance(user, dict) else None
        workspace_id = workspace.get("id") if isinstance(workspace, dict) else None
        if not isinstance(actor_id, str) or not isinstance(workspace_id, str):
            raise BusinessToolIdentityError(
                "Twenty Metadata identity response is incomplete")
        issued_at = int(time.time() * 1000) if now_ms is None else now_ms
        return IdentityAssertion(
            schema_version=1,
            source="delegated_api",
            application_id="twenty",
            tenant_id=workspace_id,
            workspace_id=workspace_id,
            actor_id=actor_id,
            issued_at=issued_at,
            expires_at=issued_at + lifetime_s * 1000,
        )

    @staticmethod
    def _identity_audit(context: OperationContext | None) -> dict:
        if context is None:
            return {"identity_binding": "unverified"}
        return {"identity_binding": "principal_bound",
                "identity": context.audit_projection()}

    def execute(self, name: str, args: dict, *,
                context: OperationContext | None = None) -> BusinessToolResult:
        if name in {"query_people", "query_opportunities"}:
            return self._execute_related_query(name, args, context=context)
        if name != "query_companies":
            raise BusinessToolRequestError("unknown Twenty business tool")
        unknown = set(args) - {"name_contains", "limit"}
        if unknown:
            raise BusinessToolRequestError(
                "query_companies received unknown arguments")
        needle = args.get("name_contains", "")
        if not isinstance(needle, str) or len(needle) > 100 \
                or re.search(r"[\x00-\x1f\x7f]", needle):
            raise BusinessToolRequestError(
                "name_contains must be a string of at most 100 characters")
        limit = args.get("limit", 20)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise BusinessToolRequestError(
                "limit must be an integer from 1 to 50")
        payload, status, refreshed = self._get_companies()
        data = payload.get("data") if isinstance(payload, dict) else None
        rows = data.get("companies") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise BusinessToolContractError(
                "Twenty companies response has an unexpected shape")
        projected = []
        folded = needle.casefold().strip()
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("name"), str):
                continue
            if folded and folded not in row["name"].casefold():
                continue
            domain_value = row.get("domainName")
            domain = ""
            if isinstance(domain_value, dict):
                domain = str(domain_value.get("primaryLinkLabel")
                             or domain_value.get("primaryLinkUrl") or "")[:300]
            record_id = row.get("id")
            ui_path = (f"/object/company/{record_id}"
                       if isinstance(record_id, str) and _UUID.fullmatch(record_id)
                       else "")
            item = {"name": row["name"][:300], "domain": domain}
            if ui_path:
                item["ui_path"] = ui_path
            projected.append(item)
        matched = len(projected)
        visible = projected[:limit]
        allowed_routes = ("/objects/companies",) + tuple(
            row["ui_path"] for row in visible if isinstance(row.get("ui_path"), str))
        pagination = payload.get("_woaPagination")
        pagination = pagination if isinstance(pagination, dict) else {}
        raw_source_total = payload.get("totalCount")
        source_total_known = (not isinstance(raw_source_total, bool)
                              and isinstance(raw_source_total, int))
        source_total = raw_source_total
        if not source_total_known:
            source_total = len(rows)
        source_complete = (
            pagination.get("complete") is True
            if pagination else source_total_known and len(rows) == source_total)
        result_truncated = matched > limit or not source_complete
        records_scanned = pagination.get("records_scanned", len(rows))
        if (isinstance(records_scanned, bool)
                or not isinstance(records_scanned, int) or records_scanned < 0):
            records_scanned = len(rows)
        incomplete_reason = pagination.get("incomplete_reason", "")
        if not isinstance(incomplete_reason, str):
            incomplete_reason = ""
        incomplete_reason = incomplete_reason[:500]
        if not source_complete and not incomplete_reason:
            incomplete_reason = "source_scan_incomplete"
        observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return BusinessToolResult(
            content={
                "companies": visible,
                "collection_ui_path": "/objects/companies",
                "matched_count": matched,
                "source_total": source_total,
                "scope": "current_delegated_workspace",
                "observed_at": observed_at,
                "completeness": {
                    "source_total_known": source_total_known,
                    "source_complete": source_complete,
                    "result_truncated": result_truncated,
                    "pages_scanned": pagination.get("pages_scanned", 1),
                    "records_scanned": records_scanned,
                    "incomplete_reason": incomplete_reason,
                },
            },
            audit={
                "application": "twenty",
                "operation": "query_companies",
                "http_status": status,
                "matched_count": matched,
                "returned_count": len(visible),
                "source_total": source_total,
                "source_total_known": source_total_known,
                "source_complete": source_complete,
                "result_truncated": result_truncated,
                "pages_scanned": pagination.get("pages_scanned", 1),
                "records_scanned": records_scanned,
                "incomplete_reason": incomplete_reason,
                "scope": "current_delegated_workspace",
                "observed_at": observed_at,
                "oauth_refreshed": refreshed,
                **self._identity_audit(context),
            },
            allowed_routes=allowed_routes,
            query_answer=StructuredQueryAnswer(
                collection="companies",
                fields=("name", "domain"),
                records=tuple((row["name"], row["domain"]) for row in visible),
                source="Twenty delegated REST /rest/companies",
                scope="current_delegated_workspace",
                observed_at=observed_at,
                source_complete=source_complete,
                result_truncated=result_truncated,
                matched_count=matched,
                source_total=(source_total if source_total_known else None),
                records_scanned=records_scanned,
                incomplete_reason=incomplete_reason,
            ),
        )

    def _execute_related_query(self, name: str, args: dict, *,
                               context: OperationContext | None) -> BusinessToolResult:
        resource = "people" if name == "query_people" else "opportunities"
        unknown = set(args) - {"name_contains", "limit"}
        if unknown:
            raise BusinessToolRequestError(f"{name} received unknown arguments")
        needle = args.get("name_contains", "")
        if (not isinstance(needle, str) or len(needle) > 100
                or re.search(r"[\x00-\x1f\x7f]", needle)):
            raise BusinessToolRequestError(
                "name_contains must be a string of at most 100 characters")
        limit = args.get("limit", 20)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise BusinessToolRequestError("limit must be an integer from 1 to 50")
        payload, status, refreshed = self._get_collection(resource)
        data = payload.get("data") if isinstance(payload, dict) else None
        rows = data.get(resource) if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise BusinessToolContractError(f"Twenty {resource} response has an unexpected shape")
        projected = []
        folded = needle.casefold().strip()
        for row in rows:
            if not isinstance(row, dict):
                continue
            if resource == "people":
                raw_name = row.get("name")
                if not isinstance(raw_name, dict):
                    continue
                display_name = " ".join(str(raw_name.get(part) or "").strip()
                                        for part in ("firstName", "lastName")).strip()
                emails = row.get("emails")
                detail = (str(emails.get("primaryEmail") or "")[:320]
                          if isinstance(emails, dict) else "")
                route_name = "person"
                fields = ("name", "email")
            else:
                display_name = str(row.get("name") or "").strip()
                detail = str(row.get("stage") or "")[:100]
                route_name = "opportunity"
                fields = ("name", "stage")
            if not display_name or (folded and folded not in display_name.casefold()):
                continue
            record_id = row.get("id")
            ui_path = (f"/object/{route_name}/{record_id}"
                       if isinstance(record_id, str) and _UUID.fullmatch(record_id) else "")
            item = {fields[0]: display_name[:300], fields[1]: detail}
            if ui_path:
                item["ui_path"] = ui_path
            projected.append(item)
        matched = len(projected)
        visible = projected[:limit]
        pagination = payload.get("_woaPagination")
        pagination = pagination if isinstance(pagination, dict) else {}
        source_total = payload.get("totalCount")
        source_total_known = not isinstance(source_total, bool) and isinstance(source_total, int)
        if not source_total_known:
            source_total = len(rows)
        source_complete = pagination.get("complete") is True
        result_truncated = matched > limit or not source_complete
        records_scanned = pagination.get("records_scanned", len(rows))
        incomplete_reason = str(pagination.get("incomplete_reason") or "")[:500]
        observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return BusinessToolResult(
            content={resource: visible, "collection_ui_path": f"/objects/{resource}",
                     "matched_count": matched, "source_total": source_total,
                     "scope": "current_delegated_workspace", "observed_at": observed_at,
                     "completeness": {"source_total_known": source_total_known,
                         "source_complete": source_complete, "result_truncated": result_truncated,
                         "pages_scanned": pagination.get("pages_scanned", 1),
                         "records_scanned": records_scanned,
                         "incomplete_reason": incomplete_reason}},
            audit={"application": "twenty", "operation": name, "http_status": status,
                   "matched_count": matched, "returned_count": len(visible),
                   "source_total": source_total, "source_complete": source_complete,
                   "result_truncated": result_truncated, "records_scanned": records_scanned,
                   "scope": "current_delegated_workspace", "observed_at": observed_at,
                   "oauth_refreshed": refreshed, **self._identity_audit(context)},
            allowed_routes=(f"/objects/{resource}",) + tuple(
                item["ui_path"] for item in visible if item.get("ui_path")),
            query_answer=StructuredQueryAnswer(
                collection=resource, fields=fields,
                records=tuple((item[fields[0]], item[fields[1]]) for item in visible),
                source=f"Twenty delegated REST /rest/{resource}",
                scope="current_delegated_workspace", observed_at=observed_at,
                source_complete=source_complete, result_truncated=result_truncated,
                matched_count=matched,
                source_total=(source_total if source_total_known else None),
                records_scanned=records_scanned, incomplete_reason=incomplete_reason))

    def prepare_mutation(self, name: str, args: dict, *,
                         context: OperationContext | None = None
                         ) -> BusinessMutationSpec:
        if not self.allow_mutations or name != "create_company":
            raise BusinessToolError("unknown Twenty business mutation")
        normalized = self._normalize_company(args)
        self._assert_no_duplicate(normalized["name"], normalized["domain"])
        return BusinessMutationSpec(
            args=normalized,
            reconcile_key=normalized["name"].casefold(),
            effect="在 Twenty CRM 中创建 1 家公司",
            targets=(f"公司：{normalized['name']}", f"域名：{normalized['domain']}"),
        )

    def execute_mutation(self, name: str, args: dict, *,
                         context: OperationContext | None = None
                         ) -> BusinessToolResult:
        if not self.allow_mutations or name != "create_company":
            raise BusinessToolError("unknown Twenty business mutation")
        normalized = self._normalize_company(args)
        # Repeat under the delegated actor immediately before dispatch. This closes the
        # ordinary time-of-check gap without ever retrying the POST itself.
        self._assert_no_duplicate(normalized["name"], normalized["domain"])
        body = {
            "name": normalized["name"],
            "domainName": {
                "primaryLinkLabel": normalized["domain"],
                "primaryLinkUrl": "https://" + normalized["domain"],
                "secondaryLinks": [],
            },
        }
        token = self.tokens.access_token()
        payload, status = self._authorized_mutation_json(
            "/rest/companies", token, body)
        data = payload.get("data") if isinstance(payload, dict) else None
        created = data.get("createCompany") if isinstance(data, dict) else None
        record_id = created.get("id") if isinstance(created, dict) else None
        if not isinstance(record_id, str) or not _UUID.fullmatch(record_id):
            raise BusinessMutationIndeterminate(
                "Twenty accepted the mutation but returned no valid company ID",
                phase="acknowledgement")
        try:
            readback = self._authorized_json(f"/rest/companies/{record_id}", token)
        except BusinessToolError as exc:
            raise BusinessMutationIndeterminate(
                "Twenty company was created but same-user readback failed",
                phase="readback", record_id=record_id) from exc
        readback_data = readback.get("data") if isinstance(readback, dict) else None
        company = readback_data.get("company") if isinstance(readback_data, dict) else None
        if not isinstance(company, dict):
            raise BusinessMutationIndeterminate(
                "Twenty company was created but same-user readback was malformed",
                phase="readback", record_id=record_id)
        actual_name = company.get("name")
        domain_value = company.get("domainName")
        actual_domain = (domain_value.get("primaryLinkLabel")
                         if isinstance(domain_value, dict) else None)
        if actual_name != normalized["name"] or actual_domain != normalized["domain"]:
            raise BusinessMutationIndeterminate(
                "Twenty company was created but same-user readback did not match",
                phase="readback", record_id=record_id)
        ui_path = f"/object/company/{record_id}"
        observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return BusinessToolResult(
            content={
                "created": True,
                "readback_verified": True,
                "company": {
                    "id": record_id,
                    "name": actual_name,
                    "domain": actual_domain,
                    "ui_path": ui_path,
                },
                "scope": "current_delegated_workspace",
                "observed_at": observed_at,
            },
            audit={
                "application": "twenty",
                "operation": "create_company",
                "http_status": status,
                "records_created": 1,
                "readback_verified": True,
                "scope": "current_delegated_workspace",
                "observed_at": observed_at,
                "oauth_refreshed": False,
                **self._identity_audit(context),
            },
            allowed_routes=(ui_path,),
        )

    @staticmethod
    def _normalize_company(args: dict) -> dict:
        if not isinstance(args, dict) or set(args) != {"name", "domain"}:
            raise BusinessToolError("create_company requires only name and domain")
        name = normalize_business_text(args.get("name"), field="company name", maximum=200)
        domain = args.get("domain")
        if not isinstance(domain, str):
            raise BusinessToolError("company domain must be a string")
        domain = unicodedata.normalize("NFKC", domain).strip().rstrip(".").casefold()
        if not _DOMAIN.fullmatch(domain):
            raise BusinessToolError("company domain must be a bare DNS name")
        return {"name": name, "domain": domain}

    def _assert_no_duplicate(self, name: str, domain: str) -> None:
        payload, _status, _refreshed = self._get_companies()
        data = payload.get("data") if isinstance(payload, dict) else None
        rows = data.get("companies") if isinstance(data, dict) else None
        total = payload.get("totalCount") if isinstance(payload, dict) else None
        pagination = payload.get("_woaPagination") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or isinstance(total, bool) or not isinstance(total, int):
            raise BusinessToolError("Twenty duplicate check returned an unexpected shape")
        if ((isinstance(pagination, dict) and pagination.get("complete") is not True)
                or len(rows) != total):
            raise BusinessToolError("Twenty duplicate check was incomplete")
        for row in rows:
            if not isinstance(row, dict):
                continue
            existing_name = row.get("name")
            domain_value = row.get("domainName")
            existing_domain = ""
            if isinstance(domain_value, dict):
                existing_domain = str(domain_value.get("primaryLinkLabel")
                                      or domain_value.get("primaryLinkUrl") or "")
                existing_domain = re.sub(r"^https?://", "", existing_domain,
                                         flags=re.I).rstrip("/").casefold()
            # Both sides are folded the same way: a record this product did not create can
            # still carry a non-breaking space or a fullwidth letter, and it must not be
            # possible to add a second company that a person cannot tell apart from it.
            if (business_text_key(existing_name) == business_text_key(name)
                    or (existing_domain and existing_domain == domain)):
                raise BusinessToolError("a company with the same name or domain already exists")

    def _get_companies(self) -> tuple[dict, int, bool]:
        return self._get_collection("companies")

    def _get_collection(self, resource: str) -> tuple[dict, int, bool]:
        if resource not in {"companies", "people", "opportunities"}:
            raise ValueError("unsupported Twenty collection")
        token = self.tokens.access_token()
        refreshed = False
        rows: list[dict] = []
        seen_ids: set[str] = set()
        seen_cursors: set[str] = set()
        cursor = ""
        expected_total: int | None = None
        incomplete_reason = "page_limit_reached"
        pages_scanned = 0
        complete = False
        for _page_number in range(_MAX_COMPANY_PAGES):
            query = {"limit": _COMPANY_PAGE_SIZE}
            if cursor:
                query["starting_after"] = cursor
            path = f"/rest/{resource}?" + urlencode(query)
            try:
                page = self._authorized_json(path, token)
            except BusinessToolAuthenticationError:
                token = self.tokens.refresh(
                    self.base_url, self.timeout_s,
                    rejected_access_token=token)
                refreshed = True
                page = self._authorized_json(path, token)
            except (BusinessToolUnavailableError, BusinessToolContractError):
                if rows:
                    incomplete_reason = "upstream_read_failed"
                    break
                raise
            pages_scanned += 1
            data = page.get("data") if isinstance(page, dict) else None
            page_rows = data.get(resource) if isinstance(data, dict) else None
            total = page.get("totalCount") if isinstance(page, dict) else None
            if (not isinstance(page_rows, list) or isinstance(total, bool)
                    or not isinstance(total, int) or total < 0):
                if rows:
                    incomplete_reason = "unexpected_page_shape"
                    break
                raise BusinessToolContractError(
                    f"Twenty {resource} response has an unexpected shape")
            if expected_total is None:
                expected_total = total
            elif total != expected_total:
                incomplete_reason = "source_changed_during_scan"
                break
            duplicate_record = False
            for row in page_rows:
                if len(rows) >= _MAX_COMPANY_RECORDS:
                    incomplete_reason = "record_limit_reached"
                    break
                if isinstance(row, dict):
                    record_id = row.get("id")
                    if isinstance(record_id, str):
                        if record_id in seen_ids:
                            duplicate_record = True
                            incomplete_reason = "repeated_record"
                            break
                        seen_ids.add(record_id)
                    rows.append(row)
            if (len(rows) >= _MAX_COMPANY_RECORDS and len(rows) < expected_total) \
                    or duplicate_record:
                break
            if len(rows) == expected_total:
                complete = True
                incomplete_reason = ""
                break
            if len(rows) > expected_total:
                incomplete_reason = "source_count_mismatch"
                break
            page_info = page.get("pageInfo")
            if not isinstance(page_info, dict):
                incomplete_reason = "missing_page_info"
                break
            has_next = page_info.get("hasNextPage")
            next_cursor = page_info.get("endCursor")
            if has_next is not True:
                incomplete_reason = "upstream_ended_before_total"
                break
            if (not isinstance(next_cursor, str) or not next_cursor
                    or next_cursor == cursor or next_cursor in seen_cursors):
                incomplete_reason = "cursor_did_not_advance"
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        return ({
            "totalCount": expected_total if expected_total is not None else len(rows),
            "data": {resource: rows},
            "_woaPagination": {
                "complete": complete,
                "pages_scanned": pages_scanned,
                "records_scanned": len(rows),
                "incomplete_reason": incomplete_reason,
            },
        }, 200, refreshed)

    def _authorized_json(self, path: str, token: str) -> dict:
        request = Request(
            self.base_url + path,
            headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
        try:
            with urlopen(request, timeout=self.timeout_s) as response:
                status = response.status
                payload = json.load(response)
        except HTTPError as exc:
            if exc.code == 401:
                raise BusinessToolAuthenticationError(
                    "Twenty API authorization expired") from exc
            if exc.code == 403:
                # The token works and names the actor; Twenty's permissions refuse this read.
                raise BusinessToolPermissionError(
                    "Twenty does not permit the delegated user this read") from exc
            raise BusinessToolUnavailableError(
                f"Twenty API returned HTTP {exc.code}") from exc
        except (URLError, TimeoutError) as exc:
            raise BusinessToolUnavailableError("Twenty API read failed") from exc
        except json.JSONDecodeError as exc:
            raise BusinessToolContractError(
                "Twenty API returned an invalid response") from exc
        if status != 200 or not isinstance(payload, dict):
            raise BusinessToolContractError(
                "Twenty API returned an invalid response")
        return payload

    def _authorized_metadata_json(self, token: str) -> dict:
        request = Request(
            self.base_url + "/metadata",
            data=json.dumps({"query": _IDENTITY_QUERY}).encode("utf-8"),
            method="POST",
            headers={"Authorization": "Bearer " + token,
                     "Accept": "application/json", "Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=self.timeout_s) as response:
                status = response.status
                payload = json.load(response)
        except HTTPError as exc:
            if exc.code == 401:
                raise BusinessToolAuthenticationError(
                    "Twenty API authorization expired") from exc
            raise BusinessToolUnavailableError(
                f"Twenty Metadata API returned HTTP {exc.code}") from exc
        except (URLError, TimeoutError) as exc:
            raise BusinessToolUnavailableError(
                "Twenty Metadata identity read failed") from exc
        except json.JSONDecodeError as exc:
            raise BusinessToolContractError(
                "Twenty Metadata identity response is invalid") from exc
        if status != 200 or not isinstance(payload, dict):
            raise BusinessToolContractError(
                "Twenty Metadata identity response is invalid")
        errors = payload.get("errors")
        if isinstance(errors, list) and any(
                isinstance(error, dict)
                and isinstance(error.get("extensions"), dict)
                and error["extensions"].get("code") == "UNAUTHENTICATED"
                for error in errors):
            raise BusinessToolAuthenticationError(
                "Twenty API authorization expired")
        if errors:
            raise BusinessToolContractError(
                "Twenty Metadata identity response is invalid")
        return payload

    def _authorized_mutation_json(self, path: str, token: str,
                                  body: dict) -> tuple[dict, int]:
        request = Request(
            self.base_url + path, data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Authorization": "Bearer " + token,
                     "Accept": "application/json", "Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=self.timeout_s) as response:
                status = response.status
                payload = json.load(response)
        except HTTPError as exc:
            if exc.code == 403:
                # A refusal Twenty made before storing anything: definite, not uncertain.
                raise BusinessToolPermissionError(
                    "Twenty does not permit the delegated user this mutation") from exc
            if 400 <= exc.code < 500:
                raise BusinessToolError(
                    f"Twenty API refused the mutation with HTTP {exc.code}") from exc
            raise BusinessMutationIndeterminate(
                f"Twenty mutation returned HTTP {exc.code}; outcome is unknown") from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise BusinessMutationIndeterminate(
                "Twenty mutation acknowledgement was unavailable; outcome is unknown") from exc
        if status != 201 or not isinstance(payload, dict):
            raise BusinessMutationIndeterminate(
                "Twenty mutation returned an unexpected acknowledgement")
        return payload, status


class _TwentyIdentityPort:
    required = True

    def __init__(self, executor: TwentyBusinessToolExecutor,
                 required_scopes: frozenset[str]):
        self._executor = executor
        self.required_scopes = required_scopes

    def bind(self, page_identity: IdentityAssertion, *, delegation_id: str,
             lifetime_s: int = 300, now_ms: int | None = None) -> PrincipalContext:
        delegated = self._executor.resolve_delegated_identity(
            lifetime_s=lifetime_s, now_ms=now_ms)
        return bind_matching_identity(
            page_identity, delegated, delegation_id=delegation_id,
            scopes=tuple(sorted(self.required_scopes)), now_ms=now_ms)

    def validate(self, principal: PrincipalContext | None, *,
                 application_id: str) -> None:
        if principal is None:
            raise PrincipalError(
                "Twenty requires a verified application principal")
        principal.require_live(application_id=application_id)


class _TwentyMutationReconciler:
    def __init__(self, executor, enabled):
        self._executor = executor
        self.supported_tools = frozenset({"create_company"}) if enabled else frozenset()

    def reconcile(self, name, business_key, *, evidence, principal):
        if name not in self.supported_tools or principal is None:
            raise ValueError("reconciliation requires an enabled tool and principal")
        principal.require_live(application_id="twenty")
        if "companies:read" not in principal.scopes:
            raise PrincipalError("reconciliation requires companies:read scope")
        delegated = self._executor.resolve_delegated_identity()
        if any(getattr(principal, field) != getattr(delegated, field)
               for field in ("tenant_id", "workspace_id", "actor_id")):
            raise PrincipalError("reconciliation delegated identity changed")
        args = self._executor._normalize_company(business_key)
        payload, _status, _refreshed = self._executor._get_companies()
        rows = (payload.get("data") or {}).get("companies", [])
        complete = (payload.get("_woaPagination", {}).get("complete") is True
                    and payload.get("totalCount") == len(rows))
        matches = [row for row in rows if isinstance(row, dict)
                   and str(row.get("name", "")).strip().casefold() == args["name"].casefold()
                   and normalize_twenty_domain((row.get("domainName") or {}).get("primaryLinkUrl")
                       or (row.get("domainName") or {}).get("primaryLinkLabel")) == args["domain"]]
        # A matching row is not proof that this proposal created it. Absence also cannot
        # rule out an upstream write still in flight. Keep the journal unresolved.
        return MutationReconciliation(
            outcome="unknown", requires_human=True,
            reason=(f"Current matching records: {len(matches)}; complete scan: {complete}. "
                    "Establish the original writer's final outcome before resolving."))


class TwentyApplicationAdapter:
    """Application-owned facts and tools for the Twenty Companies integration."""

    def __init__(self, *, base_url: str, token_path: str | Path,
                 registration_path: str | Path,
                 skill_path: str | Path | None = None,
                 mutation_skill_path: str | Path | None = None,
                 allow_mutations: bool = False,
                 mutation_journal: MutationJournal | None = None):
        if not isinstance(allow_mutations, bool):
            raise ValueError("allow_mutations must be a boolean")
        if mutation_skill_path is not None and not allow_mutations:
            raise ValueError("mutation_skill_path requires allow_mutations=True")
        if allow_mutations and mutation_journal is None:
            raise ValueError(
                "allow_mutations=True requires an explicit mutation journal")
        self.origin = _validated_origin(base_url)
        self.page_controller_module = self.origin + "/woa/page-controller.js"
        executor = TwentyBusinessToolExecutor(
            base_url=self.origin,
            tokens=TwentyOAuthTokenStore(token_path, registration_path),
            allow_mutations=allow_mutations,
        )
        query_skill = (BusinessSkill.load(skill_path) if skill_path is not None
                       else _packaged_skill(_QUERY_SKILL_RESOURCE))
        skills = [query_skill, _packaged_skill(_PEOPLE_SKILL_RESOURCE),
                  _packaged_skill(_OPPORTUNITIES_SKILL_RESOURCE)]
        if allow_mutations:
            skills.append(
                BusinessSkill.load(mutation_skill_path)
                if mutation_skill_path is not None
                else _packaged_skill(_MUTATION_SKILL_RESOURCE))
        required_scopes = {"query_companies": "companies:read",
                           "query_people": "people:read",
                           "query_opportunities": "opportunities:read"}
        if allow_mutations:
            required_scopes["create_company"] = "companies:write"
        read_classes = {
            "query_companies": "business.company",
            "query_people": "business.person",
            "query_opportunities": "business.opportunity",
        }
        data = DeclaredDataClassifier(tuple(
            (name, ((read_classes[name], "business.identifier", "application.route")
                    if name in read_classes else
                    ("business.company", "business.identifier", "application.route",
                     "business.mutation_result")))
            for name in sorted(required_scopes)))
        postconditions = ResultPostconditionVerifier(
            (("create_company",
              ("content.readback_verified", "audit.readback_verified")),)
            if allow_mutations else ())
        routes = DeclaredRoutePort(
            application_id="twenty",
            tool_route_patterns=(
                r"/objects/(companies|people|opportunities)",
                r"/object/(company|person|opportunity)/[0-9a-f-]{36}"),
        )
        self.business_tools = BusinessToolRuntime(
            tuple(skills), executor, application_id="twenty",
            required_scopes=required_scopes,
            mutation_journal=mutation_journal,
            data_classifier=data,
            postcondition_verifier=postconditions,
            route_validator=routes)
        self._executor = executor
        identity = _TwentyIdentityPort(
            executor, frozenset(required_scopes.values()))
        self.ports = ApplicationPorts(
            identity=identity,
            routes=routes,
            postconditions=postconditions,
            reconciliation=_TwentyMutationReconciler(executor, allow_mutations),
            data=data,
        )
        self.descriptor = ApplicationDescriptor(
            schema_version=APPLICATION_ADAPTER_VERSION,
            application_id="twenty",
            display_name="Twenty CRM",
            origin=self.origin,
            navigation_mode="spa",
            page_controller_module=self.page_controller_module,
            catalog_sha256=self.business_tools.catalog_sha256,
        )

    def health(self) -> dict:
        try:
            with urlopen(self.origin + "/healthz", timeout=5) as response:
                payload = json.load(response)
                return {"passed": response.status == 200 and payload.get("status") == "ok",
                        "http_status": response.status}
        except Exception:
            return {"passed": False, "http_status": 0}

    def bind_principal(self, page_identity: IdentityAssertion, *,
                       delegation_id: str, lifetime_s: int = 300,
                       now_ms: int | None = None) -> PrincipalContext:
        """Match a trusted Twenty page-session assertion to the delegated OAuth actor.

        The application host must obtain ``page_identity`` from Twenty's authenticated
        server session. DOM text or a browser-provided user id is not trusted evidence.
        """
        return self.ports.identity.bind(
            page_identity, delegation_id=delegation_id,
            lifetime_s=lifetime_s, now_ms=now_ms)

    def bind_browser_session(self, storage_state_path: str | Path, *,
                             delegation_id: str, lifetime_s: int = 300,
                             now_ms: int | None = None) -> PrincipalContext:
        """Bind the exact browser storage state to the delegated OAuth identity."""
        page = TwentyPageSessionIdentityResolver(
            base_url=self.origin,
            storage_state_path=storage_state_path,
            timeout_s=self._executor.timeout_s,
        ).resolve(lifetime_s=lifetime_s, now_ms=now_ms)
        return self.bind_principal(
            page, delegation_id=delegation_id,
            lifetime_s=lifetime_s, now_ms=now_ms)

    def page_session_binding(self, storage_state: str | Path | dict, *,
                             now_ms: int | None = None) -> str:
        """Hash the active Twenty session cookie without exposing it to the Agent."""
        source = ({"storage_state": storage_state}
                  if isinstance(storage_state, dict)
                  else {"storage_state_path": storage_state})
        return TwentyPageSessionIdentityResolver(
            base_url=self.origin,
            timeout_s=self._executor.timeout_s,
            **source,
        ).binding_digest(now_ms=now_ms)
