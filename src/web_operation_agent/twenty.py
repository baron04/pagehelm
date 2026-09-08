"""Narrow Twenty v2.39.0 adapter for company data and record handoff.

Application credentials remain in ignored 0600 files.  The adapter projects company
name/domain fields plus bounded record routes, and its audit record never contains bearer
tokens, browser cookies, or raw result rows.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from web_operation_agent.business import (BusinessMutationIndeterminate,
                                          BusinessMutationSpec, BusinessSkill,
                                          BusinessToolError, BusinessToolResult,
                                          BusinessToolRuntime)
from web_operation_agent.application import (APPLICATION_ADAPTER_VERSION,
                                             ApplicationDescriptor)
from web_operation_agent.identity import (IdentityAssertion, OperationContext,
                                          PrincipalContext, bind_matching_identity)
from web_operation_agent.mutation import MutationJournal


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
_SKILL_RESOURCE_ROOT = ("skills", "twenty")
_QUERY_SKILL_RESOURCE = "companies-query.json"
_MUTATION_SKILL_RESOURCE = "companies-create.json"
_IDENTITY_QUERY = "query WoaIdentity { currentUser { id } currentWorkspace { id } }"


def _packaged_skill(filename: str) -> BusinessSkill:
    artifact = resources.files("web_operation_agent")
    for part in (*_SKILL_RESOURCE_ROOT, filename):
        artifact = artifact.joinpath(part)
    artifact_uri = "package://web_operation_agent/" + "/".join(
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


class TwentyOAuthTokenStore:
    def __init__(self, token_path: str | Path, registration_path: str | Path):
        self.token_path = Path(token_path)
        self.registration_path = Path(registration_path)

    @staticmethod
    def _read(path: Path) -> dict:
        try:
            value = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise BusinessToolError("Twenty OAuth state is unavailable") from exc
        if not isinstance(value, dict):
            raise BusinessToolError("Twenty OAuth state is invalid")
        return value

    def access_token(self) -> str:
        value = self._read(self.token_path).get("access_token")
        if not isinstance(value, str) or not value:
            raise BusinessToolError("Twenty OAuth access token is unavailable")
        return value

    def refresh(self, base_url: str, timeout_s: float) -> str:
        tokens = self._read(self.token_path)
        registration = self._read(self.registration_path)
        refresh = tokens.get("refresh_token")
        client_id = registration.get("client_id")
        if not isinstance(refresh, str) or not refresh or not isinstance(client_id, str) \
                or not client_id:
            raise BusinessToolError("Twenty OAuth refresh state is unavailable")
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
            raise BusinessToolError("Twenty OAuth refresh failed") from exc
        if not isinstance(updated, dict) or not isinstance(updated.get("access_token"), str):
            raise BusinessToolError("Twenty OAuth refresh returned no access token")
        if not updated.get("refresh_token"):
            updated["refresh_token"] = refresh
        updated["obtained_at"] = int(time.time())
        temporary = self.token_path.with_name(f".{self.token_path.name}.{os.getpid()}.tmp")
        try:
            with open(temporary, "x", encoding="utf-8") as handle:
                json.dump(updated, handle, ensure_ascii=False, sort_keys=True, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.token_path)
        finally:
            temporary.unlink(missing_ok=True)
        return updated["access_token"]


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
        return ((QUERY_COMPANIES_SCHEMA, CREATE_COMPANY_SCHEMA)
                if self.allow_mutations else (QUERY_COMPANIES_SCHEMA,))

    def tool_effects(self) -> dict[str, str]:
        effects = {"query_companies": "read"}
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
        except BusinessToolError as exc:
            if str(exc) != "Twenty API authorization expired":
                raise
            token = self.tokens.refresh(self.base_url, self.timeout_s)
            payload = self._authorized_metadata_json(token)
        data = payload.get("data") if isinstance(payload, dict) else None
        user = data.get("currentUser") if isinstance(data, dict) else None
        workspace = data.get("currentWorkspace") if isinstance(data, dict) else None
        actor_id = user.get("id") if isinstance(user, dict) else None
        workspace_id = workspace.get("id") if isinstance(workspace, dict) else None
        if not isinstance(actor_id, str) or not isinstance(workspace_id, str):
            raise BusinessToolError("Twenty Metadata identity response is incomplete")
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
        if name != "query_companies":
            raise BusinessToolError("unknown Twenty business tool")
        unknown = set(args) - {"name_contains", "limit"}
        if unknown:
            raise BusinessToolError("query_companies received unknown arguments")
        needle = args.get("name_contains", "")
        if not isinstance(needle, str) or len(needle) > 100 \
                or re.search(r"[\x00-\x1f\x7f]", needle):
            raise BusinessToolError("name_contains must be a string of at most 100 characters")
        limit = args.get("limit", 20)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise BusinessToolError("limit must be an integer from 1 to 50")
        payload, status, refreshed = self._get_companies()
        data = payload.get("data") if isinstance(payload, dict) else None
        rows = data.get("companies") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise BusinessToolError("Twenty companies response has an unexpected shape")
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
        allowed_routes = tuple(
            row["ui_path"] for row in visible if isinstance(row.get("ui_path"), str))
        raw_source_total = payload.get("totalCount")
        source_total_known = (not isinstance(raw_source_total, bool)
                              and isinstance(raw_source_total, int))
        source_total = raw_source_total
        if not source_total_known:
            source_total = len(rows)
        source_complete = source_total_known and len(rows) == source_total
        result_truncated = matched > limit or not source_complete
        observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return BusinessToolResult(
            content={
                "companies": visible,
                "matched_count": matched,
                "source_total": source_total,
                "scope": "current_delegated_workspace",
                "observed_at": observed_at,
                "completeness": {
                    "source_total_known": source_total_known,
                    "source_complete": source_complete,
                    "result_truncated": result_truncated,
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
                "scope": "current_delegated_workspace",
                "observed_at": observed_at,
                "oauth_refreshed": refreshed,
                **self._identity_audit(context),
            },
            allowed_routes=allowed_routes,
        )

    def prepare_mutation(self, name: str, args: dict, *,
                         context: OperationContext | None = None
                         ) -> BusinessMutationSpec:
        if not self.allow_mutations or name != "create_company":
            raise BusinessToolError("unknown Twenty business mutation")
        normalized = self._normalize_company(args)
        self._assert_no_duplicate(normalized["name"], normalized["domain"])
        return BusinessMutationSpec(
            args=normalized,
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
                "Twenty accepted the mutation but returned no valid company ID")
        try:
            readback = self._authorized_json(f"/rest/companies/{record_id}", token)
        except BusinessToolError as exc:
            raise BusinessMutationIndeterminate(
                "Twenty company was created but same-user readback failed") from exc
        readback_data = readback.get("data") if isinstance(readback, dict) else None
        company = readback_data.get("company") if isinstance(readback_data, dict) else None
        if not isinstance(company, dict):
            raise BusinessMutationIndeterminate(
                "Twenty company was created but same-user readback was malformed")
        actual_name = company.get("name")
        domain_value = company.get("domainName")
        actual_domain = (domain_value.get("primaryLinkLabel")
                         if isinstance(domain_value, dict) else None)
        if actual_name != normalized["name"] or actual_domain != normalized["domain"]:
            raise BusinessMutationIndeterminate(
                "Twenty company was created but same-user readback did not match")
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
        name = args.get("name")
        domain = args.get("domain")
        if (not isinstance(name, str) or not name.strip() or len(name.strip()) > 200
                or re.search(r"[\x00-\x1f\x7f]", name)):
            raise BusinessToolError("company name must be 1 to 200 visible characters")
        if not isinstance(domain, str):
            raise BusinessToolError("company domain must be a string")
        domain = domain.strip().rstrip(".").casefold()
        if not _DOMAIN.fullmatch(domain):
            raise BusinessToolError("company domain must be a bare DNS name")
        return {"name": name.strip(), "domain": domain}

    def _assert_no_duplicate(self, name: str, domain: str) -> None:
        payload, _status, _refreshed = self._get_companies()
        data = payload.get("data") if isinstance(payload, dict) else None
        rows = data.get("companies") if isinstance(data, dict) else None
        total = payload.get("totalCount") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or isinstance(total, bool) or not isinstance(total, int):
            raise BusinessToolError("Twenty duplicate check returned an unexpected shape")
        if len(rows) != total:
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
            if ((isinstance(existing_name, str)
                 and existing_name.strip().casefold() == name.casefold())
                    or existing_domain == domain):
                raise BusinessToolError("a company with the same name or domain already exists")

    def _get_companies(self) -> tuple[dict, int, bool]:
        token = self.tokens.access_token()
        try:
            return self._authorized_json("/rest/companies", token), 200, False
        except BusinessToolError as exc:
            if str(exc) != "Twenty API authorization expired":
                raise
        token = self.tokens.refresh(self.base_url, self.timeout_s)
        return self._authorized_json("/rest/companies", token), 200, True

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
                raise BusinessToolError("Twenty API authorization expired") from exc
            raise BusinessToolError(f"Twenty API returned HTTP {exc.code}") from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise BusinessToolError("Twenty API read failed") from exc
        if status != 200 or not isinstance(payload, dict):
            raise BusinessToolError("Twenty API returned an invalid response")
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
                raise BusinessToolError("Twenty API authorization expired") from exc
            raise BusinessToolError(
                f"Twenty Metadata API returned HTTP {exc.code}") from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise BusinessToolError("Twenty Metadata identity read failed") from exc
        if (status != 200 or not isinstance(payload, dict)
                or payload.get("errors")):
            raise BusinessToolError("Twenty Metadata identity response is invalid")
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
        self.origin = _validated_origin(base_url)
        self.page_controller_module = self.origin + "/woa/page-controller.js"
        executor = TwentyBusinessToolExecutor(
            base_url=self.origin,
            tokens=TwentyOAuthTokenStore(token_path, registration_path),
            allow_mutations=allow_mutations,
        )
        query_skill = (BusinessSkill.load(skill_path) if skill_path is not None
                       else _packaged_skill(_QUERY_SKILL_RESOURCE))
        skills = [query_skill]
        if allow_mutations:
            skills.append(
                BusinessSkill.load(mutation_skill_path)
                if mutation_skill_path is not None
                else _packaged_skill(_MUTATION_SKILL_RESOURCE))
        required_scopes = {"query_companies": "companies:read"}
        if allow_mutations:
            required_scopes["create_company"] = "companies:write"
        self.business_tools = BusinessToolRuntime(
            tuple(skills), executor, application_id="twenty",
            required_scopes=required_scopes,
            mutation_journal=mutation_journal)
        self._executor = executor
        self._principal_scopes = tuple(sorted(required_scopes.values()))
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
        delegated = self._executor.resolve_delegated_identity(
            lifetime_s=lifetime_s, now_ms=now_ms)
        return bind_matching_identity(
            page_identity, delegated, delegation_id=delegation_id,
            scopes=self._principal_scopes, now_ms=now_ms)
