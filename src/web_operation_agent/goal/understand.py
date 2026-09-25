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

from ..modeling.provider import ProviderError
from .index import ApplicationIndex
from .spec import DECLARE_GOAL_TOOL, DECLARE_SCOPE_TOOL, GoalSpec, scope_from_json

UNDERSTAND_SYSTEM = """You translate one request to a business web application into a goal declaration by calling declare_goal exactly once.

Use only names that appear in the APPLICATION INDEX: entity keys, field labels, page keys, filter names, group names, section names and action names. The person may write in another language than the index; map their words to the index's names (for example a word meaning "quotations" maps to the Quotations page, a word meaning "customer" to the Customer field). Copy record references (order numbers, invoice numbers, names) exactly as the person wrote them. Never invent a name that is not in the index; if nothing fits, leave deliverables empty and say why in `unsupported`, in the person's language.

Deliverable kinds:
- fact: the value of one or more fields of one named record. Set entity, record and fields. If the record is reached through a link field of the named record (the customer of order X), set entity and record to what the person named, via to the link field's label, and fields to labels of the linked entity.
- aggregate: a count or a sum over records with conditions. Set entity, page.key to the list page whose scope applies (for example invoices to customers versus bills from vendors; confirmed orders versus quotations), metric.op, metric.field for a sum, metric.filters as field label plus values (use choice labels from the index, or the person's own words for a name), metric.group_by with metric.top for "which X has the most".
- list: which records match a description. Set entity, page.key and page.search with the search words translated into the index's language (the index is in English: a word meaning "chair" becomes "Chair").
- page: show a page. For a list page set page.key with view, filters_on, filters_off, group_by as asked. For a record's page set entity and record (and via when a linked record is meant) and section when a part of the record is asked for. A question asking where something is kept, viewed or configured is a page deliverable for that page.
- handoff: the person asks to change, create, confirm, send, pay, cancel or register something. Set write_intent true. For an existing record, set entity, record and a form action name from the index so its control can be shown. For a new record that does not exist yet, do not search for its proposed name: set entity, the collection page key and action "create" only when that page lists the create operation; the adapter will find the actual entry control. The assistant never performs the change itself.

Linked records: when the person names one record but wants another that it links to -- the customer of an order, the vendor of a bill, the payment terms an invoice uses -- entity and record are always the record they named, and via is the link field's label on that entity. "Show me the customer's details / profile / record" is a page deliverable with via; "what are the payment terms' rules" is a page deliverable with via, because the linked record's own page shows its rules.

A request that asks several things gets one deliverable per thing, in the order asked. Do not add a page deliverable merely to support a fact or aggregate answer: the page key inside an aggregate identifies its data scope and does not mean the person asked to open that page. Add a separate page deliverable only when the person explicitly asks to open, show, navigate to, or look at a page or record. Prefer the most specific page (a named record's page over a list page)."""


SCOPE_SYSTEM = """You read one request to a business web application and say which entities of the index it is about and what kind of deliverable each thing asked for is, by calling declare_scope exactly once.

The list names every entity the person may read, one per line, the likeliest first; a record the request names is already resolved under KNOWN REFERENCES, and its entity is the one meant unless the request asks about something that record links to or contains (its customer, its lines). The person may write in another language than the list; map their words to the entity keys. Data the index does not hold -- another system, a bank, email or chat, a carrier, a competitor, what will happen in the future -- has no stand-in among the entities: leave parts empty and say what is missing. A question about a rule of the application itself -- why a control is hidden, when a field can be changed, what an action refuses, whether something is required or limited -- is a fact about the entity it concerns: pick that entity; the second step shows the rules of the deployment's source and says when there is no such rule. Pick the entity a fact, count or list is about; for a change, the entity of the record to change; for a page, the entity of the record or list to open. When the person identifies one record and asks for something it links to or contains (the customer of an order, the company of a bill, the lines of an invoice), the entity is the identified record's -- the linked thing is a detail for the second step -- except that a question about a record's lines is about the line entity. If nothing fits, leave parts empty and say why in `unsupported`."""


