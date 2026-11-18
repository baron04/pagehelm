"""Answer verification, numeric grounding, delivery correction, and repeat fallback."""
from __future__ import annotations

import json
import re

from ..answer.answering import render_structured_query_answer
from ..modeling.provider import ProviderError
from .loop_guard import _NOT_EXECUTED, _NOT_EXECUTED_EN
from .model_loop_contracts import DeliveryFeedback, VerificationResult
from .read_summary import SummaryResult, render_summary


def history_messages(context) -> list[dict]:
    """Earlier turns of the conversation as the model reads them, from the bounded
    conversation state: what the person said and the answer it got, with the writes that
    turn verified. What a turn read or saw on the page is not kept."""
    told = []
    for turn in (context or {}).get("prior_turns") or ():
        answer = str(turn["assistant"])
        outcomes = (turn.get("prior_business_facts") or {}).get("outcomes")
        written = [
            "{} — {}{}".format(
                item.get("effect", ""),
                "、".join(str(t) for t in item.get("targets") or ()),
                "，记录 " + str(item["receipt"]["reference"])
                if isinstance(item.get("receipt"), dict) and item["receipt"].get("reference")
                else "",
            )
            for item in outcomes or ()
            if isinstance(item, dict) and item.get("verified") is True
        ]
        if written:
            answer += "\n（程序记录：这一轮已由用户批准、执行并回读核对的写入：" + "；".join(written) + "）"
        told += [{"role": "user", "content": str(turn["user"])}, {"role": "assistant", "content": answer}]
    return told


def delivery_correction(executor, s, turn, args, feedback, *, terminal: bool = False):
    """One correction per check/item position; then close with an honest result."""
    issue = (
        feedback
        if isinstance(feedback, DeliveryFeedback)
        else DeliveryFeedback("delivery_contract", str(feedback)[:2000])
    )
    attempts = turn.setdefault("delivery_corrections", {})
    keys = tuple((issue.code, index) for index in issue.item_indices) or ((issue.code, None),)
    returned_before = (
        bool(attempts.get((issue.code, None)))
        or (
            any(attempts.get(key) for key in keys)
            if issue.item_indices
            else any(code == issue.code for code, _index in attempts)
        )
    )
    if (
        not terminal
        and not returned_before
        and turn.get("model_calls", 0) < executor.model_steps
        and s.budget.remaining() > 1
    ):
        for key in keys:
            attempts[key] = 1
        turn["pending_delivery"] = (dict(args), issue)
        s.ledger.append(
            "model_delivery_rejected",
            reason=issue.code,
            item_indices=list(issue.item_indices),
            attempt=1,
            returned=True,
            terminal=False,
        )
        return None
    raw_items = args.get("items", ())
    items = (
        [dict(item) for item in raw_items if isinstance(item, dict)]
        if isinstance(raw_items, (list, tuple))
        else []
    )
    indices = issue.item_indices or tuple(range(len(items)))
    for i in indices:
        if i < len(items):
            items[i]["status"] = "undelivered"
    labels = "、".join(f"第 {i + 1} 项" for i in indices) or "本轮交付"
    # The reply goes out as the model wrote it. What failed the check is marked on its
    # item and said once, after it; the model's words are never swapped for ours.
    original = str(args.get("answer") or "").strip()
    note = f"（程序核对：{labels}没有通过交付检查，标为未交付。{issue.message}）"
    limit = getattr(getattr(executor, "config", None), "max_user_answer_chars", 4000)
    answer = (original[:max(0, limit - len(note) - 2)] + "\n\n" + note) if original else note[:limit]
    turn.pop("pending_delivery", None)
    s.ledger.append(
        "model_delivery_rejected",
        reason=issue.code,
        item_indices=list(indices),
        attempt=int(returned_before),
        returned=False,
        terminal=True,
        answer_kept=bool(original),
    )
    return {
        **{key: value for key, value in args.items() if key != "read_answer"},
        "status": "cannot",
        "answer": answer,
        "items": items,
    }


