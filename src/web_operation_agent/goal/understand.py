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
from copy import deepcopy

from ..modeling.provider import ProviderError
from .declaration_cache import cached_read
from .index import ApplicationIndex
from .spec import DECLARE_GOAL_TOOL, DECLARE_SCOPE_TOOL, GoalSpec, scope_from_json

UNDERSTAND_SYSTEM = """Translate the request into the application's vocabulary; call declare_goal exactly once. Follow the tool's field descriptions.

Use only entity keys, field labels, page keys, filters, groups, sections and actions in APPLICATION INDEX. Translate the person's language to those names; copy record numbers and names exactly. Record names come from the request or candidates and need not appear in this schema index; only application reads can establish nonexistence. If no capability fits, leave deliverables empty and explain unsupported in the person's language. Never invent an index name or substitute a neighbouring field for missing data.

Kinds and required details:
- fact: fields of a record; set entity, record if named, and fields. Preserve supplied date/amount/owner conditions in metric.filters even with record; candidate details do not replace filters. Datetime fields accept exact local YYYY-MM-DD HH:MM:SS instants.
- aggregate: metric.op count/sum/min/max; metric.field for sum/min/max. Numeric highest/lowest uses max/min, returning all ties among known values in this one deliverable (list + min/max also returns all ties); never group_by/top or a sorted limited list. Most records per category uses count + group_by/top. metric.filters uses index choice labels or the person's names. For calendar periods use the original YYYY-MM, YYYY or named period; explicit between endpoints include both dates. page.key identifies the list's scope (customer invoices vs vendor bills, quotations vs confirmed orders).
- list: matching records; set entity and metric.filters for data conditions. Use page.search only for an explicitly requested page-search interaction, not for a data name/contains condition.
- page: only an explicit open/show/navigate request or a question asking where something is viewed or configured. A list uses page.key, view, filters_on/off and group_by; a record uses entity, record and section if requested.
- handoff: a requested change, create, confirm, send, pay, cancel or register; set write_intent true and the existing record's indexed action. For a new record, leave record empty; use the collection page and action "create" only if its operations allow create. Never search for a proposed new name. For several fields of one form, use one handoff with field_values; copy only values the user explicitly gave. The assistant does not submit the change itself.

For a linked record, entity and record identify the record the person named, via is its link field, and fields belong to the linked entity. "Show the customer's profile" and "what are the linked payment terms' rules" are page deliverables with via. Record lines are their own entity, as the tool describes.

One deliverable per thing asked, in request order. Do not add a page just to support a fact or aggregate: its page.key defines data scope, not navigation. Add a separate page only when explicitly requested; prefer a named record's page to a list."""


_SCOPE_INTRO = 'You read one request to a business web application and say which entities of the index it is about and what kind of deliverable each thing asked for is, by calling declare_scope exactly once.\n\nThe list names actor-visible entity types, not all their records. In a large application LIKELY ENTITIES carry pages and aliases; OTHER ENTITIES carry only their key and label and may also be chosen. Only records explicitly listed under KNOWN REFERENCES have already been resolved; use their entity unless the request asks about a link or contained records (its customer, its lines). Other named records remain unresolved, not nonexistent: select a plausible entity from the request and let application reads resolve the name. Entity summaries and candidate values are incomplete samples and cannot establish record nonexistence. The person may write in another language than the list; map their words to the entity keys. Data outside the application -- another system, a bank, email or chat, a carrier, a competitor, what will happen in the future -- has no stand-in among the entities: leave parts empty and say what is missing. '

_SCOPE_RULES = "A question about a rule of the application itself -- why a control is hidden, when a field can be changed, what an action refuses, whether something is required or limited -- is a fact about the entity it concerns: pick that entity and mark the part rules; the second step shows the rules of the deployment's source and says when there is no such rule. "

