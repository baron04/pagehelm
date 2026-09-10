"""Model-free Three-object Twenty apparatus for breadth runner qualification."""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from web_operation_agent.application import (DeclaredDataClassifier, DeclaredRoutePort,
                                             ResultPostconditionVerifier)
from web_operation_agent.answering import StructuredQueryAnswer
from web_operation_agent.business import (BusinessSkill, BusinessToolRequestError,
                                           BusinessToolResult, BusinessToolRuntime)
from web_operation_agent.schemas import ElementRef
from web_operation_agent.twenty import (QUERY_COMPANIES_SCHEMA, QUERY_OPPORTUNITIES_SCHEMA,
                                        QUERY_PEOPLE_SCHEMA)


_CONFIG = {
    "companies": {"tool": "query_companies", "detail": "domain", "singular": "company",
                  "scope": "companies:read", "triggers": ["company", "companies", "公司"]},
    "people": {"tool": "query_people", "detail": "email", "singular": "person",
               "scope": "people:read", "triggers": ["person", "people", "contact", "联系人"]},
    "opportunities": {"tool": "query_opportunities", "detail": "stage",
                      "singular": "opportunity", "scope": "opportunities:read",
                      "triggers": ["opportunity", "opportunities", "deal", "商机"]},
}
_SCHEMAS = {
    "query_companies": QUERY_COMPANIES_SCHEMA,
    "query_people": QUERY_PEOPLE_SCHEMA,
    "query_opportunities": QUERY_OPPORTUNITIES_SCHEMA,
}


def synthetic_breadth_records(count: int = 5) -> dict[str, list[dict[str, str]]]:
    if isinstance(count, bool) or not isinstance(count, int) or not 4 <= count <= 20:
        raise ValueError("count must be an integer from 4 to 20")
    return {
        "companies": [{"id": str(uuid.uuid4()), "name": f"Synthetic Company {index:02d}",
                       "domain": f"company-{index:02d}.synthetic.example"}
                      for index in range(1, count + 1)],
        "people": [{"id": str(uuid.uuid4()), "name": f"Synthetic Person {index:02d}",
                    "email": f"person-{index:02d}@synthetic.example"}
                   for index in range(1, count + 1)],
        "opportunities": [{"id": str(uuid.uuid4()),
                           "name": f"Synthetic Opportunity {index:02d}",
                           "stage": ("SCREENING" if index % 2 else "MEETING")}
                          for index in range(1, count + 1)],
    }


class SyntheticBreadthExecutor:
    def __init__(self, records: dict[str, list[dict[str, str]]]):
        self.records = {name: [dict(row) for row in rows] for name, rows in records.items()}
        if set(self.records) != set(_CONFIG):
            raise ValueError("synthetic breadth records are incomplete")
        self.calls: list[dict[str, Any]] = []

    def tool_schemas(self) -> tuple[dict[str, Any], ...]:
        return tuple(json.loads(json.dumps(_SCHEMAS[_CONFIG[name]["tool"]]))
                     for name in _CONFIG)

    def tool_effects(self) -> dict[str, str]:
        return {config["tool"]: "read" for config in _CONFIG.values()}

    def execute(self, name: str, args: dict[str, Any], *, context: Any = None
                ) -> BusinessToolResult:
        entity = next((key for key, value in _CONFIG.items() if value["tool"] == name), None)
        if entity is None or not isinstance(args, dict) or set(args) - {"name_contains", "limit"}:
            raise BusinessToolRequestError("unknown synthetic breadth query")
        needle = args.get("name_contains", "")
        limit = args.get("limit", 20)
        if not isinstance(needle, str) or len(needle) > 100:
            raise BusinessToolRequestError("name_contains is invalid")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise BusinessToolRequestError("limit is invalid")
        config = _CONFIG[entity]
        matched = [row for row in self.records[entity]
                   if not needle.strip() or needle.casefold().strip() in row["name"].casefold()]
        visible = matched[:limit]
        detail = config["detail"]
        projected = [{"name": row["name"], detail: row[detail],
                      "ui_path": f"/object/{config['singular']}/{row['id']}"}
                     for row in visible]
        self.calls.append({"name": name, "args": dict(args),
                           "principal": getattr(context, "binding_digest", None)})
        observed_at = "2026-09-10T00:00:00+00:00"
        return BusinessToolResult(
            content={entity: projected, "collection_ui_path": f"/objects/{entity}",
                     "matched_count": len(matched),
                     "source_total": len(self.records[entity]),
                     "scope": "synthetic_workspace", "observed_at": observed_at,
                     "completeness": {"source_total_known": True, "source_complete": True,
                                      "result_truncated": len(matched) > limit,
                                      "pages_scanned": 1,
                                      "records_scanned": len(self.records[entity]),
                                      "incomplete_reason": ""}},
            audit={"operation": name, "returned_count": len(visible),
                   "matched_count": len(matched)},
            allowed_routes=(f"/objects/{entity}",) + tuple(
                row["ui_path"] for row in projected),
            query_answer=StructuredQueryAnswer(
                collection=entity, fields=("name", detail),
                records=tuple((row["name"], row[detail]) for row in projected),
                source="Synthetic Twenty application API", scope="synthetic_workspace",
                observed_at=observed_at, source_complete=True,
                result_truncated=len(matched) > limit, matched_count=len(matched),
                source_total=len(self.records[entity]),
                records_scanned=len(self.records[entity])))


