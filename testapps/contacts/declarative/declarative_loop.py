"""Declarative ModelGoalPort and ModelDrivenTurnExecutor profile builders."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any
from urllib.parse import urlsplit

from web_operation_agent.answer.answering import StructuredQueryAnswer
from web_operation_agent.execution.draft_handoff import add_draft_lines, field_identity, handoff_draft
from web_operation_agent.execution.model_loop_contracts import AnswerTarget, LoopProcedure, LoopToolResult, ModelLoopProfile
from web_operation_agent.execution.read_summary import (
    Missing,
    RelationValue,
    SummaryColumn,
    SummaryRead,
    SummaryRefused,
    SummaryRow,
    compute_summary,
    parse_summary_spec,
    summary_schema,
)
from web_operation_agent.execution.turn_tables import (
    BEGIN_UNTRUSTED,
    DATE_RANGE_TOOL_SCHEMA,
    END_UNTRUSTED,
    TURN_SQL_TOOL_SCHEMA,
    TURN_TABLES_TOOL_SCHEMA,
    TurnReadError,
    date_range,
)
from web_operation_agent.goal.index import ApplicationIndex, IndexEntity, IndexField, IndexPage
from web_operation_agent.goal.ports import Candidate
from web_operation_agent.privacy import SessionHandoffPolicy, redact_sensitive_assignments
from web_operation_agent.state.identity import OperationContext, PrincipalError
from .declarative import DeclarativeApplicationAdapter
from .declarative_forms import DeclarativeDraftSteps, DeclarativeMutationPrefill
from .openapi import EntitySpec, FormSpec


def _norm(value: str) -> str:
    return " ".join(value.casefold().split())


def _tool_schema(name: str, description: str, properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False},
        },
    }


def _display_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


class DeclarativeGoalPort:
    """Resolve actor-bound index, capabilities, and candidates for ``ModelDrivenTurnExecutor``."""

    def __init__(self, adapter: DeclarativeApplicationAdapter, *, form_prefill: bool = False, convention_updates: bool = False):
        if not isinstance(form_prefill, bool) or not isinstance(convention_updates, bool):
            raise TypeError("form_prefill and convention_updates must be boolean")
        self._form_prefill, self._convention_updates, self._adapter = form_prefill, convention_updates, adapter
        executor = adapter.business_tools._executor
        self._executor = getattr(executor, "reads", executor)
        self.page_reader = None
        self._entities = tuple(adapter.spec.entities)
        self._pages_by_key = {p.key: p for p in adapter.spec.pages}
        self._forms = tuple(adapter.spec.forms)
        self._read_scope, self._write_scope = adapter.read_scope, adapter.write_scope

    def _require_actor(self, context: OperationContext | None) -> frozenset[str]:
        return self._adapter.identity.verify_live_actor(
            context, required_scope=self._read_scope,
            mismatch_error_cls=PrincipalError, mismatch_message="goal read actor differs from delegated API actor",
        )

    @staticmethod
    def _entity_matches(spec: EntitySpec, name: str) -> bool:
        target = _norm(name)
        if not target:
            return False
        names = {_norm(spec.name), _norm(spec.subject_reference), _norm(spec.label), *(_norm(a) for a in spec.aliases)}
        if spec.singular_label:
            names.add(_norm(spec.singular_label))
        return target in names

    def _index(self, context: OperationContext, permissions: frozenset[str]) -> tuple[ApplicationIndex, dict[str, str]]:
        catalog = self._executor._page_entries._port.catalog(context=context)
        digest = hashlib.sha256((f"{self._adapter.spec.application_id}-goal-v2\0" + catalog.binding_digest).encode()).hexdigest()
        subject_to_entity = {e.subject_reference: e.name for e in self._entities if e.subject_reference}
        entities_by_name = {e.name: e for e in self._entities}
        pages: list[IndexPage] = []
        for entry in catalog.entries:
            if not entry.route:
                continue
            pspec = self._pages_by_key.get(entry.reference)
            if pspec is not None:
                entity_name = pspec.entity or subject_to_entity.get(entry.subject_reference, "")
                sections = pspec.sections
                ops = tuple(op for op in pspec.operations if op != "create" or (self._write_scope in permissions))
            else:
                entity_name = entry.subject_reference if entry.subject_reference in entities_by_name else subject_to_entity.get(entry.subject_reference, "")
                sections, ops = (), ()
            pages.append(IndexPage(
                key=entry.reference, label=entry.label, breadcrumb=" / ".join(entry.breadcrumbs) or entry.label,
                entity=entity_name, views=entry.views, sections=sections, operations=ops,
            ))
        index_entities = tuple(
            IndexEntity(
                key=e.name, label=e.label, aliases=e.aliases,
                fields=tuple(
                    IndexField(f.name, f.label, f.kind if f.kind in {"text", "number", "money", "date", "choice", "link", "html", "bool", "other"} else "text", aliases=f.aliases)
                    for f in e.fields if f.in_entity
                ),
            )
            for e in self._entities
        )
        return (
            ApplicationIndex(entities=index_entities, pages=tuple(pages), complete=catalog.complete, source_digest=digest),
            {entry.reference: entry.route for entry in catalog.entries if entry.route},
        )

    def index(self, *, context: OperationContext | None = None) -> ApplicationIndex:
        permissions = self._require_actor(context)
        assert context is not None
        return self._index(context, permissions)[0]

    def capabilities(self) -> frozenset[str]:
        caps: set[str] = set()
        if self._entities:
            caps.add("values")
        if any(p.sections for p in self._adapter.spec.pages):
            caps.add("sections")
        if self._forms or any(e.detail_route_template for e in self._entities):
            caps.add("handoffs")
        if self._form_prefill:
            caps.add("fill")
        if self._convention_updates:
            caps.add("updates")
        return frozenset(caps)

    def _rows_for(self, spec: EntitySpec) -> list[dict]:
        return self._executor.fetch_rows(spec.name)

    def candidates(
        self, *, entities: tuple[str, ...] = (), known: tuple = (), dates: tuple[str, ...] = (), context: OperationContext | None = None,
    ) -> tuple[Candidate, ...]:
        self._require_actor(context)
        matched = [spec for spec in self._entities if any(self._entity_matches(spec, e) for e in entities)]
        out: list[Candidate] = []
        for spec in matched:
            obs = self.page_reader(context) if self.page_reader is not None else None
            rows = self._rows_for(spec)
            current = None
            if obs is not None and spec.detail_route_template:
                path = urlsplit(obs.url).path
                if obs.render_state == "ready" and obs.record_route == path and obs.page_route == path:
                    current = next((row for row in rows if path == spec.detail_route_template.format(id=row[spec.id_field])), None)
                fresh = self.page_reader(context)
                if fresh.epoch != obs.epoch or fresh.content_digest != obs.content_digest:
                    current = None
            ordinary = [
                Candidate(kind="record", entity=spec.name, display=str(row.get(spec.name_field, row[spec.id_field])), detail=_display_cell(row.get(spec.detail_field)) if spec.detail_field else "")
                for row in rows[:20] if current is None or row[spec.id_field] != current[spec.id_field]
            ]
            if current is not None:
                ordinary.insert(0, Candidate(kind="record", entity=spec.name, display=f"{current.get(spec.name_field, current[spec.id_field])}（ID={current[spec.id_field]}）", detail="Currently rendered record read under the active actor."))
            out.extend(ordinary)
            if len(out) >= 20:
                break
        return tuple(out[:20])


class DeclarativeModelGoalPort:
    """Model-loop GoalPort exposing implemented UI/update capabilities."""

    def __init__(
        self, adapter: DeclarativeApplicationAdapter, *,
        goal_port_cls: type[Any] = DeclarativeGoalPort,
        drafts: bool = False, filters: bool = False, updates: bool = False,
    ):
        self._port = goal_port_cls(adapter, convention_updates=updates)
        self.drafts, self.filters, self.updates = drafts, filters, updates

    def __getattr__(self, name: str):
        return getattr(self._port, name)

    def capabilities(self) -> frozenset[str]:
        base = {"handoffs"}
        if any(p.sections for p in self._port._adapter.spec.pages):
            base.add("sections")
        if self.drafts:
            base |= {"fill", "lines"}
        if self.filters:
            base.add("facets")
        if self.updates:
            base.add("updates")
        return frozenset(base)


class DeclarativeProcedure:
    """Declarative handler for collection reads, TurnTables SQL, and bounded UI steps."""

    default_entities: tuple[EntitySpec, ...] = ()
    default_forms: tuple[FormSpec, ...] = ()

    def __init__(self, adapter: DeclarativeApplicationAdapter, goal_port, name: str, *, enable_sql: bool = False, today: str = "", time_zone: str = "UTC"):
        self.adapter, self.goal_port, self.name = adapter, goal_port, name
        self.enable_sql, self.today, self.time_zone = enable_sql, today, time_zone
        spec_obj = getattr(adapter, "spec", None)
        entities = getattr(spec_obj, "entities", self.default_entities)
        forms = getattr(spec_obj, "forms", self.default_forms)
        self._entities_by_list = {e.list_tool_name: e for e in entities if e.list_tool_name}
        self._form_routes = {f.route for f in forms}

    def execute(self, arguments: dict, *, context) -> LoopToolResult:
        context.page.checkpoint()
        permissions = self.goal_port._require_actor(context.operation)
        spec = self._entities_by_list.get(self.name)
        if spec is not None:
            return self._execute_list(spec, arguments, context=context, permissions=permissions)
        if self.name == "sql":
            return self._execute_sql(arguments, context=context)
        if self.name == "tables":
            return self._execute_tables(context=context)
        if self.name == "date_range":
            return self._execute_date_range(arguments)
        return self._execute_page_action(arguments, context=context)

    def _execute_list(self, spec: EntitySpec, arguments: dict, *, context, permissions: frozenset[str]) -> LoopToolResult:
        if arguments.get("deliver") and "summary" in arguments:
            return LoopToolResult("Choose summary or text delivery.", kind="read", state="refused", error_code="invalid_arguments")
        clean_args = {k: v for k, v in arguments.items() if k not in {"deliver", "summary"}}
        executor = getattr(self.adapter, "_executor", None) or getattr(getattr(self.adapter, "business_tools", None), "_executor", None)
        result = executor.execute(self.name, clean_args, context=context.operation)
        context.page.checkpoint()
        rows = result.content.get(spec.collection_key or spec.name, ())

        table_header, read_key, read_count, trace = "", "", None, ()
        if self.enable_sql and context.turn_tables is not None:
            facts = result.content
            info = context.turn_tables.add(
                path=spec.list_endpoint or self.name, params=clean_args, rows=list(rows),
                total=int(facts.get("matched_count", len(rows))),
                complete=bool(facts.get("completeness", {}).get("source_complete", True) and not facts.get("completeness", {}).get("result_truncated", False)),
            )
            read_key, read_count, trace = info["name"], len(rows), (info["receipt"],)
            table_header = f"Table {info['name']} ({info['row_count']} rows, receipt {info['receipt']}):\n"

        text = table_header + f"{BEGIN_UNTRUSTED}\n" + redact_sensitive_assignments(result.render()) + f"\n{END_UNTRUSTED}"
        index, page_routes = self.goal_port._index(context.operation, permissions)
        entity_page = index.page(spec.name)
        targets: list[AnswerTarget] = []
        if entity_page is not None and spec.name in page_routes and (not spec.list_route or page_routes[spec.name] == spec.list_route):
            targets.append(AnswerTarget("page", page_routes[spec.name], entity_page.label, f"{spec.name}:list"))
        fp = spec.list_filter_param
        if fp and arguments.get(fp) and len(rows) == 1 and spec.detail_route_template:
            row = rows[0]
            filter_val = row.get(spec.list_filter_field or spec.name_field)
            if isinstance(filter_val, str) and filter_val:
                verified = executor.execute(self.name, {fp: filter_val}, context=context.operation)
                context.page.checkpoint()
                exact = [item for item in verified.content.get(spec.collection_key or spec.name, ()) if item.get(spec.name_field) == row.get(spec.name_field)]
                expected_path = spec.detail_route_template.format(id=row[spec.id_field])
                if len(exact) == 1 and exact[0].get(spec.id_field) == row[spec.id_field] and exact[0].get("ui_path") == expected_path:
                    targets.append(AnswerTarget("record", expected_path, str(row[spec.name_field]), f"{spec.name}:{row[spec.id_field]}"))

        projection = None
        if "summary" in arguments:
            facts = result.content
            try:
                summary_spec, rewritten = parse_summary_spec(arguments["summary"])
                used = {*summary_spec.columns, *summary_spec.group_by, *(c.column for c in summary_spec.where), *(m.column for m in summary_spec.measures if m.column), *(k.column for k in summary_spec.order_by)} or {spec.id_field}
                selected = tuple(
                    SummaryColumn(f.name, "relation" if f.name == spec.id_field else f.summary_kind, display_name=f.summary_label)
                    for f in spec.fields if f.in_entity and not f.is_secret and f.name in used
                )
                routes = getattr(getattr(self.adapter, "ports", None), "routes", None)
                handoffs = SessionHandoffPolicy.extending(getattr(routes, "session_handoff_query_keys", ()) if routes is not None else ())
                for row in rows:
                    vals = {spec.id_field: row[spec.id_field], **{c.name: row.get(c.name) for c in selected if c.name != spec.id_field}}
                    if redact_sensitive_assignments(handoffs.redact(vals)) != vals:
                        raise SummaryRefused("redacted_value", "summary cannot compute or deliver redacted field values")
                projection = compute_summary(
                    SummaryRead(
                        origin=result.evidence[0].origin, actor_digest=context.operation.principal.actor_authority_digest,
                        columns=selected,
                        rows=tuple(
                            SummaryRow(str(row[spec.id_field]), tuple(RelationValue(str(row[spec.id_field]), str(row[spec.id_field])) if c.name == spec.id_field else row.get(c.name, Missing.MISSING) for c in selected))
                            for row in rows
                        ),
                        matched_count=facts["matched_count"], records_scanned=facts["source_total"],
                        complete=facts["completeness"]["source_complete"] and not facts["completeness"]["result_truncated"],
                        consistency=f"Single authenticated REST collection read ({spec.label}).",
                    ),
                    summary_spec,
                )
            except SummaryRefused as exc:
                return LoopToolResult("Summary unavailable: " + exc.detail, kind="read", state="unverified", error_code=exc.code)
            text = "Program summary prepared. Select its read_answer; other user obligations still apply." + ("\nThe program read the spec as: " + "; ".join(rewritten) if rewritten else "")
        elif arguments.get("deliver"):
            facts = result.content
            dfields = tuple(f.name for f in spec.fields if f.name != spec.id_field and f.in_entity and not f.is_secret)
            projection = StructuredQueryAnswer(
                collection=spec.name, fields=dfields,
                records=tuple(tuple(_display_cell(row.get(k)) for k in dfields) for row in rows),
                source=spec.summary_source_label or f"{spec.label} REST collection",
                actor_digest=context.operation.principal.actor_authority_digest,
                record_ids=tuple(str(row[spec.id_field]) for row in rows), scope=facts["scope"],
                observed_at=datetime.now(timezone.utc).isoformat(),
                source_complete=facts["completeness"]["source_complete"], result_truncated=facts["completeness"]["result_truncated"],
                matched_count=facts["matched_count"], source_total=facts["source_total"],
                records_scanned=facts["source_total"], incomplete_reason=facts["completeness"]["incomplete_reason"],
            )
        return LoopToolResult(text, kind="read", read_summary=(self.name, text[:1500]), read_key=read_key, read_count=read_count, trace=trace, query_answer=projection, answer_targets=tuple(targets))

    def _execute_sql(self, arguments: dict, *, context) -> LoopToolResult:
        if context.turn_tables is None:
            return LoopToolResult("SQL table workspace is unavailable.", kind="read", state="unavailable")
        try:
            res = context.turn_tables.query(arguments.get("query", ""))
        except TurnReadError as exc:
            return LoopToolResult(f"SQL refused ({exc.code}): {exc.message}", kind="read", state="refused" if "timeout" not in exc.code else "unverified", error_code=exc.code)
        body = json.dumps({k: res[k] for k in ("table_ref", "receipt", "columns", "rows", "returned_rows", "truncated", "sources_complete")} | ({"tie_note": res["tie_note"]} if res.get("tie_note") else {}), ensure_ascii=False)
        return LoopToolResult(f"SQL result {res['table_ref']} (receipt {res['receipt']}):\n{BEGIN_UNTRUSTED}\n{body}\n{END_UNTRUSTED}", kind="read", read_key=res["table_ref"], read_count=res["returned_rows"], trace=(res["receipt"],))

    def _execute_tables(self, *, context) -> LoopToolResult:
        if context.turn_tables is None:
            return LoopToolResult("SQL table workspace is unavailable.", kind="metadata", state="unavailable")
        return LoopToolResult(f"{BEGIN_UNTRUSTED}\n{json.dumps(context.turn_tables.describe(), ensure_ascii=False)}\n{END_UNTRUSTED}", kind="metadata")

    def _execute_date_range(self, arguments: dict) -> LoopToolResult:
        anchor = self.today or datetime.now(timezone.utc).date().isoformat()
        try:
            return LoopToolResult(json.dumps(date_range(today=anchor, zone=self.time_zone, **arguments), ensure_ascii=False), kind="metadata")
        except TurnReadError as exc:
            return LoopToolResult(f"Invalid date range: {exc.message}", kind="metadata", state="refused", error_code=exc.code)

    def _execute_page_action(self, arguments: dict, *, context) -> LoopToolResult:
        if self.name == "goto":
            outcome = context.page.execute("goto", {"url": self.adapter.descriptor.origin + arguments["path"]}, navigating=True)
            if outcome.state == "completed" and arguments.get("open"):
                controls = [e for e in context.obs.elements if e.role == "link" and e.name == arguments["open"] and e.attrs.get("href") in self._form_routes]
                if len(controls) != 1:
                    return LoopToolResult("No unique reviewed form entry.", kind="page", state="refused", error_code="invalid_target")
                outcome = context.page.execute("click", {"ref": controls[0].ref})
        elif self.name == "fill_fields":
            if arguments.get("then_click") and arguments.get("then_highlight"):
                return LoopToolResult("Choose query completion or form handoff.", kind="page", state="refused", error_code="invalid_arguments")
            if arguments.get("add_lines"):
                failed = add_draft_lines(context, arguments["add_lines"]["button"], arguments["add_lines"]["count"])
                if failed:
                    return failed
            before, expected = context.obs, []
            for field in arguments["fields"]:
                wanted = str(field["label"]).lower()
                matches = [e for e in context.obs.elements if wanted in {str(v).lower() for v in (e.attrs.get("field_label"), e.attrs.get("field"), e.name) if v}]
                if len(matches) != 1:
                    return LoopToolResult("No unique current field.", kind="page", state="refused", error_code="invalid_target")
                control = matches[0]
                action = "select" if control.attrs.get("aria-haspopup") == "listbox" else "fill"
                outcome = context.page.execute(action, {"ref": control.ref, "value": field["value"], **({"option_label": field["value"]} if action == "select" else {"commit": True})})
                if outcome.state != "completed":
                    labels = outcome.receipt.candidate_labels if outcome.receipt else ()
                    return LoopToolResult(f"Field entry refused.\n{BEGIN_UNTRUSTED}\n" + redact_sensitive_assignments(json.dumps(labels)) + f"\n{END_UNTRUSTED}", kind="page", state=outcome.state, dispatch=outcome.dispatch, error_code=outcome.error_code)
                expected.append((field_identity(control), field["value"]))
            if arguments.get("then_click"):
                matches = [e for e in context.obs.elements if e.tag == "button" and e.name == arguments["then_click"]]
                if len(matches) != 1:
                    return LoopToolResult("No unique query control.", kind="page", state="refused", error_code="invalid_target")
                outcome = context.page.execute("click", {"ref": matches[0].ref})
                return LoopToolResult("Query action verified.", kind="page", state=outcome.state, dispatch=outcome.dispatch, error_code=outcome.error_code, completion_command=outcome.receipt.command_id if outcome.state == "completed" else "")
            if arguments.get("then_highlight"):
                return handoff_draft(context, before, expected, arguments["then_highlight"])
            return LoopToolResult("Fields verified.", kind="page", entered=tuple((f["label"], f["value"]) for f in arguments["fields"]))
        elif self.name in {"input", "click"}:
            element = next((e for e in context.obs.elements if e.ref == "e" + str(arguments["index"])), None)
            if element is None:
                return LoopToolResult("Control unavailable.", kind="page", state="refused")
            outcome = context.page.execute("fill" if self.name == "input" else "click", {"ref": element.ref, **({"value": arguments["text"], "commit": True} if self.name == "input" else {})})
        elif self.name == "highlight":
            outcome = context.page.execute("highlight", {"ref": "e" + str(arguments["index"]), "epoch": context.obs.epoch})
        else:
            return LoopToolResult("This application does not offer that capability.", state="unavailable")
        return LoopToolResult("Page action completed." if outcome.state == "completed" else "Page action failed; check the error code and current observation.", kind="page", state=outcome.state, dispatch=outcome.dispatch, error_code=outcome.error_code)


def build_declarative_profile(
    adapter: DeclarativeApplicationAdapter, goal_port, *, procedure_cls: type[DeclarativeProcedure] = DeclarativeProcedure,
    drafts: bool = False, filters: bool = False, enable_sql: bool = False, today: str = "", time_zone: str = "UTC",
    draft_steps=None, mutation_prefill=None,
    hub_routes: frozenset[str] = frozenset(),
    deferred_dom_tools: frozenset[str] = frozenset(),
    suppress_system_operation_catalog: bool = False,
) -> ModelLoopProfile:
    """Build a complete ``ModelLoopProfile`` from ``DeclarativeAppSpec``."""
    schemas: list[tuple[dict, str, frozenset[str], str]] = []
    for spec in adapter.spec.entities:
        if not spec.list_tool_name:
            continue
        props: dict[str, Any] = {
            **({spec.list_filter_param: {"type": "string", "maxLength": 120}} if spec.list_filter_param else {}),
            "deliver": {"type": "boolean", "description": "Prepare exact server delivery; select finish.read_answer."},
            "summary": summary_schema(),
        }
        schemas.append((_tool_schema(spec.list_tool_name, spec.list_tool_description or f"Read actor-visible {spec.label.lower()} from the REST collection.", props), "read", frozenset(), spec.read_scope or adapter.read_scope))
    if enable_sql:
        schemas.extend(((TURN_SQL_TOOL_SCHEMA, "read", frozenset(), adapter.read_scope), (TURN_TABLES_TOOL_SCHEMA, "metadata", frozenset(), adapter.read_scope), (DATE_RANGE_TOOL_SCHEMA, "metadata", frozenset(), adapter.read_scope)))
    schemas.extend((
        (_tool_schema("goto", "Open a reviewed page route.", {"path": {"type": "string", "maxLength": 200}, "open": {"type": "string", "maxLength": 200}, "answer": {"type": "string", "maxLength": 2000}}, ("path",)), "page", frozenset(), adapter.read_scope),
        (_tool_schema("click", "Click a reviewed section or local line control.", {"index": {"type": "integer", "minimum": 0}, "answer": {"type": "string", "maxLength": 2000}}, ("index",)), "page", frozenset(), adapter.read_scope),
        (_tool_schema("highlight", "Mark a visible control without clicking it.", {"index": {"type": "integer", "minimum": 0}, "answer": {"type": "string", "maxLength": 2000}}, ("index",)), "page", frozenset({"handoffs"}), adapter.read_scope),
    ))
    if drafts or filters:
        fill_props: dict[str, Any] = {
            "fields": {"type": "array", "minItems": 1, "maxItems": 12, "items": {"type": "object", "properties": {"label": {"type": "string"}, "value": {"type": "string"}}, "required": ["label", "value"], "additionalProperties": False}},
            "then_highlight": {"type": "string"}, "then_click": {"type": "string"},
            "add_lines": {"type": "object", "properties": {"button": {"type": "string", "maxLength": 200}, "count": {"type": "integer", "minimum": 1, "maximum": 4}}, "required": ["button", "count"], "additionalProperties": False},
            "answer": {"type": "string", "maxLength": 2000},
        }
        schemas.append((_tool_schema("fill_fields", "Enter named local fields; verify the form and highlight Submit for manual user confirmation, never auto-submit.", fill_props, ("fields",)), "page", frozenset(), adapter.read_scope))
        schemas.append((_tool_schema("input", "Enter one reviewed local field and commit it.", {"index": {"type": "integer", "minimum": 0}, "text": {"type": "string", "maxLength": 4000}}, ("index", "text")), "page", frozenset({"fill"}), adapter.read_scope))

    query_routes = frozenset(p.query_form.route for p in adapter.spec.pages if p.query_form is not None)
    fill_reqs = lambda args, obs: frozenset({"facets" if obs.page_route in query_routes else "fill"}) | (frozenset({"lines"}) if args.get("add_lines") else frozenset()) | (frozenset({"handoffs"}) if args.get("then_highlight") else frozenset())
    click_reqs = lambda args, obs: frozenset({"sections" if (t := next((e for e in obs.elements if e.ref == "e" + str(args["index"])), None)) is not None and t.role == "tab" else "lines"})
    procedures = tuple(
        LoopProcedure(
            json.dumps(schema), procedure_cls(adapter, goal_port, schema["function"]["name"], enable_sql=enable_sql, today=today, time_zone=time_zone),
            kind, scope, required,
            fill_reqs if schema["function"]["name"] == "fill_fields" else click_reqs if schema["function"]["name"] == "click" else None,
        )
        for schema, kind, required, scope in schemas
    )
    prepare = lambda req, ctx: (goal_port._require_actor(ctx.operation), "Read the collection under the current actor; do not infer access to another workspace.")[1]
    render = lambda obs, marked: redact_sensitive_assignments(json.dumps({"url": obs.url, "epoch": obs.epoch, "route": obs.page_route, "state": obs.render_state, "controls": [{"index": e.ref, "role": e.role, "name": e.name, "attrs": e.attrs} for e in obs.elements], "marked": marked}, ensure_ascii=False))
    forms = tuple(adapter.spec.forms)
    if draft_steps is None and drafts and forms:
        draft_steps = DeclarativeDraftSteps.from_adapter(adapter)
    if mutation_prefill is None and drafts and adapter.ports.operations is not None and forms:
        mutation_prefill = DeclarativeMutationPrefill.from_adapter(adapter)
    summary_hints = " ".join(f"For counts, groups or lists, {e.list_tool_name} accepts summary over " + ", ".join(f.name for f in e.fields if f.in_entity and not f.is_secret) + ". Use columns and order_by for a complete sorted list without top or hand-copying." for e in adapter.spec.entities if e.list_tool_name)
    sql_hint = " Collected rows are loaded into in-memory SQL tables t1..tN; use sql/tables for cross-table analysis and finish(table_ref='q1') for large table export." if enable_sql else ""
    draft_hint = " Page forms only prefill fields and highlight the submit button via fill_fields(then_highlight=...) for the user to submit manually (never auto-submit)." if drafts else ""
    filter_hint = " Query forms accept filter entry followed by then_click on the search button." if filters else " This profile does not offer list filters."
    instructions = "Answer from current REST reads. Clarify ambiguous names; never choose one silently. " + summary_hints + " The server calculates and renders the selected read_answer; keep finish.answer empty. This application offers reviewed navigation and visible-control handoff." + sql_hint + draft_hint + filter_hint + " It does not offer pagination. Source tools depend on deployment configuration."
    hub_fn = (
        (lambda obs: (getattr(obs, "page_route", "") or urlsplit(getattr(obs, "url", "") or "").path) in hub_routes)
        if hub_routes
        else None
    )
    return ModelLoopProfile(
        instructions,
        procedures,
        render,
        prepare,
        draft_steps=draft_steps,
        mutation_prefill=mutation_prefill,
        is_hub_route=hub_fn,
        deferred_dom_tools=deferred_dom_tools,
        suppress_system_operation_catalog=suppress_system_operation_catalog,
    )


__all__ = ["DeclarativeGoalPort", "DeclarativeModelGoalPort", "DeclarativeProcedure", "build_declarative_profile"]
