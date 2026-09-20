"""Provider-neutral reasoning separation and planner response budgets."""
from __future__ import annotations

import json
import re

from .contract_validation import PlanningFailed


#: Tag names vendors wrap chain-of-thought in when they inline it in the content. The list is
#: open on purpose: a new one costs a string here, not a code path. Providers that return
#: reasoning out of band instead are handled by ChatModel, which normalises it onto `reasoning`.
REASONING_TAGS = ("think", "thinking", "reasoning", "thought", "scratchpad")
_REASONING_RE = re.compile(
    r"<(" + "|".join(REASONING_TAGS) + r")\b[^>]*>.*?</\1\s*>", re.S | re.I)
_REASONING_OPEN_RE = re.compile(r"<(" + "|".join(REASONING_TAGS) + r")\b[^>]*>", re.I)


def split_reasoning(text: str, out_of_band: str = "") -> tuple[str, str]:
    """(reasoning, answer). What the model thought versus what it said.

    Reasoning arrives two ways and neither is universal: some endpoints return it in a
    separate field, some inline it in the content between tags. Both are handled here so no
    caller has to know which vendor it is talking to, and adding a vendor means adding a tag
    name to `REASONING_TAGS`.
    """
    text = text or ""
    thought = [out_of_band] if out_of_band else []
    thought += [m.group(0) for m in _REASONING_RE.finditer(text)]
    answer = _REASONING_RE.sub("", text)
    opened = _REASONING_OPEN_RE.search(answer)
    if opened:                                  # opened, never closed: the rest is thought
        before, after = answer[:opened.start()], answer[opened.end():]
        thought.append(after)
        answer = before
    return "\n".join(t.strip() for t in thought if t.strip()).strip(), answer.strip()


def _extract_json(text: str) -> dict:
    # Reasoning inlined in the content has braces of its own, and they come before the
    # answer's. Strip it first, whichever convention produced it.
    _, text = split_reasoning(text)
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise PlanningFailed("the planner returned no JSON object")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise PlanningFailed(f"the planner returned malformed JSON: {e}")


#: Where the planner's completion budget starts. It is a starting point, not a ceiling: the
#: loop grows it when the provider says the answer was cut off, or when a reasoning model
#: spends the whole allowance thinking and answers with nothing. Pinning a number here was
#: wrong twice already -- 2,048 truncated four of six contracts, then 4,096 was consumed
#: entirely by 39,308 characters of reasoning -- and pinning a bigger one only moves the
#: guess. What the budget should be is a property of the model in use, so it is discovered
#: from that model's own signals rather than declared here.
PLANNER_START_TOKENS = 4096
#: How many times the budget may double before the conclusion is about the model rather than
#: the budget. Bounds cost; does not cap capability.
PLANNER_MAX_GROWTHS = 3
#: Writing a contract is a harder thought than deciding one click, so the planner asks for a
#: multiple of whatever reasoning allowance the model in use declares. A multiple, not a
#: number: the allowance itself is a property of the model and lives in its policy entry.
PLANNER_THINKING_FACTOR = 4


def thinking_options(chat, factor: float) -> dict | None:
    """Scale the model's own reasoning allowance, or return None if it declares none.

    A model with no such knob is sent no such field. This is the difference between adapting
    to the provider in use and hard-coding one: `policies/model_options.json` says whether a
    reasoning allowance exists and how big it is, and this only asks for more of it.
    """
    base = (getattr(chat, "options", None) or {}).get("thinking_budget")
    return {"thinking_budget": int(base * factor)} if base else None
