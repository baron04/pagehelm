"""How a resolved scope is described to the person, in the language they asked in.

These words become the heading of an answer ("销售订单 · 下单时间在上个月"). They are
never read back by the resolver or the model: a repair the model reads keeps the index's
terms, whatever the person's language.
"""
from __future__ import annotations

from .words import PERIODS

#: The Chinese name of each relative period, for a value the model wrote in English.
_PERIOD_NAMES_ZH = {"today": "今天", "yesterday": "昨天", "this_week": "本周", "last_week": "上周",
                    "this_month": "本月", "last_month": "上个月", "this_quarter": "本季度",
                    "last_quarter": "上季度", "this_year": "今年", "last_year": "去年",
                    "overdue": "逾期"}
_PERIOD_ZH_BY_WORD = {w: _PERIOD_NAMES_ZH[n] for w, n in PERIODS.items() if n in _PERIOD_NAMES_ZH}

_OPS_ZH = {"": "为", "=": "为", "!=": "不为", ">": "大于", ">=": "不小于", "<": "小于",
           "<=": "不大于", "contains": "包含", "starts with": "开头是", "between": "介于"}


#: The words a yes/no condition is written with, as the answer's own language says them.
_YES_NO_ZH = {"yes": "是", "true": "是", "no": "否", "false": "否"}


def _cjk(ch: str) -> bool:
    return "\u3400" <= ch <= "\u9fff"


#: Full-width punctuation takes no space on either side.
_PUNCTUATION = "“”（）：，、"


def _join(*parts: str) -> str:
    """Parts run together as Chinese is written, with a space where Latin text meets
    Chinese ("Status 为 Posted", "下单时间在上个月")."""
    out = ""
    for part in parts:
        if (out and part and _cjk(out[-1]) != _cjk(part[0])
                and not out[-1].isspace() and not part[0].isspace()
                and out[-1] not in _PUNCTUATION and part[0] not in _PUNCTUATION):
            out += " "
        out += part
    return out


class Phrases:
    def __init__(self, language: str = ""):
        self.zh = language.lower().startswith("zh")

    def _value(self, value: str) -> str:
        if not self.zh:
            return value
        # A span written as two ends ("2026-10-01..2026-10-10").
        ends = value.split("..")
        if len(ends) == 2 and all(e.strip() for e in ends):
            return _join(self._value(ends[0].strip()), "至", self._value(ends[1].strip()))
        word = " ".join(value.strip().lower().split())
        if word in _YES_NO_ZH:
            # A yes/no word is said in the answer's language, not echoed as the declarer's
            # own token: "是否已支付 = no" is neither language's sentence.
            return _YES_NO_ZH[word]
        return _PERIOD_ZH_BY_WORD.get(word, value)

    def compare(self, label: str, op: str, values) -> str:
        if not self.zh:
            return f"{label} {op or '='} {' / '.join(values)}"
        values = [self._value(v) for v in values]
        if op == "between" and len(values) == 2:
            # Two ends, not two alternatives.
            return _join(label, "介于", values[0], "至", values[1])
        return _join(label, _OPS_ZH.get(op, op), _join(*_alternatives(values)))

    def filled(self, label: str, wanted: bool) -> str:
        if not self.zh:
            return f"{label} {'set' if wanted else 'not set'}"
        return _join(label, "已填" if wanted else "未填")

    def holds(self, label: str, values) -> str:
        return (_join(label, "包含", *_alternatives(list(values))) if self.zh
                else f"{label} ~ {' / '.join(values)}")

    def words(self, words: str) -> str:
        return f"含“{words}”" if self.zh else f"~{words}"

    def during(self, label: str, op: str, value: str) -> str:
        if not self.zh:
            return f"{label} {op or 'in'} {value}"
        verb = {"": "在", "!=": "不在", "<": "早于", "<=": "不晚于", ">": "晚于", ">=": "不早于"}.get(op, "在")
        return _join(label, verb, self._value(value))

    def between(self, label: str, first: str, second: str) -> str:
        if not self.zh:
            return f"{label} between {first} and {second}"
        return _join(label, "在", self._value(first), "至", self._value(second), "之间")

    def beyond_defaults(self) -> str:
        return "不限页面默认筛选" if self.zh else "beyond the page's default filters"

    def as_meant(self, terms) -> str:
        joined = " / ".join(terms)
        return _join("按本组织对“", joined, "”的口径") if self.zh else f"as {joined} means here"

    def excluding(self, label: str) -> str:
        return _join("不含", label) if self.zh else f"not {label}"

    def ordered(self, label: str, descending: bool) -> str:
        if not self.zh:
            return f"by {label}" + (" desc" if descending else "")
        return _join("按", label, "降序" if descending else "升序")

    def any_status(self, label: str, count: int) -> str:
        return _join(label, "（不限状态）：", str(count)) if self.zh else f"{label}, any status: {count}"


def _alternatives(values: list[str]) -> list[str]:
    out: list[str] = []
    for i, value in enumerate(values):
        if i:
            out.append("或")
        out.append(value)
    return out


__all__ = ["Phrases"]
