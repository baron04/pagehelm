"""Render a verified answer: the conclusions first, the evidence behind them on request.

What the reader gets is the answer to their question, not the last thing a tool returned.
The projection that used to be the answer is still here and still useful -- five rows are
the best way to read five rows -- but it is now what backs a conclusion rather than what
stands in place of one.

Two habits this deliberately does not have. It does not stamp the prose as verified: the
conclusions carry standing, the sentences around them do not, and a single grounded flag
over a paragraph would say otherwise. And it does not fall back to the raw projection when
something fails, because falling back to the last query's rows is the original defect
wearing a different hat -- the reader asked about one customer and got a table of everyone.
"""
from __future__ import annotations

import re

from .conclusion import VerifiedAnswer

#: Any Han character. The same test `answering.py` makes, and for the same reason: a marker
#: word list misses "列出所有供应商及其欠款", which contains none of the obvious ones.
_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")

_WORDS = {
    "zh": {
        "undetermined": "无法确定",
        "not_found": "未找到",
        "partial": "这是部分回答",
        "partial_why": "尚未覆盖的部分",
        "gap": "缺口",
        "scope": "范围",
        "source_page": "来源：页面观察",
        "source_typed": "来源：应用的类型化读取",
        "source_computed": "来源：在本轮读到的数据上计算",
        "empty": "（该字段已读取，内容为空）",
        "matched": "条符合",
        "subtotal": "小计（未覆盖全部范围）",
        "total": "合计",
    },
    "en": {
        "undetermined": "Could not determine",
        "not_found": "Not found",
        "partial": "This is a partial answer",
        "partial_why": "what it does not cover",
        "gap": "gap",
        "scope": "scope",
        "source_page": "source: page observation",
        "source_typed": "source: the application's typed read",
        "source_computed": "source: computed over what this turn read",
        "empty": "(read, and holds nothing)",
        "matched": "matched",
        "subtotal": "subtotal (does not cover the whole scope)",
        "total": "total",
    },
}


def _language(user_text: str) -> str:
    return "zh" if _HAN.search(user_text or "") else "en"


def _source_line(words: dict, item: dict) -> str:
    return {"page": words["source_page"], "typed": words["source_typed"],
            "computed": words["source_computed"]}[item["source_kind"]]


def render(answer: VerifiedAnswer, *, user_text: str, max_chars: int) -> str:
    """Lay out the conclusions, then the prose, then what the answer does not cover."""
    if not isinstance(answer, VerifiedAnswer):
        raise TypeError("render takes a VerifiedAnswer")
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 512:
        raise ValueError("answer max_chars must be at least 512")
    words = _WORDS[_language(user_text)]
    lines: list[str] = []

    for item in answer.conclusions:
        lines.extend(_one(words, item))
        lines.append("")

    if answer.prose.strip():
        lines.append(answer.prose.strip())
        lines.append("")

    if answer.coverage != "complete":
        lines.append(f"> **{words['partial']}**")
        for reason in answer.lowered_because:
            lines.append(f"> - {words['partial_why']}: {reason}")
    text = "\n".join(lines).strip()
    return text if len(text) <= max_chars else text[:max_chars - 1].rstrip() + "…"


def _one(words: dict, item: dict) -> list[str]:
    kind = item["kind"]
    if kind == "field_value":
        shown = item["value"] if not item["empty"] else words["empty"]
        unit = f" {item['unit']}" if item.get("unit") else ""
        return [f"**{item['field_meaning']}**（{item['record_id']}）：{shown}{unit}",
                f"> {_source_line(words, item)} · {words['scope']}：{item['scope']}"]
    if kind == "selection":
        head = f"**{item['condition']}**：{item['matched']} {words['matched']}"
        rows = "".join(f"\n- {name}" for name in item["members"])
        return [head + rows,
                f"> {_source_line(words, item)} · {words['scope']}：{item['scope']}"]
    if kind == "aggregate":
        label = words["total"] if item["covers_whole_scope"] else words["subtotal"]
        unit = f" {item['unit']}" if item.get("unit") else ""
        return [f"**{item['operation']}** {label}：{item['value']}{unit}",
                f"> {_source_line(words, item)} · {words['scope']}：{item['scope']}"]
    if kind == "not_found":
        conditions = "；".join(item["conditions"])
        return [f"**{words['not_found']}**：{item['looked_for']}",
                f"> {_source_line(words, item)} · {words['scope']}：{item['scope']}"
                f" · {conditions}"]
    if kind == "undetermined":
        return [f"**{words['undetermined']}**：{item['about']}",
                f"> {words['gap']}：{item['gap']}"]
    raise ValueError(f"no way to render a {kind!r} conclusion")  # pragma: no cover
