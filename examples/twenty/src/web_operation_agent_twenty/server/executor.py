"""Typed Twenty reads and fail-closed mutation dispatch."""
from __future__ import annotations

import json
import re
import time
import unicodedata
from datetime import datetime, timezone
from http.client import HTTPConnection, HTTPSConnection, HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from web_operation_agent.answer.answering import StructuredQueryAnswer
from web_operation_agent.business.runtime import (BusinessMutationIndeterminate,
                                                  BusinessMutationSpec,
                                                  BusinessToolAuthenticationError,
                                                  BusinessToolContractError,
                                                  BusinessToolError,
                                                  BusinessToolIdentityError,
                                                  BusinessToolPermissionError,
                                                  BusinessToolRequestError,
                                                  BusinessToolResult,
                                                  BusinessToolUnavailableError,
                                                  business_text_key,
                                                  normalize_business_text)
from web_operation_agent.state.identity import IdentityAssertion, OperationContext

from .identity import TwentyOAuthTokenStore
from .specs import (_COMPANY_PAGE_SIZE, _DOMAIN, _IDENTITY_QUERY,
                    _MAX_COMPANY_PAGES, _MAX_COMPANY_RECORDS, _MUTATION_BY_TOOL,
                    _READ_BY_TOOL, _UUID, MUTATION_SPECS, READ_SPECS,
                    _validated_origin, normalize_twenty_domain)
from .transport import TwentyApiClient


class TwentyBusinessToolExecutor(TwentyApiClient):
    def __init__(self, *, base_url: str, tokens: TwentyOAuthTokenStore,
                 timeout_s: float = 15, allow_mutations: bool = False):
        if not isinstance(allow_mutations, bool):
            raise ValueError("allow_mutations must be a boolean")
        self.base_url = _validated_origin(base_url)
        self.tokens = tokens
        self.timeout_s = timeout_s
        self.allow_mutations = allow_mutations

    def tool_schemas(self) -> tuple[dict, ...]:
        return tuple(spec.schema for spec in READ_SPECS) + (
            tuple(spec.schema for spec in MUTATION_SPECS) if self.allow_mutations else ())

    def tool_effects(self) -> dict[str, str]:
        effects = {spec.name: spec.effect for spec in READ_SPECS}
        if self.allow_mutations:
            effects.update({spec.name: spec.effect for spec in MUTATION_SPECS})
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
        spec = _READ_BY_TOOL.get(name)
        if spec is None:
            raise BusinessToolRequestError("unknown Twenty business tool")
        return getattr(self, spec.execute_method)(args, context=context)

    def _execute_companies_query(self, args: dict, *,
                                 context: OperationContext | None) -> BusinessToolResult:
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

    def _execute_people_query(self, args: dict, *,
                              context: OperationContext | None) -> BusinessToolResult:
        return self._execute_related_query("query_people", args, context=context)

    def _execute_opportunities_query(self, args: dict, *,
                                     context: OperationContext | None) -> BusinessToolResult:
        return self._execute_related_query("query_opportunities", args, context=context)

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
        spec = _MUTATION_BY_TOOL.get(name)
        if not self.allow_mutations or spec is None:
            raise BusinessToolError("unknown Twenty business mutation")
        return self._prepare_create_company(args, context=context)

    def _prepare_create_company(self, args: dict, *,
                                context: OperationContext | None) -> BusinessMutationSpec:
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
        spec = _MUTATION_BY_TOOL.get(name)
        if not self.allow_mutations or spec is None:
            raise BusinessToolError("unknown Twenty business mutation")
        return getattr(self, spec.execute_method)(args, context=context)

    def _dispatch_create_company(self, args: dict, *,
                                 context: OperationContext | None) -> BusinessToolResult:
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
