"""The business values a finished answer states, traced to this turn's own reads.

A closing check the adapter offers the common loop through its profile: every
amount, count, percentage or mixed letter/digit identifier the answer states must appear in the turn's tool
results, starting page or the person's own words, after unifying thousand separators
and trailing decimal zeros; the sign stays significant: -500 is not 500. Identifiers retain every character,
including leading zeros, case and separators. Dates, times, numeric record ids,
addresses and list markers stay out of the extraction; a
missed fabrication is cheaper here than a false alarm on a right answer.

This hook returns feedback only. The common loop sends the list back once,
then marks unresolved deliverable values incomplete or names narrative values
in a footer, according to their delivery scope.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

#: Web addresses and in-app paths carry record ids, not stated values.
_ADDRESSES = re.compile(r"https?://\S+|/[A-Za-z][A-Za-z0-9_\-/]*")
# Token boundaries must be ASCII: adjacent Chinese prose is not part of a code.
_IDENTIFIER = re.compile(r"(?<![A-Za-z0-9_./-])(?=[A-Za-z0-9_.-]*[A-Za-z])"
                         r"(?=[A-Za-z0-9_.-]*\d)[A-Za-z0-9]+(?:[_.-][A-Za-z0-9]+)*"
                         r"(?![A-Za-z0-9_/-]|\.[A-Za-z0-9])")
_MIXED_IDS = _IDENTIFIER
_ISO_DATETIME = re.compile(r"(?<![A-Za-z0-9_])\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}"
                           r"(?::\d{2}(?:\.\d+)?)?(?:[Zz]|[+-]\d{2}:?\d{2})?")
_PHONES = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_LABELED = re.compile(
    r"(?:(?<![A-Za-z])(?:ID|Id|id)|编号|单号|序号|编码|代码|电话|手机号?|联系方式|邮编|身份证)"
    r"[：:为是=]?\s*[\d,，.\-]{3,}")
#: Calendar and clock readings. A dotted year is a date only with a day part,
#: so a decimal amount such as 2000.50 stays a value.
_DATES = re.compile(
    r"\d{4}\s*年(?:\s*\d{1,2}\s*月)?(?:\s*\d{1,2}\s*日?)?"
    r"|(?:19|20)\d{2}[\-/]\d{1,2}(?:[\-/]\d{1,2})?"
    r"|(?:19|20)\d{2}\.\d{1,2}\.\d{1,2}"
    r"|\d{1,2}\s*月(?:\s*\d{1,2}\s*日?)?"
    r"|\d{1,2}\s*[:：]\s*\d{2}(?:\s*[:：]\s*\d{2})?"
    r"|\d{1,2}\s*点(?:\s*\d{1,2}\s*分|半)?")
#: Ordinals and list markers position records; they state nothing about them.
_STRUCTURAL = re.compile(
    r"第\s*\d+(?:\.\d+)?(?:\s*[-–—~～至到]\s*\d+(?:\.\d+)?)?\s*[页行条名位步层章节期届号班排组]?"
    r"|(?<![\d.,，])\d+\s*[、.。)）](?![\d.,，])"
    r"|[（(]\d{1,2}[)）]")
_NON_VALUES = (_ADDRESSES, _ISO_DATETIME, _MIXED_IDS, _PHONES, _LABELED, _DATES, _STRUCTURAL)
_DECODED_CHOICE_JSON_RE = re.compile(r'"choices"\s*:\s*\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}')
_QUERY_CHOICE_BLOCK_RE = re.compile(r"原生查询选项[^\n]*\nBEGIN_UNTRUSTED_APPLICATION_DATA\n(\{.*?\})\nEND_UNTRUSTED_APPLICATION_DATA", re.S)
_DECODED_CHOICE_VAL_RE = re.compile(r'"[^"]+"\s*:\s*"([A-Za-z][A-Za-z0-9]{0,31})"')
_INLINE_LABEL_VAL_RE = re.compile(r'"[A-Za-z0-9_]+_label"\s*:\s*"([A-Za-z][A-Za-z0-9]{0,31})"')
_ADJACENT_LABEL_COUNT_RE = re.compile(r"^([A-Za-z][A-Za-z0-9]*?)([1-9]\d*|0)$")

#: A `-` counts as a sign only where it cannot be a hyphen: not right after a
#: digit (ranges "500-600"), a dot, a percent sign, an identifier character
#: ("PO-000017", "SKU-500") or a slash. The same boundary applies to answers
#: and sources so a range reads the same numbers on both sides.
_SIGN_BOUND = r"(?<![\d.%A-Za-z0-9_/])"
_NUMBER = re.compile(_SIGN_BOUND + r"[+\-−]?\d{1,3}(?:[,，]\d{3})+(?:\.\d+)?%?"
                     r"|" + _SIGN_BOUND + r"[+\-−]?\d+(?:\.\d+)?%?")
#: Sources are read permissively: a value shown anywhere in them is a value the
#: turn saw, whatever context it stood in there.
_ANY_NUMBER = (_SIGN_BOUND + r"[+\-−]?\d{1,3}(?:[,，]\d{3})+(?:\.\d+)?"
               r"|" + _SIGN_BOUND + r"[+\-−]?\d+(?:\.\d+)?")


def _canon(token: str) -> str:
    """One comparison form: no thousand separators, trailing zeros or plus signs."""
    text = token.rstrip("%").replace(",", "").replace("，", "").replace("−", "-")
    if text.startswith("+"):
        text = text[1:]
    try:
        value = Decimal(text)
    except InvalidOperation:
        return ""
    return format(value.normalize(), "f")


#: `负 500` states -500: the Chinese minus signs like the ASCII ones do.
_NEGATIVE = re.compile(r"负\s*(?=\d)")


def _signed(text: str) -> str:
    return _NEGATIVE.sub("-", text)


def stated_numbers(text: str) -> list[tuple[str, str]]:
    """The (display token, normalized value) pairs a text states, in order, once each."""
    cleaned = _signed(text)
    for pattern in _NON_VALUES:
        cleaned = pattern.sub(" ", cleaned)
    pairs, seen = [], set()
    for token in _NUMBER.findall(cleaned):
        value = _canon(token)
        if value and value not in seen:
            seen.add(value)
            pairs.append((token, value))
    return pairs


def answer_numbers(text: str) -> list[str]:
    """The normalized business values a text states."""
    return [value for _token, value in stated_numbers(text)]


def untraced(answer: str, sources, *, decoded_labels=()) -> tuple[str, ...]:
    """Unseen quantities and exact identifiers; never infer or repair a similar code."""
    sources = tuple(str(text) for text in sources)
    def codes(text):
        return _IDENTIFIER.findall(_ISO_DATETIME.sub(" ", _ADDRESSES.sub(" ", text)))
    identifiers = tuple(dict.fromkeys(codes(answer)))
    shown_ids = {token for text in sources for token in codes(text)}
    raw_missing_ids = tuple(token for token in identifiers if token not in shown_ids)
    stated = stated_numbers(answer)
    if not stated and not raw_missing_ids:
        return ()
    shown = set()
    known_labels = {str(lbl) for lbl in (decoded_labels or ()) if lbl}
    for text in sources:
        s_txt = str(text)
        for token in re.findall(_ANY_NUMBER, _signed(s_txt)):
            value = _canon(token)
            if value:
                shown.add(value)
        if raw_missing_ids:
            if '"choices"' in s_txt:
                for block in _DECODED_CHOICE_JSON_RE.findall(s_txt):
                    known_labels.update(_DECODED_CHOICE_VAL_RE.findall(block))
            if "原生查询选项" in s_txt:
                for block in _QUERY_CHOICE_BLOCK_RE.findall(s_txt):
                    known_labels.update(_DECODED_CHOICE_VAL_RE.findall(block))
            if "_label" in s_txt:
                known_labels.update(_INLINE_LABEL_VAL_RE.findall(s_txt))
    if raw_missing_ids and known_labels:
        filtered_ids = []
        for token in raw_missing_ids:
            m = _ADJACENT_LABEL_COUNT_RE.fullmatch(token)
            if m and m.group(1) in known_labels and _canon(m.group(2)) in shown:
                continue
            filtered_ids.append(token)
        missing_ids = tuple(filtered_ids)
    else:
        missing_ids = raw_missing_ids
    return (*missing_ids, *(token for token, value in stated if value not in shown))


__all__ = ["answer_numbers", "stated_numbers", "untraced"]
