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

from dataclasses import replace
from decimal import Decimal, InvalidOperation
import re
import time
from typing import Protocol
from urllib.parse import urlsplit

from .model_loop_contracts import LoopToolResult
from .submit_contract import (ContractRefused, FormData, SubmitCard, SubmitPermit, build_card,
                              diff_models, digest, is_empty, same_value, strip_volatile, terminal_state)
from ..business.write_pipeline import (
    WritePipeline,
    check_append_preservation,
    verify_value_sources,
)
from ..privacy import redact_sensitive_assignments, redact_session_handoffs
from .value_source import Entry, SourceLedger, note_receipt

#: The control the model names; the program decides everything else.
REQUEST_SUBMIT_TOOL = {"type": "function", "function": {
    "name": "request_submit",
    "description": (
        "用户要真正保存或执行时使用：先把原生表单按用户给的值填好，再用本工具请求提交；"
        "也用于表格某一行的启停、审核、关闭等行内按钮。index 是最新观察里的提交按钮（保存、确定、提交）"
        "或那一行的按钮序号。程序读取表单全部数据，把目标和每一项变化做成批准卡给用户；用户批准后由程序点击一次并回读核对。"
        "必填信息缺失、说法含糊或目标有多个候选时，用 finish(need_input) 问清楚，不沿用旧值、不猜。用户只要求预填、自己提交时不要用，改用 fill_fields 的 then_highlight。读不出表单数据或不能核对时，程序会说明并改为交接。"
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
    "append_overwrite": "用户要求追加或补充内容，但当前填写覆盖了原有内容（{detail}）。"
                        "请保留该字段原有内容并在其后追加本次内容后再提交。",
    "conventions_undeclared": "当前集成没有声明应用的记录约定，不能由助手提交。",
}


class SubmitPort(Protocol):
    """What the application contributes; every method acts as the signed-in person."""

    def contract(self):
        """The application's declared record conventions, or None when it cannot submit."""
        return None
    def recent_reads(self) -> tuple: ...
    def prefixes(self, reads: tuple, *, mode: str, record_id: str, source: str = "") -> tuple[str, ...]: ...
    def row_prefixes(self, reads: tuple, row: dict) -> tuple[str, ...]: ...
    def row_request(self, prefixes: tuple, row: dict, element, record: dict): ...
    def catalog(self, prefixes: tuple[str, ...]) -> frozenset: ...
    def read_record(self, prefixes: tuple[str, ...], record_id: str): ...
    def dangerous(self, element) -> bool: ...
    def arm(self, permit: SubmitPermit) -> None: ...
    def disarm(self) -> None: ...
    def settled(self) -> None: ...
    # -- the person's own press after a handoff -------------------------------------------
    def user_write_mark(self):
        """An opaque position in the actor's observed accepted user writes; never raises."""
        return None
    def user_write_event(self, prefixes: tuple[str, ...], record_id: str, mark,
                         create: bool = False):
        """The first accepted user-originated write under `prefixes` after `mark`, or None.

        Observation only: the adapter says what accepted means and never holds or changes
        the person's request. A body `id` matching `record_id` binds the write to the
        handed-off target; an empty `record_id` with `create=True` accepts only a write
        the adapter identifies as the create interface for the prefix, and the returned
        dict then carries the new record's id in `record_id`.
        """
        return None


_RANGE_FILL = re.compile(r"\s*(?:~|～|—|至|到)\s*")
_SELECT_ROLES = {"combobox", "select", "radiogroup", "listbox"}


def _control_label(element) -> str:
    """What a person would call this control: its form label, its place in a line
    table, else its own text."""
    for key in ("field_label", "field", "title", "placeholder"):
        value = str(element.attrs.get(key) or "").rstrip(":：").strip()
        if value:
            return value[:40]
    return (element.name.splitlines()[0][:40] if element.name else "") or element.role


def _contains(held, wanted, *, zone=None) -> bool:
    """The record holds what the form held. Lines and other nested data may carry keys of their
    own (computed, display), so for them only the keys the form has must agree."""
    if isinstance(wanted, dict) and isinstance(held, dict):
        return all(_contains(held.get(key), value, zone=zone) for key, value in wanted.items())
    if isinstance(wanted, list) and isinstance(held, list):
        return len(wanted) == len(held) and all(
            _contains(a, b, zone=zone) for a, b in zip(held, wanted))
    return same_value(held, wanted, zone=zone)


def record_model(record, shape, conventions):
    """Compare submitted fields; extra read-only record/line keys are not submitted."""
    key = conventions.line_key
    if isinstance(record, dict) and isinstance(shape, dict):
        return {k: record_model(record.get(k), v, conventions) for k, v in shape.items()}
    if isinstance(record, list) and isinstance(shape, list):
        keyed = (all(isinstance(v, dict) and v.get(key) is not None for v in record)
                 and all(isinstance(v, dict) for v in shape))
        if keyed:
            by_id = {str(v[key]): v for v in record}
            shape_ids = [str(v[key]) for v in shape if v.get(key) is not None]
            if len(by_id) != len(record) or len(set(shape_ids)) != len(shape_ids):
                raise ContractRefused("form_data_unreadable", "duplicate line identities")
            paired = [(by_id.pop(str(v[key])), v) for v in shape
                      if v.get(key) is not None and str(v[key]) in by_id]
            return [record_model(a, b, conventions) for a, b in paired] + list(by_id.values())
        return [record_model(value, shape[index] if index < len(shape) else value, conventions)
                for index, value in enumerate(record)]
    return record


def submit_conversion(record, loaded) -> bool:
    """The two supported opening representations; never write stored units into these."""
    if record is None or isinstance(record, str) and record == "":
        if type(record) is type(loaded) and record == loaded:
            return False
        if not isinstance(loaded, bool) and isinstance(loaded, (int, float)):
            return loaded == 0
        if isinstance(loaded, str):
            if loaded == "" or re.fullmatch(r"1970-01-01(?:[ T]00:00(?::00(?:\.0+)?)?(?:Z|[+]00:00)?)?", loaded):
                return True
            try:
                return Decimal(loaded).is_finite() and Decimal(loaded) == 0
            except InvalidOperation:
                return False
        return False
    if isinstance(record, (int, float)) and not isinstance(record, bool) and isinstance(loaded, str):
        try:
            old, new = Decimal(str(record)), Decimal(loaded)
            if old.is_finite() and new.is_finite() and old and new:
                ratio = new / old
                return ratio > 0 and ratio.normalize().as_tuple().digits == (1,)
        except (InvalidOperation, ValueError):
            pass
    return False


def conversion_fields(record, loaded, path=(), *, conventions):
    key = conventions.line_key
    if isinstance(record, dict) and isinstance(loaded, dict):
        return tuple(field for key_ in record.keys() & loaded.keys()
                     for field in conversion_fields(record[key_], loaded[key_],
                                                    (*path, key_), conventions=conventions))
    if isinstance(record, list) and isinstance(loaded, list):
        if all(isinstance(v, dict) and v.get(key) is not None for v in [*record, *loaded]):
            old = {str(v[key]): v for v in loaded}
            if len(old) != len(loaded) or len({str(v[key]) for v in record}) != len(record):
                raise ContractRefused("form_data_unreadable", "duplicate line identities")
            return tuple(field for row in record if str(row[key]) in old
                         for field in conversion_fields(row, old[str(row[key])],
                                                      (*path, "#" + str(row[key])),
                                                      conventions=conventions))
        if len(record) == len(loaded):
            return tuple(field for index, (a, b) in enumerate(zip(record, loaded))
                         for field in conversion_fields(a, b, (*path, index),
                                                      conventions=conventions))
    return (path,) if path and submit_conversion(record, loaded) else ()


def opening_values(record, loaded, current, conventions):
    """Undo opening-only differences while preserving subsequent edits at each leaf."""
    key, zone = conventions.line_key, conventions.epoch_zone
    if isinstance(record, dict) and isinstance(loaded, dict) and isinstance(current, dict):
        return {k: opening_values(record[k], loaded[k], v, conventions)
                if k in record and k in loaded else v for k, v in current.items()}
    if isinstance(record, list) and isinstance(loaded, list) and isinstance(current, list):
        keyed = all(isinstance(v, dict) and v.get(key) is not None for v in [*record, *loaded])
        if keyed:
            old = {str(v[key]): v for v in loaded}
            base = {str(v[key]): v for v in record}
            ids = [str(v[key]) for v in current if isinstance(v, dict) and v.get(key) is not None]
            if len(old) != len(loaded) or len(base) != len(record) or len(set(ids)) != len(ids):
                raise ContractRefused("opening_changes", "明细编号重复，无法核对页面自动变化，请自行核对提交")
            if base.keys() != old.keys():
                raise ContractRefused("opening_changes", "页面打开时增删了明细，无法安全还原，请自行核对提交")
            if any(not isinstance(v, dict) for v in current) or (
                    len(ids) != len(current) and not old.keys() <= set(ids)):
                if not same_value(record, loaded, zone=zone):
                    raise ContractRefused("opening_changes", "明细身份无法对应，不能区分页面自动变化与填写变化，请自行核对提交")
                return current
            # Current membership/order is the assistant's edit. Match every retained old
            # line independently, even when rows were added or removed after opening.
            return [opening_values(base[str(v[key])], old[str(v[key])], v, conventions)
                    if v.get(key) is not None and str(v[key]) in old else v for v in current]
        if same_value(record, loaded, zone=zone):
            return current
        if (any(isinstance(v, dict) for v in [*record, *loaded, *current])
                or len(record) != len(loaded) or len(loaded) != len(current)
                or not same_value(loaded, current, zone=zone)):
            raise ContractRefused("opening_changes", "无法区分没有编号的明细行的页面自动变化与填写变化，请自行核对提交")
        return [opening_values(a, b, c, conventions) for a, b, c in zip(record, loaded, current)]
    if submit_conversion(record, loaded):
        return current
    return record if same_value(loaded, current, zone=zone) and not same_value(record, loaded, zone=zone) else current


def _confirm_words(text: str) -> bool:
    """A yes. Any refusal or hesitation anywhere in the words makes it not one ("不可以删除")."""
    plain = text.replace("没问题", "")
    return bool(_AFFIRMATIVE.search(text)) and not _NEGATIVE.search(plain)


class NativeSubmitMixin:
    """`request_submit` for a model-driven executor that has a `native_submit` port."""

    native_submit: SubmitPort | None = None

    def _private_submit_record(self, s, event, **fields):
        """Optional local evidence collector; the normal ledger never receives whole records."""
        collect = getattr(self.native_submit, "record_private", None)
        if collect is not None:
            try:
                collect(event, {"run_id": getattr(s.ledger, "run_id", ""),
                    **getattr(s.ledger, "base", {}), "step": s.step,
                    **redact_sensitive_assignments(redact_session_handoffs(fields))})
            except Exception as exc:
                s.ledger.append("native_submit_evidence_error", error_type=type(exc).__name__)

    # -- offered ----------------------------------------------------------------------
    def _submit_offered(self, turn) -> bool:
        return self.native_submit is not None and self._submit_allowed(turn)

    def _submit_allowed(self, turn) -> bool:           # an adapter narrows this
        return True

    # -- what the turn typed ----------------------------------------------------------
    def note_entries(self, s, turn, entered, observation, receipts=()) -> None:
        """Record each value typed this turn keyed on the field it was actually typed into.

        A fill/select receipt names its element by ref: the entry gets that element's
        own label and kind, so line-table cells and title-named controls are checked
        like any other field. A fill whose element cannot be resolved is still
        recorded and fails closed; query/filter controls are not business values.
        `turn["source_checked"]` remembers every fill already classified, so later
        calls with the accumulated `entered` list do not re-judge older fills.
        """
        ledger = turn.setdefault("source_ledger", SourceLedger())
        # Classified fills are remembered two ways: (field, value) pairs so one
        # field's fill never stands in for another's, and bare values so the same
        # fill is not re-judged under the model's own shorter label for it.
        covered = turn.setdefault("source_checked", set())
        covered_values = turn.setdefault("source_checked_values", set())
        observed = observation
        # The wire never echoes typed values (args_echo redacts "value"), so a
        # fill receipt over a real channel proves the element and its kind, not
        # the text: the value has to come from the model's declared fills below.
        deferred: dict[str, tuple[str, str]] = {}
        for receipt in receipts or ():
            if receipt.action not in {"fill", "select"}:
                observed = receipt.observation or observed
                continue
            target = next((e for e in getattr(observed, "elements", ())
                           if e.ref == receipt.args_echo.get("ref")), None)
            observed = receipt.observation or observed
            if not receipt.ok:
                continue
            value = str(receipt.args_echo.get("option_label")
                        or receipt.args_echo.get("value") or "")
            if target is not None \
                    and not (target.attrs.get("form_scope")
                             and target.attrs.get("page_control") != "query"):
                covered_values.add(value)
                continue      # a query/filter control is not a business form value
            if target is None:
                ledger.add(Entry("", value, placed=False))
                continue
            label = _control_label(target)
            if receipt.action == "select":
                selected = [e for e in getattr(receipt.observation, "elements", ())
                            if e.attrs.get("form_scope") == target.attrs.get("form_scope")
                            and _control_label(e) == label
                            and e.attrs.get("selected_option")]
                if len(selected) == 1:
                    value = str(selected[0].attrs["selected_option"])
            kind = "select" if (receipt.action == "select"
                                or target.role in _SELECT_ROLES) else "text"
            if value == "[redacted]":
                deferred[label.casefold()] = (label, kind)
                continue
            covered.add((label, value))
            covered.add((label, str(receipt.args_echo.get("value") or "")))
            covered_values.add(value)
            ledger.add(Entry(label, value, kind=kind))
        for label, value in entered:
            shown = str(value)
            wanted = str(label).rstrip(":：").strip()[:40]
            matches = [e for e in getattr(observation, "elements", ())
                       if _control_label(e).casefold() == wanted.casefold()]
            business = [e for e in matches if e.attrs.get("form_scope")
                        and e.attrs.get("page_control") != "query"]
            if business:
                own = _control_label(business[0])
                if own.casefold() in deferred:
                    kind = deferred.pop(own.casefold())[1]
                    covered.add((own, shown))
                    covered_values.add(shown)
                    ledger.add(Entry(own, shown, kind=kind))
                    continue
                if (wanted, shown) in covered or (own, shown) in covered or any(
                        e.field == wanted and e.value == shown for e in ledger.entries):
                    continue
                ledger.add(Entry(wanted, shown, kind="select" if (
                    business[0].role in _SELECT_ROLES) else "text"))
            elif matches:
                continue              # every match is a query/filter control
            else:
                parts = [part.strip() for part in _RANGE_FILL.split(shown) if part.strip()]
                if shown in covered_values or (len(parts) > 1
                        and all(part in covered_values for part in parts)):
                    continue  # already classified under the controls' own labels
                # A reported fill with no business field it could have gone into:
                # check it anyway and let the source check refuse it.
                ledger.add(Entry(wanted, shown, placed=False))
        # A redacted-echo fill nobody declared a value for cannot be checked:
        # record it as itself so the source check fails closed, never open.
        for own, kind in deferred.values():
            ledger.add(Entry(own, "[redacted]", kind=kind))

    def note_receipt(self, turn, field: str, value: str) -> None:
        note_receipt(turn, field, value)

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
            baselines[key], raw = self._read_form(s, element)
            form = baselines[key]
            port = self.native_submit
            record = None
            prefixes = port.prefixes(port.recent_reads(), mode=form.mode,
                                     record_id=form.record_id, source=raw.get("source", ""))
            if form.mode == "update":
                if not prefixes:
                    raise ContractRefused("entity_unknown")
                record = port.read_record(prefixes, form.record_id)
                if record is None:
                    raise ContractRefused("record_unreadable")
                turn.setdefault("form_record_baselines", {})[key] = record
            if prefixes:
                # Where the person's own press would land if this form is handed over.
                turn.setdefault("submit_targets", {})[
                    key or (form.mode, "")] = {"kind": "form", "mode": form.mode,
                                               "prefixes": list(prefixes),
                                               "record_id": form.record_id,
                                               "baseline": record if form.mode == "update" else None}
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

    @staticmethod
    def _note_unrestorable(turn, key, element, detail, effects=()) -> None:
        """A form whose opening self-edits could not be restored or proven: saving it
        directly would carry those edits, so no handoff may mark its submit control.
        `effects` are "label：before→after" as the person would check them."""
        if key is None:
            return
        blocked = turn.setdefault("unrestorable_forms", {})
        record = blocked.setdefault(key, {
            "scope": key[0], "instance": element.attrs.get("form_instance"),
            "lifetime": element.attrs.get("form_lifetime"), "effects": [], "detail": ""})
        for effect in effects or ():
            if effect not in record["effects"]:
                record["effects"].append(effect)
        if not record["detail"]:
            record["detail"] = str(detail or "")[:300]
        # A mark made before this block was discovered must not pair the wait with
        # this form's saved target anymore.
        turn.get("submit_targets", {}).pop(key, None)
        if turn.get("handoff_target_key") == key:
            turn["handoff_target_key"] = None

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
            refused = self._not_sent(s, exc.code, exc.detail)
            if exc.code in {"dangerous_unconfirmed", "record_changed", "record_unreadable",
                            "validation", "no_change", "opening_changes", "credential_field",
                            "value_not_from_user", "append_overwrite"}:
                turn["submit_handoff_blocked"] = exc.code
                return refused
            return self._submit_handoff(s, element, turn, refused, form=form)

    def _submit_handoff(self, s, element, turn, refused, *, form):
        if form:
            handoff = getattr(self, "_default_handoff", None)
            marked = handoff(s, turn) if handoff else ""
            handed = LoopToolResult("未能核验本次表单交接，助手未提交。", kind="page",
                                   state="completed" if marked else "unverified", marked=marked)
        else:
            handed = self._invoke_tool(s, "highlight", {"index": int(element.ref[1:])}, turn)
            marked = handed.marked if handed.state == "completed" else ""
            if handed.state == "completed" and not marked:
                handed = replace(handed, state="unverified", feedback="未能核验本次按钮交接，助手未提交。")
        s.ledger.append("native_submit_handoff", step=s.step, code=refused.error_code,
                        state=handed.state, marked=marked, handoff_error_code=handed.error_code,
                        dispatched=False, side_effect_possible=False)
        note = (f"「{marked}」已标出；助手尚未提交，请核对后自己点击。" if marked else handed.feedback)
        return replace(handed, feedback=refused.feedback + "\n" + note,
                       error_code=refused.error_code, marked=marked)

    def _not_sent(self, s, code, detail="") -> LoopToolResult:
        s.ledger.append("native_submit_not_sent", step=s.step, code=code, detail=detail[:300],
                        dispatched=False, side_effect_possible=False)
        names = dict(zip(("button", "target"), detail.split("\x1f"))) if code == "dangerous_unconfirmed" else {}
        template = MESSAGES.get(code)
        text = (template.format(detail=detail, **{"button": "", "target": "", **names})
                if template is not None else "没有提交：" + (detail or code))
        retry = code in {"value_not_from_user", "append_overwrite", "dangerous_unconfirmed", "validation", "no_change"}
        return LoopToolResult(text, kind="write", state="needs_input" if retry else "refused",
                              error_code=code)

    # -- waiting for the person's own press ---------------------------------------------
    def _await_handoff_press(self, s, turn) -> str:
        """A handoff marked a control: wait for the person to press it, the way approval
        waits are counted (outside the turn clock), then read the record back with the
        same actor. Returns the answer note; "" when nothing was handed over, the adapter
        cannot observe the person's writes, or the turn was cancelled."""
        port = self.native_submit
        contract_fn = getattr(port, "contract", None) if port is not None else None
        conventions = contract_fn() if callable(contract_fn) else None
        zone = conventions.epoch_zone if conventions is not None else None
        targets = turn.get("submit_targets") or {}
        target = targets.get(turn.get("handoff_target_key")) or {}
        marked = str(turn.get("handoff_button") or "")
        mark_fn = getattr(port, "user_write_mark", None)
        event_fn = getattr(port, "user_write_event", None)
        if (port is None or not marked or not target.get("prefixes")
                or not callable(mark_fn) or not callable(event_fn)):
            return ""
        prefixes = tuple(target["prefixes"])
        record_id = str(target.get("record_id") or "")
        create = target.get("mode") == "create" or (target.get("kind") == "form"
                                                  and not record_id)
        mark = turn.get("handoff_mark")
        if mark is None:
            mark = mark_fn()
        if mark is None:
            return ""           # without a position, observed history cannot be bounded
        deadline = time.monotonic() + max(1.0, s.approvals.approval_hold_ms / 1000)
        s.ledger.append("handoff_wait", step=s.step, marked=marked[:80],
                        kind=str(target.get("kind") or ""), record_id=record_id[:80],
                        prefixes=list(prefixes)[:4], dispatched=False, side_effect_possible=False)
        event = None
        with s.approvals.awaiting_person():
            while event is None and not s.cancelled():
                event = event_fn(prefixes, record_id, mark, create=create)
                if event is None:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        break
                    time.sleep(min(0.3, left))
        if s.cancelled():
            return ""
        if event is None:
            s.ledger.append("handoff_wait_result", step=s.step, result="no_press",
                            dispatched=False, side_effect_possible=False)
            return (f"已标出「{marked}」，等待时间内没有看到你点击它；表单未提交，"
                    "仍可自己核对后点击。")
        s.ledger.append("handoff_wait_result", step=s.step, result="pressed",
                        method=str(event.get("method") or ""), path=str(event.get("path") or "")[:200],
                        status=int(event.get("status") or 0),
                        dispatched=False, side_effect_possible=False)
        new_id = str(event.get("record_id") or record_id)
        record = None
        if new_id:
            try:
                record = port.read_record(prefixes, new_id)
            except Exception as exc:  # noqa: BLE001 -- readback failure is reported, not hidden
                s.ledger.append("handoff_readback_failed", step=s.step,
                                error_type=type(exc).__name__,
                                dispatched=False, side_effect_possible=False)
        if record is None:
            return (f"已看到你点击「{marked}」，应用已接受这次写入；"
                    "但按同一账号回读记录没有成功，请到应用里核对结果。")
        changes = _readback_changes(target.get("baseline"), record, zone=zone)
        if changes:
            return (f"已看到你点击「{marked}」，应用已接受写入；同一账号回读核对到变化："
                    + "、".join(changes) + "。")
        if not record_id and new_id:
            return f"已看到你点击「{marked}」，应用已接受写入，回读到新记录（编号 {new_id}）。"
        return f"已看到你点击「{marked}」，应用已接受写入；同一账号回读未发现待核对的变化。"

    # -- the adapter's declared record conventions --------------------------------------
    def _conventions(self, port):
        contract_fn = getattr(port, "contract", None)
        conventions = contract_fn() if callable(contract_fn) else None
        if conventions is None:
            raise ContractRefused("conventions_undeclared")
        return conventions

    # -- forms ------------------------------------------------------------------------
    def _submit_form(self, s, element, turn) -> LoopToolResult:
        port = self.native_submit
        conventions = self._conventions(port)
        current, raw = self._read_form(s, element)
        if raw.get("withheld"):
            raise ContractRefused("credential_field")
        key = self._form_key(element)
        loaded = turn.get("form_baselines", {}).get(key)
        if key in turn.get("form_baselines", {}) and loaded is None:
            raise ContractRefused("form_data_unreadable", "填写前未能读取完整表单和记录")
        if loaded is None:
            loaded = current                     # never typed into: the form as it stands
        if loaded.mode != current.mode or loaded.record_id != current.record_id:
            raise ContractRefused("form_data_unreadable", "the form changed record")
        ledger = turn.get("source_ledger") or SourceLedger()
        words = self._said(s.message.text)
        existing_fields: dict[str, str] = {}
        if loaded.mode == "update":
            for prop, val in loaded.model.items():
                if isinstance(val, str) and val.strip():
                    existing_fields[prop] = val
                    label = loaded.label(prop)
                    if label:
                        existing_fields[label] = val
        verdicts, missing = verify_value_sources(ledger, words, existing=existing_fields or None)
        s.ledger.append("native_submit_value_sources", step=s.step,
                        checks=[{"field": v.entry.field, "ok": v.ok, "source": v.source,
                                 "reason": v.reason, **({"value": v.entry.value} if not v.ok else {})} for v in verdicts])
        if missing:
            # An unsourced value is refused before the card: the model must ask the
            # person instead of presenting invented values for approval.
            raise ContractRefused("value_not_from_user",
                                  "、".join(f"{v.entry.field}={v.entry.value}" for v in missing))
        if current.mode == "update":
            self._check_append_overwrite(s.message.text, loaded.model, current)
        requested = self._requested_fields(current, ledger)
        reads = port.recent_reads()
        prefixes = port.prefixes(reads, mode=current.mode, record_id=current.record_id, source=raw.get("source", ""))
        if not prefixes:
            raise ContractRefused("entity_unknown")
        target_key = key or (current.mode, "")
        turn.setdefault("submit_targets", {})[target_key] = {
            "kind": "form", "mode": current.mode, "prefixes": list(prefixes),
            "record_id": current.record_id, "button": element.name.strip()}
        baseline = None
        comparison, restored = None, ()
        if current.mode == "update":
            baseline = port.read_record(prefixes, current.record_id)
            if baseline is None:
                raise ContractRefused("record_unreadable")
            turn["submit_targets"][target_key]["baseline"] = baseline
            self._check_append_overwrite(s.message.text, baseline, current)
            earlier = turn.get("form_record_baselines", {}).get(key)
            if earlier is not None and not same_value(
                    strip_volatile(earlier, conventions.volatile_keys),
                    strip_volatile(baseline, conventions.volatile_keys),
                    zone=conventions.epoch_zone):
                raise ContractRefused("record_changed", "填写期间目标记录被别人修改，请重新核对")
            try:
                comparison = record_model(baseline, current.model, conventions)
                conversions = conversion_fields(strip_volatile(comparison, conventions.volatile_keys),
                                                strip_volatile(loaded.model, conventions.volatile_keys),
                                                conventions=conventions)
                proposed = opening_values(strip_volatile(comparison, conventions.volatile_keys),
                                          strip_volatile(loaded.model, conventions.volatile_keys),
                                          current.model, conventions)
            except ContractRefused as exc:
                # An unreadable line identity prevents proving that a handoff would not
                # carry an opening-only change. It is a hard stop, never a generic handoff.
                self._note_unrestorable(turn, key, element, exc.detail)
                raise ContractRefused("opening_changes", exc.detail or "无法核对页面打开时的明细变化") from exc
            restored = tuple(diff_models(current.model, proposed, conventions=conventions))
            if restored:
                values = {c.field: proposed[c.field] for c in restored}
                effects = [f"{current.label(k)}：{proposed[k]}→{current.model[k]}" for k in sorted(values)]
                try:
                    raw = self._read(s, element, "restore", expected=current.model, values=values)
                except ContractRefused:
                    self._note_unrestorable(turn, key, element, "页面自动变化还原失败", effects)
                    raise ContractRefused("opening_changes", "页面自动变化还原失败，请自行核对提交；保存可能改为：" +
                                          "、".join(f"{current.label(k)}={current.model[k]}" for k in sorted(values))) from None
                current = FormData.from_json(raw)
                if raw.get("invalid") or raw.get("withheld") or not same_value(
                        current.model, proposed, zone=conventions.epoch_zone):
                    self._note_unrestorable(turn, key, element, "页面打开时改了字段，还原未通过", effects)
                    raise ContractRefused("opening_changes", "页面打开时改了字段，还原未通过或页面又改回；保存会改变：" + "、".join(sorted(values)))
                turn["restored_form_labels"] = frozenset(current.label(c.field) for c in restored)
        if not restored:
            raw = self._read(s, element, "validate")
        final = FormData.from_json(raw)
        if final.mode != current.mode or final.record_id != current.record_id or raw.get("withheld"):
            raise ContractRefused("form_data_unreadable", "校验期间表单目标发生变化")
        if raw.get("validation") is not True or raw.get("invalid"):
            raise ContractRefused("validation", "、".join(str(p) for p in raw.get("invalid", [])[:8]) or "应用没有确认校验通过")
        if any(not same_value(final.model.get(c.field), current.model.get(c.field),
                              zone=conventions.epoch_zone) for c in restored):
            changed = [f"{current.label(c.field)}：{current.model.get(c.field)}→{final.model.get(c.field)}"
                       for c in restored
                       if not same_value(final.model.get(c.field), current.model.get(c.field),
                                         zone=conventions.epoch_zone)]
            self._note_unrestorable(turn, key, element, "页面在还原后又自动改值", changed)
            raise ContractRefused("opening_changes", "页面在还原后又自动改值，请自行核对提交")
        current = final
        if baseline is not None and conversions:
            raise ContractRefused("pre_submit_conversion", "表单使用提交前转换，不能把原始记录值写回表单；已保留表单供你核对提交")
        comparison = record_model(baseline, current.model, conventions) if baseline is not None else None
        card = build_card(loaded=loaded, form=current, page=self._page_name(s), button=element.name.strip(),
                          mode=current.mode, record_id=current.record_id, requested=requested,
                          record=comparison, restored=restored, conventions=conventions)
        card = replace(card, value_sources=tuple((v.entry.field, v.entry.value, v.source)
                                                for v in verdicts))
        return self._approve_and_press(s, element, turn, card, prefixes, baseline,
                                       kind="form", check=raw.get("check", ""), loaded=loaded)

    @staticmethod
    def _check_append_overwrite(prompt: str, before: dict | None, current: FormData) -> None:
        if not isinstance(before, dict):
            return
        checks = (
            ((current.label(prop) or prop, prop), before.get(prop), new_val)
            for prop, new_val in current.model.items()
            if isinstance(new_val, str)
        )
        overwritten = check_append_preservation(prompt, checks)
        if overwritten:
            raise ContractRefused("append_overwrite", "、".join(overwritten))

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
        reads = port.recent_reads()
        prefixes = port.row_prefixes(reads, row)
        if not prefixes:
            raise ContractRefused("entity_unknown")
        turn.setdefault("submit_targets", {})["row"] = {
            "kind": "row", "prefixes": list(prefixes), "record_id": record_id,
            "button": button}
        baseline = port.read_record(prefixes, record_id)
        if baseline is None:
            raise ContractRefused("record_unreadable")
        turn["submit_targets"]["row"]["baseline"] = baseline
        request = port.row_request(prefixes, row, element, baseline)
        if request is None:
            raise ContractRefused("action_unknown", "无法认定这个控件对应的接口和具体字段变化，助手未提交")
        card = build_card(loaded=None, form=None, page=self._page_name(s), button=button, mode="action",
            target_name=name, record_id=record_id, record=baseline, action_request=request,
            conventions=self._conventions(port))
        return self._approve_and_press(s, element, turn, card, prefixes, baseline,
                                       kind="row", record_id=record_id, row_check=str(row.get("check") or ""))

    def _submit_pipeline(self) -> WritePipeline:
        pipeline = getattr(self, "_native_submit_pipeline", None)
        if pipeline is None:
            pipeline = WritePipeline()
            self._native_submit_pipeline = pipeline
        return pipeline

    # -- approval, press, readback -----------------------------------------------------
    def _approve_and_press(self, s, element, turn, card: SubmitCard, prefixes, baseline, *,
                           kind, check="", record_id="", loaded=None, row_check="") -> LoopToolResult:
        port = self.native_submit
        conventions = self._conventions(port)
        step = s.step
        context = s.approvals.context(step, "native_submit", hold_ms=s.approvals.approval_hold_ms)
        proposal = card.proposal(context_digest=context.binding_digest if context is not None else "")
        s.ledger.append(
            "native_submit_card", step=step, mode=card.mode, target=card.target, page=card.page,
            button=card.button, form_digest=card.form.digest if card.form else "",
            loaded_digest=card.loaded.digest if card.loaded else "",
            changes=[d.to_json() for d in card.details()],
            restored_fields=sorted({c.field for c in card.restored}))
        self._private_submit_record(s, "card", loaded=card.loaded.model if card.loaded else None,
            submitted=card.form.model if card.form else None, record=baseline,
            validation={"check": check}, changes=[d.to_json() for d in card.details()])
        s.write_intent = True
        turn["submit_started"] = True
        identity = context.audit_projection() if context is not None else {}

        def on_not_approved(approval) -> LoopToolResult:
            if approval.reason == "prefill_selected":
                default_handoff = getattr(self, "_default_handoff", None)
                marked = default_handoff(s, turn) if callable(default_handoff) else ""
                if not marked:
                    marked = element.name.strip()[:200]
                    if hasattr(self, "_page_fingerprint") and getattr(s, "obs", None) is not None:
                        turn["handoff"] = (self._page_fingerprint(s.obs), marked)
                from .loop_guard import build_handoff_payload
                outcomes = dict(turn.get("form_field_outcomes") or {})
                if not outcomes.get("applied_fields") and card.form is not None:
                    outcomes["applied_fields"] = {
                        card.form.label(c.field): str(c.new)
                        for c in card.changes
                        if c.new not in (None, "")
                    }
                if hasattr(s, "handoff_payload"):
                    s.handoff_payload = build_handoff_payload(
                        outcomes,
                        observation=getattr(s, "obs", None),
                        fallback_entered=turn.get("touched", ()),
                    )
                s.ledger.append(
                    "native_submit_prefill_selected",
                    step=step,
                    target=card.target,
                    marked=marked,
                    dispatched=False,
                    side_effect_possible=False,
                )
                s.terminal_reason = "agent_finished"
                turn["final"] = (
                    f"已按你的选择只填到表单并保留提交，提交按钮「{marked}」已交接给你；请核对后自己点击提交。"
                    if marked
                    else "已按你的选择只填到表单并保留提交，请核对当前表单后自己提交。"
                )
                return LoopToolResult(
                    turn["final"],
                    kind="write",
                    state="completed",
                    dispatch="not_dispatched",
                    marked=marked,
                    terminal_reason=s.terminal_reason,
                )
            s.terminal_reason, s.detail = approval.reason, approval.detail[:200]
            turn["final"] = {"approval_denied": "你没有批准，助手未提交这项修改。",
                             "approval_expired": "批准卡片已过期，助手未提交这项修改。",
                             "user_cancelled": "助手操作已停止，未提交这项修改。"}.get(approval.reason, "助手未提交这项修改。")
            return LoopToolResult(turn["final"], kind="write", state="refused",
                                  terminal_reason=s.terminal_reason)

        def pre_dispatch_reread():
            if kind == "form":
                fresh, fresh_raw = self._read_form(s, element)
                if fresh_raw.get("check") != check or fresh.digest != card.form.digest:
                    raise ContractRefused("form_changed", "批准之后表单里的数据又变了")
            else:
                row = self._read(s, element, "row")
                if str(row.get("record_id") or "") != record_id or str(row.get("check") or "") != row_check:
                    raise ContractRefused("row_changed", "批准之后这一行不再是原来的记录")
            now = port.read_record(prefixes, card.record_id) if card.mode != "create" else None
            if card.mode != "create":
                if now is None:
                    raise ContractRefused("record_unreadable", "没能重新读取目标记录")
                if not same_value(strip_volatile(now, conventions.volatile_keys),
                                  strip_volatile(baseline, conventions.volatile_keys),
                                  zone=conventions.epoch_zone):
                    raise ContractRefused("record_changed", "批准之后这条记录被别人改过")
            return now

        def on_reread_drift(exc: Exception) -> LoopToolResult:
            if isinstance(exc, ContractRefused):
                return self._stopped(s, turn, exc.code, exc.detail or "核对没有通过")
            return self._stopped(s, turn, "record_unreadable", "核对没有通过")

        state_holder: dict = {}

        def dispatch_once(approval, now):
            s.ledger.append("native_submit_reread", step=step, form_digest=card.form.digest if card.form else "",
                            prefixes=list(prefixes), documented_writes=sorted(f"{m} {p}" for m, p in port.catalog(prefixes))[:12],
                            record_digest=digest(now) if now is not None else "",
                            dispatched=False, side_effect_possible=False)
            self._private_submit_record(s, "before_click", record=now)
            permit = SubmitPermit(
                actor="", card=card, prefixes=prefixes, catalog=port.catalog(prefixes),
                conventions=conventions, baseline=baseline,
                reread=(lambda: port.read_record(prefixes, card.record_id)) if card.mode != "create" else None,
                reread_observer=lambda record: self._private_submit_record(s, "before_release", record=record))
            s.ledger.append("native_submit_approved", step=step, approval_id=approval.approval_id,
                            permit=permit.permit_id, dispatched=False,
                            side_effect_possible=False)
            state_holder["permit"] = permit
            label = element.name.strip()
            press = ({"ref": element.ref, "kind": "form", "check": check, "label": label} if kind == "form"
                     else {"ref": element.ref, "kind": "row", "record_id": record_id, "label": label, "row_check": row_check})
            broken, confirmed = "", ""
            try:
                clicked = self._press(s, permit, press)
                if not clicked:
                    return False, ("permit_unavailable", None)
                confirmed = self._handle_confirmation(s, permit, card)
            except Exception as exc:  # noqa: BLE001 -- after a click nothing can be assumed not to have been sent
                broken = type(exc).__name__
                s.ledger.append("native_submit_exception", step=step, permit=permit.permit_id, error_type=broken,
                                dispatched=True, side_effect_possible=True)
            return bool(broken), confirmed

        def settle_and_readback(_approval, uncertain: bool, confirmed):
            permit = state_holder["permit"]
            if isinstance(confirmed, tuple) and confirmed[0] == "permit_unavailable":
                return self._stopped(s, turn, "permit_unavailable", "这次提交的许可没能建立（上一次请求结果未确认，或另一个许可仍在使用）")
            if confirmed == "refused":
                return self._finish_submit(s, turn, permit, card, prefixes, baseline, readback=None,
                                           extra="应用弹出的确认框里有卡上没有的后果，已取消，没有执行。")
            return self._finish_submit(s, turn, permit, card, prefixes, baseline, uncertain=uncertain)

        offer_prefill = (
            kind == "form"
            and card.form is not None
            and card.mode in {"create", "update"}
            and not turn.get("submit_handoff_blocked")
            and bool(getattr(self, "_offer_prefill_for_form", lambda *_: True)(s, turn, card))
        )
        return self._submit_pipeline().run_submit_pipeline(
            proposal=proposal,
            approvals=s.approvals,
            step=step,
            context=context,
            identity=identity,
            on_not_approved=on_not_approved,
            pre_dispatch_reread=pre_dispatch_reread,
            on_reread_drift=on_reread_drift,
            dispatch_once=dispatch_once,
            settle_and_readback=settle_and_readback,
            offer_prefill=offer_prefill,
        )

    def _stopped(self, s, turn, code, detail) -> LoopToolResult:
        s.ledger.append("native_submit_not_sent", step=s.step, code=code, detail=detail,
                        dispatched=False, side_effect_possible=False)
        s.terminal_reason, s.detail = "business_mutation_refused", code
        turn["submit_handoff_blocked"] = code
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
                       "detail": list(r.detail), "reread_digest": r.reread_digest,
                       "answer": r.answer if isinstance(r.answer, (int, str)) else None}
                      for r in permit.log],

            duplicates=permit.duplicates, readback=outcome, notes=notes,
            side_effect_possible=True)
        self._private_submit_record(s, "result", state=state, readback_record=self._last_readback,
            requests=[{"sequence": r.sequence, "method": r.method, "path": r.path,
                "body": r.sent, "decision": r.decision, "status": r.status, "answer": r.answer}
                for r in permit.log])
        what = {"create": "新增", "update": "修改", "action": "执行"}[card.mode]
        label = f"{card.effect()}"
        blocked = [r for r in permit.blocked() if r.decision != "duplicate"]
        if state == "completed":
            turn["written"].append(label)
            details = [f"{d.label}：{d.text}"[:200] for d in card.details() if d.kind in ("change", "body")][:12]
            if not details and baseline is not None and self._last_readback is not None:
                details = _readback_changes(baseline, self._last_readback, zone=conventions.epoch_zone)
            s.mutation_outcomes.append({"position": len(s.mutation_outcomes), "effect": card.effect(),
                                        "targets": [card.target], "verified": True,
                                        **({"details": details} if details else {}),
                                        "receipt": {"reference": permit.created_id() or card.record_id}})
            text = "已执行并核对（用户已批准，应用回读确认）：" + label
            if notes:
                text += "。" + "；".join(notes)
            if permit.duplicates:
                text += f"。另有 {permit.duplicates} 次重复请求被拦下，没有重复执行"
            turn["final"] = text
            return LoopToolResult(text, kind="write", state="completed", dispatch="acknowledged")
        if state == "not_executed" and blocked and all(r.decision in {
                "body_differs_from_form", "request_not_object", "request_without_card_change",
                "change_not_on_card", "interface_not_for_target", "action_not_on_card",
                "action_body_not_on_card", "query_not_on_card", "target_mismatch"} for r in blocked):
            targets = [e for e in getattr(s.obs, "elements", ()) if e.name.strip() == card.button]
            anchor = self._anchor(s) if card.mode != "action" else targets[0] if len(targets) == 1 else None
            if anchor is not None:
                refused = self._not_sent(s, "request_contract", "应用请求与批准卡不符，写请求已拦下")
                result = self._submit_handoff(s, anchor, turn, refused, form=card.mode != "action")
                turn["final"] = result.feedback
                return result
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
        conventions = self._conventions(self.native_submit)
        zone = conventions.epoch_zone
        if card.mode == "action":
            return not same_value(strip_volatile(now, conventions.volatile_keys),
                                  strip_volatile(baseline, conventions.volatile_keys), zone=zone)
        # An edit: our write is in the record if a field the card changed now holds the new value
        # it did not hold before. Another person's change to some other field is not ours.
        form = card.form.model
        return any(isinstance(now, dict) and same_value(now.get(f), form.get(f), zone=zone)
                   and not same_value(baseline.get(f), form.get(f), zone=zone)
                   for f in card.changed_fields)

    _last_readback = None

    def _readback(self, card: SubmitCard, permit: SubmitPermit, prefixes, baseline):
        """(True|False|None, notes): the record as the same person reads it after the click."""
        port = self.native_submit
        conventions = self._conventions(port)
        zone = conventions.epoch_zone
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
            expected = card.action_request
            if expected is None or not isinstance(now, dict):
                return None, ["无法回读动作要求的字段"]
            wrong = [k for k, v in expected.body.items()
                     if k != conventions.record_key and not same_value(now.get(k), v, zone=zone)]
            previous = strip_volatile(baseline, conventions.volatile_keys)
            latest = strip_volatile(now, conventions.volatile_keys)
            other = sorted(k for k in previous.keys() | latest.keys() if k not in card.changed_fields
                           and (k not in previous or k not in latest
                                or not same_value(previous[k], latest[k], zone=zone)))
            return not wrong, (["回读时动作字段不符：" + "、".join(wrong)] if wrong else []) + (
                ["回读时其它字段与点击前不同：" + "、".join(other)] if other else [])
        if now is None:
            return None, ["回读时找不到这条记录"]
        form = card.form.model
        wrong, checked = [], 0
        for change in (*card.changes, *card.other, *card.unrequested):
            top = change.field
            if top not in now:
                continue
            checked += 1
            if not _contains(now.get(top), form.get(top), zone=zone):
                wrong.append(card.form.label(top))
        notes = []
        if card.mode == "update" and isinstance(baseline, dict):
            moved = [k for k in now if k not in card.changed_fields and k in baseline
                     and not same_value(strip_volatile({k: baseline[k]}, conventions.volatile_keys),
                                        strip_volatile({k: now[k]}, conventions.volatile_keys),
                                        zone=zone)]
            if moved:
                notes.append("回读时其它字段与点击前不同：" + "、".join(card.form.label(k) for k in moved[:6]))
        if wrong:
            notes.append("回读与批准卡不一致：" + "、".join(wrong[:6]))
            return False, notes
        return (True if checked else None), notes


def _readback_changes(baseline, record, *, zone=None) -> list:
    """"label：before→after" pairs between the baseline and the same-actor readback."""
    if not isinstance(baseline, dict) or not isinstance(record, dict):
        return []
    out = []
    for key, after in record.items():
        if not isinstance(key, str) or key.startswith("_"):
            continue
        before = baseline.get(key)
        if same_value(before, after, zone=zone) or (is_empty(before) and is_empty(after)):
            continue
        out.append(f"{key}：{before}→{after}")
        if len(out) >= 8:
            break
    return out
