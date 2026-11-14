"""A native form submit or row action, approved first and pressed once by the program.

The model fills the application's own form (or finds the row's control) and calls
``request_submit``. From there the program, not the model, runs the contract:

    read the whole form data -> check where every typed value came from -> show a card
    (target, every change, defaults, the button) -> one fresh approval -> re-read the form
    and the record -> press the one control, under a permit that lets exactly the requests
    on the card through, each effect once -> read the record back with the same actor.

Nothing here names an application. The adapter supplies ``SubmitPort``: which interfaces
belong to the target, how to read a record as the same person, and the guard that holds
the permit. A form whose data cannot be read, or whose submit does something other than
send what the form holds, is not submitted: the person is handed the filled form instead.
"""
from __future__ import annotations

import re
from typing import Protocol
from urllib.parse import urlsplit

from .model_loop_contracts import LoopToolResult
from .submit_contract import (ContractRefused, FormData, SubmitCard, SubmitPermit, build_card,
                              is_empty, same_value, strip_volatile, terminal_state)
from .value_source import Entry, Receipt, SourceLedger, check_entries

#: The control the model names; the program decides everything else.
REQUEST_SUBMIT_TOOL = {"type": "function", "function": {
    "name": "request_submit",
    "description": (
        "用户要真正保存或执行时使用：先把原生表单按用户给的值填好，再用本工具请求提交；"
        "也用于表格某一行的启停、审核、关闭等行内按钮。index 是最新观察里的提交按钮（保存、确定、提交）"
        "或那一行的按钮序号。程序读取表单全部数据，把目标和每一项变化做成批准卡给用户；用户批准后由程序点击一次并回读核对。"
        "用户只要求预填、自己提交时不要用，改用 fill_fields 的 then_highlight。读不出表单数据或不能核对时，程序会说明并改为交接。"
        "删除、作废、关闭等危险动作要先在对话里说明对这条记录的影响并取得用户明确确认，之后才能请求。"),
    "parameters": {"type": "object", "properties": {
        "index": {"type": "integer"}}, "required": ["index"], "additionalProperties": False}}}

_DANGEROUS = re.compile(r"删除|移除|清除|作废|关闭|撤销|取消订单|注销|清空|反审|退款|终止|重置密码")
_BENIGN_CONFIRM = re.compile(r"确认|确定|是否|要.{0,12}吗|继续")
_CONSEQUENCE = re.compile(r"不可恢复|无法恢复|不可逆|永久|彻底|连带|同时(?:删除|作废|清理|取消)|级联|清理|将会?(?:被)?(?:删除|清空)|删除后|关联.{0,6}(?:删除|清除)")
_AFFIRMATIVE = re.compile(r"确认|确定|同意|是的|好的|可以|继续|没问题|删吧|执行|办吧|照做")
_NEGATIVE = re.compile(r"不|别|勿|否|没有|没法|取消|算了|等等|先不|暂停|停下")

MESSAGES = {
    "form_data_unreadable": "这张表单的整份数据读不出来，没有提交；已填好的表单留给你自己确认提交。",
    "credential_field": "表单里有密码等凭据字段，不能由助手提交；请你自己填写并提交。",
    "validation": "应用对表单的校验还没通过，没有提交：{detail}。",
    "no_change": "表单里没有任何改动，没有提交。",
    "card_too_large": "这次变化太多，没法在批准卡上完整列出，没有提交；已填好的表单留给你自己确认提交。",
    "entity_unknown": "找不到这张表单对应的接口（页面没有读取过目标记录），没有提交；已填好的表单留给你自己确认提交。",
    "record_unreadable": "没能按你的账号读取目标记录，无法核对它没有被别人改过，没有提交。",
    "not_a_row": "这个控件不属于表格里的某一行，没有提交。",
    "dangerous_unconfirmed": "「{button}」会影响这条记录（{target}）。先向用户说明这个动作对这条记录的影响，"
                             "并用 finish(need_input) 请用户明确确认；用户确认后下一轮再请求。",
    "value_not_from_user": "这些值不是用户给的，也不是用户选定的候选，不能写入：{detail}。"
                           "请用 finish(need_input) 向用户确认这些值，不要自己编。",
}


