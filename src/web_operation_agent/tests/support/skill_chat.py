"""Readable scripted declarations using explicit user-text source references."""
import json

from .business import _Chat


class SkillDeclarationChat(_Chat):
    """The script uses installed inputs; emit their declaration wire format.

    Tests of malformed source references supply the raw object unchanged. This
    helper does not interpret requests, fabricate values or choose an intent.
    A native operation in the script represents an explicit execute proposal.
    """
    def complete(self, messages, tools, run_id, **kwargs):
        reply = super().complete(messages, tools, run_id, **kwargs)
        schemas = {tool["function"]["name"]: tool["function"]["parameters"] for tool in tools}
        for call in reply.get("tool_calls", []):
            schema = schemas.get(call["function"]["name"], {})
            if "request_intent" not in schema.get("properties", {}):
                continue
            inputs = json.loads(call["function"]["arguments"])
            if {"request_intent", "inputs"} <= set(inputs):
                continue  # Raw wire responses are not interpreted by this fixture.
            def sources(value, field):
                if not isinstance(value, dict):
                    return
                for key, item in list(value.items()):
                    spec = field.get("properties", {}).get(key, {})
                    if set(spec.get("properties", {})) == {"source", "text"}:
                        if isinstance(item, str):
                            value[key] = {"source": 0, "text": item}
                    elif isinstance(item, dict):
                        sources(item, spec)
            sources(inputs, schema["properties"]["inputs"])
            call["function"]["arguments"] = json.dumps({"request_intent": "execute", "inputs": inputs,
                **({"request_part": 0} if "request_part" in schema["properties"] else {})})
        return reply
