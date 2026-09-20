"""Business-skill phrase matching and Unicode-safe business text."""
from __future__ import annotations

import re
import unicodedata

from .business_errors import BusinessToolRequestError


def _trigger_tokens(value: str) -> tuple[str, ...]:
    """Tokenize Latin words and individual Han characters for bounded phrase matching."""
    return tuple(re.findall(r"[a-z0-9]+|[\u3400-\u4dbf\u4e00-\u9fff]", value.casefold()))


def _trigger_matches(trigger: str, task: str, *, max_gap_tokens: int = 3) -> bool:
    """Match a Skill phrase with word boundaries and a small amount of natural-language fill."""
    wanted = _trigger_tokens(trigger)
    available = _trigger_tokens(task)
    if not wanted or not available:
        return False
    cursor = -1
    for position, token in enumerate(wanted):
        # A phrase can occur anywhere in the task. Only the distance between its own words
        # is bounded; otherwise a one-word domain trigger would inexplicably stop matching
        # near the end of an ordinary sentence.
        upper = (len(available) if position == 0
                 else min(len(available), cursor + max_gap_tokens + 2))
        try:
            cursor = available.index(token, cursor + 1, upper)
        except ValueError:
            return False
    return True


#: Han markers that turn the verb after them into a description of state, or deny it. English
#: carries the distinction in the word itself -- "confirmed orders" or "unconfirmed quotations"
#: never match a "confirm order" trigger -- but "已确认销售订单" (orders already confirmed)
#: matches "确认订单" once the matcher allows a gap, and so does "未确认报价单" (quotations not
#: yet confirmed): a question about either became a request to confirm one, and every
#: read-only answer to it was refused. Done (已), done to (被), not yet (未), not (没), pending
#: (待), and 不/别, which ask for the action *not* to happen.
_STATIVE_MARKERS = frozenset({"已", "被", "未", "没", "待", "不", "别"})
#: Two-character forms of the same markers: 已经, 没有, 不要, 不用.
_STATIVE_PAIRS = frozenset({("已", "经"), ("没", "有"), ("不", "要"), ("不", "用")})
#: Aspect markers after or inside the verb: "开票了吗" (has it been invoiced), "开过票"
#: (was it ever). Neither is how anyone asks for the action to happen.
_ASPECT_MARKERS = frozenset({"了", "过"})
_QUESTION_MARKERS = frozenset({"吗", "么", "没"})
#: What makes "确认" mean "check" rather than "confirm": whether something holds ("是否"), or a
#: question about an attribute of the object ("确认报价单 S00007 的金额是多少").
_CHECK_QUESTION = re.compile(r"(?:谁|哪|几|吗|多少|什么)")
#: A clause ends at sentence punctuation or at "然后"; the checks above stay inside one, so
#: "确认报价单 S00007，然后告诉我它的金额是多少" is still a request followed by a question.
_CLAUSE_BREAK = re.compile(r"[，,。；;！!？?\n]|然后")


def trigger_requests_action(trigger: str, task: str, *, max_gap_tokens: int = 3) -> bool:
    """Whether `trigger` occurs in `task` as something asked for, not as a description.

    The same match as `_trigger_matches`, tried at every start in every clause, and a start
    is discarded when the words around it say the sentence is about a state rather than a
    request: a marker before the verb (已, 未, 没有, 不要 ...), an aspect marker inside or after
    it in a question (开过票, 开票了吗), or a check of a fact about the object (是否, 的 ... 是多少).
    Selection keeps the plain matcher: a question about confirmed or unconfirmed orders still
    loads the order Skills. Only the claim that the user asked for a change is narrowed.
    """
    wanted = _trigger_tokens(trigger)
    return bool(wanted) and any(
        _clause_requests(wanted, _trigger_tokens(clause), max_gap_tokens)
        for clause in _CLAUSE_BREAK.split(task))


def _clause_requests(wanted: tuple[str, ...], available: tuple[str, ...],
                     max_gap_tokens: int) -> bool:
    for start, token in enumerate(available):
        if token != wanted[0]:
            continue
        before = available[max(0, start - 2):start]
        if before and (before[-1] in _STATIVE_MARKERS or tuple(before) in _STATIVE_PAIRS):
            continue
        positions, matched = [start], True
        for next_token in wanted[1:]:
            upper = min(len(available), positions[-1] + max_gap_tokens + 2)
            try:
                positions.append(available.index(next_token, positions[-1] + 1, upper))
            except ValueError:
                matched = False
                break
        if not matched:
            continue
        gap = {available[i] for i in range(start, positions[-1] + 1) if i not in positions}
        after = available[positions[-1] + 1:]
        tail = "".join(after)
        if (gap & _ASPECT_MARKERS
                or (after[:1] and after[0] in _ASPECT_MARKERS
                    and set(after[1:]) & _QUESTION_MARKERS)
                or "是否" in tail
                or ("的" in tail and _CHECK_QUESTION.search(tail[tail.index("的"):]))):
            continue
        return True
    return False


#: Scripts whose letters are visually interchangeable with Latin ones. A name mixing Latin
#: with either is a homograph, not a language: "Аcme" with a Cyrillic А renders exactly like
#: "Acme". Han, Kana and Hangul mix with Latin in ordinary company names and are not listed.
_CONFUSABLE_WITH_LATIN = frozenset({"CYRILLIC", "GREEK"})


def _script_of(character: str) -> str:
    try:
        return unicodedata.name(character).split(" ", 1)[0]
    except ValueError:
        return ""


def business_text_key(value: str) -> str:
    """Fold one business identifier into the key two records are compared on.

    Applied to both sides of a duplicate check, so a record this product did not create
    still compares equal to a visually identical name. Lenient by design: it never raises.
    Use :func:`normalize_business_text` for text this product is about to write.
    """
    if not isinstance(value, str):
        return ""
    folded = unicodedata.normalize("NFKC", value)
    folded = "".join(character for character in folded
                     if unicodedata.category(character) not in {"Cc", "Cf"})
    return " ".join(folded.split()).casefold()


def normalize_business_text(value: object, *, field: str, maximum: int) -> str:
    """Return `value` as text safe to write and to compare, or refuse it.

    Three defects make two records look identical to a person and different to a byte
    comparison, so each is removed or refused here rather than at the duplicate check:
    compatibility forms (fullwidth, ligatures) are folded by NFKC; every run of whitespace,
    including NBSP and ideographic space, becomes one ordinary space; control and format
    characters -- zero-width spaces, bidirectional overrides, default-ignorables, all
    category ``Cc``/``Cf`` -- are refused outright. Category ``Zs`` is deliberately not
    refused: U+0020 belongs to it.

    A name that mixes Latin letters with Cyrillic or Greek ones is refused as a homograph.
    """
    if not isinstance(value, str):
        raise BusinessToolRequestError(f"{field} must be a string")
    normalized = unicodedata.normalize("NFKC", value)
    for character in normalized:
        if unicodedata.category(character) in {"Cc", "Cf"}:
            raise BusinessToolRequestError(
                f"{field} contains an invisible or directional control character")
    normalized = " ".join(normalized.split())
    if not normalized:
        raise BusinessToolRequestError(f"{field} must not be empty")
    if len(normalized) > maximum:
        raise BusinessToolRequestError(f"{field} must be 1 to {maximum} visible characters")
    scripts = {_script_of(character) for character in normalized if character.isalpha()}
    if "LATIN" in scripts and scripts & _CONFUSABLE_WITH_LATIN:
        raise BusinessToolRequestError(
            f"{field} mixes Latin letters with a visually identical alphabet")
    return normalized