def finalize_untraced(executor, s, turn, finish, untraced):
    """Close a finish payload when untraced numbers remain after retry or budget exhaustion."""
    status, answer = str(finish.get("status") or "done"), str(finish.get("answer") or "").strip()
    items = [item for item in finish.get("items") or () if isinstance(item, dict)]
    items_required = "items" in executor._finish_tool(turn)[
        "function"]["parameters"].get("required", ())
    if items:
        def in_item(token, item):
            # Boundary-aware and display-tolerant: "5" must not match
            # "15", "3.5" or the "10" inside "2026-10-21"; "4,499" still
            # matches "4499", "−500" matches "-500". Trailing sentence
            # punctuation on the label does not hide the value.
            label = str(item.get("item") or "").rstrip(".。,，、;；:：!！?？ ")
            label = re.sub(r"负\s*(?=\d)", "-", label)
            forms = {token}
            for _ in range(2):
                forms |= {v.replace("−", "-") for v in forms}
                forms |= {v.lstrip("+") for v in forms}
                forms |= {v.replace(",", "").replace("，", "") for v in forms}
                forms |= {v.rstrip("%") for v in forms}
            forms.discard("")
            return any(
                re.search(
                    r"(?<![\d.,，%+\-−A-Za-z0-9_/])"
                    + re.escape(v)
                    + r"(?![\d.,，%+\-−A-Za-z0-9_/])",
                    label,
                )
                for v in forms
            )

        deliverable = [
            str(item.get("item") or "")[:80]
            for item in items
            if any(in_item(token, item) for token in untraced)
        ]
        narrative = [
            token
            for token in untraced
            if not any(in_item(token, item) for item in items)
        ]
    elif items_required:
        # The turn's finish contract requires items and this finish
        # has none -- only the page-action shortcut can produce that,
        # since model calls are schema-validated. The answer itself
        # is the deliverable, so every untraced value fails closed.
        deliverable = ["答复内容"]
        narrative = []
    else:
        deliverable = []
        narrative = list(untraced)
    failed_indices = (
        [i for i, item in enumerate(items) if any(in_item(token, item) for token in untraced)]
        if items
        else []
    )
    for i in failed_indices:
        items[i]["status"] = "undelivered"
    if failed_indices:
        finish = {**finish, "items": items, "status": "cannot"}
    s.ledger.append(
        "untraced_numbers",
        numbers=list(untraced),
        sent_back=False,
        deliverable=list(deliverable),
        narrative=list(narrative),
        **({"undelivered_indices": failed_indices} if failed_indices else {}),
    )
    max_chars = getattr(getattr(executor, "config", None), "max_user_answer_chars", 4000)
    if deliverable:
        status = "cannot"
        note = (
            "\n\n交付项「"
            + "、".join(deliverable[:4])
            + "」的数值在本轮读取中找不到出处，该项与本轮均标为未完成。"
            + ("另有无出处数值：" + "、".join(narrative[:10]) + "。" if narrative else "")
        )
    else:
        note = "\n\n以下数值未在本轮读取中找到出处：" + "、".join(untraced[:20]) + "。"
    note = note[:max_chars]
    answer = answer[:max(0, max_chars - len(note))] + note
    return {**finish, "status": status, "answer": answer}


def verify_completion(executor, s, prompt: str, answer: str, turn) -> VerificationResult:
    """Run the profile's independent completion verification check."""
    if executor.profile.verification_messages is None:
        return VerificationResult("unavailable")
    messages = executor.profile.verification_messages(
        prompt, answer, turn, s.obs, executor._said(prompt)
    )
    try:
        message = s.chat.complete_cancellable(
            messages,
            None,
            s.run_id,
            cancelled=s.cancelled,
            max_tokens=600,
            timeout_s=max(1.0, s.budget.remaining()),
        )
    except ProviderError:
        return VerificationResult("unavailable")
    match = re.search(r"\{.*\}", message.get("content") or "", re.S)
    try:
        verdict = json.loads(match.group(0)) if match else {}
    except ValueError:
        return VerificationResult("unavailable")
    if (
        type(verdict.get("ok")) is not bool
        or not isinstance(verdict.get("problems"), list)
        or any(not isinstance(p, str) for p in verdict["problems"])
    ):
        return VerificationResult("unavailable")
    if verdict["ok"] is True and not verdict["problems"]:
        return VerificationResult("passed")
    if verdict["ok"] is False and verdict["problems"]:
        return VerificationResult("disputed", tuple(p[:200] for p in verdict["problems"][:4]))
    return VerificationResult("unavailable")


