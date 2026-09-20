"""Typed tool and Skill specifications for the Twenty integration."""
from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources
from urllib.parse import urlsplit

from web_operation_agent.business.runtime import BusinessSkill


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


@dataclass(frozen=True)
class _ToolSpec:
    """One adapter tool's schema, authority and operational implementation."""

    name: str
    schema: dict
    effect: str
    scope: str
    data_types: tuple[str, ...]
    execute_method: str


READ_SPECS = (
    _ToolSpec("query_companies", QUERY_COMPANIES_SCHEMA, "read", "companies:read",
              ("business.company", "business.identifier", "application.route"),
              "_execute_companies_query"),
    _ToolSpec("query_people", QUERY_PEOPLE_SCHEMA, "read", "people:read",
              ("business.person", "business.identifier", "application.route"),
              "_execute_people_query"),
    _ToolSpec("query_opportunities", QUERY_OPPORTUNITIES_SCHEMA, "read",
              "opportunities:read",
              ("business.opportunity", "business.identifier", "application.route"),
              "_execute_opportunities_query"),
)
MUTATION_SPECS = (
    _ToolSpec("create_company", CREATE_COMPANY_SCHEMA, "write", "companies:write",
              ("business.company", "business.identifier", "application.route",
               "business.mutation_result"), "_dispatch_create_company"),
)
_READ_BY_TOOL = {spec.name: spec for spec in READ_SPECS}
_MUTATION_BY_TOOL = {spec.name: spec for spec in MUTATION_SPECS}

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
