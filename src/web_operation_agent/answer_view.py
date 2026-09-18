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
        "lang": "zh",
        "undetermined": "无法确定",
        "not_found": "未找到",
        "not_found_scope": "仅指本次查询过、且当前账号有权访问的范围，不代表整个系统中不存在",
        "capability_gap": "当前会话不支持这项操作，本次没有执行任何更改。",
        "partial": "已回答目前可以确认的部分",
        "gap": "缺口",
        "scope": "范围",
        "source_page": "来源：页面观察",
        "source_typed": "来源：应用的类型化读取",
        "source_computed": "来源：在本轮读到的数据上计算",
        "empty": "（该字段已读取，内容为空）",
        "page_scope": "只代表当时页面上显示的内容，不代表完整数据",
        "subtotal": "小计（未覆盖全部范围）",
        "total": "合计",
    },
    "en": {
        "lang": "en",
        "undetermined": "Could not determine",
        "not_found": "Not found",
        "not_found_scope": ("only within what this search covered and the current account can "
                            "access; not a claim that it exists nowhere"),
        "capability_gap": ("That operation is not supported in this session, "
                            "so it was not executed."),
        "partial": "Answered the part supported by current data",
        "gap": "gap",
        "scope": "scope",
        "source_page": "source: page observation",
        "source_typed": "source: the application's typed read",
        "source_computed": "source: computed over what this turn read",
        "empty": "(read, and holds nothing)",
        "page_scope": "only what the page showed at that moment; not a complete collection",
        "subtotal": "subtotal (does not cover the whole scope)",
        "total": "total",
    },
}


def _language(user_text: str) -> str:
    return "zh" if _HAN.search(user_text or "") else "en"


def _source_line(words: dict, item: dict) -> str:
    return {"page": words["source_page"], "typed": words["source_typed"],
            "computed": words["source_computed"]}[item["source_kind"]]


def _named(words: dict, text: str) -> str:
    """What the application calls this field or scope in the reader's language, if it said."""
    return words.get("labels", {}).get(text, text)


def _field_label(words: dict, name: str, meaning: str) -> str:
    return words.get("labels", {}).get(name, meaning)


def _value_label(words: dict, field: str, value: str) -> str:
    """A coded value -- a state such as "sent" -- as the application names it for the reader.

    Keyed by field and value together, so a customer who happens to be called "Sale" is
    never translated as an order state.
    """
    return words.get("labels", {}).get(f"{field}={value}", value)


def _basis(words: dict, item: dict) -> str:
    return (f"> {_source_line(words, item)} · {words['scope']}："
            f"{_literal(_named(words, item['scope']))}")


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
    labels = (data.pop("labels", None) or {}).get(_language(user_text), {})
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
                  max_chars=max_chars, presentation=presentation, action_text=action_text,
                  labels=labels)


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
           presentation: str = "brief", action_text: str = "",
           labels: dict[str, str] | None = None) -> str:
    """Lay out the conclusions, then the prose, then what the answer does not cover."""
    if not isinstance(answer, VerifiedAnswer):
        raise TypeError("render takes a VerifiedAnswer")
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 512:
        raise ValueError("answer max_chars must be at least 512")
    words = {**_WORDS[_language(user_text)], "labels": dict(labels or {})}
    lines: list[str] = [action_text, ""] if action_text else []

    items = answer.conclusions
    index = 0
    while index < len(items):
        end = index
        while end < len(items) and items[end]["kind"] == "field_value":
            end += 1
        if len({item["record_id"] for item in items[index:end]}) > 1:
            lines.extend(_field_table(words, items[index:end]))
            lines.append("")
            index = end
            continue
        lines.extend(_one(words, items[index], presentation=presentation))
        lines.append("")
        index += 1

    if answer.prose.strip():
        lines.append(answer.prose.strip())
        lines.append("")

    footer = []
    if answer.coverage != "complete":
        footer.append(f"> **{words['partial']}**")
        for reason in _partial_reasons(words, answer):
            footer.append(f"> - {reason}")
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