_SCOPE_BUSINESS = "Pick the entity a fact, count or list is about; for a change, the entity of the record to change, which for an action one of its controls offers (pay a bill, invoice an order) is that record's entity; for a page, the entity of the record or list to open. When the person identifies one record and asks for something it links to or contains (the customer of an order, the company of a bill, the lines of an invoice), the entity is the identified record's -- the linked thing is a detail for the second step -- except that a question about a record's lines is about the line entity. If nothing fits, leave parts empty and say what is missing in `unsupported`."

SCOPE_SYSTEM = _SCOPE_INTRO + _SCOPE_RULES + _SCOPE_BUSINESS


def understand_scope(chat, *, prompt: str, summaries: str, run_id: str,
                     timeout_s: float | None = None, cancelled=None,
                     today: str = "", earlier: str = "", cache=None, cache_namespace="",
                     repository_source: bool = False
                     ) -> tuple[list[dict], bool, str, bool, dict]:
    """The first step of a staged declaration: entities and kinds, from the ranked
    entity summaries rather than the full index. Returns (parts, write_intent,
    unsupported, about_assistant, audit projection)."""
    tool = DECLARE_SCOPE_TOOL
    system = SCOPE_SYSTEM
    if repository_source:
        from .source_answers import source_scope_tool, SOURCE_SCOPE_INSTRUCTIONS
        tool = source_scope_tool(deepcopy(tool))
        system = (_SCOPE_INTRO + _SCOPE_BUSINESS).replace("leave parts empty", "set kind_0=none") + SOURCE_SCOPE_INSTRUCTIONS
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": (summaries + (f"\n\nTODAY\n{today}" if today else "")
                                     + _earlier_block(earlier)
                                     + "\n\nREQUEST\n" + prompt.strip())},
    ]
    if repository_source:
        # Keep the request as its own final message. A metadata-heavy scope must
        # not make a small model treat the index itself as the user's question.
        messages[-1]["content"] = messages[-1]["content"].rsplit("\n\nREQUEST\n", 1)[0]
        messages.append({"role": "user", "content": "REQUEST\n" + prompt.strip()})
    key, held = cached_read(cache, cache_namespace, chat, messages, [tool], cancelled)
    if held is not None:
        return held
    kwargs = {"max_tokens": 1100 if repository_source else 600, "timeout_s": timeout_s}
    attempts = 0
    while True:
        attempts += 1
        try:
            if cancelled is not None:
                reply = chat.complete_cancellable(messages, [tool], run_id,
                                                  cancelled=cancelled, **kwargs)
            else:
                reply = chat.complete(messages, [tool], run_id, **kwargs)
            payload, source = _declaration_from(reply, name="declare_scope")
            sources = ()
            if repository_source:
                from .source_answers import split_source_scope
                payload, sources = split_source_scope(payload)
            parts, write_intent, unsupported, about_assistant = scope_from_json(payload)
            break
        except (ValueError, ProviderError) as exc:
            limit = 3 if isinstance(exc, ProviderError) else 2
            if attempts >= limit or (isinstance(exc, ProviderError)
                                     and not getattr(exc, "retryable", True)):
                raise
            if repository_source and isinstance(exc, ValueError):
                messages = [*messages[:3], {"role": "user", "content":
                    "Your declaration was invalid: " + str(exc)[:200] +
                    ". Call declare_scope again for the original REQUEST. "
                    "Use consecutive flat fields kind_0, about_0, terms_0, entity_0, then "
                    "kind_1, etc. kind_0=source requires a short about_0 phrase and "
                    "terms_0 as a real JSON array of one to three queries; omit entity_0. "
                    "Do not wrap JSON in a string or put quotes inside the topic phrase."}]
    result = (parts, write_intent, unsupported, about_assistant, {
        "source": source, "attempts": attempts, "part_count": len(parts),
        **({"source_requests": sources} if repository_source else {})})
    if cache is not None and not write_intent:
        cache.put(key, result)
    return result


