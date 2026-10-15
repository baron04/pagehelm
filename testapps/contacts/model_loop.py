"""Contacts REST/plain-HTML composition of the common model-driven turn."""
import json

from web_operation_agent.execution.model_loop_contracts import (
    LoopProcedure, LoopToolResult, ModelLoopProfile)
from web_operation_agent.privacy import redact_sensitive_assignments


def tool(name, description, properties, required=()):
    return {"type": "function", "function": {"name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": list(required),
                       "additionalProperties": False}}}


class ContactsModelGoalPort:
    """Capabilities implemented by this profile, independently of the goal planner."""
    def __init__(self, adapter, *, drafts=False):
        from .goal import ContactsGoalPort
        self._port, self.drafts = ContactsGoalPort(adapter), drafts

    def __getattr__(self, name):
        return getattr(self._port, name)

    def capabilities(self):
        return frozenset({"handoffs"} | ({"fill", "lines"} if self.drafts else set()))


class ContactsProcedure:
    def __init__(self, adapter, goal_port, name):
        self.adapter, self.goal_port, self.name = adapter, goal_port, name

    def execute(self, arguments, *, context):
        context.page.checkpoint()
        self.goal_port._require_actor(context.operation)
        if self.name == "list_contacts":
            result = self.adapter.business_tools._executor.execute(self.name, arguments, context=context.operation)
            context.page.checkpoint()
            text = "BEGIN_UNTRUSTED_APPLICATION_DATA\n" + redact_sensitive_assignments(result.render()) \
                + "\nEND_UNTRUSTED_APPLICATION_DATA"
            return LoopToolResult(text, kind="read", read_summary=(self.name, text[:1500]))
        if self.name == "goto":
            outcome = context.page.execute("goto", {"url": self.adapter.descriptor.origin + arguments["path"]},
                                           navigating=True)
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


def contacts_profile(adapter, goal_port, *, drafts=False):
    schemas = (
        (tool("list_contacts", "Read actor-visible contacts from the REST collection.",
              {"name_contains": {"type": "string", "maxLength": 120}}), "read", frozenset()),
        (tool("goto", "Open a reviewed page route.", {"path": {"type": "string", "maxLength": 200},
              "answer": {"type": "string", "maxLength": 2000}}, ("path",)), "page", frozenset()),
        (tool("highlight", "Mark a visible control without clicking it.",
              {"index": {"type": "integer", "minimum": 0}, "answer": {"type": "string", "maxLength": 2000}},
              ("index",)), "page", frozenset({"handoffs"})))
    if drafts:
        schemas += (
            (tool("input", "Enter one reviewed local field and commit it.",
                  {"index": {"type": "integer", "minimum": 0}, "text": {"type": "string", "maxLength": 4000}},
                  ("index", "text")), "page", frozenset({"fill"})),
            (tool("click", "Add one local line; never save.",
                  {"index": {"type": "integer", "minimum": 0}}, ("index",)), "page", frozenset({"lines"})))
    procedures = tuple(LoopProcedure(json.dumps(schema),
        ContactsProcedure(adapter, goal_port, schema["function"]["name"]), kind, "contacts:read", required)
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
    return ModelLoopProfile("Answer from current REST reads. Clarify ambiguous names; never choose one silently. "
        "This application offers reviewed navigation and visible-control handoff. " +
        ("It offers two local date fields and temporary note lines for manual saving. " if drafts else "") +
        "It does not offer list filters or pagination. Source tools depend on deployment configuration.",
        procedures, render, prepare, draft_steps=ContactsDraftSteps(adapter) if drafts else None)
