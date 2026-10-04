"""Small redacted server projections for goal model reference context.

These contain ordinary dialogue and already-observed page text, never action
references, execution authority or current actor facts from a prior turn.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

from ..privacy import SessionHandoffPolicy, redact_sensitive_assignments
from ..schemas.browser import PageObservation


_CONVERSATION_BYTES = 8 * 1024
_PAGE_CONTEXT_BYTES = 4 * 1024
_PAGE_MARKUP = re.compile(r"(?:^|\n)\s*\*?\[\d+\]\s*<|<\s*/?\s*[A-Za-z][\w-]*(?:\s|/?>|$)")
_CONTROL_ROLES = frozenset({"button", "textbox", "searchbox", "combobox", "spinbutton",
                            "checkbox", "radio", "switch", "tab", "slider"})


def _json_bytes(value) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _context_text(value, *, handoffs, chars, bytes_limit):
    """Redact before clipping; keep spelling, quotation and whitespace intact."""
    text = redact_sensitive_assignments(handoffs.redact(value))
    clipped = text[:chars]
    # Escaped quotes/control characters also consume provider bytes. Bound the
    # serialized string, not just its raw bytes, without splitting a character.
    if _json_bytes(clipped) > bytes_limit:
        low, high = 0, len(clipped)
        while low < high:
            middle = (low + high + 1) // 2
            if _json_bytes(clipped[:middle]) <= bytes_limit:
                low = middle
            else:
                high = middle - 1
        clipped = clipped[:low]
    return clipped, clipped != text


def conversation_context(context, *, handoffs=None):
    """Ordinary prior dialogue for resolving references, never prior typed facts.

    Consume the server-owned ConversationState projection, then apply this stage's
    smaller text budget. Unknown fields, prior business payloads and clarification
    internals never enter the source model request.
    """
    handoffs = handoffs or SessionHandoffPolicy.extending(())
    raw = list((context or {}).get("prior_turns") or [])
    result = {"prior_turns": [], "truncated": len(raw) > 4}
    for turn in raw[-4:]:
        projected = {}
        for key in ("user", "assistant"):
            value = turn.get(key, "")
            if not isinstance(value, str):
                continue
            projected[key], clipped = _context_text(
                value, handoffs=handoffs, chars=2_000, bytes_limit=2_000)
            result["truncated"] |= clipped
        if projected:
            result["prior_turns"].append(projected)
    while _json_bytes(result) > _CONVERSATION_BYTES:
        result["prior_turns"].pop(0)
        result["truncated"] = True
    return result if result["prior_turns"] else None


def page_context(observation: PageObservation, *, handoffs=None):
    """Small redacted visible-text projection of the fresh runtime observation.

    Structured control attributes, values, action refs and URL query strings
    are not forwarded.
    The text is useful for a phrase such as 'this button', but cannot establish a
    current actor fact or grant authority.
    """
    handoffs = handoffs or SessionHandoffPolicy.extending(())
    # Runtime content is text-DOM, not plain visible prose. Indexed records can
    # contain selectors, input values and multiline or incomplete attributes. Keep
    # only the plain prefix before any markup; names already have a separate typed
    # projection below. Do not parse an untrusted partial record into model context.
    markup = _PAGE_MARKUP.search(observation.content)
    content = observation.content[:markup.start()] if markup else observation.content
    result = {"epoch": observation.epoch, "render_state": observation.render_state,
              "observation_truncated": observation.truncated, "truncated": markup is not None,
              "landmarks": [], "messages": [], "control_names": []}
    for key, value, chars, limit in (
            ("path", urlsplit(observation.url).path, 300, 300),
            ("active_view", observation.active_view, 120, 120),
            ("title", observation.title, 200, 300),
            ("content", content, 2_000, 2_000)):
        result[key], clipped = _context_text(
            value, handoffs=handoffs, chars=chars, bytes_limit=limit)
        result["truncated"] |= clipped
    # Large shells put menu containers before the working page in the observation.
    # Reserve the bounded name projection for actual controls before navigation and
    # container text. Names are only reference hints, never attributes or authority.
    controls = sorted((e for e in observation.elements if e.name),
                      key=lambda e: 0 if e.role in _CONTROL_ROLES else
                                    1 if e.role == "link" else 2)
    control_names = list(dict.fromkeys(e.name for e in controls))
    for key, values, count, limit in (
            ("landmarks", observation.landmarks, 5, 120),
            ("messages", observation.messages, 3, 180),
            ("control_names", control_names, 12, 120)):
        result["truncated"] |= len(values) > count
        for value in values[:count]:
            text, clipped = _context_text(value, handoffs=handoffs, chars=limit, bytes_limit=limit)
            result["truncated"] |= clipped
            result[key].append(text)
            if _json_bytes(result) > _PAGE_CONTEXT_BYTES:
                result[key].pop()
                result["truncated"] = True
                break
    return result
