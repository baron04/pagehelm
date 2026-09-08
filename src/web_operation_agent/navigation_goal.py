"""Application-neutral binding of simple navigation goals to observed page links."""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit


_WORD = re.compile(r"[a-z0-9]{2,}")
_IGNORED = frozenset({
    "an", "the", "page", "screen", "open", "goto", "navigate", "please", "help",
    "me", "to", "one", "new", "create", "add",
})
_CREATE_WORDS = frozenset({"new", "create", "add"})


def _terms(text: str) -> set[str]:
    terms: set[str] = set()
    for raw in _WORD.findall(text.casefold()):
        word = raw[:-1] if raw.endswith("s") and len(raw) > 3 else raw
        if word not in _IGNORED:
            terms.add(word)
    return terms


def resolve_visible_navigation_path(task: str, observation: object) -> str | None:
    """Return one same-origin control-link path when task and link text are unambiguous.

    The resolver does not invent routes and knows nothing about a particular application. A
    create/new intent only prefers a visible create/new link over a list link with the same
    entity word. Submit controls are never treated as navigation.
    """
    task_folded = task.casefold()
    task_terms = _terms(task_folded)
    if not task_terms:
        return None
    create_intent = bool(
        _CREATE_WORDS.intersection(_WORD.findall(task_folded))
        or "创建" in task_folded or "新建" in task_folded or "新增" in task_folded)
    current_url = str(getattr(observation, "url", ""))
    current = urlsplit(current_url)
    candidates: dict[str, int] = {}
    for element in getattr(observation, "elements", ()):
        if getattr(element, "role", "") != "link" or getattr(element, "tag", "") != "a":
            continue
        name_folded = str(getattr(element, "name", "")).casefold()
        overlap = task_terms.intersection(_terms(name_folded))
        if not overlap:
            continue
        href = str(getattr(element, "attrs", {}).get("href", ""))
        parsed = urlsplit(urljoin(current_url, href))
        if parsed.netloc != current.netloc or "/control/" not in parsed.path:
            continue
        create_link = bool(_CREATE_WORDS.intersection(_WORD.findall(name_folded)))
        score = len(overlap) * 10 + (4 if create_intent and create_link else 0)
        candidates[parsed.path] = max(candidates.get(parsed.path, 0), score)
    if not candidates:
        return None
    best_score = max(candidates.values())
    best = [path for path, score in candidates.items() if score == best_score]
    return best[0] if len(best) == 1 else None
