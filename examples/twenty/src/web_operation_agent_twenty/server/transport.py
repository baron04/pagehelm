"""Bounded HTTP transport and paginated collection reads for Twenty."""
from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from web_operation_agent.business.runtime import (BusinessMutationIndeterminate,
                                                  BusinessToolAuthenticationError,
                                                  BusinessToolContractError,
                                                  BusinessToolError,
                                                  BusinessToolPermissionError,
                                                  BusinessToolUnavailableError)

from .specs import (_COMPANY_PAGE_SIZE, _IDENTITY_QUERY, _MAX_COMPANY_PAGES,
                    _MAX_COMPANY_RECORDS)


class TwentyApiClient:
    """HTTP behavior shared by the typed executor without exposing a generic client API."""

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
