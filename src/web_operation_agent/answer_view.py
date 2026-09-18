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
        "not_found_scope": "仅指本次查询过、且当前账号有权访问的范围，不代表整个系统中不存在",
        "capability_gap": "当前会话不支持这项操作，本次没有执行任何更改。",
        "partial": "已回答目前可以确认的部分",
        "partial_why": "还无法确认",
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
        "not_found_scope": ("only within what this search covered and the current account can "
                            "access; not a claim that it exists nowhere"),
        "capability_gap": ("That operation is not supported in this session, "
                            "so it was not executed."),
        "partial": "Answered the part supported by current data",
        "partial_why": "still not confirmed",
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


def capability_gap_answer(user_text: str) -> str:
    """The one sentence a capability gap gets, fixed here rather than left to the model.

    It says what the check found and what followed: this session cannot perform the change,
    and nothing ran. It is deliberately about *this session*, not about the product forever --
    installing the capability is a configuration away, and a sentence claiming permanent
    non-support would be false the moment someone installs it.

    It also deliberately avoids saying the capability was "not found". The first draft read
    "没有找到可执行这项操作的能力", which is literally a not-found sentence, and a not-found
    sentence is the exact confusion this whole path exists to end: a missing capability is
    not an absent record.
    """
    return ("当前会话不支持这项操作，本次没有执行任何更改。"
            if _language(user_text) == "zh" else
            "That operation is not supported in this session, so it was not executed.")


def withdrawn_answer(user_text: str) -> str:
    """The reply to "never mind" after this session asked the user a question.

    Fixed here for the same reason as the capability gap. Left to the model, it was written
    as ``finish``'s reason, which the model treats as a note about the user rather than a
    reply to them, and the panel showed "the user said never mind and withdrew the request".
    """
    return ("好的，已取消，本次没有执行任何操作。"
            if _language(user_text) == "zh" else
            "OK, cancelled. Nothing was done.")


def unavailable_answer(user_text: str) -> str:
    return ("目前掌握的信息还不足以可靠回答这个问题。可以缩小查询范围，或补充需要查询的对象。"
            if _language(user_text) == "zh" else
            "The available information is not enough to answer reliably. "
            "Try narrowing the scope or specifying the record to look up.")


def render_payload(payload: dict, *, user_text: str, max_chars: int) -> str:
    data = dict(payload)
    presentation = data.pop("presentation", "brief")
    data.pop("sources", None)
    action = data.pop("action", None)
    action_text = ""
    if action is not None:
        if action["kind"] == "navigation":
            label = "已打开页面" if _language(user_text) == "zh" else "Opened the page"
            action_text = f"{label}：{_literal(action['url'])}"
        else:
            label = "已完成已批准的操作" if _language(user_text) == "zh" else "Completed the approved action"
            action_text = (f"{label}：{_literal(action['effect'])}\n" +
                           "、".join(_literal(x) for x in action["targets"]))
            if action.get("navigation_url"):
                opened = "已打开页面" if _language(user_text) == "zh" else "Opened the page"
                action_text += f"\n{opened}：{_literal(action['navigation_url'])}"
    return render(VerifiedAnswer(**data), user_text=user_text,
                  max_chars=max_chars, presentation=presentation, action_text=action_text)


def _literal(value: object) -> str:
    """Application values are data, including any Markdown punctuation inside them."""
    text = str(value).replace("\r", " ").replace("\n", " ")
    return re.sub(r"([\\`*_{}\[\]()<>#!|])", r"\\\1", text)


def render_sources(payload: dict, *, user_text: str, max_chars: int) -> str:
    """Optional provenance view; limitations remain visible in the main answer."""
    chinese = _language(user_text) == "zh"
    words = _WORDS["zh" if chinese else "en"]
    blocks = []
    for source in payload.get("sources", ()):
        lines = [f"**{_source_line(words, {'source_kind': source['kind']})}**",
                 f"{'读取或计算时间' if chinese else 'Read or computation time'}: {_literal(source['observed_at'])}",
                 f"{words['scope']}: {_literal(source['scope'])}"]
        if "operation" in source:
            lines.append(f"{'运算' if chinese else 'Operation'}: {_literal(source['operation'])}")
        if "collection" in source:
            lines.append(f"{'集合' if chinese else 'Collection'}: {_literal(source['collection'])}")
            lines.append(f"{'扫描记录' if chinese else 'Records scanned'}: {source['records_scanned']}; "
                         f"{'匹配记录' if chinese else 'Matches'}: {source['matched']}")
            coverage = ("覆盖该次读取范围" if source['covers_whole_scope'] else "只覆盖部分读取范围") if chinese else (
                "Covers this read scope" if source['covers_whole_scope'] else "Covers only part of this read scope")
            lines.append(coverage)
        blocks.append("\n\n".join(lines))
    text = "\n\n---\n\n".join(blocks)
    if len(text) <= max_chars:
        return text
    suffix = "\n\n来源详情达到显示限制。" if chinese else "\n\nSource details reached the display limit."
    return text[:max(0, max_chars - len(suffix))] + suffix


def render(answer: VerifiedAnswer, *, user_text: str, max_chars: int,
           presentation: str = "brief", action_text: str = "") -> str:
    """Lay out the conclusions, then the prose, then what the answer does not cover."""
    if not isinstance(answer, VerifiedAnswer):
        raise TypeError("render takes a VerifiedAnswer")
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 512:
        raise ValueError("answer max_chars must be at least 512")
    words = _WORDS[_language(user_text)]
    lines: list[str] = [action_text, ""] if action_text else []

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
    if kind == "capability_gap":
        # No source line: this conclusion stands on a search of this session's own catalog,
        # not on anything read from the application, and labelling it "source: typed read"
        # would say the application answered a question it was never asked.
        return [words["capability_gap"]]
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
        looked_for = str(item.get("looked_for") or "").strip()
        if looked_for:
            if words is _WORDS["zh"]:
                head = f"**已存在匹配记录**：{_literal(looked_for)}（{item['matched']} 条）"
            else:
                noun = "record" if item["matched"] == 1 else "records"
                head = (f"**Matching {noun} found**: {_literal(looked_for)} "
                        f"({item['matched']})")
        else:
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
        # The normal answer names the thing that was absent. Executed filters and scope are
        # already available in the separate answer-basis disclosure; repeating adapter query
        # syntax here turns a one-line response into an internal diagnostic. What stays is
        # the limit of the claim: a search sees only what this actor may see, so absence
        # there says nothing about records in a scope the actor cannot read.
        return [f"**{words['not_found']}**：{_literal(item['looked_for'])}",
                f"> {words['not_found_scope']}"]
    if kind == "undetermined":
        return [f"**{words['undetermined']}**：{_literal(item['about'])}",
                f"> {words['gap']}：{_literal(item['gap'])}"]
    raise ValueError(f"no way to render a {kind!r} conclusion")  # pragma: no cover
