"""One immutable definition per installed tool, independent of task knowledge."""
from __future__ import annotations

import json
import re
import math
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol

from ..errors import BusinessToolRequestError
from .contracts import BusinessToolResult, BusinessMutationSpec, ToolOperation
from ..effects import EffectContract


class OperationHandler(Protocol):
    def execute(self, args: dict, *, context=None) -> BusinessToolResult: ...
    def prepare(self, args: dict, *, context=None) -> BusinessMutationSpec: ...
    def dispatch(self, args: dict, *, context=None) -> BusinessToolResult: ...


def validate_input(schema: dict, value, path="input") -> None:
    """Validate the bounded JSON-schema subset accepted by installed typed tools.

    Fail at installation for unsupported keywords rather than silently treating them
    as validation. This is not a general JSON-schema interpreter.
    """
    if "anyOf" in schema:
        for branch in schema["anyOf"]:
            try:
                validate_input(branch, value, path)
                return
            except BusinessToolRequestError:
                pass
        raise BusinessToolRequestError(f"{path} does not match an allowed input shape")
    kind = schema["type"]
    valid = {"object": lambda: isinstance(value, dict),
             "array": lambda: isinstance(value, list),
             "string": lambda: isinstance(value, str),
             "integer": lambda: type(value) is int,
             "number": lambda: type(value) in (int, float) and math.isfinite(value),
             "boolean": lambda: type(value) is bool,
             "null": lambda: value is None}[kind]()
    if not valid:
        raise BusinessToolRequestError(f"{path} must be {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise BusinessToolRequestError(f"{path} has an unsupported value")
    if kind == "object":
        properties = schema["properties"]
        missing = sorted(set(schema.get("required", ())) - set(value))
        unsupported = len(set(value) - set(properties))
        if missing or unsupported:
            # Only installed schema names cross the error boundary. Unexpected
            # keys can themselves contain private values, so report their count.
            detail = ("missing required fields: " + ", ".join(missing)) if missing else ""
            if unsupported:
                detail += ("; " if detail else "") + f"unsupported field count: {unsupported}"
            raise BusinessToolRequestError(f"{path} has invalid object fields ({detail})")
        for key, item in value.items():
            validate_input(properties[key], item, f"{path}.{key}")
    elif kind == "array":
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 100):
            raise BusinessToolRequestError(f"{path} exceeds its item bounds")
        for item in value:
            validate_input(schema["items"], item, path + "[]")
    elif kind == "string":
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 32000):
            raise BusinessToolRequestError(f"{path} exceeds its text bounds")
    elif kind in {"number", "integer"}:
        if not schema.get("minimum", float("-inf")) <= value <= schema.get("maximum", float("inf")):
            raise BusinessToolRequestError(f"{path} exceeds its numeric bounds")


