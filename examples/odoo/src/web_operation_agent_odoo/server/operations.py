"""Typed definitions reuse the application's reviewed RPC and wizard handlers."""
from copy import deepcopy
from dataclasses import replace
import json

from web_operation_agent.business.tools.contracts import ToolOperation
from web_operation_agent.business.tools.registry import ToolDefinition, ToolRegistry
from web_operation_agent.schemas.approval_details import ApprovalDetail

from .specs import MUTATION_SPECS


class OdooOperation:
    def __init__(self, executor, name):
        self.executor, self.name = executor, name

    def prepare(self, args, *, context=None):
        spec = self.executor.prepare_mutation(self.name, args, context=context)
        return replace(spec, details=tuple(
            ApprovalDetail("change", "Approved effect", target) for target in spec.targets))

    def dispatch(self, args, *, context=None):
        return self.executor.execute_mutation(self.name, args, context=context)


def operation_registry(executor, operations):
    definitions = []
    if executor.allow_mutations:
        for spec in MUTATION_SPECS:
            function = deepcopy(spec.schema["function"])
            # The existing application normalizer enforces the email format. The
            # core's deliberately small schema subset carries its type and bounds.
            for field in function["parameters"]["properties"].values():
                field.pop("format", None)
            entity, operation = operations[spec.name]
            definitions.append(ToolDefinition(spec.name, function["description"],
                json.dumps(function["parameters"]), "write", spec.write_scope,
                ToolOperation(entity, operation), OdooOperation(executor, spec.name)))
    return ToolRegistry(tuple(definitions))


class OdooRegisteredTools:
    def __init__(self, executor, registry):
        self.executor, self.registry = executor, registry
    def tool_schemas(self):
        reads = tuple(schema for schema in self.executor.tool_schemas()
                      if self.executor.tool_effects()[schema["function"]["name"]] == "read")
        return (*reads, *self.registry.tool_schemas())
    def tool_effects(self):
        return {schema["function"]["name"]: self.executor.tool_effects()[schema["function"]["name"]]
                for schema in self.tool_schemas()}
    def execute(self, name, args, *, context=None):
        return self.executor.execute(name, args, context=context)
    def prepare_mutation(self, name, args, *, context=None):
        return self.registry.prepare_mutation(name, args, context=context)
    def execute_mutation(self, name, args, *, context=None):
        return self.registry.execute_mutation(name, args, context=context)
