"""Bind declared inputs to exact user text without another model interpretation.

Source provenance proves copying, not semantic completeness or write authority.
"""
from __future__ import annotations

from copy import deepcopy
import json
import re

from ..business.errors import BusinessToolRequestError
from ..business.tools.registry import validate_input

_QUOTED_TEXT = re.compile(r'''"([^"\n]*)"|(?<!\w)'([^'\n]*)'(?!\w)|“([^”]*)”|‘([^’]*)’|「([^」]*)」|『([^』]*)』|«([^»]*)»''')
_LITERAL_TOKEN = re.compile(r"[A-Za-z0-9_]+(?:[.@+\-][A-Za-z0-9_]+)*")


def _spans(text):
    quoted = [match.span(group) for match in _QUOTED_TEXT.finditer(text)
              for group in range(1, 8) if match.group(group) is not None]
    return quoted, [match.span() for match in _LITERAL_TOKEN.finditer(text)]


def literal_supplied(value, user_texts):
    """Reject invented copies and partial quotes/tokens, without interpreting intent."""
    for text in user_texts:
        if not isinstance(text, str):
            continue
        quoted, tokens = _spans(text)
        for match in re.finditer(re.escape(value), text):
            span = start, end = match.span()
            if any(left <= start and end <= right and span != (left, right)
                   for left, right in quoted):
                continue
            if span in quoted or span in tokens:
                return True
            if not any(left < start < right or left < end < right for left, right in tokens):
                return True
    return False


def user_texts(request, conversation=None):
    return (request, *(item.get("user", "") for item in (conversation or {}).get("prior_turns", [])
        if isinstance(item, dict))) if isinstance(conversation, dict) else (request,)


def require_literal_inputs(inputs, definition, texts):
    """Every plain literal input is complete text the person supplied, found by the server.

    For a proposer that writes the installed inputs directly, without the planning source
    envelope. The person's own messages are the only anchor; an empty string remains an
    explicit clearing and missing fields are left to the installed schema.
    """
    for path in definition.literal_inputs:
        target = inputs
        keys = path.split(".")
        for key in keys[:-1]:
            target = target.get(key) if isinstance(target, dict) else None
        if not isinstance(target, dict) or keys[-1] not in target:
            continue
        value = target[keys[-1]]
        if isinstance(value, str) and value and not literal_supplied(value, texts):
            raise BusinessToolRequestError(
                f"input.{path} must copy a value supplied by the person in full, preserving case")


def bind_input_sources(inputs, definition, texts):
    """Resolve literal source references before validating the installed inputs.

    The task declaration is the only semantic reading. References can select
    current or bounded historical user text, never assistant or application data.
    A source reference cannot change case or copy a visibly truncated value.
    """
    result = deepcopy(inputs)
    for path in definition.literal_inputs:
        target = result
        keys = path.split(".")
        for key in keys[:-1]:
            target = target.get(key) if isinstance(target, dict) else None
        if not isinstance(target, dict) or keys[-1] not in target:
            continue  # The installed schema diagnoses missing required fields.
        source = target[keys[-1]]
        if (not isinstance(source, dict) or set(source) != {"source", "text"}
                or type(source["source"]) is not int or not 0 <= source["source"] < len(texts)
                or not isinstance(source["text"], str)):
            raise BusinessToolRequestError(f"input.{path} requires a user-text source reference")
        value = source["text"]
        if value and not literal_supplied(value, (texts[source["source"]],)):
            raise BusinessToolRequestError(f"input.{path} must copy a value supplied by the person in full, preserving case")
        target[keys[-1]] = value
    validate_input(json.loads(definition.input_json), result)
    return result
