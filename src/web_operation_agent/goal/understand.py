"""One model call: the person's words, the application index, one goal declaration.

The model is not choosing among tools or planning steps here. It is translating a request
into the index's vocabulary, which is the one thing in this turn that needs language.

The prompt is kept small on purpose. With the index at about 20 KB a small model declared,
on a fifth of the development requests, that "the message contains only the index", or
misread the record numbers in the request -- whichever message the request was placed in,
cached prefix or not. At 15 KB the same model read every request. The index is therefore
compiled compactly (an adapter's concern), the system text below stays short, and new
declaration vocabulary goes into the field descriptions of `spec.DECLARE_GOAL_TOOL` rather
than into more prose here.
"""
from __future__ import annotations

import json

from .index import ApplicationIndex
from .spec import DECLARE_GOAL_TOOL, GoalSpec

UNDERSTAND_SYSTEM = """You translate one request to a business web application into a goal declaration by calling declare_goal exactly once.

Use only names that appear in the APPLICATION INDEX: entity keys, field labels, page keys, filter names, group names, section names and action names. The person may write in another language than the index; map their words to the index's names (for example a word meaning "quotations" maps to the Quotations page, a word meaning "customer" to the Customer field). Copy record references (order numbers, invoice numbers, names) exactly as the person wrote them. Never invent a name that is not in the index; if nothing fits, leave deliverables empty and say why in `unsupported`.

Deliverable kinds:
- fact: the value of one or more fields of one named record. Set entity, record and fields. If the record is reached through a link field of the named record (the customer of order X), set entity and record to what the person named, via to the link field's label, and fields to labels of the linked entity.
- aggregate: a count or a sum over records with conditions. Set entity, page.key to the list page whose scope applies (for example invoices to customers versus bills from vendors; confirmed orders versus quotations), metric.op, metric.field for a sum, metric.filters as field label plus values (use choice labels from the index, or the person's own words for a name), metric.group_by with metric.top for "which X has the most".
- list: which records match a description. Set entity, page.key and page.search with the search words translated into the index's language (the index is in English: a word meaning "chair" becomes "Chair").
- page: show a page. For a list page set page.key with view, filters_on, filters_off, group_by as asked. For a record's page set entity and record (and via when a linked record is meant) and section when a part of the record is asked for. A question asking where something is kept, viewed or configured is a page deliverable for that page.
- handoff: the person asks to change, create, confirm, send, pay, cancel or register something. Set entity, record and action (an action name from the index) so the control can be shown to the person; set write_intent true. The assistant never performs the change itself.

Linked records: when the person names one record but wants another that it links to -- the customer of an order, the vendor of a bill, the payment terms an invoice uses -- entity and record are always the record they named, and via is the link field's label on that entity. "Show me the customer's details / profile / record" is a page deliverable with via; "what are the payment terms' rules" is a page deliverable with via, because the linked record's own page shows its rules.

A request that asks several things gets one deliverable per thing, in the order asked. A question that also names a record to look at gets both a fact and a page deliverable. Prefer the most specific page (a named record's page over a list page)."""


def understand(chat, *, prompt: str, index: ApplicationIndex, run_id: str,
               timeout_s: float | None = None, cancelled=None) -> tuple[GoalSpec, dict]:
    """Ask once. Returns the parsed declaration and a bounded audit projection."""
    messages = [
        {"role": "system", "content": UNDERSTAND_SYSTEM},
        {"role": "user", "content": ("APPLICATION INDEX\n" + index.render()
                                     + "\n\nREQUEST\n" + prompt.strip())},
    ]
    kwargs = {"max_tokens": 1_500, "timeout_s": timeout_s}
    if cancelled is not None:
        reply = chat.complete_cancellable(messages, [DECLARE_GOAL_TOOL], run_id,
                                          cancelled=cancelled, **kwargs)
    else:
        reply = chat.complete(messages, [DECLARE_GOAL_TOOL], run_id, **kwargs)
    payload, source = _declaration_from(reply)
    spec = GoalSpec.from_json(payload)
    return spec, {"source": source, "deliverables": len(spec.deliverables),
                  "kinds": [d.kind for d in spec.deliverables],
                  "write_intent": spec.write_intent}


def _declaration_from(reply: dict) -> tuple[dict, str]:
    calls = reply.get("tool_calls") or []
    for call in calls:
        function = call.get("function") or {}
        if function.get("name") != "declare_goal":
            continue
        raw = function.get("arguments") or "{}"
        try:
            payload = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError("declare_goal arguments are not valid JSON") from exc
        return payload, "tool_call"
    content = str(reply.get("content") or "").strip()
    if content:
        start, end = content.find("{"), content.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(content[start:end + 1]), "content"
            except json.JSONDecodeError:
                pass
    raise ValueError("the model did not declare a goal")


__all__ = ["UNDERSTAND_SYSTEM", "understand"]