def _selection_head(words: dict, item: dict) -> str:
    """How many records meet the condition, how many are listed below, and -- for a search
    by name -- whether a record of exactly that name is among them.

    Not the condition itself: that is adapter query syntax ("customer_rank > 0"), and it stays
    in the answer basis. Not the listed count presented as the matching count: a read limited
    to 20 rows of 72 matches headlined "20 match". And not "a matching record exists" on the
    strength of a search that matches parts of names: asked whether a customer existed, a
    search for the first word of its name found six other customers and the answer said one
    existed.
    """
    zh = words["lang"] == "zh"
    shown = item["matched"]
    total = item.get("total", shown)
    if item.get("count_exact", item["covers_whole_scope"]) and shown >= total:
        count = f"共 {total} 条" if zh else f"{total} in total"
    elif item.get("count_exact", False):
        count = (f"共 {total} 条，下面列出其中 {shown} 条" if zh
                 else f"{total} in total, {shown} listed below")
    else:
        count = (f"下面列出 {shown} 条，符合条件的总数未能确认" if zh
                 else f"{shown} listed below; the total could not be confirmed")
    if item.get("source_kind") == "computed":
        # A computed selection's condition is the model's own filter over what was read --
        # "date_order ge 2026-08-18" for "orders in the last month" -- and it is part of the
        # answer: the reader needs the dates the list stands on.
        applied = _readable_condition(str(item.get("condition") or ""), words)
        if applied:
            count += f"（筛选：{applied}）" if zh else f" (filter: {applied})"
    looked_for = _literal(str(item.get("looked_for") or "").strip())
    if not looked_for:
        return f"**{count}**"
    exact = item.get("exact_matches") or 0
    if exact and exact == total:
        return (f"**已存在匹配记录**：{looked_for}（{exact} 条）" if zh else
                f"**{'Matching record' if exact == 1 else 'Matching records'} found**: "
                f"{looked_for} ({exact})")
    if exact:
        return (f"**已存在匹配记录**：{looked_for}（{exact} 条）；与“{looked_for}”匹配的{count}"
                if zh else
                f"**{'Matching record' if exact == 1 else 'Matching records'} found**: "
                f"{looked_for} ({exact}); records matching \"{looked_for}\": {count}")
    return (f"**与“{looked_for}”匹配的记录**：{count}" if zh else
            f"**Records matching \"{looked_for}\"**: {count}")


_SYMBOLS = {"gt": ">", "ge": "≥", "lt": "<", "le": "≤", "eq": "=", "ne": "≠"}


def _readable_condition(condition: str, words: dict) -> str:
    """A compute condition in the reader's notation: "amount_total gt 2000" as "含税总额 >
    2000", "argmax(amount_total)" as the highest total. Anything else as written."""
    zh = words["lang"] == "zh"
    parts = []
    for part in (piece.strip() for piece in condition.split(";")):
        if not part:
            continue
        if match := re.fullmatch(r"(\S+) (gt|ge|lt|le|eq|ne) (.+)", part):
            part = (f"{_named(words, match[1])} {_SYMBOLS[match[2]]} "
                    f"{_value_label(words, match[1], match[3])}")
        elif match := re.fullmatch(r"(\S+) in (.+)", part):
            part = f"{_named(words, match[1])} {'属于' if zh else 'in'} {match[2]}"
        elif match := re.fullmatch(r"(argmax|argmin)\((\S+)\)", part):
            highest = match[1] == "argmax"
            field = _named(words, match[2])
            part = (f"{field}最大" if highest else f"{field}最小") if zh else (
                f"{'highest' if highest else 'lowest'} {field}")
        parts.append(_literal(part))
    return "；".join(parts) if zh else "; ".join(parts)