def _validate_schema(schema):
    if isinstance(schema, dict) and "anyOf" in schema:
        if (set(schema) != {"anyOf"} or not isinstance(schema["anyOf"], list)
                or not 2 <= len(schema["anyOf"]) <= 6):
            raise ValueError("typed-tool alternatives must have 2–6 explicit branches")
        for branch in schema["anyOf"]:
            _validate_schema(branch)
        return
    keywords = {"type", "description", "properties", "required", "additionalProperties",
                "items", "minItems", "maxItems", "minLength", "maxLength",
                "minimum", "maximum", "enum"}
    if not isinstance(schema, dict) or set(schema) - keywords or schema.get("type") not in {
            "object", "array", "string", "integer", "number", "boolean", "null"}:
        raise ValueError("unsupported typed-tool input schema")
    if "description" in schema and (not isinstance(schema["description"], str) or len(schema["description"]) > 4000):
        raise ValueError("invalid typed-tool schema description")
    if "enum" in schema:
        enum = schema["enum"]
        if not isinstance(enum, list) or not 1 <= len(enum) <= 100:
            raise ValueError("typed-tool enum must be bounded")
        for item in enum:
            validate_input({k: v for k, v in schema.items() if k != "enum"}, item)
    for lower, upper in (("minLength", "maxLength"), ("minItems", "maxItems"), ("minimum", "maximum")):
        for key in (lower, upper):
            if key in schema and (type(schema[key]) not in (int, float) or not math.isfinite(schema[key])):
                raise ValueError("invalid typed-tool schema bound")
            if key in schema and key not in {"minimum", "maximum"} and (type(schema[key]) is not int or schema[key] < 0):
                raise ValueError("typed-tool length bounds must be nonnegative integers")
        if lower in schema and upper in schema and schema[lower] > schema[upper]:
            raise ValueError("reversed typed-tool schema bounds")
    if schema["type"] == "object":
        if not isinstance(schema.get("properties"), dict) or schema.get("additionalProperties") is not False:
            raise ValueError("typed-tool objects must enumerate properties and reject extras")
        required = schema.get("required", [])
        if not isinstance(required, list) or any(not isinstance(k, str) for k in required) or len(set(required)) != len(required):
            raise ValueError("typed-tool required fields must be unique strings")
        if any(not isinstance(k, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", k) for k in schema["properties"]):
            raise ValueError("invalid typed-tool property identity")
        if set(schema.get("required", ())) - set(schema["properties"]):
            raise ValueError("typed-tool required fields are not properties")
        for child in schema["properties"].values():
            _validate_schema(child)
    if schema["type"] == "array":
        _validate_schema(schema.get("items"))


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    input_json: str
    effect: str
    scope: str
    operation: ToolOperation
    handler: OperationHandler
    version: str = "1"
    reference_inputs: tuple[str, ...] = ()
    literal_inputs: tuple[str, ...] = ()
    # Reviewed input -> application vocabulary. These bindings describe request
    # coverage; they grant neither a capability nor permission to mutate.
    input_fields: tuple[tuple[str, str, str], ...] = ()
    effect_contract: EffectContract | None = None
    scope_entities: tuple[str, ...] = ()
    preservation_fields: tuple[str, ...] = ()
    preserves_unmentioned: bool = False

    def __post_init__(self):
        if self.effect_contract is not None and (self.effect != "write" or
                not callable(getattr(self.effect_contract, "project_inputs", None))):
            raise ValueError("effect contract requires a write and an input projection")
        if (not isinstance(self.preservation_fields, tuple) or len(self.preservation_fields) > 80
                or any(not isinstance(field, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", field)
                       for field in self.preservation_fields)
                or len(set(self.preservation_fields)) != len(self.preservation_fields)
                or type(self.preserves_unmentioned) is not bool):
            raise ValueError("preservation requires bounded application field identities")
        if (self.preservation_fields or self.preserves_unmentioned) and self.effect_contract is None:
            raise ValueError("preservation requires an application effect contract")
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", self.name):
            raise ValueError("invalid tool identity")
        if self.effect not in {"read", "write"} or not isinstance(self.operation, ToolOperation):
            raise ValueError("invalid tool effect or operation")
        if (self.effect == "read") != (self.operation.operation == "read"):
            raise ValueError("tool operation and effect disagree")
        if not re.fullmatch(r"[a-z][a-z0-9:._-]{1,99}", self.scope):
            raise ValueError("tool requires an actor scope")
        schema = json.loads(self.input_json)
        _validate_schema(schema)
        if schema["type"] != "object":
            raise ValueError("tool arguments must be an object")
        if not isinstance(self.version, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", self.version):
            raise ValueError("invalid tool version")
        if (not isinstance(self.reference_inputs, tuple) or len(set(self.reference_inputs)) != len(self.reference_inputs)):
            raise ValueError("reference inputs must be unique field paths")
        for path in self.reference_inputs:
            if not isinstance(path, str):
                raise ValueError("reference input paths must be strings")
            field = schema
            for key in path.split("."):
                field = field.get("properties", {}).get(key, {})
            if not (field.get("type") == "string" or
                    field.get("type") == "array" and field.get("items", {}).get("type") == "string"):
                raise ValueError("reference inputs must name strings or arrays of strings")
        if not isinstance(self.literal_inputs, tuple) or len(set(self.literal_inputs)) != len(self.literal_inputs):
            raise ValueError("literal inputs must be unique field paths")
        for path in self.literal_inputs:
            if not isinstance(path, str):
                raise ValueError("literal input paths must be strings")
            field = schema
            for key in path.split("."):
                field = field.get("properties", {}).get(key, {})
            if field.get("type") != "string" or path in self.reference_inputs:
                raise ValueError("literal inputs must name string fields distinct from references")
        if not isinstance(self.input_fields, tuple) or len(self.input_fields) > 80:
            raise ValueError("input field bindings must be bounded immutable metadata")
        seen_fields = set()
        for binding in self.input_fields:
            if (not isinstance(binding, tuple) or len(binding) != 3
                    or any(not isinstance(v, str) or not 0 < len(v) <= 80 for v in binding)
                    or binding in seen_fields):
                raise ValueError("invalid input field binding")
            path, entity, key = binding
            field = schema
            for item in path.split("."):
                field = field.get("properties", {}).get(item, {})
            if not field or self.effect != "write":
                raise ValueError("input field binding must name an installed write input")
            seen_fields.add(binding)
        if (not isinstance(self.scope_entities, tuple) or len(self.scope_entities) > 80
                or len(set(self.scope_entities)) != len(self.scope_entities)
                or any(not isinstance(entity, str) or not 0 < len(entity) <= 80
                       for entity in self.scope_entities)):
            raise ValueError("operation scope entities must be bounded unique identities")
        # Additional target identities must not replace the identities of the
        # operation's own fields. Either may be the scope reader's correct choice.
        entities = tuple(dict.fromkeys((*self.scope_entities,
            *(entity for _, entity, _ in self.input_fields))))
        if not entities:
            entities = (self.operation.entity,)
        if len(entities) > 80:
            raise ValueError("operation scope entities exceed their bound")
        object.__setattr__(self, "scope_entities", entities)
        for method in (("execute",) if self.effect == "read" else ("prepare", "dispatch")):
            if not callable(getattr(self.handler, method, None)):
                raise ValueError("tool handler lacks its effect contract")
        object.__setattr__(self, "input_json", json.dumps(schema, sort_keys=True, allow_nan=False))

    @property
    def schema(self):
        return {"type": "function", "function": {"name": self.name,
                "description": self.description, "parameters": json.loads(self.input_json)}}


class ToolRegistry:
    def __init__(self, definitions: tuple[ToolDefinition, ...], *, mutation_dispatch_enabled: bool = True):
        if type(mutation_dispatch_enabled) is not bool:
            raise TypeError("mutation_dispatch_enabled must be boolean")
        self._mutation_dispatch_enabled = mutation_dispatch_enabled
        tools = {t.name: t for t in definitions}
        if len(tools) != len(definitions):
            raise ValueError("duplicate installed tool")
        self._tools = MappingProxyType(tools)

    @property
    def definitions(self): return tuple(self._tools.values())

    def get(self, name):
        try:
            return self._tools[name]
        except KeyError as exc:
            raise BusinessToolRequestError("tool is not installed") from exc

    def tool_schemas(self): return [t.schema for t in self.definitions]
    def tool_effects(self): return {t.name: t.effect for t in self.definitions}

    def execute(self, name, args, *, context=None):
        tool = self.get(name)
        if tool.effect != "read":
            raise BusinessToolRequestError("mutation requires preparation and approval")
        validate_input(json.loads(tool.input_json), args)
        return tool.handler.execute(args, context=context)

    def prepare_mutation(self, name, args, *, context=None):
        tool = self.get(name)
        if tool.effect != "write":
            raise BusinessToolRequestError("tool is read only")
        validate_input(json.loads(tool.input_json), args)
        return tool.handler.prepare(args, context=context)

    def execute_mutation(self, name, args, *, context=None):
        # Prepared private arguments need not have the public input shape. Only the
        # write service may reach this internal handler after digest-bound approval.
        tool = self.get(name)
        if tool.effect != "write":
            raise BusinessToolRequestError("tool is read only")
        if not self._mutation_dispatch_enabled:
            raise BusinessToolRequestError("mutation dispatch is disabled for preparation")
        return tool.handler.dispatch(args, context=context)