def synthetic_breadth_runtime(executor: SyntheticBreadthExecutor) -> BusinessToolRuntime:
    skills = []
    for entity, config in _CONFIG.items():
        raw = {
            "schema_version": 1, "skill_id": f"synthetic.{entity}-query",
            "title": f"Query synthetic {entity}",
            "intent": f"Read {entity} visible to the synthetic workspace user.",
            "triggers": config["triggers"],
            "instructions": [
                f"Call {config['tool']} for current workspace data.",
                "Use name_contains only when the user supplied a record-name filter.",
                "For a record open, use only the exact ui_path returned by that query.",
            ],
            "tool_names": [config["tool"]], "provenance": [f"synthetic:{entity}"],
            "read_only": True,
        }
        skills.append(BusinessSkill.from_bytes(
            json.dumps(raw).encode("utf-8"), artifact_uri=f"memory://synthetic/{entity}.json"))
    required = {config["tool"]: config["scope"] for config in _CONFIG.values()}
    data = DeclaredDataClassifier(tuple(
        (tool, ("business.identifier", "application.route")) for tool in sorted(required)))
    routes = DeclaredRoutePort(
        application_id="twenty",
        tool_route_patterns=tuple(
            [f"/objects/{entity}" for entity in _CONFIG]
            + [f"/object/{config['singular']}/[0-9a-f-]{{36}}"
               for config in _CONFIG.values()]))
    return BusinessToolRuntime(
        tuple(skills), executor, application_id="twenty", required_scopes=required,
        data_classifier=data, postcondition_verifier=ResultPostconditionVerifier(()),
        route_validator=routes)


def breadth_projection(records: dict[str, list[dict[str, str]]]) -> dict[str, Any]:
    timestamp = "2026-09-10T00:00:00+00:00"
    companies = [{**row, "created_at": timestamp, "updated_at": timestamp,
                  "deleted_at": None} for row in records["companies"]]
    people = [{**row, "company_id": "", "created_at": timestamp,
               "updated_at": timestamp, "deleted_at": None}
              for row in records["people"]]
    opportunities = [{**row, "amount_micros": "", "currency": "", "company_id": "",
                      "created_at": timestamp, "updated_at": timestamp,
                      "deleted_at": None} for row in records["opportunities"]]
    return {"companies": companies, "people": people, "opportunities": opportunities}


class SyntheticBreadthPageModel:
    def __init__(self, records: dict[str, list[dict[str, str]]]):
        self.records = records

    def render(self, path: str) -> tuple[str, str]:
        for entity, config in _CONFIG.items():
            if path == f"/objects/{entity}":
                return entity.title(), entity.title()
            prefix = f"/object/{config['singular']}/"
            if path.startswith(prefix):
                record_id = path[len(prefix):]
                row = next((item for item in self.records[entity]
                            if item["id"] == record_id), None)
                if row is None:
                    return "Record not found", "Record not found"
                detail = config["detail"]
                return row["name"], f"{row['name']}\n{detail.title()}\n{row[detail]}"
        return "Synthetic Twenty", "Synthetic Twenty"

    def elements(self, _path: str, _epoch: int) -> list[ElementRef]:
        return []


def breadth_records_digest(records: dict[str, list[dict[str, str]]]) -> str:
    raw = json.dumps(records, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


__all__ = ["SyntheticBreadthExecutor", "SyntheticBreadthPageModel",
           "breadth_projection", "breadth_records_digest", "synthetic_breadth_records",
           "synthetic_breadth_runtime"]