def _partial_reasons(words: dict, answer: VerifiedAnswer) -> list[str]:
    """What the answer does not cover, said to the reader.

    `lowered_because` records the same facts for the ledger, in the terms the verifier
    checks them in: adapter conditions and scope strings, in English whatever the reader's
    language. Shown as they were, a Chinese answer ended with "customer_rank > 0 covered only
    part of customers visible to the delegated user".
    """
    zh = words["lang"] == "zh"
    reasons = []
    for item in answer.conclusions:
        kind = item["kind"]
        if kind == "undetermined":
            reasons.append(f"{words['undetermined']}：{_literal(item['about'])}")
        elif kind == "selection" and not item["covers_whole_scope"]:
            rest = item.get("total", item["matched"]) - item["matched"]
            if item.get("count_exact", False) and rest > 0:
                reasons.append(
                    f"其余 {rest} 条没有列出。可以加上条件缩小范围后再问，或到应用的列表页查看全部。"
                    if zh else
                    f"The other {rest} are not listed. Narrow the question, or open the list "
                    "in the application to see them all.")
            else:
                reasons.append("列出的只是读取到的部分记录，可能还有其他记录符合条件。" if zh else
                               "Only some of the records were read; others may match as well.")
        elif kind == "aggregate" and not item["covers_whole_scope"]:
            reasons.append("上面的计算只基于读取到的部分记录，不是整个范围的结果。" if zh else
                           "The computation covers only the records that were read, not the "
                           "whole scope.")
        elif kind == "not_found" and not item["covers_whole_scope"]:
            looked_for = _literal(item["looked_for"])
            reasons.append(f"对“{looked_for}”的搜索只覆盖了部分范围。" if zh else
                           f"The search for {looked_for} covered only part of the scope.")
    return reasons or [_literal(reason) for reason in answer.lowered_because]


def _field_table(words: dict, run: tuple[dict, ...]) -> list[str]:
    """Field values of several records, one row per record.

    Asked for last month's orders and their dates, the model cited each order's reference
    and date as separate field values, and eight of them rendered as eight headed lines, each
    with an internal id ("sale.order/13") and its own source line. The facts were right; the
    answer was unreadable. A record's own name identifies its row when it is among the
    fields; otherwise the record id has to.
    """
    zh = words["lang"] == "zh"
    records = list(dict.fromkeys(item["record_id"] for item in run))
    fields = list(dict.fromkeys(item["field_name"] for item in run))
    meaning = {}
    for item in run:
        meaning.setdefault(item["field_name"], item["field_meaning"])
    cells = {(item["record_id"], item["field_name"]): item for item in run}
    show_id = "name" not in fields
    header = (["记录" if zh else "record"] if show_id else []) + [
        _field_label(words, f, meaning[f]) for f in fields]
    rows = ["| " + " | ".join(_literal(x) for x in header) + " |",
            "| " + " | ".join("---" for _ in header) + " |"]
    for record in records:
        values = [_literal(record)] if show_id else []
        for field in fields:
            item = cells.get((record, field))
            if item is None:
                values.append("—")
            elif item["empty"]:
                values.append(words["empty"])
            else:
                unit = f" {_literal(item['unit'])}" if item.get("unit") else ""
                values.append(_literal(_value_label(words, field, item["value"])) + unit)
        rows.append("| " + " | ".join(values) + " |")
    sources = dict.fromkeys(_basis(words, item) for item in run)
    return [*rows, "", *sources]


def _gap(words: dict, item: dict) -> str:
    """Why something could not be determined, in the reader's language.

    The verifier records the gap in its own terms ("phone of res.partner/13 was not read");
    the reader gets the same fact without the record id.
    """
    zh = words["lang"] == "zh"
    field = _literal(_field_label(words, item.get("field_name", ""), item.get("field_name", "")))
    return {
        "field_not_read": f"没有读取{field}" if zh else f"{field} was not read",
        "control_only": (f"页面上只有“{_literal(item.get('detail', ''))}”，没有显示值" if zh
                         else f"the page showed {_literal(item.get('detail', ''))} and no value"),
        "partial_read": ("这次读取只覆盖了部分范围" if zh
                         else "the read covered only part of the scope"),
    }.get(item.get("gap_kind", ""), _literal(item["gap"]))


