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


def unavailable_answer(user_text: str) -> str:
    return ("目前掌握的信息还不足以可靠回答这个问题。可以缩小查询范围，或补充需要查询的对象。"
            if _language(user_text) == "zh" else
            "The available information is not enough to answer reliably. "
            "Try narrowing the scope or specifying the record to look up.")


def render_payload(payload: dict, *, user_text: str, max_chars: int) -> str:
    data = dict(payload)
    presentation = data.pop("presentation", "brief")
    action = data.pop("action", None)
    if action is not None:
        if action["kind"] == "navigation":
            label = "已打开页面" if _language(user_text) == "zh" else "Opened the page"
            return f"{label}：{_literal(action['url'])}"[:max_chars]
        label = "已完成已批准的操作" if _language(user_text) == "zh" else "Completed the approved action"
        text = (f"{label}：{_literal(action['effect'])}\n" +
                "、".join(_literal(x) for x in action["targets"]))
        if action.get("navigation_url"):
            opened = "已打开页面" if _language(user_text) == "zh" else "Opened the page"
            text += f"\n{opened}：{_literal(action['navigation_url'])}"
        return text[:max_chars]
    return render(VerifiedAnswer(**data), user_text=user_text,
                  max_chars=max_chars, presentation=presentation)


def _literal(value: object) -> str:
    """Application values are data, including any Markdown punctuation inside them."""
    text = str(value).replace("\r", " ").replace("\n", " ")
    return re.sub(r"([\\`*_{}\[\]()<>#!|])", r"\\\1", text)


def render(answer: VerifiedAnswer, *, user_text: str, max_chars: int,
           presentation: str = "brief") -> str:
    """Lay out the conclusions, then the prose, then what the answer does not cover."""
    if not isinstance(answer, VerifiedAnswer):
        raise TypeError("render takes a VerifiedAnswer")
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 512:
        raise ValueError("answer max_chars must be at least 512")
    words = _WORDS[_language(user_text)]
    lines: list[str] = []

    for item in answer.conclusions:
        lines.extend(_one(words, item, presentation=presentation))
        lines.append("")

    if answer.prose.strip():
        lines.append(answer.prose.strip())
        lines.append("")

    footer = []
    if answer.coverage != "complete":
        footer.append(f"> **{words['partial']}**")
        for reason in answer.lowered_because:
            footer.append(f"> - {words['partial_why']}: {_literal(reason)}")
    text = "\n".join(lines + footer).strip()
    if len(text) <= max_chars:
        return text
    notice = ("显示限制：部分内容未展示。" if _language(user_text) == "zh"
              else "Display limit: some content is not shown.")
    # Preserve the partial-result qualification even when a long table reaches the bound.
    footer_text = "\n".join(footer)
    if len(footer_text) > max_chars // 2:
        footer_text = f"> **{words['partial']}**"
    suffix = "\n\n" + notice + ("\n" + footer_text if footer_text else "")
    kept = []
    for line in "\n".join(lines).splitlines():
        if len("\n".join(kept + [line])) + len(suffix) > max_chars:
            break
        kept.append(line)
    return "\n".join(kept).rstrip() + suffix


def _one(words: dict, item: dict, *, presentation: str) -> list[str]:
    kind = item["kind"]
    if kind == "page_observation":
        label = "页面显示" if words is _WORDS["zh"] else "The page shows"
        return [f"**{label}**（{_literal(item['detail'])}）：{_literal(item['value'])}",
                f"> {_source_line(words, item)} · {_literal(item['page_route'])} · "
                f"{_literal(item['scope'])}"]
    if kind == "field_value":
        shown = _literal(item["value"]) if not item["empty"] else words["empty"]
        unit = f" {_literal(item['unit'])}" if item.get("unit") else ""
        return [f"**{_literal(item['field_meaning'])}**（{_literal(item['record_id'])}）：{shown}{unit}",
                f"> {_source_line(words, item)} · {words['scope']}：{_literal(item['scope'])}"]
    if kind == "selection":
        head = f"**{_literal(item['condition'])}**：{item['matched']} {words['matched']}"
        if item.get("fields"):
            rows = []
            if presentation != "list":
                rows += ["| " + " | ".join(_literal(x) for x in item["fields"]) + " |",
                         "| " + " | ".join("---" for _ in item["fields"]) + " |"]
            for values in item["rows"]:
                cells = [(words["empty"] if x["value"] is None else _literal(x["value"])) +
                         (" " + _literal(x["unit"]) if x["unit"] else "") for x in values]
                if presentation == "list":
                    rows.append("- " + "; ".join(f"{_literal(key)}: {value}"
                                                for key, value in zip(item["fields"], cells)))
                else:
                    rows.append("| " + " | ".join(cells) + " |")
            return [head, "", *rows, "",
                    f"> {_source_line(words, item)} · {words['scope']}：{_literal(item['scope'])}"]
        rows = "".join(f"\n- {_literal(name)}" for name in item["members"])
        return [head + rows,
                f"> {_source_line(words, item)} · {words['scope']}：{_literal(item['scope'])}"]
    if kind == "aggregate":
        operation = item["operation"].partition("(")[0]
        chinese = words is _WORDS["zh"]
        labels = ({"count": "数量", "min": "最小值", "max": "最大值"} if chinese else
                  {"count": "Count", "min": "Minimum", "max": "Maximum"})
        label = (words["total"] if item["covers_whole_scope"] else words["subtotal"]
                 ) if operation == "sum" else labels.get(operation, _literal(item["operation"]))
        if operation != "sum" and not item["covers_whole_scope"]:
            label += "（仅限已读取范围）" if chinese else " (observed scope only)"
        unit = f" {_literal(item['unit'])}" if item.get("unit") else ""
        return [f"**{label}**：{_literal(item['value'])}{unit}",
                f"> {_source_line(words, item)} · {words['scope']}：{_literal(item['scope'])}"]
    if kind == "not_found":
        conditions = "；".join(_literal(x) for x in item["conditions"])
        return [f"**{words['not_found']}**：{_literal(item['looked_for'])}",
                f"> {_source_line(words, item)} · {words['scope']}：{_literal(item['scope'])}"
                f" · {conditions}"]
    if kind == "undetermined":
        return [f"**{words['undetermined']}**：{_literal(item['about'])}",
                f"> {words['gap']}：{_literal(item['gap'])}"]
    raise ValueError(f"no way to render a {kind!r} conclusion")  # pragma: no cover
