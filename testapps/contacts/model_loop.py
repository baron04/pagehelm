"""Contacts REST/plain-HTML composition of the common model-driven turn."""
import json
from datetime import datetime, timezone
from web_operation_agent.answer.answering import StructuredQueryAnswer
from web_operation_agent.execution.read_summary import (Missing, RelationValue, SummaryColumn,
    SummaryRead, SummaryRefused, SummaryRow, SummarySpec, compute_summary, summary_schema)

from web_operation_agent.execution.model_loop_contracts import (
    AnswerTarget, LoopProcedure, LoopToolResult, ModelLoopProfile)
from web_operation_agent.privacy import SessionHandoffPolicy, redact_sensitive_assignments


def tool(name, description, properties, required=()):
    return {"type": "function", "function": {"name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": list(required),
                       "additionalProperties": False}}}


class ContactsModelGoalPort:
    """Capabilities implemented by this profile, independently of the goal planner."""
    def __init__(self, adapter, *, drafts=False, filters=False):
        from .goal import ContactsGoalPort
        self._port, self.drafts, self.filters = ContactsGoalPort(adapter), drafts, filters

    def __getattr__(self, name):
        return getattr(self._port, name)

    def capabilities(self):
        return frozenset({"sections", "handoffs"} | ({"fill", "lines"} if self.drafts else set())
                         | ({"facets"} if self.filters else set()))


