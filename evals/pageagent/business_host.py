"""Principal-bound typed-tool boundary for the PageAgent Skill/API baseline."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import threading
from typing import Any

from web_operation_agent.business import BusinessToolError, BusinessToolRuntime
from web_operation_agent.identity import OperationContext


_SAFE_TOKEN = re.compile(r"[^\x00-\x1f\x7f]{16,128}\Z")


class PageAgentBusinessHostError(RuntimeError):
    """A content-free business-host failure safe to expose to the browser bridge."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _bridge_tool(schema: dict[str, Any]) -> dict[str, Any]:
    function = schema.get("function") if isinstance(schema, dict) else None
    parameters = function.get("parameters") if isinstance(function, dict) else None
    if (schema.get("type") != "function" or not isinstance(function, dict)
            or not isinstance(parameters, dict)):
        raise ValueError("business tool schema is invalid")
    input_schema = dict(parameters)
    input_schema.setdefault("required", [])
    return {
        "name": function.get("name"),
        "description": function.get("description"),
        "input_schema": input_schema,
    }


class PageAgentBusinessProxy:
    """Expose selected read-only Skills through one principal-bound baseline run."""

    def __init__(self, *, run_token: str, runtime: BusinessToolRuntime,
                 loaded_skill_ids: tuple[str, ...], context: OperationContext,
                 expected_catalog_sha256: str, max_business_calls: int,
                 cancelled: threading.Event | None = None):
        if not isinstance(run_token, str) or not _SAFE_TOKEN.fullmatch(run_token):
            raise ValueError("run_token is invalid")
        if not isinstance(runtime, BusinessToolRuntime):
            raise TypeError("runtime must be BusinessToolRuntime")
        if not isinstance(context, OperationContext):
            raise TypeError("context must be OperationContext")
        if (not isinstance(expected_catalog_sha256, str)
                or not re.fullmatch(r"[0-9a-f]{64}", expected_catalog_sha256)
                or runtime.catalog_sha256 != expected_catalog_sha256):
            raise ValueError("business tool catalog does not match the frozen digest")
        if (not isinstance(loaded_skill_ids, tuple) or not loaded_skill_ids
                or len(loaded_skill_ids) != len(set(loaded_skill_ids))):
            raise ValueError("loaded_skill_ids must be a non-empty unique tuple")
        if (isinstance(max_business_calls, bool) or not isinstance(max_business_calls, int)
                or not 1 <= max_business_calls <= 200):
            raise ValueError("max_business_calls is invalid")
        if cancelled is not None and not isinstance(cancelled, threading.Event):
            raise TypeError("cancelled must be a threading.Event or None")
        selected_ids = set(loaded_skill_ids)
        skills = []
        for skill_id in sorted(selected_ids):
            skill, reason = runtime.load(skill_id, selected_ids)
            if skill is None:
                raise ValueError(f"business Skill is unavailable: {reason}")
            skills.append(skill)
        schemas = runtime.schemas_for(selected_ids)
        if not schemas:
            raise ValueError("selected Skills expose no tools")
        if any(runtime.is_mutation(schema["function"]["name"]) for schema in schemas):
            raise ValueError("PageAgent baseline business tools must be read-only")
        cards = [skill.loaded_card() for skill in skills]
        instructions = "APP SKILLS\n" + json.dumps(
            cards, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(instructions.encode("utf-8")) > 100_000:
            raise ValueError("PageAgent Skill instructions are oversized")
        self._run_token = run_token
        self.runtime = runtime
        self.loaded_skill_ids = frozenset(selected_ids)
        self.context = context
        self.max_business_calls = max_business_calls
        self.cancelled = cancelled or threading.Event()
        self._tool_specs = tuple(_bridge_tool(schema) for schema in schemas)
        self._tool_names = frozenset(item["name"] for item in self._tool_specs)
        self.instructions = instructions
        self.skill_digest = _digest(cards)
        self.tool_catalog_digest = "sha256:" + runtime.catalog_sha256
        self._lock = threading.Lock()
        self._attempts = 0
        self._completed = 0
        self._allowed_routes: set[str] = set()
        self._last_error_code = ""

    def handle(self, payload: Any) -> dict[str, Any]:
        if self.cancelled.is_set():
            raise PageAgentBusinessHostError("cancelled")
        if not isinstance(payload, dict) or set(payload) != {"run_token", "name", "args"}:
            raise PageAgentBusinessHostError("tool_contract")
        token = payload.get("run_token")
        if not isinstance(token, str) or not hmac.compare_digest(token, self._run_token):
            raise PageAgentBusinessHostError("tool_contract")
        name = payload.get("name")
        if name not in self._tool_names:
            raise PageAgentBusinessHostError("tool_contract")
        with self._lock:
            if self._attempts >= self.max_business_calls:
                raise PageAgentBusinessHostError("business_call_budget")
            self._attempts += 1
        try:
            result = self.runtime.execute(
                name, payload.get("args"), self.loaded_skill_ids,
                context=self.context)
            if self.cancelled.is_set():
                raise PageAgentBusinessHostError("cancelled")
        except PageAgentBusinessHostError as exc:
            self._last_error_code = exc.code
            raise
        except BusinessToolError:
            self._last_error_code = "business_tool"
            raise PageAgentBusinessHostError("business_tool") from None
        except Exception:
            self._last_error_code = "business_tool"
            raise PageAgentBusinessHostError("business_tool") from None
        with self._lock:
            self._completed += 1
            self._allowed_routes.update(result.allowed_routes)
        return result.content

    @property
    def tool_specs(self) -> tuple[dict[str, Any], ...]:
        return tuple(json.loads(json.dumps(item)) for item in self._tool_specs)

    @property
    def allowed_routes(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._allowed_routes)

    def report(self) -> dict[str, Any]:
        with self._lock:
            return {
                "business_attempts": self._attempts,
                "business_responses": self._completed,
                "allowed_route_count": len(self._allowed_routes),
                "last_error_code": self._last_error_code or None,
                "skill_digest": self.skill_digest,
                "tool_catalog_digest": self.tool_catalog_digest,
            }