def understand_scope(chat, *, prompt: str, summaries: str, run_id: str,
                     timeout_s: float | None = None, cancelled=None,
                     today: str = "") -> tuple[list[dict], bool, str, dict]:
    """The first step of a staged declaration: entities and kinds, from the ranked
    entity summaries rather than the full index. Returns (parts, write_intent,
    unsupported, audit projection)."""
    messages = [
        {"role": "system", "content": SCOPE_SYSTEM},
        {"role": "user", "content": (summaries + (f"\n\nTODAY\n{today}" if today else "")
                                     + "\n\nREQUEST\n" + prompt.strip())},
    ]
    kwargs = {"max_tokens": 600, "timeout_s": timeout_s}
    attempts = 0
    while True:
        attempts += 1
        try:
            if cancelled is not None:
                reply = chat.complete_cancellable(messages, [DECLARE_SCOPE_TOOL], run_id,
                                                  cancelled=cancelled, **kwargs)
            else:
                reply = chat.complete(messages, [DECLARE_SCOPE_TOOL], run_id, **kwargs)
            payload, source = _declaration_from(reply, name="declare_scope")
            parts, write_intent, unsupported = scope_from_json(payload)
            break
        except (ValueError, ProviderError) as exc:
            limit = 3 if isinstance(exc, ProviderError) else 2
            if attempts >= limit or (isinstance(exc, ProviderError)
                                     and not getattr(exc, "retryable", True)):
                raise
    return parts, write_intent, unsupported, {"source": source, "attempts": attempts,
                                              "part_count": len(parts)}


def understand(chat, *, prompt: str, index: ApplicationIndex, run_id: str,
               timeout_s: float | None = None, cancelled=None, today: str = "",
               previous: GoalSpec | None = None,
               problems: tuple[str, ...] = (), scope: str = "") -> tuple[GoalSpec, dict]:
    """Ask once. Returns the parsed declaration and a bounded audit projection.

    `today` is the application's current date, so that a month or day the person says
    without a year is declared in the right year; it sits after the index, whose cached
    prefix it must not break. With `previous` and `problems`, this is the one repair the
    turn allows: the first declaration and what the resolver found wrong with it (a
    field the entity does not have, several records a description matched) are shown,
    and the model declares again.
    """
    before = ""
    if scope:
        # The staged path: what the first step settled, and the known references, so
        # that the second step fills in details rather than choosing again.
        before += "\n\nSCOPE\n" + scope
    if previous is not None and problems:
        before += ("\n\nYOUR DECLARATION\n" + json.dumps(previous.to_json(), ensure_ascii=False)
                   + "\n\nPROBLEMS\n" + "\n".join(f"- {p}" for p in problems[:4])
                   + "\n\nDeclare again, correcting these and keeping what was right.")
    # The request is the last thing in the message: a small model reads the tail most
    # reliably, and everything else is context for it.
    messages = [
        {"role": "system", "content": UNDERSTAND_SYSTEM},
        {"role": "user", "content": ("APPLICATION INDEX\n" + index.render()
                                     + (f"\n\nTODAY\n{today}" if today else "")
                                     + before + "\n\nREQUEST\n" + prompt.strip())},
    ]
    kwargs = {"max_tokens": 1_500, "timeout_s": timeout_s}
    attempts = 0
    while True:
        attempts += 1
        try:
            if cancelled is not None:
                reply = chat.complete_cancellable(messages, [DECLARE_GOAL_TOOL], run_id,
                                                  cancelled=cancelled, **kwargs)
            else:
                reply = chat.complete(messages, [DECLARE_GOAL_TOOL], run_id, **kwargs)
            payload, source = _declaration_from(reply)
            spec = GoalSpec.from_json(payload)
            break
        except (ValueError, ProviderError) as exc:
            # One more try: a small model now and then answers in prose or returns a
            # malformed object; a provider that drops the call gets two more tries. A
            # failure after that ends it.
            limit = 3 if isinstance(exc, ProviderError) else 2
            if attempts >= limit or (isinstance(exc, ProviderError)
                                     and not getattr(exc, "retryable", True)):
                raise
    return spec, {"source": source, "deliverables": len(spec.deliverables),
                  "kinds": [d.kind for d in spec.deliverables],
                  "write_intent": spec.write_intent, "attempts": attempts}


def _declaration_from(reply: dict, name: str = "declare_goal") -> tuple[dict, str]:
    calls = reply.get("tool_calls") or []
    for call in calls:
        function = call.get("function") or {}
        if function.get("name") != name:
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


__all__ = ["SCOPE_SYSTEM", "UNDERSTAND_SYSTEM", "understand", "understand_scope"]