class ContactsProcedure:
    def __init__(self, adapter, goal_port, name):
        self.adapter, self.goal_port, self.name = adapter, goal_port, name

    def execute(self, arguments, *, context):
        context.page.checkpoint()
        permissions = self.goal_port._require_actor(context.operation)
        if self.name == "list_contacts":
            if arguments.get("deliver") and "summary" in arguments:
                return LoopToolResult("Choose summary or text delivery.", kind="read", state="refused",
                                      error_code="invalid_arguments")
            result = self.adapter.business_tools._executor.execute(self.name,
                {key: value for key, value in arguments.items() if key not in {"deliver", "summary"}}, context=context.operation)
            context.page.checkpoint()
            text = "BEGIN_UNTRUSTED_APPLICATION_DATA\n" + redact_sensitive_assignments(result.render()) \
                + "\nEND_UNTRUSTED_APPLICATION_DATA"
            index, page_routes = self.goal_port._index(context.operation, permissions)
            contacts_page = index.page("contacts")
            targets = []
            if contacts_page is not None and page_routes.get("contacts") == "/contacts":
                targets.append(AnswerTarget("page", "/contacts", contacts_page.label,
                                            "contacts:list"))
            rows = result.content.get("contacts", ())
            if arguments.get("name_contains") and len(rows) == 1:
                row = rows[0]
                # A unique substring match is not enough to identify an answer link. Re-read
                # the exact name under this actor and require the same record and app route.
                verified = self.adapter.business_tools._executor.execute(
                    self.name, {"name_contains": row["name"]}, context=context.operation)
                context.page.checkpoint()
                exact = [item for item in verified.content.get("contacts", ())
                         if item.get("name") == row["name"]]
                if (len(exact) == 1 and exact[0].get("id") == row["id"]
                        and exact[0].get("ui_path") == f"/contacts/{row['id']}"):
                    targets.append(AnswerTarget("record", exact[0]["ui_path"],
                                                row["name"], f"contacts:{row['id']}"))
            projection = None
            if "summary" in arguments:
                facts = result.content
                try:
                    spec = SummarySpec.from_dict(arguments["summary"])
                    used = {*spec.columns, *spec.group_by, *(condition.column for condition in spec.where),
                            *(measure.column for measure in spec.measures if measure.column)} or {"id"}
                    selected = tuple(column for column in (SummaryColumn("id", "relation"),
                        SummaryColumn("name", "text"), SummaryColumn("email", "text")) if column.name in used)
                    handoffs = SessionHandoffPolicy.extending(self.adapter.ports.routes.session_handoff_query_keys)
                    for row in rows:
                        values = {"id": row["id"], **{column.name: row.get(column.name)
                            for column in selected if column.name != "id"}}
                        if redact_sensitive_assignments(handoffs.redact(values)) != values:
                            raise SummaryRefused("redacted_value", "summary cannot compute or deliver redacted field values")
                    projection = compute_summary(SummaryRead(
                        origin=result.evidence[0].origin,
                        actor_digest=context.operation.principal.actor_authority_digest,
                        columns=selected,
                        rows=tuple(SummaryRow(str(row["id"]), tuple(
                            RelationValue(str(row["id"]), str(row["id"])) if column.name == "id"
                            else row.get(column.name, Missing.MISSING) for column in selected)) for row in rows),
                        matched_count=facts["matched_count"], records_scanned=facts["source_total"],
                        complete=facts["completeness"]["source_complete"] and not facts["completeness"]["result_truncated"],
                        consistency="Single authenticated REST collection read."), spec)
                except SummaryRefused as exc:
                    return LoopToolResult("Summary unavailable: " + exc.detail,
                        kind="read", state="unverified", error_code=exc.code)
                text = "Program summary prepared. Select its read_answer; other user obligations still apply."
            elif arguments.get("deliver"):
                facts = result.content
                projection = StructuredQueryAnswer(collection="contacts", fields=("name", "email"),
                    records=tuple((row["name"], row["email"]) for row in rows), source="Contacts REST collection",
                    actor_digest=context.operation.principal.actor_authority_digest,
                    record_ids=tuple(str(row["id"]) for row in rows),
                    scope=facts["scope"], observed_at=datetime.now(timezone.utc).isoformat(),
                    source_complete=facts["completeness"]["source_complete"],
                    result_truncated=facts["completeness"]["result_truncated"],
                    matched_count=facts["matched_count"], source_total=facts["source_total"],
                    records_scanned=facts["source_total"],
                    incomplete_reason=facts["completeness"]["incomplete_reason"])
            return LoopToolResult(text, kind="read", read_summary=(self.name, text[:1500]), query_answer=projection,
                                  answer_targets=tuple(targets))
        if self.name == "goto":
            outcome = context.page.execute("goto", {"url": self.adapter.descriptor.origin + arguments["path"]},
                                           navigating=True)
            if outcome.state == "completed" and arguments.get("open"):
                controls = [e for e in context.obs.elements if e.role == "link"
                            and e.name == arguments["open"] and e.attrs.get("href") == "/contacts/new"]
                if len(controls) != 1:
                    return LoopToolResult("No unique reviewed form entry.", kind="page", state="refused",
                                          error_code="invalid_target")
                outcome = context.page.execute("click", {"ref": controls[0].ref})
        elif self.name == "fill_fields":
            from web_operation_agent.execution.draft_handoff import add_draft_lines, field_identity, handoff_draft
            if arguments.get("then_click") and arguments.get("then_highlight"):
                return LoopToolResult("Choose query completion or form handoff.", kind="page", state="refused",
                                      error_code="invalid_arguments")
            if arguments.get("add_lines"):
                addition = arguments["add_lines"]
                failed = add_draft_lines(context, addition["button"], addition["count"])
                if failed:
                    return failed
            before, expected = context.obs, []
            for field in arguments["fields"]:
                matches = [e for e in context.obs.elements if e.attrs.get("field_label") == field["label"]]
                if len(matches) != 1:
                    return LoopToolResult("No unique current field.", kind="page", state="refused",
                                          error_code="invalid_target")
                control = matches[0]
                action = "select" if control.attrs.get("aria-haspopup") == "listbox" else "fill"
                outcome = context.page.execute(action, {"ref": control.ref, "value": field["value"],
                    **({"option_label": field["value"]} if action == "select" else {"commit": True})})
                if outcome.state != "completed":
                    labels = outcome.receipt.candidate_labels if outcome.receipt else ()
                    return LoopToolResult("Field entry refused.\nBEGIN_UNTRUSTED_APPLICATION_DATA\n" +
                        redact_sensitive_assignments(json.dumps(labels)) + "\nEND_UNTRUSTED_APPLICATION_DATA",
                        kind="page", state=outcome.state, dispatch=outcome.dispatch, error_code=outcome.error_code)
                expected.append((field_identity(control), field["value"]))
            if arguments.get("then_click"):
                matches = [e for e in context.obs.elements if e.tag == "button"
                           and e.name == arguments["then_click"]]
                if len(matches) != 1:
                    return LoopToolResult("No unique query control.", kind="page", state="refused",
                                          error_code="invalid_target")
                outcome = context.page.execute("click", {"ref": matches[0].ref})
                return LoopToolResult("Query action verified.", kind="page", state=outcome.state,
                    dispatch=outcome.dispatch, error_code=outcome.error_code,
                    completion_command=outcome.receipt.command_id if outcome.state == "completed" else "")
            if arguments.get("then_highlight"):
                return handoff_draft(context, before, expected, arguments["then_highlight"])
            return LoopToolResult("Fields verified.", kind="page", entered=tuple(
                (f["label"], f["value"]) for f in arguments["fields"]))
        elif self.name in {"input", "click"}:
            element = next((e for e in context.obs.elements if e.ref == "e" + str(arguments["index"])), None)
            if element is None:
                return LoopToolResult("Control unavailable.", kind="page", state="refused")
            command = {"ref": element.ref}
            if self.name == "input":
                command.update(value=arguments["text"], commit=True)
            outcome = context.page.execute("fill" if self.name == "input" else "click", command)
        elif self.name == "highlight":
            outcome = context.page.execute("highlight", {"ref": "e" + str(arguments["index"]),
                                                         "epoch": context.obs.epoch})
        else:
            return LoopToolResult("This application does not offer that capability.", state="unavailable")
        return LoopToolResult("Page action completed." if outcome.state == "completed" else
                              "Page action failed; check the error code and current observation.",
                              kind="page", state=outcome.state, dispatch=outcome.dispatch,
                              error_code=outcome.error_code)


