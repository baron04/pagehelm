"""Reusable same-actor JSON HTTP client with write transport fault classification."""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Mapping
import urllib.error
import urllib.parse
import urllib.request

from web_operation_agent.business.dispatch_evidence import mutation_transport
from web_operation_agent.business.errors import (
    BusinessMutationIndeterminate,
    BusinessToolAuthenticationError,
    BusinessToolError,
    BusinessToolPermissionError,
)
from web_operation_agent.business.write_pipeline import DEFAULT_SECRET_FIELD_PATTERN


def valid_record_id(rid: Any) -> bool:
    if isinstance(rid, int) and not isinstance(rid, bool):
        return rid >= 0
    return isinstance(rid, str) and bool(re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", rid))


def strip_secret_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: strip_secret_fields(v)
            for k, v in value.items()
            if not DEFAULT_SECRET_FIELD_PATTERN.search(str(k))
        }
    if isinstance(value, list):
        return [strip_secret_fields(item) for item in value]
    return value


class DeclarativeRestClient:
    """Reusable same-actor JSON HTTP client with write transport fault classification."""

    def __init__(
        self,
        origin: str,
        *,
        api_token: str = "",
        headers_factory: Callable[[], Mapping[str, str]] | None = None,
        timeout_s: float = 3.0,
    ):
        self.origin = origin.rstrip("/")
        self._api_token = api_token
        self._headers_factory = headers_factory
        self.timeout_s = timeout_s

    def _headers(self) -> dict[str, str]:
        if self._headers_factory is not None:
            return dict(self._headers_factory())
        return {"Authorization": f"Bearer {self._api_token}"} if self._api_token else {}

    def get_json(self, path: str, **params) -> dict:
        if params:
            path += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(self.origin + path, headers=self._headers())
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise BusinessToolAuthenticationError("the delegated credential was refused") from exc
            if exc.code == 403:
                raise BusinessToolPermissionError("the application denied this actor read access") from exc
            raise BusinessToolError("the application API is unavailable") from exc
        except (OSError, ValueError, urllib.error.URLError) as exc:
            raise BusinessToolError("the application API is unavailable") from exc
        if not isinstance(payload, dict):
            raise BusinessToolError("the application API returned an invalid document")
        return payload

    def get_collection(
        self,
        path: str,
        *,
        collection_key: str = "",
        id_field: str = "id",
        required_keys: tuple[str, ...] = (),
        max_rows: int = 10_000,
    ) -> list[dict]:
        payload = self.get_json(path)
        source = payload.get(collection_key) if collection_key else payload
        if not isinstance(source, list):
            raise BusinessToolError("the application API returned an invalid collection")
        if len(source) > max_rows:
            raise BusinessToolError("the application API returned too many records")
        rows: list[dict] = []
        seen: set[str] = set()
        expected = set(required_keys) | {id_field} if required_keys else {id_field}
        for raw in source:
            if not isinstance(raw, dict):
                raise BusinessToolError("the application API returned an invalid record")
            if not (expected <= set(raw)):
                raise BusinessToolError("the application API returned an incomplete record")
            rid = raw.get(id_field)
            if not valid_record_id(rid) or str(rid) in seen:
                raise BusinessToolError("the application API returned an invalid record identity")
            seen.add(str(rid))
            rows.append(strip_secret_fields(raw))
        return rows

    def get_record(self, path: str, *, detail_key: str = "") -> dict | None:
        request = urllib.request.Request(self.origin + path, headers=self._headers())
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as resp:
                payload = json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            if exc.code in (401, 403):
                raise BusinessToolPermissionError("readback denied") from exc
            raise BusinessToolError(f"read failed: HTTP {exc.code}") from exc
        except (OSError, ValueError, urllib.error.URLError) as exc:
            raise BusinessToolError("the application API is unavailable") from exc
        doc = payload.get(detail_key) if (detail_key and isinstance(payload, dict)) else payload
        return strip_secret_fields(doc) if isinstance(doc, dict) else None

    def put_once(self, path: str, payload: dict) -> dict:
        request = urllib.request.Request(
            self.origin + path,
            data=json.dumps(payload).encode("utf-8"),
            method="PUT",
            headers={**self._headers(), "Content-Type": "application/json"},
        )
        with mutation_transport() as transport:
            try:
                with urllib.request.urlopen(request, timeout=max(self.timeout_s, 5.0)) as resp:
                    if resp.status != 200:
                        raise BusinessMutationIndeterminate("unexpected status", phase="dispatch")
                    parsed = json.load(resp)
                    transport.applied()
                    return parsed
            except urllib.error.HTTPError as exc:
                if exc.code == 401:
                    transport.rejected()
                    raise BusinessToolAuthenticationError("the delegated credential was refused") from exc
                if exc.code == 403:
                    transport.rejected()
                    raise BusinessToolPermissionError("application denied write") from exc
                raise BusinessMutationIndeterminate("HTTP error during PUT", phase="dispatch") from exc
            except (OSError, ValueError, urllib.error.URLError) as exc:
                raise BusinessMutationIndeterminate("transport error during PUT", phase="dispatch") from exc

    def post_mutation(
        self,
        path: str,
        payload: dict,
        *,
        record_key: str = "",
        id_field: str = "id",
        conflict_message: str = "a conflicting record already exists",
        forbidden_message: str = "the application does not allow this actor to create records",
    ) -> dict:
        request = urllib.request.Request(
            self.origin + path,
            json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={**self._headers(), "Content-Type": "application/json"},
        )
        with mutation_transport() as transport:
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                    result = json.load(response)
                    record = result.get(record_key) if (record_key and isinstance(result, dict)) else result
                    if not isinstance(record, dict) or not valid_record_id(record.get(id_field)):
                        raise BusinessMutationIndeterminate(
                            "mutation returned no acknowledgement", phase="acknowledgement"
                        )
                    transport.applied()
                    return result
            except urllib.error.HTTPError as exc:
                if exc.code == 409:
                    transport.rejected()
                    raise BusinessToolError(conflict_message) from exc
                if exc.code == 401:
                    transport.rejected()
                    raise BusinessToolAuthenticationError("the delegated credential was refused") from exc
                if exc.code == 403:
                    transport.rejected()
                    raise BusinessToolPermissionError(forbidden_message) from exc
                raise BusinessMutationIndeterminate("mutation returned no safe outcome", phase="dispatch") from exc
            except (OSError, ValueError, urllib.error.URLError) as exc:
                raise BusinessMutationIndeterminate("the application API did not report an outcome", phase="dispatch") from exc


__all__ = ["DeclarativeRestClient", "strip_secret_fields", "valid_record_id"]
