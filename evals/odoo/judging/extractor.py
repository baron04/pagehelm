"""Bounded, deterministic fact extraction from the answer a user sees by default.

Every compared system is judged only on its default-visible final answer text. This module
finds the facts a task specification asks for -- entities, e-mails, phones, references,
short text values, counts, amounts with their currency and a closed set of outcome markers --
without repairing the answer and without scoring similarity. Markdown wrapping never changes
a verdict. When the text does not decide a fact, the extraction records an ambiguity and the
judgement is a FAIL marked ambiguous; nothing is ever passed on a guess.

Standard library only, and no product import: the evaluator using it must stay independent of
the system it measures.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


@dataclass(frozen=True)
class Entity:
    name: str


@dataclass(frozen=True)
class Email:
    value: str


@dataclass(frozen=True)
class Phone:
    value: str


@dataclass(frozen=True)
class Reference:
    value: str


@dataclass(frozen=True)
class Text:
    value: str


@dataclass(frozen=True)
class Count:
    value: int


@dataclass(frozen=True)
class Money:
    amount: Decimal
    currency: str


@dataclass(frozen=True)
class Marker:
    kind: str


Fact = Entity | Email | Phone | Reference | Text | Count | Money | Marker

#: Closed marker vocabulary. Lists are literal phrases matched on casefolded normalized text.
MARKERS: dict[str, tuple[str, ...]] = {
    "not_found": ("未找到", "没有找到", "找不到", "不存在", "没有匹配", "查无", "no matching",
                  "not found", "does not exist", "doesn't exist", "could not find",
                  "couldn't find", "no record", "no customer", "no quotation"),
    "undetermined": ("无法确定", "不能确定", "无法读取", "未读取", "读不到", "cannot determine",
                     "can't determine", "unable to determine", "not available", "not readable"),
    "partial": ("部分", "仅列出", "partial", "partially", "only some"),
    "permission_refusal": ("没有权限", "无权", "权限不足", "不允许", "只读", "not permitted",
                           "not allowed", "not authorized", "not authorised", "permission",
                           "access denied", "read-only", "read only"),
    "duplicate_refusal": ("已存在", "重复", "同名", "already exists", "already exist",
                          "duplicate"),
    "nothing_to_invoice_refusal": ("没有可开票", "无可开票", "无需开票", "没有需要开票",
                                   "已全部开票", "已经开票", "没有可以开票", "nothing to invoice",
                                   "nothing left to invoice", "no invoiceable",
                                   "already invoiced", "already fully invoiced",
                                   "something still to invoice"),
    "unsupported_refusal": ("不支持", "无法删除", "不能删除", "不能发送", "无法发送", "无法过账",
                            "不在支持范围", "超出支持范围", "not supported", "unsupported",
                            "cannot delete", "can't delete", "unable to delete", "cannot send",
                            "cannot post", "outside the supported"),
    "clarification_request": ("请问", "请选择", "请确认是哪", "哪一个", "哪个", "哪一位",
                              "多个匹配", "有两个", "which one", "which of", "please choose",
                              "please select", "please specify", "did you mean",
                              "multiple matches", "more than one"),
    "cancelled_no_action": ("已取消", "不再执行", "没有执行任何", "不做任何", "未进行任何操作",
                            "cancelled", "canceled", "no action", "nothing was changed",
                            "won't proceed", "will not proceed"),
    "not_executed": ("未执行", "没有执行", "未创建", "没有创建", "未确认", "没有确认", "未开票",
                     "已拒绝", "拒绝了", "not created", "not confirmed", "not executed",
                     "was not", "declined", "denied", "did not"),
    "write_succeeded": ("已创建", "创建成功", "已成功创建", "已确认", "确认成功", "已开具",
                        "已生成发票", "已创建发票", "created", "has been created", "was created",
                        "confirmed", "has been confirmed", "invoiced", "invoice was created",
                        "invoice has been created"),
}
_NEGATION_CJK = ("未", "没", "没有", "不", "无法")
_NEGATION_WORDS = ("not", "never", "wasn't", "was not", "hasn't", "has not", "no")
_NEGATABLE = frozenset({"write_succeeded"})

_CURRENCY_BEFORE = re.compile(r"(US\$|\$|€|USD|EUR|美元|欧元)\s*[:]?\s*$", re.I)
_CURRENCY_AFTER = re.compile(r"^\s*(USD|EUR|美元|欧元|dollars?|euros?|€)", re.I)
_CURRENCY_ANYWHERE = re.compile(r"US\$|\$|€|\bUSD\b|\bEUR\b|美元|欧元|\bdollars?\b|\beuros?\b",
                                re.I)
_CURRENCY_CODES = {"us$": "USD", "$": "USD", "usd": "USD", "美元": "USD", "dollar": "USD",
                   "dollars": "USD", "€": "EUR", "eur": "EUR", "欧元": "EUR", "euro": "EUR",
                   "euros": "EUR"}
_NUMBER = re.compile(r"(?<![\d.,])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?(?![\d])")
_EUROPEAN = re.compile(r"(?<![\d.,])\d{1,3}(?:\.\d{3})+,\d{2}(?!\d)|(?<![\d.,])\d+,\d{2}(?![\d,])")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"\+?\d[\d ().-]*\d")
_STRICT_PHONE = re.compile(r"\+\d[\d -]{6,}\d|(?<!\d)\d{3}[ -]\d{3}[ -]\d{4}(?!\d)")
_DATE = re.compile(r"\d{4}-\d{1,2}-\d{1,2}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?|\d{4}/\d{1,2}/\d{1,2}"
                   r"|\d{4}年\d{1,2}月(?:\d{1,2}日)?|\d{1,2}:\d{2}(?::\d{2})?")
_MIXED_TOKEN = re.compile(r"[A-Za-z0-9#_/.-]*(?:[A-Za-z][A-Za-z0-9#_/.-]*\d|\d[A-Za-z0-9#_/.-]*"
                          r"[A-Za-z])[A-Za-z0-9#_/.-]*")
_MEASURE = "个位家张条笔项份"
_COUNT_PREFIX = re.compile(r"(?:一共|总共|共有|合计|共|数量\s*:?|总数\s*:?|条数\s*:?|件数\s*:?"
                           r"|count\s*:?|total(?:\s+of)?\s*:?|there\s+(?:are|is))\s*$", re.I)
_URL = re.compile(r"https?://\S+", re.I)


def normalize(text: str) -> str:
    """NFKC, Markdown markers removed, dashes unified, horizontal spaces collapsed.

    CJK list punctuation becomes spaced ASCII first: NFKC alone turns "，" into "," and would
    glue "USD，2,290.50" into one malformed number.
    """
    value = (text or "").replace("，", ", ").replace("；", "; ").replace("、", " 、 ")
    value = unicodedata.normalize("NFKC", value)
    lines = []
    for line in value.splitlines():
        stripped = line.strip()
        if re.fullmatch(r"\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?", stripped):
            continue
        stripped = re.sub(r"^#{1,6}\s+", "", stripped)
        stripped = re.sub(r"^(?:[-*•]|\d+[.)])\s+", "", stripped)
        stripped = stripped.replace("|", " ; ")
        lines.append(stripped)
    value = "\n".join(lines)
    value = value.replace("**", "").replace("__", "").replace("`", "")
    value = re.sub(r"[‒–—―−]", " - ", value)
    value = value.replace("‘", "'").replace("’", "'").replace("“", '"').replace(
        "”", '"')
    value = re.sub(r"[ \t 　]+", " ", value)
    return value.strip()


def _boundary_pattern(value: str) -> re.Pattern[str]:
    parts = [re.escape(part) for part in normalize(value).split()]
    return re.compile(r"(?<![A-Za-z0-9])" + r"\s+".join(parts) + r"(?![A-Za-z0-9])", re.I)


@dataclass(frozen=True)
class Extraction:
    normalized: str
    emails: tuple[str, ...]
    phones: tuple[str, ...]
    counts: tuple[int, ...]
    bare_integers: tuple[int, ...]
    money: tuple[Money, ...]
    unattached_amounts: tuple[Decimal, ...]
    markers: frozenset[str]
    ambiguities: tuple[str, ...]


@dataclass(frozen=True)
class Judgement:
    status: str
    ambiguous: bool
    missing: tuple[str, ...]
    contradictions: tuple[str, ...]


def _spans(pattern: re.Pattern[str], text: str) -> list[tuple[int, int]]:
    return [match.span() for match in pattern.finditer(text)]


def _inside(position: tuple[int, int], spans: list[tuple[int, int]]) -> bool:
    return any(start <= position[0] and position[1] <= end for start, end in spans)


def _marker_hits(folded: str) -> frozenset[str]:
    found = set()
    for kind, phrases in MARKERS.items():
        for phrase in phrases:
            for match in re.finditer(re.escape(phrase), folded):
                if kind in _NEGATABLE:
                    before = folded[max(0, match.start() - 4):match.start()]
                    words = folded[max(0, match.start() - 12):match.start()]
                    if any(before.endswith(token) or token in before for token in _NEGATION_CJK):
                        continue
                    if re.search(r"\b(?:" + "|".join(re.escape(word) for word in _NEGATION_WORDS)
                                 + r")\s+(?:\w+\s+)?$", words):
                        continue
                if phrase.isascii() and phrase[0].isalpha():
                    left = folded[match.start() - 1] if match.start() else " "
                    right = folded[match.end()] if match.end() < len(folded) else " "
                    if left.isalnum() or right.isalnum():
                        continue
                found.add(kind)
                break
    return frozenset(found)


def extract(text: str, *, count_units: Sequence[str] = (),
            protected: Sequence[str] = ()) -> Extraction:
    normalized = normalize(text)
    folded = normalized.casefold()
    ambiguities: list[str] = []
    emails = tuple(sorted({match.group(0).casefold() for match in _EMAIL.finditer(normalized)}))
    phones = tuple(sorted({re.sub(r"\D", "", match.group(0)) for match in _PHONE.finditer(
        normalized) if 7 <= len(re.sub(r"\D", "", match.group(0))) <= 15}))
    excluded = (_spans(_EMAIL, normalized) + _spans(_STRICT_PHONE, normalized)
                + _spans(_DATE, normalized) + _spans(_MIXED_TOKEN, normalized)
                + _spans(_URL, normalized))
    for value in protected:
        excluded += _spans(_boundary_pattern(value), normalized)
    european = _spans(_EUROPEAN, normalized)
    if european:
        ambiguities.append("european_decimal_format")
    currencies = {_CURRENCY_CODES[match.group(0).casefold()]
                  for match in _CURRENCY_ANYWHERE.finditer(normalized)}
    money: list[Money] = []
    unattached: list[Decimal] = []
    counts: list[int] = []
    bare: list[int] = []
    units = [unit.casefold() for unit in count_units]
    for match in _NUMBER.finditer(normalized):
        span = match.span()
        if _inside(span, excluded) or _inside(span, european):
            continue
        whole, fraction = match.group(1), match.group(2)
        try:
            number = Decimal(whole.replace(",", "") + ("." + fraction if fraction else ""))
        except InvalidOperation:
            continue
        before = normalized[max(0, span[0] - 6):span[0]]
        after = normalized[span[1]:span[1] + 10]
        currency_match = _CURRENCY_BEFORE.search(before) or _CURRENCY_AFTER.search(after)
        if currency_match:
            money.append(Money(number.quantize(Decimal("0.01")),
                               _CURRENCY_CODES[currency_match.group(1).casefold()]))
            continue
        if fraction is not None:
            if len(currencies) == 1:
                money.append(Money(number.quantize(Decimal("0.01")), next(iter(currencies))))
            else:
                unattached.append(number.quantize(Decimal("0.01")))
            continue
        integer = int(number)
        tail = normalized[span[1]:span[1] + 30].casefold()
        head = normalized[max(0, span[0] - 12):span[0]]
        unit_match = (re.match(r"^\s*[" + _MEASURE + "]", tail)
                      or any(re.match(r"^\s*(?:[" + _MEASURE + r"]\s*)?(?:[a-z]+\s+){0,2}"
                                      + re.escape(unit) + r"(?![a-z])", tail)
                             for unit in units))
        if unit_match or _COUNT_PREFIX.search(head):
            counts.append(integer)
        elif len(currencies) == 1 and "," in whole:
            money.append(Money(number.quantize(Decimal("0.01")), next(iter(currencies))))
        else:
            bare.append(integer)
    return Extraction(normalized=normalized, emails=emails, phones=phones, counts=tuple(counts),
                      bare_integers=tuple(bare), money=tuple(money),
                      unattached_amounts=tuple(unattached), markers=_marker_hits(folded),
                      ambiguities=tuple(ambiguities))


def _describe(fact: Fact) -> str:
    return type(fact).__name__


def _present(fact: Fact, extraction: Extraction) -> bool:
    text = extraction.normalized
    if isinstance(fact, (Entity, Text)):
        value = fact.name if isinstance(fact, Entity) else fact.value
        return bool(_boundary_pattern(value).search(text))
    if isinstance(fact, Reference):
        return bool(re.search(r"(?<![A-Za-z0-9/_-])" + re.escape(normalize(fact.value))
                              + r"(?![A-Za-z0-9/_-])", text, re.I))
    if isinstance(fact, Email):
        return fact.value.casefold() in extraction.emails
    if isinstance(fact, Phone):
        return re.sub(r"\D", "", fact.value) in extraction.phones
    if isinstance(fact, Count):
        if fact.value in extraction.counts:
            return True
        return not extraction.counts and extraction.bare_integers.count(fact.value) == len(
            extraction.bare_integers) == 1
    if isinstance(fact, Money):
        target = Money(Decimal(fact.amount).quantize(Decimal("0.01")), fact.currency)
        return target in extraction.money
    if isinstance(fact, Marker):
        return fact.kind in extraction.markers
    raise TypeError("unknown fact")


def judge(text: str, *, required: Sequence[Fact], forbidden: Sequence[Fact] = (),
          count_units: Sequence[str] = ()) -> Judgement:
    protected = [fact.name if isinstance(fact, Entity) else fact.value
                 for fact in (*required, *forbidden)
                 if isinstance(fact, (Entity, Text, Reference))]
    extraction = extract(text, count_units=count_units, protected=protected)
    missing = [_describe(fact) for fact in required if not _present(fact, extraction)]
    contradictions = [_describe(fact) for fact in forbidden if _present(fact, extraction)]
    required_counts = [fact.value for fact in required if isinstance(fact, Count)]
    if required_counts and any(value not in required_counts for value in extraction.counts):
        contradictions.append("Count:other_value")
    ambiguous = False
    if any(isinstance(fact, Money) for fact in (*required, *forbidden)):
        amounts = {Decimal(fact.amount).quantize(Decimal("0.01"))
                   for fact in (*required, *forbidden) if isinstance(fact, Money)}
        if extraction.ambiguities or amounts & set(extraction.unattached_amounts):
            ambiguous = True
    if (required_counts and not extraction.counts and len(extraction.bare_integers) > 1
            and any(value in extraction.bare_integers for value in required_counts)):
        ambiguous = True
    passed = not missing and not contradictions and not ambiguous
    return Judgement(status="PASS" if passed else "FAIL", ambiguous=ambiguous and not passed,
                     missing=tuple(missing), contradictions=tuple(contradictions))