def contacts_profile(adapter, goal_port, *, drafts=False, filters=False):
    schemas = (
        (tool("list_contacts", "Read actor-visible contacts from the REST collection.",
              {"name_contains": {"type": "string", "maxLength": 120},
               "deliver": {"type": "boolean", "description": "Prepare exact server delivery; select finish.read_answer."},
               "summary": summary_schema()}), "read", frozenset()),
        (tool("goto", "Open a reviewed page route.", {"path": {"type": "string", "maxLength": 200},
              "open": {"type": "string", "maxLength": 200},
              "answer": {"type": "string", "maxLength": 2000}}, ("path",)), "page", frozenset()),
        (tool("click", "Click a reviewed section or local line control. Give answer only for a final section selection.",
              {"index": {"type": "integer", "minimum": 0}, "answer": {"type": "string", "maxLength": 2000}},
              ("index",)), "page", frozenset()),
        (tool("highlight", "Mark a visible control without clicking it.",
              {"index": {"type": "integer", "minimum": 0}, "answer": {"type": "string", "maxLength": 2000}},
              ("index",)), "page", frozenset({"handoffs"})))
    if drafts or filters:
        schemas += (
            (tool("fill_fields", "Enter named local fields; optionally verify the whole form and highlight Save, never submit.",
                  {"fields": {"type": "array", "minItems": 1, "maxItems": 12, "items": {
                      "type": "object", "properties": {"label": {"type": "string"}, "value": {"type": "string"}},
                      "required": ["label", "value"], "additionalProperties": False}},
                   "then_highlight": {"type": "string"}, "answer": {"type": "string", "maxLength": 2000}},
                  ("fields",)), "page", frozenset()),
            (tool("input", "Enter one reviewed local field and commit it.",
                  {"index": {"type": "integer", "minimum": 0}, "text": {"type": "string", "maxLength": 4000}},
                  ("index", "text")), "page", frozenset({"fill"})))
    for schema, _, _ in schemas:
        if schema["function"]["name"] == "fill_fields":
            properties = schema["function"]["parameters"]["properties"]
            properties["then_click"] = {"type": "string"}
            properties["add_lines"] = {"type": "object", "properties": {
                "button": {"type": "string", "maxLength": 200},
                "count": {"type": "integer", "minimum": 1, "maximum": 4}},
                "required": ["button", "count"], "additionalProperties": False}

    def requirements(arguments, observation):
        query = observation.page_route == "/contacts/filter"
        return frozenset({"facets" if query else "fill"}) | (
            frozenset({"lines"}) if arguments.get("add_lines") else frozenset()) | (
            frozenset({"handoffs"}) if arguments.get("then_highlight") else frozenset())

    def click_requirements(arguments, observation):
        target = next((e for e in observation.elements if e.ref == "e" + str(arguments["index"])), None)
        return frozenset({"sections" if target is not None and target.role == "tab" else "lines"})

    procedures = tuple(LoopProcedure(json.dumps(schema),
        ContactsProcedure(adapter, goal_port, schema["function"]["name"]), kind, "contacts:read", required,
        requirements if schema["function"]["name"] == "fill_fields" else
        click_requirements if schema["function"]["name"] == "click" else None)
        for schema, kind, required in schemas)

    def prepare(request, context):
        goal_port._require_actor(context.operation)
        return "Read the collection under the current actor; do not infer access to another workspace."

    def render(observation, marked):
        return redact_sensitive_assignments(json.dumps({"url": observation.url, "epoch": observation.epoch,
            "route": observation.page_route, "state": observation.render_state,
            "controls": [{"index": e.ref, "role": e.role, "name": e.name, "attrs": e.attrs} for e in observation.elements],
            "marked": marked}, ensure_ascii=False))

    from .draft_steps import ContactsDraftSteps
    from .mutation_prefill import ContactsMutationPrefill
    return ModelLoopProfile("Answer from current REST reads. Clarify ambiguous names; never choose one silently. "
        "For counts, groups or lists, list_contacts accepts summary over id, name and email. "
        "The server calculates and renders the selected read_answer; keep finish.answer empty. "
        "This application offers reviewed navigation and visible-control handoff. " +
        ("It offers two local date fields, priority, and temporary note lines for manual saving. "
         "Use goto(open=\"New contact\") to open the form, then fill_fields with then_highlight=\"Save contact\" "
         "and answer to finish after verified handoff. Use add_lines(button, count) within fill_fields to create local lines first. " if drafts else "") +
        ("On /contacts/filter fill Name contains and use then_click=\"Search contacts\" with answer to finish after a verified query. "
         if filters else "It does not offer list filters. ") +
        "It does not offer pagination. Source tools depend on deployment configuration.",
        procedures, render, prepare, draft_steps=ContactsDraftSteps(adapter) if drafts else None,
        mutation_prefill=(ContactsMutationPrefill(adapter)
                          if drafts and adapter.ports.operations is not None else None))