def _one(words: dict, item: dict, *, presentation: str) -> list[str]:
    kind = item["kind"]
    if kind == "capability_gap":
        # No source line: this conclusion stands on a search of this session's own catalog,
        # not on anything read from the application, and labelling it "source: typed read"
        # would say the application answered a question it was never asked.
        return [words["capability_gap"]]
    if kind == "page_observation":
        label = "页面显示" if words["lang"] == "zh" else "The page shows"
        # The scope of a page fact is always the same claim -- this page, at that moment --
        # and its own text carries an observation epoch the reader has no use for; that text
        # stays in the answer basis.
        return [f"**{label}**（{_literal(item['detail'])}）：{_literal(item['value'])}",
                f"> {_source_line(words, item)} · {_literal(item['page_route'])} · "
                f"{words['page_scope']}"]
    if kind == "field_value":
        shown = (_literal(_value_label(words, item["field_name"], item["value"]))
                 if not item["empty"] else words["empty"])
        unit = f" {_literal(item['unit'])}" if item.get("unit") else ""
        meaning = _field_label(words, item["field_name"], item["field_meaning"])
        who = item.get("record_label") or item["record_id"]
        return [f"**{_literal(meaning)}**（{_literal(who)}）：{shown}{unit}", _basis(words, item)]
    if kind == "selection":
        head = _selection_head(words, item)
        if item.get("fields"):
            rows = []
            names = [_field_label(words, name, name) for name in item["fields"]]
            if presentation != "list":
                rows += ["| " + " | ".join(_literal(x) for x in names) + " |",
                         "| " + " | ".join("---" for _ in names) + " |"]
            for values in item["rows"]:
                cells = [(words["empty"] if x["value"] is None
                          else _literal(_value_label(words, field, x["value"]))) +
                         (" " + _literal(x["unit"]) if x["unit"] else "")
                         for field, x in zip(item["fields"], values)]
                if presentation == "list":
                    rows.append("- " + "; ".join(f"{_literal(key)}: {value}"
                                                for key, value in zip(names, cells)))
                else:
                    rows.append("| " + " | ".join(cells) + " |")
            return [head, "", *rows, "", _basis(words, item)]
        rows = "".join(f"\n- {_literal(name)}" for name in item["members"])
        return [head + rows, _basis(words, item)]
    if kind == "aggregate":
        operation = item["operation"].partition("(")[0]
        chinese = words["lang"] == "zh"
        labels = ({"count": "数量", "min": "最小值", "max": "最大值"} if chinese else
                  {"count": "Count", "min": "Minimum", "max": "Maximum"})
        label = (words["total"] if item["covers_whole_scope"] else words["subtotal"]
                 ) if operation == "sum" else labels.get(operation, _literal(item["operation"]))
        if operation != "sum" and not item["covers_whole_scope"]:
            label += "（仅限已读取范围）" if chinese else " (observed scope only)"
        # A total of the USD orders is not a total of the orders: the filter a compute chain
        # applied is part of what the number is, so it is said with it.
        applied = _readable_condition(str(item.get("filter") or ""), words)
        if applied:
            label += f"（筛选：{applied}）" if chinese else f" (filter: {applied})"
        unit = f" {_literal(item['unit'])}" if item.get("unit") else ""
        return [f"**{label}**：{_literal(item['value'])}{unit}", _basis(words, item)]
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
                f"> {words['gap']}：{_gap(words, item)}"]
    raise ValueError(f"no way to render a {kind!r} conclusion")  # pragma: no cover