class SubmitPort(Protocol):
    """What the application contributes; every method acts as the signed-in person."""

    def recent_reads(self) -> tuple: ...
    def prefixes(self, reads: tuple, *, mode: str, record_id: str) -> tuple[str, ...]: ...
    def catalog(self, prefixes: tuple[str, ...]) -> frozenset: ...
    def read_record(self, prefixes: tuple[str, ...], record_id: str): ...
    def dangerous(self, element) -> bool: ...
    def arm(self, permit: SubmitPermit) -> None: ...
    def disarm(self) -> None: ...
    def settled(self) -> None: ...


def _contains(held, wanted) -> bool:
    """The record holds what the form held. Lines and other nested data may carry keys of their
    own (computed, display), so for them only the keys the form has must agree."""
    if isinstance(wanted, dict) and isinstance(held, dict):
        return all(_contains(held.get(key), value) for key, value in wanted.items())
    if isinstance(wanted, list) and isinstance(held, list):
        return len(wanted) == len(held) and all(_contains(a, b) for a, b in zip(held, wanted))
    return same_value(held, wanted)


def _confirm_words(text: str) -> bool:
    """A yes. Any refusal or hesitation anywhere in the words makes it not one ("不可以删除")."""
    plain = text.replace("没问题", "")
    return bool(_AFFIRMATIVE.search(text)) and not _NEGATIVE.search(plain)