def understand(chat, *, prompt: str, index: ApplicationIndex, run_id: str,
               timeout_s: float | None = None, cancelled=None, today: str = "",
               previous: GoalSpec | None = None,
               problems: tuple[str, ...] = (), scope: str = "",
               values: str = "", earlier: str = "", cache=None, cache_namespace="",
               form_values: bool = True) -> tuple[GoalSpec, dict]:
    """Ask once. Returns the parsed declaration and a bounded audit projection.

    `today` is the application's current date, so that a month or day the person says
    without a year is declared in the right year; it sits after the index, whose cached
    prefix it must not break. With `previous` and `problems`, this is the one repair the
    turn allows: the first declaration and what the resolver found wrong with it (a
    field the entity does not have, several records a description matched) are shown,
    and the model declares again.
    """
    before = _earlier_block(earlier)
    if scope:
        # The staged path: what the first step settled, and the known references, so
        # that the second step fills in details rather than choosing again.
        before += "\n\nSCOPE\n" + scope
    if values:
        # Real values of the application around the records the request names; the
        # model declares their ids and the core binds them to the exact names.
        before += ("\n\nVALUES (id kind entity: exact name — detail); write the id "
                   "(v3) where one of these is meant\n" + values)
    if previous is not None and problems:
        before += ("\n\nYOUR DECLARATION\n" + json.dumps(previous.to_json(), ensure_ascii=False, separators=(",", ":"))
                   + "\n\nPROBLEMS\n" + "\n".join(f"- {p}" for p in problems[:4])
                   + "\n\nDeclare again, correcting these and keeping what was right.")
    # The request is the last thing in the message: a small model reads the tail most
    # reliably, and everything else is context for it. On the staged path it also
    # opens the message: in front of a small neighbourhood the model otherwise often
    # declared nothing or misread the request's numbers (a replay of the same message:
    # right 12 of 12 with the request at both ends, 7 of 12 at the end alone).
    head = ("REQUEST\n" + prompt.strip() + "\n\n") if scope else ""
    tail = "\n\nREQUEST (again)\n" if scope else "\n\nREQUEST\n"
    messages = [
        {"role": "system", "content": UNDERSTAND_SYSTEM},
        {"role": "user", "content": (head + "APPLICATION INDEX\n" + index.render()
                                     + (f"\n\nTODAY\n{today}" if today else "")
                                     + before + tail + prompt.strip())},
    ]
    # Repairs must remain new readings, even if an identical repair was seen before.
    if previous is not None or problems:
        cache = None
    tool = DECLARE_GOAL_TOOL
    if not form_values:
        tool = deepcopy(tool)
        tool["function"]["parameters"]["properties"]["deliverables"]["items"]["properties"].pop("field_values")
    key, held = cached_read(cache, cache_namespace, chat, messages, [tool], cancelled)
    if held is not None:
        return held
    kwargs = {"max_tokens": 1_500, "timeout_s": timeout_s}
    attempts = 0
    while True:
        attempts += 1
        try:
            if cancelled is not None:
                reply = chat.complete_cancellable(messages, [tool], run_id,
                                                  cancelled=cancelled, **kwargs)
            else:
                reply = chat.complete(messages, [tool], run_id, **kwargs)
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
    result = (spec, {"source": source, "deliverables": len(spec.deliverables),
                  "kinds": [d.kind for d in spec.deliverables],
                  "write_intent": spec.write_intent, "attempts": attempts})
    if cache is not None and not spec.write_intent and all(d.kind != "handoff" for d in spec.deliverables):
        cache.put(key, result)
    return result


def _earlier_block(earlier: str) -> str:
    """What was said before in this conversation, which a follow-up ("its total?", "the
    Right Sit one", "both together") refers to; nothing on a conversation's first turn."""
    return ("\n\nEARLIER IN THIS CONVERSATION (the request may refer to it)\n" + earlier.strip()
            if earlier.strip() else "")


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