def unsaid_entries(executor, prompt: str, turn) -> list[str]:
    """Values typed or chosen on the page that did not come from the person's own words."""
    said = re.sub(r"[\s,，。.：:]", "", " ".join(executor._said(prompt)))
    return [
        f"{label}←{value}"
        for label, value in turn["touched"]
        if value and re.sub(r"[\s,，。.：:]", "", value) not in said
    ]


def untraced_numbers(executor, prompt: str, answer: str, turn) -> tuple[str, ...]:
    """Stated answer values not traceable to the turn's own sources."""
    hook = getattr(getattr(executor, "profile", None), "untraced_numbers", None)
    if hook is None:
        return ()
    sources = (prompt, *executor._earlier_words, *turn.get("tool_texts", ()))
    return tuple(str(item) for item in hook(answer, sources))[:20]


def with_not_executed(s, turn, answer: str) -> str:
    """Append page steps a read-only turn declined without substituting an answer."""
    items = turn.get("not_executed") or ()
    if not items:
        return answer
    chinese = getattr(s, "chinese", True)
    reasons = _NOT_EXECUTED if chinese else _NOT_EXECUTED_EN
    told = "；".join(
        f"{verb}「{label}」（{reasons[code]}）" if label else f"{verb}（{reasons[code]}）"
        for verb, label, code in items[:4]
    )
    return (
        answer
        + ("\n\n未执行的页面操作：" if chinese else "\n\nPage steps not performed: ")
        + told
        + ("。" if chinese else ".")
    )


def repeat_fallback(executor, s, prompt: str, turn, name: str, code: str) -> str:
    """Deliver already-read complete results when a failed read is repeated unchanged."""
    chinese = getattr(s, "chinese", True)
    max_chars = getattr(getattr(executor, "config", None), "max_user_answer_chars", 8000)
    head = (
        f"同一读取反复以相同参数失败（{name}：{code}），已停止；"
        if chinese
        else f"The same read failed repeatedly with the same arguments ({name}: {code}); stopped. "
    )
    intro = "下面是本轮已读到的结果。\n\n" if chinese else "Results already read this turn:\n\n"
    found = ""
    # Use only an explicit program summary already read under this actor.
    principal = getattr(getattr(s, "approvals", None), "principal", None)
    tail = executor._with_not_executed(s, turn, "")
    no_result = "本轮没有可完整交付的读取结果。" if chinese else "No result can be delivered in full."
    if len(head) + len(no_result) + len(tail) > max_chars:
        head = "相同读取反复失败，已停止；" if chinese else "Repeated reads failed; stopped. "
        compact = {
            **turn,
            "not_executed": [
                (verb, label[:12], reason)
                for verb, label, reason in turn.get("not_executed", ())[:4]
            ],
        }
        tail = executor._with_not_executed(s, compact, "")
    remaining = max_chars - len(head) - len(intro) - len(tail)
    for reference, projection in reversed(tuple(turn.get("query_answers", {}).items())):
        if not projection.complete or (
            projection.actor_digest
            and (principal is None or projection.actor_digest != principal.actor_authority_digest)
        ):
            continue
        if remaining < 512:
            break
        renderer = render_summary if isinstance(projection, SummaryResult) else render_structured_query_answer
        rendering = renderer(projection, user_text=prompt, max_chars=remaining)
        if rendering.presentation_truncated:
            continue
        found = rendering.text
        trace = getattr(executor, "_trace", None)
        if trace is not None:
            trace.record_delivery(
                reference=reference,
                projection=projection.to_json(),
                rendered_text=found,
                presented_records=rendering.presented_records,
                presentation_complete=True,
                source_reference=turn.get("query_answer_sources", {}).get(reference, ""),
            )
        break
    return head + (intro + found if found else no_result) + tail
