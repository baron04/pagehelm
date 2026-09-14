"""Answer variants generated only from a task's fact specification.

The extractor is qualified on a corpus written before any compared system has answered: each
specification yields correct answers in several legitimate styles, incorrect answers that
change exactly one fact, and answers the extractor must refuse to decide. A single variant
judged differently from its label is a misjudgment, and the qualification requires none.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from decimal import Decimal

from .extractor import Count, Email, Entity, Fact, Marker, Money, Phone, Reference, Text

MARKER_PHRASES = {
    "not_found": ("没有找到符合条件的记录", "No matching record was found"),
    "undetermined": ("目前无法确定这个值", "I cannot determine this value"),
    "partial": ("以下只是部分结果", "These are partial results"),
    "permission_refusal": ("当前账号没有权限执行这个操作", "This account is not permitted to do that"),
    "duplicate_refusal": ("同名客户已存在", "A customer with that name already exists"),
    "nothing_to_invoice_refusal": ("这张订单没有可开票的内容", "There is nothing left to invoice"),
    "unsupported_refusal": ("这个操作不在支持范围内", "That operation is not supported"),
    "clarification_request": ("请问您指的是哪一个", "Which one do you mean"),
    "cancelled_no_action": ("好的，已取消，不再执行", "Okay, cancelled; no action was taken"),
    "not_executed": ("操作未执行", "The change was not executed"),
    "write_succeeded": ("已创建成功", "The record has been created"),
}
SUCCESS_SENTENCE = ("已创建成功。", "The record has been created.")
_SYMBOL = {"USD": "$", "EUR": "€"}
_CHINESE = {"USD": "美元", "EUR": "欧元"}


@dataclass(frozen=True)
class Variant:
    text: str
    expected: str
    label: str


def _money_forms(fact: Money) -> list[str]:
    amount = Decimal(fact.amount).quantize(Decimal("0.01"))
    grouped = f"{amount:,.2f}"
    plain = f"{amount:.2f}"
    return [f"{_SYMBOL[fact.currency]}{grouped}", f"{grouped} {fact.currency}",
            f"{fact.currency} {plain}", f"{grouped} {_CHINESE[fact.currency]}"]


def _phrase(fact: Fact, *, locale: str, style: int, count_units: Sequence[str],
            bold: bool = False) -> str:
    wrap = (lambda value: f"**{value}**") if bold else (lambda value: value)
    zh = locale == "zh"
    if isinstance(fact, Entity):
        return wrap(fact.name if style % 2 == 0 else fact.name.upper() if style == 3
                    else fact.name)
    if isinstance(fact, Email):
        return f"{'邮箱' if zh else 'email'}: {wrap(fact.value)}"
    if isinstance(fact, Phone):
        value = fact.value.replace(" ", "-") if style == 1 else (
            fact.value.lstrip("+") if style == 2 else fact.value)
        return f"{'电话' if zh else 'phone'}: {wrap(value)}"
    if isinstance(fact, Reference):
        return f"{'单号' if zh else 'reference'} {wrap(fact.value)}"
    if isinstance(fact, Text):
        return wrap(fact.value)
    if isinstance(fact, Count):
        number = f"{fact.value:,}" if style == 1 else str(fact.value)
        english = next((unit for unit in count_units if unit.isascii()), "records")
        return f"共 {wrap(number)} 个" if zh else f"{wrap(number)} {english}"
    if isinstance(fact, Money):
        return wrap(_money_forms(fact)[style % 4])
    if isinstance(fact, Marker):
        return MARKER_PHRASES[fact.kind][0 if zh else 1]
    raise TypeError("unknown fact")


def _sentence(facts: Sequence[Fact], *, locale: str, style: int, count_units: Sequence[str],
              bold: bool = False) -> str:
    parts = [_phrase(fact, locale=locale, style=style, count_units=count_units, bold=bold)
             for fact in facts]
    return ("，".join(parts) + "。") if locale == "zh" else ("; ".join(parts) + ".")


def render_variants(required: Sequence[Fact], forbidden: Sequence[Fact] = (), *,
                    distractors: Sequence[Fact] = (),
                    count_units: Sequence[str] = ()) -> list[Variant]:
    variants: list[Variant] = []
    required = list(required)
    for locale in ("zh", "en"):
        for style in range(4):
            variants.append(Variant(_sentence(required, locale=locale, style=style,
                                              count_units=count_units),
                                    "PASS", f"correct:{locale}:style{style}"))
        variants.append(Variant(_sentence(required, locale=locale, style=0,
                                          count_units=count_units, bold=True),
                                "PASS", f"correct:{locale}:bold"))
        variants.append(Variant("\n".join("- " + _phrase(fact, locale=locale, style=0,
                                                         count_units=count_units)
                                          for fact in required),
                                "PASS", f"correct:{locale}:bullets"))
        header = "| 项目 | 内容 |" if locale == "zh" else "| Item | Value |"
        rows = "\n".join(f"| {index + 1} | {_phrase(fact, locale=locale, style=0, count_units=count_units)} |"
                         for index, fact in enumerate(required))
        variants.append(Variant(f"{header}\n| --- | --- |\n{rows}", "PASS",
                                f"correct:{locale}:table"))
        tail = "如需更多信息请告诉我。" if locale == "zh" else "Let me know if you need more."
        variants.append(Variant(_sentence(required, locale=locale, style=0,
                                          count_units=count_units) + " " + tail,
                                "PASS", f"correct:{locale}:extra_sentence"))
    base = dict(locale="zh", style=0, count_units=count_units)
    for index, fact in enumerate(required):
        rest = required[:index] + required[index + 1:]
        if rest:
            variants.append(Variant(_sentence(rest, **base), "FAIL",
                                    f"incorrect:drop:{type(fact).__name__}:{index}"))
        swapped: list[Fact] = []
        if isinstance(fact, Money):
            swapped = [replace(fact, amount=Decimal(fact.amount) + Decimal("0.01")),
                       replace(fact, amount=Decimal(fact.amount) * 10),
                       replace(fact, currency="EUR" if fact.currency == "USD" else "USD")]
        elif isinstance(fact, Count):
            swapped = [Count(fact.value + 1), Count(max(0, fact.value - 1))]
        else:
            swapped = [item for item in distractors if type(item) is type(fact) and item != fact]
        for position, other in enumerate(swapped):
            candidate = required[:index] + [other] + required[index + 1:]
            variants.append(Variant(_sentence(candidate, **base), "FAIL",
                                    f"incorrect:swap:{type(fact).__name__}:{index}:{position}"))
    for index, fact in enumerate(forbidden):
        variants.append(Variant(_sentence(required + [fact], **base), "FAIL",
                                f"incorrect:forbidden:{type(fact).__name__}:{index}"))
    if Marker("write_succeeded") in forbidden:
        variants.append(Variant(_sentence(required, **base) + SUCCESS_SENTENCE[0], "FAIL",
                                "incorrect:success_claim:zh"))
        variants.append(Variant(_sentence(required, locale="en", style=0,
                                          count_units=count_units) + " " + SUCCESS_SENTENCE[1],
                                "FAIL", "incorrect:success_claim:en"))
    counts = [fact for fact in required if isinstance(fact, Count)]
    if counts:
        value = counts[0].value
        others = [fact for fact in required if not isinstance(fact, Count)]
        prefix = _sentence(others, **base) + " " if others else ""
        variants.append(Variant(f"{prefix}可能是 {value} 或 {value + 3}。", "AMBIGUOUS",
                                "ambiguous:two_bare_integers"))
    money = [fact for fact in required if isinstance(fact, Money)]
    if len(money) == 1:
        amount = Decimal(money[0].amount).quantize(Decimal("0.01"))
        european = f"{amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        others = [fact for fact in required if not isinstance(fact, Money)]
        prefix = _sentence(others, **base) + " " if others else ""
        variants.append(Variant(f"{prefix}合计 {european} {_SYMBOL[money[0].currency]}。",
                                "AMBIGUOUS", "ambiguous:european_decimal"))
        variants.append(Variant(f"{prefix}合计 {amount:,.2f}（订单包含 USD 与 EUR）。",
                                "AMBIGUOUS", "ambiguous:unattached_two_currencies"))
    return variants
