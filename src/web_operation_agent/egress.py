"""Deterministic data boundary for requests sent to a model provider."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .privacy import (has_unredacted_sensitive_assignment,
                      has_unredacted_session_handoff)


class ProviderEgressError(ValueError):
    """A provider request violates the configured data boundary."""


@dataclass(frozen=True, slots=True)
class ProviderEgressReport:
    schema_version: int
    input_digest: str
    input_bytes: int
    message_count: int
    tool_count: int
    roles: tuple[tuple[str, int], ...]
    data_classes: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ProviderEgressError("provider egress report version is unsupported")
        if not isinstance(self.input_digest, str) or len(self.input_digest) != 64:
            raise ProviderEgressError("provider input digest is invalid")
        for name in ("input_bytes", "message_count", "tool_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ProviderEgressError(f"{name} must be a non-negative integer")

    def audit_projection(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "input_digest": self.input_digest,
            "input_bytes": self.input_bytes,
            "message_count": self.message_count,
            "tool_count": self.tool_count,
            "roles": dict(self.roles),
            "data_classes": list(self.data_classes),
            "contains_raw_payload": False,
        }


class ProviderEgressPolicy:
    """Bound and classify model inputs while rejecting known credential forms.

    Business and page data are intentionally allowed: they are required for the Agent to
    operate. The report records their class and digest without copying their values into the
    audit ledger. Application hosts remain responsible for deciding which business fields a
    configured provider is permitted to receive.
    """

    def __init__(self, *, max_request_bytes: int = 2 * 1024 * 1024,
                 max_messages: int = 256, max_tools: int = 128):
        limits = {
            "max_request_bytes": (max_request_bytes, 1_024, 16 * 1024 * 1024),
            "max_messages": (max_messages, 1, 2_048),
            "max_tools": (max_tools, 0, 1_024),
        }
        for name, (value, minimum, maximum) in limits.items():
            if (isinstance(value, bool) or not isinstance(value, int)
                    or not minimum <= value <= maximum):
                raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
        self.max_request_bytes = max_request_bytes
        self.max_messages = max_messages
        self.max_tools = max_tools

    def inspect(self, messages: list[dict], tools: list[dict] | None) -> ProviderEgressReport:
        if not isinstance(messages, list) or not messages:
            raise ProviderEgressError("provider messages must be a non-empty array")
        if len(messages) > self.max_messages:
            raise ProviderEgressError("provider message count exceeds the egress limit")
        selected_tools = [] if tools is None else tools
        if not isinstance(selected_tools, list):
            raise ProviderEgressError("provider tools must be an array or null")
        if len(selected_tools) > self.max_tools:
            raise ProviderEgressError("provider tool count exceeds the egress limit")
        if any(not isinstance(tool, dict) for tool in selected_tools):
            raise ProviderEgressError("every provider tool must be an object")
        if (has_unredacted_session_handoff(messages, url_decode_depth=3)
                or has_unredacted_session_handoff(selected_tools, url_decode_depth=3)
                or has_unredacted_sensitive_assignment(messages, url_decode_depth=3)
                or has_unredacted_sensitive_assignment(selected_tools, url_decode_depth=3)):
            raise ProviderEgressError(
                "provider request contains an unredacted credential value")
        try:
            encoded = json.dumps(
                {"messages": messages, "tools": selected_tools},
                sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, OverflowError) as exc:
            raise ProviderEgressError("provider request must be finite JSON") from exc
        if len(encoded) > self.max_request_bytes:
            raise ProviderEgressError("provider request exceeds the egress byte limit")

        role_counts: dict[str, int] = {}
        classes = {"agent_instructions"}
        for message in messages:
            if not isinstance(message, dict):
                raise ProviderEgressError("every provider message must be an object")
            role = message.get("role")
            if role not in {"system", "user", "assistant", "tool"}:
                raise ProviderEgressError("provider message role is unsupported")
            role_counts[role] = role_counts.get(role, 0) + 1
            content = message.get("content", "")
            if content is None:
                tool_calls = message.get("tool_calls")
                if role != "assistant" or not isinstance(tool_calls, list) or not tool_calls:
                    raise ProviderEgressError(
                        "null provider message content requires assistant tool calls")
                content = ""
            if not isinstance(content, str):
                raise ProviderEgressError("provider message content must be a string")
            if role == "user":
                classes.add("user_intent")
            if role == "tool":
                classes.add("application_tool_result")
            if "BEGIN_UNTRUSTED_PAGE_OBSERVATION" in content:
                classes.add("application_page_observation")
            if "BEGIN_UNTRUSTED_SOURCE_EVIDENCE" in content:
                classes.add("application_source_evidence")
            if "PRIOR CONVERSATION CONTEXT" in content:
                classes.add("application_conversation_history")
            if any(marker in content for marker in (
                    "APP SKILLS", "BUSINESS SKILL", "SOURCE AFFORDANCES",
                    "PLAN STATUS")):
                classes.add("application_knowledge")
        return ProviderEgressReport(
            schema_version=1,
            input_digest=hashlib.sha256(encoded).hexdigest(),
            input_bytes=len(encoded),
            message_count=len(messages),
            tool_count=len(selected_tools),
            roles=tuple(sorted(role_counts.items())),
            data_classes=tuple(sorted(classes)),
        )

    def inspect_completion_body(self, body: dict) -> ProviderEgressReport:
        """Inspect a caller-serialized completion without trusting its extra fields."""
        if not isinstance(body, dict):
            raise ProviderEgressError("provider request body must be an object")
        if (has_unredacted_session_handoff(body, url_decode_depth=3)
                or has_unredacted_sensitive_assignment(body, url_decode_depth=3)):
            raise ProviderEgressError(
                "provider request contains an unredacted credential value")
        return self.inspect(body.get("messages"), body.get("tools"))


DEFAULT_PROVIDER_EGRESS_POLICY = ProviderEgressPolicy()


class AuditedChat:
    """Apply an egress policy and record only its non-content projection."""

    def __init__(self, delegate, ledger, *, policy: ProviderEgressPolicy | None = None):
        if not callable(getattr(delegate, "complete", None)):
            raise TypeError("chat delegate must provide complete")
        if not callable(getattr(ledger, "append", None)):
            raise TypeError("egress ledger must provide append")
        self._delegate = delegate
        self._ledger = ledger
        self.policy = policy or DEFAULT_PROVIDER_EGRESS_POLICY
        if hasattr(delegate, "meter"):
            self.meter = delegate.meter

    def __getattr__(self, name: str):
        return getattr(self._delegate, name)

    def complete(self, messages: list[dict], tools: list[dict] | None, run_id: str,
                 **kwargs) -> dict:
        report = self.policy.inspect(messages, tools)
        self._ledger.append("provider_egress", **report.audit_projection())
        return self._delegate.complete(messages, tools, run_id, **kwargs)
