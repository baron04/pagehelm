"""The business values a finished answer states, traced to this turn's own reads.

A closing check the adapter offers the common loop through its profile: every
amount, count, percentage or mixed letter/digit identifier the answer states must appear in the turn's tool
results, starting page or the person's own words, after unifying thousand separators,
trailing decimal zeros and signs for quantities. Identifiers retain every character,
including leading zeros, case and separators. Dates, times, numeric record ids,
addresses and list markers stay out of the extraction; a
missed fabrication is cheaper here than a false alarm on a right answer.

The check is feedback, never a gate: nothing is withheld, the common loop sends
the list back to the same model once and the second answer ends as it is.
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
    r"(?:编号|单号|序号|编码|代码|ID|Id|id|电话|手机号?|联系方式|邮编|身份证)[：:为是]?\s*[\d,，.\-]{3,}")
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
    r"|(?m:^\s*\d+\s*[.、)）](?=\s))|[（(]\d{1,2}[)）]")
_NON_VALUES = (_ADDRESSES, _ISO_DATETIME, _MIXED_IDS, _PHONES, _LABELED, _DATES, _STRUCTURAL)

_NUMBER = re.compile(r"[+\-−]?\d{1,3}(?:[,，]\d{3})+(?:\.\d+)?%?|[+\-−]?\d+(?:\.\d+)?%?")
#: Sources are read permissively: a value shown anywhere in them is a value the
#: turn saw, whatever context it stood in there.
_ANY_NUMBER = r"\d{1,3}(?:[,，]\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"


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


def stated_numbers(text: str) -> list[tuple[str, str]]:
    """The (display token, normalized value) pairs a text states, in order, once each."""
    cleaned = text
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


def untraced(answer: str, sources) -> tuple[str, ...]:
    """Unseen quantities and exact identifiers; never infer or repair a similar code."""
    sources = tuple(str(text) for text in sources)
    def codes(text):
        return _IDENTIFIER.findall(_ISO_DATETIME.sub(" ", _ADDRESSES.sub(" ", text)))
    identifiers = tuple(dict.fromkeys(codes(answer)))
    shown_ids = {token for text in sources for token in codes(text)}
    missing_ids = tuple(token for token in identifiers if token not in shown_ids)
    stated = stated_numbers(answer)
    if not stated:
        return missing_ids
    shown = set()
    for text in sources:
        for token in re.findall(_ANY_NUMBER, str(text)):
            value = _canon(token)
            if value:
                shown.add(value.lstrip("-"))
    return (*missing_ids, *(token for token, value in stated if value.lstrip("-") not in shown))


__all__ = ["answer_numbers", "stated_numbers", "untraced"]
