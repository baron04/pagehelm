"""The part a real integration writes: ports, a typed tool, and a Skill artifact.

This is the file to copy. It implements what `ApplicationPorts` asks for -- identity, route
validation and result data classification -- exposes one read-only typed tool over the
application's own data, and loads the Skill that tells the model when that tool applies.

Nothing here knows about the Agent, the Runtime, or the model. That is the whole point of
the SPI: an application author describes their application, and the framework decides what
to do with it.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from web_operation_agent.application import (
    APPLICATION_ADAPTER_VERSION, AnonymousIdentityPort, ApplicationDescriptor, ApplicationPorts, ConservativeMutationReconciler, DeclaredDataClassifier, DeclaredRoutePort, ResultPostconditionVerifier)
from web_operation_agent.business import (
    BusinessMutationSpec,
    BusinessSkill,
    BusinessToolError,
    BusinessToolResult,
    BusinessToolRuntime,
)


#: The Skill is an artifact, not a literal. A real integration ships one file per Skill and
#: version-controls it alongside the application it describes; loading it through
#: `BusinessSkill.from_bytes` is what validates its shape and its tool references.
SKILL_PATH = Path(__file__).parent / "skills" / "contacts-query.json"
_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_contacts",
        "description": "Read current contacts from the delegated synthetic workspace.",
        "parameters": {
            "type": "object",
            "properties": {
                "name_contains": {
                    "type": "string",
                    "description": "Optional case-insensitive contact-name filter.",
                },
            },
            "additionalProperties": False,
        },
    },
}


class _ContactsToolExecutor:
    def __init__(self, origin: str):
        self.origin = origin.rstrip("/")

    @staticmethod
    def tool_schemas() -> tuple[dict, ...]:
        return (_TOOL_SCHEMA,)

    @staticmethod
    def tool_effects() -> dict[str, str]:
        return {"list_contacts": "read"}

    def execute(self, name: str, args: dict, *, context=None) -> BusinessToolResult:
        if name != "list_contacts" or set(args) - {"name_contains"}:
            raise BusinessToolError("invalid contact query")
        query = args.get("name_contains", "")
        if not isinstance(query, str) or len(query) > 120:
            raise BusinessToolError("name_contains must be a string of at most 120 characters")
        try:
            with urllib.request.urlopen(self.origin + "/api/contacts", timeout=3) as response:
                payload = json.load(response)
        except (OSError, ValueError, urllib.error.URLError) as exc:
            raise BusinessToolError("the contacts API is unavailable") from exc
        source = payload.get("contacts") if isinstance(payload, dict) else None
        if not isinstance(source, list):
            raise BusinessToolError("the contacts API returned an invalid document")
        matched = []
        for raw in source:
            if not isinstance(raw, dict) or set(raw) != {"id", "name", "email"}:
                raise BusinessToolError("the contacts API returned an invalid record")
            if query and query.casefold() not in str(raw["name"]).casefold():
                continue
            matched.append({
                "id": str(raw["id"]),
                "name": str(raw["name"]),
                "email": str(raw["email"]),
                "ui_path": f"/contacts/{raw['id']}",
            })
        routes = tuple(item["ui_path"] for item in matched)
        return BusinessToolResult(
            content={
                "contacts": matched,
                "matched_count": len(matched),
                "source_total": len(source),
                "scope": "synthetic_local_workspace",
                "completeness": {
                    "source_complete": True,
                    "result_truncated": False,
                    "incomplete_reason": "",
                },
            },
            audit={
                "application": "minimal-contacts",
                "operation": name,
                "returned_count": len(matched),
                "source_complete": True,
            },
            allowed_routes=routes,
        )

    @staticmethod
    def prepare_mutation(_name: str, _args: dict, *, context=None) -> BusinessMutationSpec:
        raise BusinessToolError("the minimal demo is read-only")

    @staticmethod
    def execute_mutation(_name: str, _args: dict, *, context=None) -> BusinessToolResult:
        raise BusinessToolError("the minimal demo is read-only")


class MinimalContactsAdapter:
    """A second application adapter implemented only through the public SPI."""

    def __init__(self, origin: str):
        skill = BusinessSkill.from_bytes(
            SKILL_PATH.read_bytes(), artifact_uri=f"file://{SKILL_PATH.name}")
        data = DeclaredDataClassifier((
            ("list_contacts", (
                "synthetic.contact", "business.identifier", "application.route")),
        ))
        postconditions = ResultPostconditionVerifier(())
        routes = DeclaredRoutePort(
            application_id="minimal.contacts",
            tool_route_patterns=(r"/contacts/[a-z0-9-]+",),
        )
        self.business_tools = BusinessToolRuntime(
            (skill,), _ContactsToolExecutor(origin), application_id="minimal.contacts",
            data_classifier=data, postcondition_verifier=postconditions,
            route_validator=routes)
        self.ports = ApplicationPorts(
            identity=AnonymousIdentityPort(),
            routes=routes,
            postconditions=postconditions,
            reconciliation=ConservativeMutationReconciler(),
            data=data,
        )
        self.descriptor = ApplicationDescriptor(
            schema_version=APPLICATION_ADAPTER_VERSION,
            application_id="minimal.contacts",
            display_name="Minimal Contacts",
            origin=origin,
            navigation_mode="spa",
            page_controller_module=origin + "/woa/page-controller.js",
            catalog_sha256=self.business_tools.catalog_sha256,
        )

    def health(self) -> dict:
        try:
            with urllib.request.urlopen(self.descriptor.origin + "/healthz", timeout=2) as response:
                payload = json.load(response)
            passed = response.status == 200 and payload == {"ready": True}
        except (OSError, ValueError, urllib.error.URLError):
            passed = False
        return {"passed": passed, "application_id": self.descriptor.application_id}