class NativeSubmitMixin:
    """`request_submit` for a model-driven executor that has a `native_submit` port."""

    native_submit: SubmitPort | None = None

    # -- offered ----------------------------------------------------------------------
    def _submit_offered(self, turn) -> bool:
        return self.native_submit is not None and self._submit_allowed(turn)

    def _submit_allowed(self, turn) -> bool:           # an adapter narrows this
        return True

    # -- what the turn typed ----------------------------------------------------------
    def note_entries(self, s, turn, entered, observation) -> None:
        """Record each value typed this turn with where it came from, for the value check."""
        ledger = turn.setdefault("source_ledger", SourceLedger())
        for label, value in entered:
            # A dropdown takes no exception: what was typed into it must itself come from the
            # person's words (the option it matched is the page's), or a receipt.
            ledger.add(Entry(str(label), str(value), candidate=False))

    def note_receipt(self, turn, field: str, value: str) -> None:
        turn.setdefault("source_ledger", SourceLedger()).receipt(Receipt(field, value))

    def capture_form_baseline(self, s, turn, element) -> None:
        """The form as loaded, read before the first value is typed into it."""
        if self.native_submit is None:
            return
        key = self._form_key(element)
        baselines = turn.setdefault("form_baselines", {})
        if key is None or key in baselines:
            if key is None:
                s.ledger.append("native_submit_baseline", captured=False, reason="form_not_identified",
                                scope=str(element.attrs.get("form_scope", ""))[:80])
            return
        try:
            baselines[key] = self._read_form(s, element)[0]
            s.ledger.append("native_submit_baseline", captured=True, mode=baselines[key].mode,
                            record_id=baselines[key].record_id, digest=baselines[key].digest)
        except ContractRefused as exc:
            baselines[key] = None
            s.ledger.append("native_submit_baseline", captured=False, reason=exc.code, detail=exc.detail[:80])

    @staticmethod
    def _form_key(element):
        attrs = element.attrs
        scope = attrs.get("form_scope")
        instance = attrs.get("form_lifetime") or attrs.get("form_instance")
        return (scope, instance) if scope and instance else None

    # -- reads through the browser ----------------------------------------------------
    def _read(self, s, element, kind, **extra):
        receipt = self._dispatch(s, "form_model", {"ref": element.ref, "kind": kind, **extra})
        if receipt is None or not receipt.ok or not isinstance(receipt.form_data, dict):
            why = re.search(r"\(([a-z_]+)\)", getattr(receipt, "error_detail", "") or "")
            raise ContractRefused("form_data_unreadable", why.group(1) if why else "")
        return receipt.form_data

    def _read_form(self, s, element):
        raw = self._read(s, element, "form")
        return FormData.from_json(raw), raw

    # -- the request ------------------------------------------------------------------
    def _request_submit(self, s, args, turn) -> LoopToolResult:
        port = self.native_submit
        element = self._element(s, args["index"])
        if port is None or not self._submit_allowed(turn):
            return LoopToolResult("这一轮不能提交：用户只要求预填，或当前部署没有开放写入。", kind="write",
                                  state="refused", error_code="tool_unavailable")
        if element is None or element.role not in {"button", "switch", "link", "menuitem"} and element.tag not in {"button", "a"}:
            return LoopToolResult("这个序号不是按钮；提交请指向最新观察里的保存、确定或提交按钮。", kind="write",
                                  state="refused", error_code="invalid_target")
        attrs = element.attrs
        form = bool(attrs.get("form_scope")) and attrs.get("page_control") != "query"
        s.step += 1
        try:
            return (self._submit_form(s, element, turn) if form
                    else self._submit_row(s, element, turn))
        except ContractRefused as exc:
            return self._not_sent(s, exc.code, exc.detail)

    def _not_sent(self, s, code, detail="") -> LoopToolResult:
        s.ledger.append("native_submit_not_sent", step=s.step, code=code, detail=detail[:300],
                        dispatched=False, side_effect_possible=False)
        names = dict(zip(("button", "target"), detail.split("\x1f"))) if code == "dangerous_unconfirmed" else {}
        text = MESSAGES.get(code, "没有提交：" + (detail or code)).format(
            detail=detail, **{"button": "", "target": "", **names})
        retry = code in {"value_not_from_user", "dangerous_unconfirmed", "validation", "no_change"}
        return LoopToolResult(text, kind="write", state="needs_input" if retry else "refused",
                              error_code=code)

    # -- forms ------------------------------------------------------------------------
    def _submit_form(self, s, element, turn) -> LoopToolResult:
        port = self.native_submit
        current, raw = self._read_form(s, element)
        if raw.get("withheld"):
            raise ContractRefused("credential_field")
        if raw.get("invalid"):
            raise ContractRefused("validation", "、".join(str(p) for p in raw["invalid"][:8]))
        key = self._form_key(element)
        loaded = turn.get("form_baselines", {}).get(key)
        if loaded is None:
            loaded = current                     # never typed into: the form as it stands
        if loaded.mode != current.mode or loaded.record_id != current.record_id:
            raise ContractRefused("form_data_unreadable", "the form changed record")
        ledger = turn.get("source_ledger") or SourceLedger()
        words = self._said(s.message.text)
        verdicts = check_entries(ledger, words)
        invented = [v.entry.field for v in verdicts if not v.ok]
        s.ledger.append("native_submit_value_sources", step=s.step,
                        checks=[{"field": v.entry.field, "ok": v.ok, "source": v.source,
                                 "reason": v.reason} for v in verdicts])
        if invented:
            raise ContractRefused("value_not_from_user", "、".join(invented))
        requested = self._requested_fields(current, ledger)
        card = build_card(loaded=loaded, form=current, page=self._page_name(s), button=element.name.strip(),
                          mode=current.mode, record_id=current.record_id, requested=requested)
        reads = port.recent_reads()
        prefixes = port.prefixes(reads, mode=card.mode, record_id=card.record_id)
        if not prefixes:
            raise ContractRefused("entity_unknown")
        baseline = None
        if card.mode == "update":
            baseline = port.read_record(prefixes, card.record_id)
            if baseline is None:
                raise ContractRefused("record_unreadable")
        return self._approve_and_press(s, element, turn, card, prefixes, baseline,
                                       kind="form", check=raw.get("check", ""), loaded=loaded)

    @staticmethod
    def _requested_fields(form: FormData, ledger: SourceLedger) -> frozenset:
        """The model fields the person's values went into, or nothing when any cannot be placed."""
        if not ledger.entries:
            return frozenset()
        props = set()
        for entry in ledger.entries:
            matched = [item.prop.split(".")[0] for item in form.items if item.label == entry.field]
            if not matched:
                return frozenset()
            props.add(matched[0])
        return frozenset(props)

    @staticmethod
    def _page_name(s) -> str:
        obs = s.obs
        return (getattr(obs, "page_route", "") or urlsplit(getattr(obs, "url", "")).path or "")[:200]

    # -- rows -------------------------------------------------------------------------
    def _submit_row(self, s, element, turn) -> LoopToolResult:
        port = self.native_submit
        try:
            row = self._read(s, element, "row")
        except ContractRefused:
            raise ContractRefused("not_a_row") from None
        record_id, name = str(row.get("record_id") or ""), str(row.get("name") or "")
        button = element.name.strip() or str(element.attrs.get("aria-label") or element.attrs.get("title") or "")
        target = f"编号 {record_id}" + (f"「{name}」" if name else "")
        if port.dangerous(element) or _DANGEROUS.search(button):
            history = self._earlier_words
            if not history or not _confirm_words(s.message.text):
                raise ContractRefused("dangerous_unconfirmed", button + "\x1f" + target)
        card = build_card(loaded=None, form=None, page=self._page_name(s), button=button, mode="action",
                          target_name=name, record_id=record_id)
        reads = port.recent_reads()
        prefixes = port.prefixes(reads, mode="action", record_id=record_id)
        if not prefixes:
            raise ContractRefused("entity_unknown")
        baseline = port.read_record(prefixes, record_id)
        if baseline is None:
            raise ContractRefused("record_unreadable")
        return self._approve_and_press(s, element, turn, card, prefixes, baseline,
                                       kind="row", record_id=record_id)

    # -- approval, press, readback -----------------------------------------------------
    def _approve_and_press(self, s, element, turn, card: SubmitCard, prefixes, baseline, *,
                           kind, check="", record_id="", loaded=None) -> LoopToolResult:
        port = self.native_submit
        step = s.step
        context = s.approvals.context(step, "native_submit", hold_ms=s.approvals.approval_hold_ms)
        proposal = card.proposal(context_digest=context.binding_digest if context is not None else "")
        s.ledger.append(
            "native_submit_card", step=step, mode=card.mode, target=card.target, page=card.page,
            button=card.button, form_digest=card.form.digest if card.form else "",
            loaded_digest=card.loaded.digest if card.loaded else "",
            changes=[d.to_json() for d in card.details()],
            loaded=card.loaded.model if card.loaded else None,
            submitted=card.form.model if card.form else None)
        s.write_intent = True
        turn["submit_started"] = True
        identity = context.audit_projection() if context is not None else {}
        approval = s.approvals.approve_mutation(proposal, step=step, tool=proposal.tool,
                                                context=context, identity=identity)
        if not approval.approved:
            s.terminal_reason, s.detail = approval.reason, approval.detail[:200]
            turn["final"] = {"approval_denied": "你没有批准这项修改，没有执行。",
                             "approval_expired": "批准卡片已过期，这项修改没有执行。",
                             "user_cancelled": "操作已停止，这项修改没有执行。"}.get(approval.reason, "这项修改没有执行。")
            return LoopToolResult(turn["final"], kind="write", state="refused",
                                  terminal_reason=s.terminal_reason)
        # What was approved must still be what is on the page and in the record.
        try:
            if kind == "form":
                fresh, fresh_raw = self._read_form(s, element)
                if fresh_raw.get("check") != check or fresh.digest != card.form.digest:
                    return self._stopped(s, turn, "form_changed", "批准之后表单里的数据又变了")
            else:
                row = self._read(s, element, "row")
                if str(row.get("record_id") or "") != record_id:
                    return self._stopped(s, turn, "row_changed", "批准之后这一行不再是原来的记录")
            now = port.read_record(prefixes, card.record_id) if card.mode != "create" else None
            if card.mode != "create":
                if now is None:
                    return self._stopped(s, turn, "record_unreadable", "没能重新读取目标记录")
                if card.mode == "update" and not self._same_as_loaded(now, loaded):
                    return self._stopped(s, turn, "record_changed", "批准之后这条记录被别人改过")
                if not same_value(strip_volatile(now), strip_volatile(baseline)):
                    return self._stopped(s, turn, "record_changed", "批准之后这条记录被别人改过")
        except ContractRefused as exc:
            return self._stopped(s, turn, exc.code, exc.detail or "核对没有通过")
        s.ledger.append("native_submit_reread", step=step, form_digest=card.form.digest if card.form else "",
                        prefixes=list(prefixes), documented_writes=sorted(f"{m} {p}" for m, p in port.catalog(prefixes))[:12],
                        record=now if card.mode != "create" else None, dispatched=False, side_effect_possible=False)
        permit = SubmitPermit(
            actor="", card=card, prefixes=prefixes, catalog=port.catalog(prefixes), baseline=baseline,
            reread=(lambda: port.read_record(prefixes, card.record_id)) if card.mode != "create" else None)
        s.ledger.append("native_submit_approved", step=step, approval_id=approval.approval_id,
                        permit=permit.permit_id, baseline=baseline, dispatched=False,
                        side_effect_possible=False)
        label = element.name.strip()
        press = ({"ref": element.ref, "kind": "form", "check": check, "label": label} if kind == "form"
                 else {"ref": element.ref, "kind": "row", "record_id": record_id, "label": label})
        broken, confirmed = "", ""
        try:
            clicked = self._press(s, permit, press)
            if not clicked:
                return self._stopped(s, turn, "permit_unavailable", "这次提交的许可没能建立（上一次请求结果未确认，或另一个许可仍在使用）")
            confirmed = self._handle_confirmation(s, permit, card)
        except Exception as exc:  # noqa: BLE001 -- after a click nothing can be assumed not to have been sent
            broken = type(exc).__name__
            s.ledger.append("native_submit_exception", step=step, permit=permit.permit_id, error_type=broken,
                            dispatched=True, side_effect_possible=True)
        if confirmed == "refused":
            return self._finish_submit(s, turn, permit, card, prefixes, baseline, readback=None,
                                       extra="应用弹出的确认框里有卡上没有的后果，已取消，没有执行。")
        return self._finish_submit(s, turn, permit, card, prefixes, baseline, uncertain=bool(broken))

    def _same_as_loaded(self, record, loaded) -> bool:
        if loaded is None or not isinstance(record, dict):
            return True
        for key, value in loaded.model.items():
            if key in record and isinstance(value, (str, int, float, bool)) and not same_value(record[key], value):
                return False
        return True

    def _stopped(self, s, turn, code, detail) -> LoopToolResult:
        s.ledger.append("native_submit_not_sent", step=s.step, code=code, detail=detail,
                        dispatched=False, side_effect_possible=False)
        s.terminal_reason, s.detail = "business_mutation_refused", code
        turn["final"] = f"{detail}，这项修改没有执行。请重新发起。"
        return LoopToolResult(turn["final"], kind="write", state="refused", terminal_reason=s.terminal_reason)

    def _press(self, s, permit: SubmitPermit, args: dict) -> bool:
        """Arm the permit and press once. False when the permit could not be armed: nothing was sent."""
        port = self.native_submit
        try:
            port.arm(permit)
        except Exception as exc:  # noqa: BLE001
            s.ledger.append("native_submit_not_armed", step=s.step, permit=permit.permit_id,
                            error_type=type(exc).__name__, dispatched=False, side_effect_possible=False)
            return False
        s.ledger.append("native_submit_clicked", step=s.step, permit=permit.permit_id, args={
            k: v for k, v in args.items() if k != "check"}, dispatched=True, side_effect_possible=True)
        try:
            receipt = self._dispatch(s, "form_submit", args)
        finally:
            port.disarm()
            port.settled()
        s.ledger.append("native_submit_click_receipt", step=s.step, permit=permit.permit_id,
                        ok=bool(receipt is not None and receipt.ok),
                        error_code=getattr(receipt, "error_code", "") if receipt is not None else "no_receipt")
        return True

    def _handle_confirmation(self, s, permit: SubmitPermit, card: SubmitCard) -> str:
        """An application confirmation box: the same action is confirmed, anything more is not."""
        if permit.allowed():
            return ""
        element = self._anchor(s)
        if element is None:
            return ""
        try:
            box = self._read(s, element, "confirm")
        except ContractRefused:
            return ""
        if not box.get("open"):
            return ""
        text = str(box.get("text") or "")
        s.ledger.append("native_submit_confirmation", step=s.step, text=text[:300], buttons=box.get("buttons"))
        if _CONSEQUENCE.search(text) and not _CONSEQUENCE.search(card.button + card.target):
            self._dispatch(s, "form_submit", {"ref": element.ref, "kind": "confirm", "label": "取消"})
            return "refused"
        labels = [str(b) for b in box.get("buttons") or ()]
        yes = next((b for b in labels if b in {"确定", "确认", "是", "好"}), "")
        if not yes:
            return ""
        self._press(s, permit, {"ref": element.ref, "kind": "confirm", "label": yes})
        return "confirmed"

    def _anchor(self, s):
        return next((e for e in getattr(s.obs, "elements", ()) if e.role in {"button", "link", "textbox"}), None)

    def _finish_submit(self, s, turn, permit: SubmitPermit, card: SubmitCard, prefixes, baseline, *,
                       readback=None, extra="", uncertain=False) -> LoopToolResult:
        port = self.native_submit
        try:
            port.settled()
        except Exception:  # noqa: BLE001
            uncertain = True
        self._last_readback = None
        outcome, notes = self._readback(card, permit, prefixes, baseline)
        state = terminal_state(permit, readback_ok=outcome if readback is None else readback)
        if state in {"not_executed", "failed"} and (uncertain or self._record_moved(card, prefixes, baseline)):
            # No request was recorded as released, yet the click happened and something may have
            # gone out (a late request, a lost answer): never say "not executed" about that.
            state = "indeterminate"
            notes = [*notes, "记录在点击后发生了变化，或点击后出现异常，无法证明没有写入"]
        s.ledger.append(
            "native_submit_result", step=s.step, permit=permit.permit_id, state=state,
            requests=[{"seq": r.sequence, "method": r.method, "path": r.path, "decision": r.decision,
                       "status": r.status, "fields": list(r.fields), "digest": r.body_digest,
                       "detail": list(r.detail), "body": r.sent, "reread_digest": r.reread_digest,
                       "answer": r.answer if isinstance(r.answer, (int, str)) else None}
                      for r in permit.log],
            readback_record=self._last_readback,
            duplicates=permit.duplicates, readback=outcome, notes=notes,
            side_effect_possible=True)
        what = {"create": "新增", "update": "修改", "action": "执行"}[card.mode]
        label = f"{card.effect()}"
        blocked = [r for r in permit.blocked() if r.decision != "duplicate"]
        if state == "completed":
            turn["written"].append(label)
            s.mutation_outcomes.append({"position": len(s.mutation_outcomes), "effect": card.effect(),
                                        "targets": [card.target], "verified": True,
                                        "receipt": {"reference": permit.created_id() or card.record_id}})
            text = "已执行并核对（用户已批准，应用回读确认）：" + label
            if notes:
                text += "。" + "；".join(notes)
            if permit.duplicates:
                text += f"。另有 {permit.duplicates} 次重复请求被拦下，没有重复执行"
            turn["final"] = text
            return LoopToolResult(text, kind="write", state="completed", dispatch="acknowledged")
        if state == "not_executed":
            s.terminal_reason, s.detail = "business_mutation_refused", "no request was released"
            why = (extra or "点击没有发出符合批准卡的请求，页面上的应用校验可能没有通过，").rstrip("，。")
            if blocked:
                why += "；被拦下的请求：" + "、".join(sorted({r.decision for r in blocked}))
            turn["final"] = f"{label}：没有执行（{why}）。表单保持当前状态，请你核对后自己处理。"
            return LoopToolResult(turn["final"], kind="write", state="refused", terminal_reason=s.terminal_reason)
        if state == "failed":
            s.terminal_reason, s.detail = "business_mutation_refused", "the application refused the request"
            turn["final"] = f"{label}：应用拒绝了这次请求，没有生效。不会自动重试。"
            return LoopToolResult(turn["final"], kind="write", state="refused", terminal_reason=s.terminal_reason)
        accepted = [r for r in permit.allowed() if r.status is not None and 200 <= r.status < 300]
        s.terminal_reason, s.detail = "indeterminate_write", state
        if state == "partial":
            turn["final"] = (f"{label}：只有一部分生效（已被应用接受的请求 {len(accepted)} 个"
                             + (f"，被拦下或被拒绝的 {len(blocked) + len([r for r in permit.allowed() if r not in accepted])} 个"
                                if blocked or len(accepted) < len(permit.allowed()) else "")
                             + "）。不会自动重试，请到应用里核对这条记录。")
        else:
            turn["final"] = ("这项修改已经发出，但结果没有得到确认：可能已经生效，也可能没有。"
                             "已停止后续操作，不会重发；请到应用里核对这条记录后再决定。")
        return LoopToolResult(turn["final"], kind="write", state="indeterminate", dispatch="uncertain",
                              terminal_reason=s.terminal_reason)

    # -- readback ---------------------------------------------------------------------
    def _record_moved(self, card, prefixes, baseline) -> bool:
        if card.mode == "create" or baseline is None:
            return False
        try:
            now = self.native_submit.read_record(prefixes, card.record_id)
        except Exception:  # noqa: BLE001
            return True
        if now is None:
            return True
        if card.mode == "action":
            return not same_value(strip_volatile(now), strip_volatile(baseline))
        # An edit: our write is in the record if a field the card changed now holds the new value
        # it did not hold before. Another person's change to some other field is not ours.
        form = card.form.model
        return any(isinstance(now, dict) and same_value(now.get(f), form.get(f))
                   and not same_value(baseline.get(f), form.get(f)) for f in card.changed_fields)

    _last_readback = None

    def _readback(self, card: SubmitCard, permit: SubmitPermit, prefixes, baseline):
        """(True|False|None, notes): the record as the same person reads it after the click."""
        port = self.native_submit
        if not permit.allowed():
            return None, []
        record_id = card.record_id or permit.created_id()
        if card.mode == "create" and not record_id:
            return None, ["应用没有返回新记录的编号，无法回读核对"]
        try:
            now = port.read_record(prefixes, record_id)
        except Exception:  # noqa: BLE001
            return None, ["回读失败"]
        self._last_readback = now
        if card.mode == "action":
            gone = now is None
            moved = gone or not same_value(strip_volatile(now), strip_volatile(baseline))
            return (True if moved else None), ([] if moved else ["回读时记录没有变化，无法确认动作生效"])
        if now is None:
            return None, ["回读时找不到这条记录"]
        form = card.form.model
        wrong, checked = [], 0
        for change in (*card.changes, *card.other, *card.unrequested):
            top = change.field
            if top not in now:
                continue
            checked += 1
            if not _contains(now.get(top), form.get(top)):
                wrong.append(card.form.label(top))
        notes = []
        if card.mode == "update" and isinstance(baseline, dict):
            moved = [k for k in now if k not in card.changed_fields and k in baseline
                     and not same_value(strip_volatile({k: baseline[k]}), strip_volatile({k: now[k]}))]
            if moved:
                notes.append("回读时其它字段与点击前不同：" + "、".join(card.form.label(k) for k in moved[:6]))
        if wrong:
            notes.append("回读与批准卡不一致：" + "、".join(wrong[:6]))
            return False, notes
        return (True if checked else None), notes
