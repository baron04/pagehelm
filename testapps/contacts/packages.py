"""Standard Skill/operation proof over the synthetic application's existing REST API."""
import json
from dataclasses import replace
from pathlib import Path

from web_operation_agent.skills import SkillRegistry
from web_operation_agent.business.tools.contracts import ToolOperation
from web_operation_agent.business.tools.registry import ToolDefinition, ToolRegistry
from web_operation_agent.composition.business_operations import RegisteredOperations
from web_operation_agent.schemas.approval_details import ApprovalDetail
from web_operation_agent.business.effects import RequestedEffect, EffectProjection, canonical


class ContactCreation:
    def __init__(self, executor): self.executor = executor
    def prepare(self, args, *, context=None):
        spec = self.executor.prepare_mutation("create_contact", args, context=context)
        return replace(spec, details=tuple(ApprovalDetail("change", name, value)
                                          for name, value in spec.args.items()),
            effect_projection=EffectProjection(self.project_inputs(spec.args), required_receipts=("record",)))
    def project_inputs(self, args):
        return RequestedEffect("contacts", "create", values_json=canonical(args))
    def dispatch(self, args, *, context=None):
        result = self.executor.execute_mutation("create_contact", args, context=context)
        contact = result.content["contact"]
        return replace(result, content={**result.content, "verified": True,
            "entity": "contact", "reference": contact["name"], "record_id": contact["id"],
            "effects": [{"effect": "record", "verified": True, "id": contact["id"]}]})


def contact_operations(executor, enabled):
    skills = SkillRegistry.discover(Path(__file__).parent / "packages", namespace="minimal.contacts")
    schema = next((s["function"] for s in executor.tool_schemas()
                   if s["function"]["name"] == "create_contact"), None)
    definitions = () if not enabled else (ToolDefinition("create_contact", schema["description"],
        json.dumps(schema["parameters"]), "write", "contacts:write",
        ToolOperation("contact", "create"), ContactCreation(executor),
        literal_inputs=("name", "email"),
        input_fields=(("name", "contacts", "name"), ("email", "contacts", "email")),
        effect_contract=ContactCreation(executor)),)
    return RegisteredOperations(ToolRegistry(definitions), skills, lambda entities: None,
        observed_mutation_operations=frozenset(d.name for d in definitions))


class ContactsTools:
    def __init__(self, reads, writes): self.reads, self.writes = reads, writes
    def tool_schemas(self):
        return (*[s for s in self.reads.tool_schemas() if s["function"]["name"] != "create_contact"], *self.writes.tool_schemas())
    def tool_effects(self): return {s["function"]["name"]: ("write" if s["function"]["name"] == "create_contact" else "read") for s in self.tool_schemas()}
    def execute(self, name, args, *, context=None): return self.reads.execute(name, args, context=context)
    def prepare_mutation(self, name, args, *, context=None): return self.writes.prepare_mutation(name, args, context=context)
    def execute_mutation(self, name, args, *, context=None): return self.writes.execute_mutation(name, args, context=context)
