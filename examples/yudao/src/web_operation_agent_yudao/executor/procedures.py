"""Application knowledge and bounded page procedures for YudaoTurnExecutor."""
from __future__ import annotations

import contextlib
from dataclasses import replace
import hashlib
import json
import re
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlsplit

from web_operation_agent.business.errors import (
    BusinessToolIdentityError,
    BusinessToolPermissionError,
    BusinessToolUnavailableError,
)
from web_operation_agent.execution.checks import record_check_fired
from web_operation_agent.execution.draft_handoff import (
    add_draft_lines,
    field_identity,
    handoff_draft,
    opening_changes_refusal,
    unrestorable_form,
)
from web_operation_agent.execution.model_loop_contracts import AnswerTarget, LoopToolResult
from web_operation_agent.execution.read_summary import SummaryRefused
from web_operation_agent.execution.value_source import note_candidates, note_receipt
from web_operation_agent.goal.data import DataRefused
from web_operation_agent.privacy import redact_sensitive_assignments
from web_operation_agent.schemas.browser import MAX_SCROLL_PAGES

from ..reads import bound_data_reads
from ..reads.api_content import fenced, redacted
from ..reads.live_filters import active_view_or_tab, canonical_filter_pairs, control_identity
from ..reads.record_access import contact_can_edit, customer_can_edit
from ..reads.turn_tables import TurnReadError, date_range
from ..writes.live_forms import SUBMIT_WORDS, observed_radio_choices, plain
from ..writes.write_intent import WriteIntent, classify_write_intent
from .actor_knowledge import QueryParametersError
from .page_prompts import (
    VERIFY_SYSTEM,
    VERIFY_WRITES,
    SlimTurnContext,
    _DEPTH,
    _FORM_BATCH_FIELDS,
    _LINE,
    _LOOK_MS,
    _PAGING,
    _PENDING_MS,
    _STILL_LOOKS,
    _TABLE_TOOLS,
    _business_handoff_ready,
    _control_kind,
    _control_name,
    _commits,
    _date_pair,
    _date_range_receipt_values,
    _fields,
    _lines,
    _list_scroll_region,
    _name_parts,
    _native_input_text,
    _of_kind,
    _own_list_facts,
    _own_list_path,
    _page_lines,
    _precomputed_dates,
    _record_handoff_control,
    _record_page,
    _scroll_anchor,
    _shown,
    _unavailable_already_matches,
    page_text,
)

_SCOPE = re.compile(r"(?:form|dialog|dialog_labelledby|dialog_describedby):")


def _business_scope(scope) -> bool:
    """A form or dialog scope as the Runtime names it, whichever way the dialog is labelled."""
    return isinstance(scope, str) and _SCOPE.match(scope) is not None


def _draft_only_request(text: str) -> bool:
    """A current-turn request to prepare a form without a persistent write."""
    return classify_write_intent(text).mode == "draft"


def _draft_field_named(label: str, text: str, *, labels=()) -> bool:
    def base(value):
        return re.sub(r"\s*[（(]\s*(?:%|％|元|万元|千元|KG|kg|吨|千克|公斤|件|个)\s*[）)]$", "", value)

    plain_label = base(label)
    if plain_label != label and not any(other != label and base(other) == plain_label for other in labels):
        label = plain_label
    aliases = {
        "手机号码": ("手机号码", "手机号", "手机"),
        "联系电话": ("联系电话", "固定电话", "电话"),
        "下次联系时间": ("下次联系时间", "下次联系日期"),
    }
    return any(word in text for word in aliases.get(label, (label,)))


def _unrequested_draft_fields(
    fields: list[dict],
    current: str,
    earlier: tuple[str, ...] | list[str],
    *,
    other_labels: list[str] | tuple[str, ...] = (),
) -> list[str]:
    """Reject extra draft fields before any page action, using only the person's words."""
    current = re.sub(r"\s+", "", current)
    history = re.sub(r"\s+", "", " ".join(earlier))
    items = [(str(item.get("label", "")).strip().rstrip(":：").strip(), str(item.get("value", ""))) for item in fields]
    labels = {label for label, _ in items if label and label not in _PAGING} | {label for label in other_labels if label}
    named_now = {label for label in labels if _draft_field_named(label, current, labels=labels)}
    return [
        label for label, value in items
        if label in labels and label not in named_now
        and not (value and re.sub(r"\s+", "", value) in current)
        and (named_now or not _draft_field_named(label, history, labels=labels))
    ]


def _new_form_draft_request(text: str) -> bool:
    """Distinguish a new draft from editing an existing record in this turn."""
    return _draft_only_request(text) and not re.search(r"编辑|修改|已有|原值|当前值|改成|改为", text)


def _empty_filter_key(route: str, active_view: str = "", filter_pairs=()) -> tuple[str, str, tuple[tuple[str, str], ...]]:
    return (str(route or ""), str(active_view or ""), canonical_filter_pairs(filter_pairs))


def _verified_empty_store(ctx, policy_turn=None) -> set:
    if isinstance(policy_turn, dict) or (policy_turn is not None and hasattr(policy_turn, "setdefault")):
        store = policy_turn.setdefault("verified_empty_filters", set())
        if isinstance(store, set):
            return store
    store = getattr(ctx, "verified_empty_filters", None) if ctx is not None else None
    if not isinstance(store, set):
        store = set()
        if ctx is not None:
            try:
                setattr(ctx, "verified_empty_filters", store)
            except Exception:
                pass
    return store


def record_verified_empty_filter(ctx, policy_turn, *, route: str, active_view: str = "", filter_pairs=()):
    key = _empty_filter_key(route, active_view, filter_pairs)
    from web_operation_agent.execution import loop_guard
    core_fn = getattr(loop_guard, "record_verified_empty_filter", None)
    if callable(core_fn) and policy_turn is not None:
        try:
            core_fn(policy_turn, route=key[0], active_view_or_tab=key[1], filter_pairs=key[2])
        except TypeError:
            try:
                core_fn(policy_turn, key[0], key[1], key[2])
            except Exception:
                pass
    _verified_empty_store(ctx, policy_turn).add(key)
    return key


def is_verified_empty_filter(ctx, policy_turn, *, route: str, active_view: str = "", filter_pairs=()) -> bool:
    key = _empty_filter_key(route, active_view, filter_pairs)
    from web_operation_agent.execution import loop_guard
    core_fn = getattr(loop_guard, "is_verified_empty_filter", None)
    if callable(core_fn) and policy_turn is not None:
        try:
            if core_fn(policy_turn, route=key[0], active_view_or_tab=key[1], filter_pairs=key[2]):
                return True
        except TypeError:
            try:
                if core_fn(policy_turn, key[0], key[1], key[2]):
                    return True
            except Exception:
                pass
    return key in _verified_empty_store(ctx, policy_turn)


def _current_page_query_filters(observation, overrides=None) -> tuple[tuple[str, str], ...]:
    """Extract current visible query-control `(label, value)` pairs merged with optional overrides."""
    merged: dict[str, str] = {}
    if observation is not None:
        for e in getattr(observation, "elements", ()) or ():
            attrs = getattr(e, "attrs", {}) or {}
            if attrs.get("page_control") != "query":
                continue
            label = str(attrs.get("field_label") or attrs.get("title") or "").strip().rstrip(":：").strip()
            if not label:
                continue
            val = attrs.get("selected_option") if attrs.get("selected_option") is not None else attrs.get("value")
            if isinstance(val, str) and val.strip():
                merged[label] = val.strip()
    if overrides:
        for item in overrides:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or "").strip().rstrip(":：").strip()
            if not label or label in _PAGING:
                continue
            val = str(item.get("value") or "").strip()
            if val:
                merged[label] = val
            else:
                merged.pop(label, None)
    return canonical_filter_pairs(merged)


def _is_settled_action_receipt(action: str, receipt, *, expected_path: str = "") -> bool:
    if action not in {"click", "select", "goto"} or receipt is None or not getattr(receipt, "ok", False):
        return False
    obs = getattr(receipt, "observation", None)
    summary = getattr(receipt, "observation_summary", None)
    if obs is None or getattr(obs, "render_state", "") != "ready" or not isinstance(summary, dict) or not summary:
        return False
    if expected_path:
        return urlsplit(obs.url).path == expected_path and getattr(obs, "page_route", "") == expected_path
    return True


def _format_observation_summary(summary: dict | None, handoffs=None) -> str:
    if not isinstance(summary, dict) or not summary:
        return ""
    parts: list[str] = []
    if summary.get("url_changed"):
        parts.append("页面已跳转")
    if summary.get("active_view"):
        parts.append(f"当前视图={summary['active_view']}")
    if summary.get("dialog_title"):
        parts.append(f"弹窗={summary['dialog_title']}")
    if isinstance(summary.get("row_count"), int):
        parts.append(f"可见行数={summary['row_count']}")
    msgs = summary.get("new_messages")
    if isinstance(msgs, (list, tuple)) and msgs:
        parts.append("提示=" + " | ".join(str(m) for m in msgs[:2]))
    if not parts:
        return ""
    line = "页面变化摘要：" + "；".join(parts)
    if handoffs is not None:
        line = redacted(line, handoffs)
    return redact_sensitive_assignments(line)[:400]


def _is_restricted_control(element) -> str:
    if element is None:
        return ""
    attrs = getattr(element, "attrs", {}) or {}
    for key in ("restricted", "form_input_kind", "input_type", "type"):
        val = str(attrs.get(key) or "").strip().lower()
        if val in {"password", "file"}:
            return val
    if attrs.get("restricted") in {True, "true"}:
        return "restricted"
    return ""


def _collect_restricted_controls(observation, scope: str = "") -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for e in getattr(observation, "elements", ()) or ():
        attrs = getattr(e, "attrs", {}) or {}
        if scope and attrs.get("form_scope") != scope:
            continue
        kind = _is_restricted_control(e)
        if not kind:
            continue
        label = _control_name(e)
        if not label or (label, kind) in seen:
            continue
        seen.add((label, kind))
        item: dict[str, str] = {"label": label, "kind": kind}
        if getattr(e, "ref", ""):
            item["ref"] = str(e.ref)
        found.append(item)
    return found


def _field_outcome_bucket(turn) -> dict:
    bucket = turn.setdefault("field_outcomes", {})
    bucket.setdefault("applied_fields", {})
    bucket.setdefault("rejected_fields", {})
    bucket.setdefault("previous_fields", {})
    bucket.setdefault("restricted_fields", [])
    return bucket


def _merge_field_outcomes(
    turn,
    *,
    applied: dict | None = None,
    rejected: dict | None = None,
    previous: dict | None = None,
    restricted=(),
) -> dict:
    bucket = _field_outcome_bucket(turn)
    if isinstance(previous, dict):
        for k, v in previous.items():
            if k:
                bucket["previous_fields"].setdefault(str(k), "" if v is None else str(v))
    if isinstance(applied, dict):
        for k, v in applied.items():
            if k:
                key = str(k)
                bucket["applied_fields"][key] = "" if v is None else str(v)
                bucket["rejected_fields"].pop(key, None)
    if isinstance(rejected, dict):
        for k, v in rejected.items():
            if k:
                key = str(k)
                bucket["rejected_fields"][key] = v
                bucket["applied_fields"].pop(key, None)
    if restricted:
        existing = list(bucket["restricted_fields"])
        for item in restricted:
            val = str(item or "").strip()
            if val and val not in existing:
                existing.append(val)
        bucket["restricted_fields"] = existing
    return bucket


def build_handoff_facts(turn, observation=None, *, scope: str = "") -> dict | None:
    """Build bounded `HandoffCardFacts` from turn field outcomes and restricted controls."""
    bucket = (turn or {}).get("field_outcomes")
    if not isinstance(bucket, dict):
        return None
    applied = bucket.get("applied_fields") or {}
    rejected = bucket.get("rejected_fields") or {}
    previous = bucket.get("previous_fields") or {}
    restricted_labels = list(bucket.get("restricted_fields") or ())
    completed = [{"name": str(k), "value": str(v)} for k, v in applied.items() if k]
    pending = []
    seen_pending: set[str] = set()
    for k in rejected:
        name = str(k)
        if name and name not in seen_pending:
            seen_pending.add(name)
            pending.append({"name": name})
    restricted_controls = _collect_restricted_controls(observation, scope=scope) if observation is not None else []
    restricted_map = {c["label"]: c for c in restricted_controls}
    for label in restricted_labels:
        if label not in restricted_map:
            restricted_controls.append({"label": label, "kind": "password" if "密码" in label else "restricted"})
        if label not in seen_pending:
            seen_pending.add(label)
            pending.append({"name": label})
    for ctrl in restricted_controls:
        if ctrl["label"] not in seen_pending:
            seen_pending.add(ctrl["label"])
            pending.append({"name": ctrl["label"]})
    if not completed and not pending and not restricted_controls:
        return None
    out: dict = {
        "completed_fields": completed,
        "pending_fields": pending,
        "can_undo": bool(previous and completed),
    }
    if restricted_controls:
        out["restricted_controls"] = restricted_controls
    return out


class _Procedure:
    def __init__(self, owner, name):
        self.owner, self.name = owner, name

    def execute(self, arguments, *, context):
        return self.owner.execute(self.name, arguments, context)


class _ConcurrentReadProcedure(_Procedure):
    """Pure-read procedure supporting threaded prepare_read + main-thread commit_read."""

    def prepare_read(self, arguments, *, observation=None, operation=None):
        return self.owner.prepare_read(self.name, arguments, observation=observation, operation=operation)

    def commit_read(self, prepared, arguments, *, context):
        return self.owner.commit_read(self.name, prepared, arguments, context)


class YudaoProceduresBase:
    """Application knowledge and bounded page procedures for the shared loop."""

    def __init__(self, knowledge, goal_port, origin, *, turn_context: SlimTurnContext | None = None,
                 publish_step=None, check_fired=None):
        self.knowledge, self.goal_port = knowledge, goal_port
        self.application = SimpleNamespace(descriptor=SimpleNamespace(origin=origin))
        #: The last business-form batch this turn entered, kept for a handoff made at its end.
        self.form_fill = None
        self.turn_context = turn_context if turn_context is not None else SlimTurnContext()
        self._publish_step_cb = publish_step
        self._check_fired_cb = check_fired
        self._last_action_receipt = None

    def _ctx(self) -> SlimTurnContext:
        ctx = getattr(self, "turn_context", None)
        if ctx is None:
            ctx = SlimTurnContext()
            self.turn_context = ctx
        return ctx

    def _publish_read_page(self, s, page_no: int) -> None:
        if not isinstance(page_no, int) or isinstance(page_no, bool) or page_no < 1:
            return
        facts = {"stage": "read_page", "page": page_no}
        turn_id = str(getattr(getattr(s, "message", None), "message_id", "")
                      or getattr(getattr(s, "operation", None), "turn_id", "") or "")
        cb = getattr(self, "_publish_step_cb", None)
        if callable(cb):
            cb(facts, turn_id)
            return
        pub = getattr(s, "_publish_step", None)
        if callable(pub):
            pub(facts, turn_id)
            return
        progress = getattr(s, "progress", None)
        if callable(progress):
            progress(facts)

    def _emit_check(self, s, check: str, *, outcome: str = "blocked",
                    attempt: int = 1, answer_preserved: bool = False) -> None:
        cb = getattr(self, "_check_fired_cb", None)
        if callable(cb):
            cb(s, check, outcome=outcome, attempt=attempt, answer_preserved=answer_preserved)
            return
        ledger = getattr(s, "ledger", None)
        if ledger is not None:
            record_check_fired(
                ledger,
                s=s,
                trace=None,
                check=check,
                outcome=outcome,
                attempt=attempt,
                answer_preserved=answer_preserved,
            )

    def _bound_reads(self, data, operation, checkpoint):
        return bound_data_reads.BoundDataReads(data, operation, checkpoint, turn_context=self._ctx())

    # -- the page, through the executor's own dispatch ------------------------------------
    def _settled(self, s, *, path: str = "", waits: int = 8) -> str:
        """Let the page finish what the last step started before it is read again."""
        looks, last = 0, None
        history = s.page.action_observations()
        if history and history[-1].to_json() == s.obs.to_json():
            for obs in history[:-1]:
                arrived = not path or (urlsplit(obs.url).path == path and obs.page_route == path)
                if arrived and obs.render_state == "ready":
                    shown = _shown(obs)
                    looks, last = (looks + 1 if shown != last else looks), shown
                else:
                    looks, last = 0, None
        for _ in range(waits + _STILL_LOOKS):
            obs = s.obs
            arrived = not path or (urlsplit(obs.url).path == path and obs.page_route == path)
            if arrived and obs.render_state == "blocked":
                return "blocked"
            ready = arrived and obs.render_state != "pending" and (not path or obs.render_state == "ready")
            if ready:
                shown = _shown(obs)
                if shown == last:
                    return "still"
                if looks == _STILL_LOOKS:
                    return "moving"
                looks, last = looks + 1, shown
            else:
                looks, last = 0, None
            if s.page.execute("wait_for", {"ms": _LOOK_MS if ready else _PENDING_MS}).state != "completed":
                return "gone"
        return "waiting"

    def _element(self, s, index):
        """The element the model named, if the page text it was last shown listed it."""
        number = str(int(index))
        lines, _hints = _page_lines(s.obs)
        shown = {m.group(2) for line in lines if (m := _DEPTH.match(line))}
        if number not in shown:
            return None
        return next((e for e in s.obs.elements if e.ref == f"e{number}"), None)

    def _act(self, s, action: str, args: dict, *, navigating: bool = False, complete_query=False) -> LoopToolResult | None:
        self._last_action_receipt = None
        element = (next((e for e in s.obs.elements if e.ref == args.get("ref")), None)
                   if getattr(s, "obs", None) is not None and args.get("ref") else None)
        if element is not None and callable(getattr(s, "audit", None)):
            try:
                s.audit("page_control_identity", action=action, **control_identity(s.obs, element))
            except TypeError:
                pass
        if action == "select":
            if element is not None and element.role == "radiogroup":
                choices = [c for c in observed_radio_choices(element) if c.label == args.get("option_label")]
                if len(choices) != 1:
                    return LoopToolResult("未执行：当前单选组没有唯一可用的选项，请根据最新观察核对。",
                                          kind="page", state="refused", error_code="invalid_target")
                args = {**args, "value": choices[0].value}
        outcome = s.page.execute(action, args, navigating=navigating)
        receipt = getattr(outcome, "receipt", None)
        self._last_action_receipt = receipt
        policy_turn = self._ctx().executor_turn
        if receipt is not None:
            if getattr(receipt, "observation", None) is not None and not isinstance(getattr(type(s), "obs", None), property):
                s.obs = receipt.observation
            if policy_turn is not None:
                if isinstance(getattr(receipt, "observation_summary", None), dict) and receipt.observation_summary:
                    policy_turn["last_observation_summary"] = dict(receipt.observation_summary)
                if any(getattr(receipt, k, None) for k in ("applied_fields", "rejected_fields", "previous_fields", "restricted_fields")):
                    _merge_field_outcomes(
                        policy_turn,
                        applied=getattr(receipt, "applied_fields", None),
                        rejected=getattr(receipt, "rejected_fields", None),
                        previous=getattr(receipt, "previous_fields", None),
                        restricted=getattr(receipt, "restricted_fields", ()),
                    )
        if outcome.state == "completed":
            if complete_query:
                summary_note = _format_observation_summary(
                    getattr(receipt, "observation_summary", None),
                    getattr(getattr(self, "knowledge", None), "handoffs", None),
                )
                msg = "查询动作已核验。" + (f"\n{summary_note}" if summary_note else "")
                return LoopToolResult(msg, kind="page", dispatch=outcome.dispatch,
                                      completion_command=outcome.receipt.command_id)
            return None
        err_code = outcome.error_code or (receipt.error_code if receipt is not None else "")
        reasons = {
            "unreviewed": "本部署未开放这个页面动作（应用尚未审核它的效果）；同一动作不会执行，"
                          "不要重复点击同一控件。这不代表账号无权限。要打开某条记录的详情，"
                          "用 find_record 取得它的详情页地址后 goto 打开；没有详情页时，筛选到唯一一行即可。"
                          "已读到的接口和页面结果不受影响，它们能回答用户问题时直接作答。",
            "invalid_target": "当前页面状态或目标地址未通过校验；不能据此断定菜单未审核或账号无权限。"
                              "已读到的结果不受影响；它们能回答用户问题时直接作答。",
            "stale_binding": "页面已变化，请根据最新观察重新选择。",
            "permission_denied": "当前账号没有执行这项操作的权限。",
            "expired": "本次动作授权已过期。",
            "form_closed_or_readonly": "目标表单或弹窗已关闭、分离或为只读状态，请勿编造值或用旧控件序号重试；"
                                       "请先核对当前页面观察，再决定是否重新打开表单或如实说明。",
        }
        feedback = ("未执行：" if outcome.dispatch == "not_dispatched" else
                    "操作已发出但结果未确认，不会自动重发：") + reasons.get(err_code, "请根据错误码和最新观察决定下一步。")
        if outcome.state == "refused" and err_code == "option_unavailable":
            labels = receipt.candidate_labels if receipt is not None else ()
            feedback = ("未选择：应用已核验这个下拉操作没有改变表单。请从它的选项中核对精确名称；保留空格，不会自动替换。\n"
                        + fenced(redacted("\n".join(labels), self.knowledge.handoffs)))
        elif receipt is not None and receipt.error_code == "option_unavailable":
            feedback = ("下拉框没有请求的精确选项，且未能确认当前表单完全保持原样；"
                        "不会自动重发，请手工核对当前草稿。")
        terminal = (bool(getattr(s, "terminal_reason", ""))
                    and outcome.state in {"refused", "unverified"}
                    and (err_code == "option_unavailable" or receipt is not None
                         and receipt.error_code == "option_unavailable"))
        return LoopToolResult(feedback, kind="page", state=outcome.state,
                              dispatch=outcome.dispatch, error_code=err_code,
                              terminal_answer=(feedback[:3500] + " 后续操作已停止，没有点击提交按钮，请核对当前草稿。") if terminal else "")

    def _choice_label(self, s, element, value):
        resolve = getattr(getattr(getattr(self, "goal_port", None), "data", None), "choice_label", None)
        if element.role == "combobox" and callable(resolve):
            try:
                value = resolve(urlsplit(s.obs.url).path, _control_name(element), value)
            except DataRefused:
                pass
        return value

    def _select(self, s, element, value):
        """Select directly; the fixed Runtime owns the exact-label choice process."""
        value = self._choice_label(s, element, value)
        attrs = element.attrs
        if (element.role == "combobox" and attrs.get("aria-haspopup") == "listbox"
                and str(attrs.get("form_scope", "")).startswith("dialog")
                and attrs.get("selected_option") != value):
            owner = attrs.get("aria-controls", "")
            if not owner or not attrs.get("form_instance"):
                return LoopToolResult("尚未选择：下拉框缺少当前表单或选项归属，请重新观察。",
                                      kind="page", state="refused", error_code="invalid_target")
        return self._act(s, "select", {"ref": element.ref, "value": value, "option_label": value})

    @staticmethod
    def _note(s, **facts):
        s.progress(facts)

    def _tool(self, s, name: str, args: dict, turn: dict) -> LoopToolResult:
        know = self.knowledge
        ctx = self._ctx()
        policy_turn = ctx.executor_turn
        table_context = ctx.turn_tables
        if name in _TABLE_TOOLS and table_context is not None:
            store, today, zone = table_context
            try:
                if name == "tables":
                    data = store.describe()
                elif name == "sql":
                    data = store.query(args["query"])
                else:
                    data = date_range(today=today, zone=zone, **args)
                    if policy_turn is not None:
                        for value in _date_range_receipt_values(data):
                            note_receipt(policy_turn, "", value)
                body = fenced(json.dumps(data, ensure_ascii=False))
                if name == "sql" and isinstance(data, dict) and data.get("table_ref"):
                    if data.get("returned_rows", 0) >= 20 or data.get("truncated"):
                        body += f"\n本结果集较大，可用 finish(..., table_ref=\"{data['table_ref']}\") 原样交付完整表格。"
                return LoopToolResult(body, kind="read")
            except TurnReadError as exc:
                return LoopToolResult(str(exc), kind="read", state="refused", error_code=exc.code)
        review_completion = False
        said = {key: value for key, value in args.items() if isinstance(value, (str, int, float))
                and not isinstance(value, bool)}
        if name == "search_menu":
            self._note(s, stage="search", what="page", term=args["keyword"], tool=name, arguments=said)
            text = know.search_menu(args["keyword"])
            matches = know.menu_matches(args["keyword"])
            targets = ()
            if len(matches) == 1:
                match = matches[0]
                targets = (AnswerTarget("page", match["path"], match["leaf"],
                                        "menu:" + match["path"]),)
            review = getattr(self.goal_port, "review", None)
            if review is not None:
                for line in text.splitlines():
                    path = line.split(" ", 1)[0]
                    entries = sorted({control for family, route, _, _, controls in review.scopes
                        if family == "form_entry" and re.fullmatch(
                            re.escape(route).replace(r"\{id\}", r"[1-9][0-9]*"), path)
                        for control in controls})
                    if entries:
                        text += f"\n{path} 已审核表单入口：" + "、".join(entries) + "（goto 的 open；仍核对当前唯一可见入口）"
            return LoopToolResult(text, answer_targets=targets)
        if name == "search_api":
            self._note(s, stage="search", what="source", term=args["keyword"], tool=name, arguments=said)
            return LoopToolResult(know.search_api(args["keyword"]))
        if name == "entity_rules":
            self._note(s, stage="search", what="rules", term=args["target"], tool=name, arguments=said)
            return LoopToolResult(self._entity_rules(s, args["target"], args.get("operation", "")), kind="read")
        if name == "load_domain_guide":
            from ..rules.skill_packages import domain_playbooks
            domain = str(args.get("domain") or "").strip()
            self._note(s, stage="search", what="guide", term=domain, tool=name, arguments=said)
            guide = domain_playbooks().get(domain)
            if not guide:
                return LoopToolResult(f"未知业务域：{domain}", kind="read", state="refused", error_code="unknown_domain")
            return LoopToolResult(guide, kind="read")
        if name == "find_record":
            self._note(s, stage="search", what="record", term=args["name"], tool=name, arguments=said)
            targets: list[AnswerTarget] = []
            found: list = []
            try:
                text = self._find_record(s, args["name"].strip(), args.get("kind", "").strip(),
                                         answer_targets=targets, found_values=found)
            except QueryParametersError as exc:
                return LoopToolResult("查找未完成：" + str(exc) + "。用 search_api 核对接口，再用 call_api 读取。",
                                      kind="read", state="refused", error_code="invalid_arguments")
            if policy_turn is not None:
                note_candidates(policy_turn, (("", value) for value in found))
            return LoopToolResult(text, kind="read", answer_targets=tuple(targets))
        if name == "call_api":
            self._note(s, stage="tool", tool=name, arguments=said)
            params = json.loads(args.get("params", "{}"))
            if not isinstance(params, dict):
                raise ValueError("query parameters must be an object")
            called_path = "/admin-api/" + args["path"].removeprefix("/admin-api/").lstrip("/")
            req_filters = {key: value for key, value in params.items() if key not in ("pageNo", "pageSize")}
            if is_verified_empty_filter(ctx, policy_turn, route=called_path, active_view="", filter_pairs=req_filters):
                self._emit_check(s, "verified_empty_filter_repeated")
                return LoopToolResult(
                    "未执行：同一接口与筛选条件在本轮已核验为空结果（total=0）；请勿重复相同查询，请更换条件或如实说明查无记录。",
                    kind="read", state="refused", error_code="verified_empty_filter_repeated",
                )
            with s.measure("business"):
                try:
                    sink = {"row_sink": table_context[0].add} if table_context is not None else {}
                    text = know.call_api(args["path"], params, args.get("fields", ()),
                                         **sink,
                                         row_offset=args.get("row_offset", 0),
                                         on_page=lambda page_no: self._publish_read_page(s, page_no),
                                         **({"summary": args["summary"]} if "summary" in args else {}),
                                         **({"actor_digest": s.operation.principal.actor_authority_digest}
                                            if "summary" in args else {}))
                except TurnReadError as exc:
                    return LoopToolResult(str(exc), kind="read", state="unverified", error_code=exc.code)
                except SummaryRefused as exc:
                    return LoopToolResult("程序汇总未能核实：" + exc.detail,
                        kind="read", state="unverified", error_code=exc.code)
                except QueryParametersError as exc:
                    return LoopToolResult("未执行：" + str(exc), kind="read", state="refused",
                                          error_code="invalid_arguments")
                except DataRefused as exc:
                    if not exc.permission_denied:
                        raise
                    s.audit("yudao_api_read_refused", path=called_path, reason="permission_denied")
                    return LoopToolResult(
                        "此只读接口明确拒绝了当前账号。这次拒绝只说明该接口本次不可读，"
                        "不影响其它已读到的结果；不能据此断定其它接口、整类数据或整个问题都无权限。"
                        "已有的成功读取能回答用户问题时照常作答，只说明这个接口未能读取；"
                        "还需要补充证据时，依据已发布的只读接口和当前账号范围寻找。",
                        kind="read", state="refused", error_code="permission_denied")
            if know.last_read:
                if getattr(know, "last_is_list", False) and (
                        know.last_read.get("declared_total") == 0 or know.last_read.get("count") == 0):
                    record_verified_empty_filter(
                        ctx, policy_turn,
                        route=called_path, active_view="",
                        filter_pairs=getattr(know, "last_filters", req_filters),
                    )
                def valid_key(key):
                    return isinstance(key, str) and bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", key))
                published_fields = know._row_schema(called_path)
                paging = {key: int(params[key]) for key in ("pageNo", "pageSize")
                          if key in params and re.fullmatch(r"[1-9][0-9]{0,5}", str(params[key]))}
                s.audit("yudao_api_read", path=called_path,
                                query_keys=sorted(key for key in params if valid_key(key))[:32],
                                paging=paging,
                                field_keys=sorted({key for key in args.get("fields") or ()
                                                   if valid_key(key) and key in published_fields})[:12],
                                read_count=know.last_read["count"],
                                declared_total=know.last_read["declared_total"],
                                complete=know.last_read["complete"])
            review = getattr(self.goal_port, "review", None)
            if know.last_read and review and s.obs and s.obs.page_read is not None:
                route = urlsplit(s.obs.url).path
                observed_path = s.obs.page_read.get("path")
                if s.obs.page_route == route and observed_path == called_path:
                    labels = review.labels_on(route)
                    named = [f"{key}→{labels[key]}" for key in know.last_keys if key in labels][:30]
                    if named:
                        text += "\n本页表格列名（接口字段→页面列）：" + "、".join(named)
            if know.last_read and policy_turn is not None:
                note_candidates(policy_turn, know.last_read.get("values") or ())
            turn["reads"].append((json.dumps(args, ensure_ascii=False)[:160], text[:1500]))
            if know.last_read:
                self._note(s, stage="read_done", tool=name,
                           **{k: v for k, v in know.last_read.items() if k != "values"})
            return LoopToolResult(text, kind="read", state="completed" if know.last_read else "refused",
                                  query_answer=getattr(know, "last_query_answer", None)
                                  if "summary" in args else None,
                                  read_key=called_path if know.last_read else "",
                                  read_count=know.last_read["count"] if know.last_read else None)
        if name == "fill_fields":
            return self._fill_fields(s, args["fields"], args.get("then_click", ""),
                                     turn, then_highlight=args.get("then_highlight", ""),
                                     add_lines=args.get("add_lines"))
        if name == "observe":
            outcome = s.page.execute("observe", {})
            return LoopToolResult("观察如下。", kind="page",
                                  state=outcome.state, dispatch=outcome.dispatch, error_code=outcome.error_code)
        if name == "goto":
            path = "/" + args["path"].lstrip("/")
            if not re.fullmatch(r"/[A-Za-z0-9_\-/]{1,200}", path):
                return LoopToolResult("路径不合法。用 search_menu 查找。", kind="page", state="refused")
            failed = self._act(s, "goto", {"url": self.application.descriptor.origin + path}, navigating=True)
            if failed:
                return failed
            goto_receipt = getattr(self, "_last_action_receipt", None)
            if _is_settled_action_receipt("goto", goto_receipt, expected_path=path):
                settled = "still"
            else:
                settled = self._settled(s, path=path)
            there = urlsplit(s.obs.url).path
            if there != path:
                return LoopToolResult(f"没有成功：浏览器现在在 {there}，没有到 {path}。", kind="page", state="unverified")
            if settled == "blocked":
                return LoopToolResult(f"浏览器到了 {path}，但页面报告了错误或有数据没有读出来：看下面的页面再决定。",
                                      kind="page", state="unverified", dispatch="acknowledged")
            if settled not in ("still", "moving"):
                return LoopToolResult(f"浏览器到了 {path}，但应用还没有报告这个页面已经显示出来：看下面的页面再决定。",
                                      kind="page", state="unverified", dispatch="acknowledged")
            access = self._record_access(s, path)
            handoffs = getattr(getattr(self, "knowledge", None), "handoffs", None)
            summary_note = _format_observation_summary(getattr(goto_receipt, "observation_summary", None), handoffs)
            opening = str(args.get("open", "")).strip()
            if opening:
                lines, _hints = _page_lines(s.obs)
                shown = {m.group(2) for line in lines if (m := _DEPTH.match(line))}
                wanted = re.sub(r"\s+", "", opening)
                review = getattr(self.goal_port, "review", None)
                buttons = [e for e in s.obs.elements if e.ref[1:] in shown and e.tag == "button"
                           and re.sub(r"\s+", "", e.name) == wanted and review is not None
                           and review.permits("form_entry", path, e.name.strip())]
                if len(buttons) != 1:
                    return LoopToolResult(f"没有找到唯一的“{opening}”表单入口，没有打开。", kind="page",
                                          state="refused", error_code="invalid_target")
                failed = self._act(s, "click", {"ref": buttons[0].ref})
                if failed:
                    return failed
                open_receipt = getattr(self, "_last_action_receipt", None)
                if not _is_settled_action_receipt("click", open_receipt):
                    self._settled(s)
                open_note = _format_observation_summary(getattr(open_receipt, "observation_summary", None), handoffs)
                msg = "已打开。" + access + f" 已进入“{opening}”表单。" + (f"\n{open_note}" if open_note else "")
                return LoopToolResult(msg, kind="page", dispatch="acknowledged")
            msg = "已打开。" + access + (f"\n{summary_note}" if summary_note else "")
            return LoopToolResult(msg, kind="page", dispatch="acknowledged")
        if name in ("click", "input", "select", "highlight") or (name == "scroll" and args.get("index") is not None):
            element = self._element(s, args.get("index", -1))
            if element is None:
                return LoopToolResult("没有这个序号的元素，请根据最新观察重新选择。", kind="page", state="refused")
        if name == "input" and _control_kind(element) == "〔下拉〕" and args["text"].strip():
            name, args = "select", {**args, "option": args["text"]}
        if name == "click":
            if (element.role == "tab" and s.obs.record_route == urlsplit(s.obs.url).path
                    and not self._detail_handoff_target(s, ctx.current_request)):
                review_completion = True
            btn_plain = plain(element.name)
            is_query_btn = element.attrs.get("page_control") == "query" or btn_plain in {"搜索", "查询", "重置"}
            route = s.obs.page_route or urlsplit(s.obs.url).path
            tab = active_view_or_tab(s.obs)
            if is_query_btn and btn_plain in {"搜索", "查询"}:
                cur_filters = _current_page_query_filters(s.obs)
                if is_verified_empty_filter(ctx, policy_turn, route=route, active_view=tab, filter_pairs=cur_filters):
                    self._emit_check(s, "verified_empty_filter_repeated")
                    return LoopToolResult(
                        "未执行：当前筛选条件在本轮已核验为空结果（0条）；请勿重复相同查询，请更换条件或如实说明查无记录。",
                        kind="page", state="refused", error_code="verified_empty_filter_repeated",
                    )
            elif is_query_btn and btn_plain == "重置":
                cur_filters = _current_page_query_filters(s.obs)
                if not cur_filters and is_verified_empty_filter(ctx, policy_turn, route=route, active_view=tab, filter_pairs=()):
                    self._emit_check(s, "verified_empty_filter_repeated")
                    return LoopToolResult(
                        "未执行：当前无筛选条件的结果在本轮已核验为空（0条）；请勿重复重置查询。",
                        kind="page", state="refused", error_code="verified_empty_filter_repeated",
                    )
            tree_query = element.role == "treeitem" and element.attrs.get("page_control") == "query"
            failed = self._act(s, "click", {"ref": element.ref}, complete_query=tree_query)
            if tree_query and failed is not None and failed.state == "completed":
                return failed
            click_receipt = getattr(self, "_last_action_receipt", None)
            if not failed:
                if not _is_settled_action_receipt("click", click_receipt):
                    self._settled(s)
                if is_query_btn and getattr(s.obs, "page_read", None) is not None:
                    read_info = s.obs.page_read
                    if read_info.get("total") == 0 or read_info.get("rows") == 0:
                        record_verified_empty_filter(
                            ctx, policy_turn,
                            route=s.obs.page_route or urlsplit(s.obs.url).path,
                            active_view=active_view_or_tab(s.obs),
                            filter_pairs=_current_page_query_filters(s.obs),
                        )
            text = ("已点开下拉。要选其中一项，用 select(index, option) 按选项文字选："
                    "展开的列表只显示了一部分，在里面点容易点错。") if _control_kind(element) == "〔下拉〕" else "已点击。"
            if not failed:
                handoffs = getattr(getattr(self, "knowledge", None), "handoffs", None)
                summary_note = _format_observation_summary(getattr(click_receipt, "observation_summary", None), handoffs)
                if summary_note:
                    text += "\n" + summary_note
        elif name in ("input", "select"):
            value = args["text"] if name == "input" else args["option"]
            intent = ctx.write_intent
            scope = str(element.attrs.get("form_scope") or "")
            label = _control_name(element)
            restricted_kind = _is_restricted_control(element)
            if restricted_kind:
                if policy_turn is not None:
                    _merge_field_outcomes(policy_turn, restricted=(label,))
                return LoopToolResult(
                    f"「{label}」属于受限控件（{restricted_kind}），不能由助手自动填入，请留给用户手动输入。",
                    kind="page", state="refused", error_code="invalid_target",
                )
            if (isinstance(intent, WriteIntent) and intent.mode == "draft"
                    and _business_scope(scope)
                    and element.attrs.get("page_control") != "query"):
                unrequested = _unrequested_draft_fields(
                    [{"label": label, "value": value}],
                    ctx.current_request, ctx.earlier_requests,
                    other_labels=[str(e.attrs.get("field_label") or "") for e in s.obs.elements
                                  if e.attrs.get("form_scope") == scope])
                if unrequested:
                    return LoopToolResult("这个表单字段没有得到用户请求支持，未填写："
                                          + "、".join(unrequested) + "。请只填用户指定的字段。",
                                          kind="page", state="refused",
                                          error_code="unrequested_draft_field")
            if name == "input":
                value = _native_input_text(element, value)
                command = {"ref": element.ref, "value": value, "commit": _commits(element, s.obs.elements)}
            else:
                value = self._choice_label(s, element, value)
                command = {"ref": element.ref, "value": value, "option_label": value}
            if _unavailable_already_matches(element, value):
                return LoopToolResult(
                    f"「{label}」当前已是「{value}」（只读/禁用字段，保持原值）。",
                    kind="page", state="completed", dispatch="not_dispatched")
            previous_value = str(
                element.attrs.get("selected_option")
                if element.attrs.get("selected_option") is not None
                else element.attrs.get("value") or ""
            )
            failed = (self._act(s, "fill", command) if name == "input"
                      else self._select(s, element, value))
            act_receipt = getattr(self, "_last_action_receipt", None)
            if not failed:
                turn["touched"].append((label, value))
                if policy_turn is not None:
                    _merge_field_outcomes(
                        policy_turn,
                        applied={label: value},
                        previous={label: previous_value},
                    )
                if not (name == "select" and _is_settled_action_receipt("select", act_receipt)):
                    self._settled(s, waits=2)
            elif policy_turn is not None:
                _merge_field_outcomes(
                    policy_turn,
                    rejected={label: failed.error_code or "rejected"},
                    previous={label: previous_value},
                )
            text = "已填入。" if name == "input" else "已选中。"
            if not failed:
                handoffs = getattr(getattr(self, "knowledge", None), "handoffs", None)
                summary_note = _format_observation_summary(getattr(act_receipt, "observation_summary", None), handoffs)
                if summary_note:
                    text += "\n" + summary_note
        elif name == "scroll":
            command = {"direction": "down" if args["down"] else "up", "pages": 1}
            if args.get("index") is not None:
                command["ref"] = "e" + _scroll_anchor(s.obs, element.ref[1:])
            before = _shown(s.obs)
            failed = self._act(s, "scroll", command)
            text = ("滚动了，但屏幕上的内容没有变化：这个方向已经到头，或者该换一个区域滚动。"
                    if _shown(s.obs) == before else "已滚动。")
        elif name == "highlight":
            if policy_turn is None:
                return LoopToolResult("程序没有本轮状态可核对，不能标出控件。",
                                      kind="page", state="refused", error_code="turn_state_missing")
            blocked_form = unrestorable_form(policy_turn.get("unrestorable_forms"),
                                             element.attrs.get("form_scope"),
                                             (element.attrs.get("form_lifetime"),
                                              element.attrs.get("form_instance")))
            if blocked_form is not None and plain(element.name) in SUBMIT_WORDS:
                return opening_changes_refusal(blocked_form)
            blocked_code = policy_turn.get("submit_handoff_blocked")
            if blocked_code and plain(element.name) in SUBMIT_WORDS and element.attrs.get("form_scope"):
                return LoopToolResult("不能标出提交按钮：本轮这项提交的请求已被拒绝，"
                                      "按反馈处理后再请求提交。", kind="page",
                                      state="refused", error_code=blocked_code)
            request = ctx.current_request
            needs_target = _record_handoff_control(element)
            detail_target = (self._detail_handoff_target(s, request)
                             if needs_target and s.obs.record_route == urlsplit(s.obs.url).path else False)
            status_target = element.role == "switch" and self._user_status_target(s, element, request)
            list_target = (self._list_handoff_target(s, element, request)
                           if needs_target and not detail_target and not status_target else False)
            if not _business_handoff_ready(s.obs, element, request, detail_target=detail_target,
                                           status_target=status_target, list_target=list_target):
                return LoopToolResult("未标出：业务按钮须在已核对用户目标的记录详情页，或先把列表筛选到已核验的唯一一行。"
                                      "请核对目标记录和按钮归属；不能在多行列表上直接交接。",
                                      kind="page", state="refused", error_code="invalid_target")
            failed = self._act(s, "highlight", {"ref": element.ref, "epoch": s.obs.epoch})
            if not failed:
                turn["marked"] = element.name.splitlines()[0][:60] if element.name else "(控件)"
                s.audit("page_handoff_ready", observation_epoch=s.obs.epoch,
                                submitted=False, persisted=False)
            text = "已标出：" + turn["marked"]
        else:
            return LoopToolResult("当前应用没有提供这个工具。", state="unavailable")
        return failed or LoopToolResult(text, kind="page", dispatch="acknowledged",
                                       review_completion=review_completion)

    def _paging(self, s, control: str, label: str, value: str) -> LoopToolResult:
        """The list's rows per page or its page, through the paging control."""
        number = value.strip().rstrip("条页").strip()
        if not number.isdigit() or int(number) < 1:
            return LoopToolResult(f"{label}：值写一个数字", kind="page", state="unverified")

        def control_of(kind, page=""):
            return next((e for e in s.obs.elements if e.attrs.get("page_control") == kind
                         and (not page or e.attrs.get("page_number") == page)), None)

        kind, page = ("page_size", "") if control == "page_size" else ("pagination", str(int(number)))
        if control_of(kind, page) is None and control_of("page_size") is None and control_of("pagination") is None:
            region = _list_scroll_region(s.obs)
            command = {"direction": "down", "pages": MAX_SCROLL_PAGES}
            if region is not None:
                command["ref"] = "e" + _scroll_anchor(s.obs, region)
            if not self._act(s, "scroll", command):
                self._settled(s, waits=2)
        if control == "page_size":
            found = control_of("page_size")
            if found is None:
                return LoopToolResult(f"{label}：页面上没有每页条数的选择（先 scroll 到列表底部的分页）", kind="page", state="unverified")
            failed = self._act(s, "click", {"ref": found.ref})
            if not failed:
                self._settled(s, waits=2)
                wanted = f"{int(number)}条/页"
                option = next((e for e in s.obs.elements if e.role == "option"
                               and re.sub(r"\s+", "", e.name) == wanted), None)
                failed = (LoopToolResult(f"每页条数的选项里没有“{wanted}”", kind="page",
                                         state="refused", error_code="option_unavailable") if option is None
                          else self._act(s, "click", {"ref": option.ref}, complete_query=True))
        else:
            found = control_of("pagination", page)
            if found is None:
                return LoopToolResult(f"{label}：页面上没有翻到第 {int(number)} 页的按钮（已经在这一页、页码超出，或先 scroll 到列表底部的分页）", kind="page", state="unverified")
            failed = self._act(s, "click", {"ref": found.ref}, complete_query=True)
        completion = failed.completion_command if failed and failed.state == "completed" else ""
        if failed and failed.state != "completed":
            return replace(failed, feedback=f"{label}：{failed.feedback}")
        self._settled(s)
        read = s.obs.page_read or {}
        got = read.get("page_size" if control == "page_size" else "page")
        if str(got) == str(int(number)):
            return LoopToolResult(f"{label}：已改为{'每页 ' + number + ' 条' if control == 'page_size' else '第 ' + number + ' 页'}（列表读回：第 {read.get('page')} 页，每页 {read.get('page_size')} 条，共 {read.get('total')} 条）", kind="page", state="completed", completion_command=completion)
        return LoopToolResult(f"{label}：已操作，但列表读回的不是 {number}（{got if got is not None else '没有读回'}），看下面的页面再决定", kind="page", state="unverified")

    def _fill_fields(self, s, fields, then_click: str, turn: dict, *, then_highlight="", add_lines=None,
                     query_only=False, locate=None, choose=None) -> LoopToolResult:
        """Fill or choose several named fields one after another, each found afresh."""
        ctx = self._ctx()
        policy_turn = ctx.executor_turn
        if not 1 <= len(fields) <= _FORM_BATCH_FIELDS:
            return LoopToolResult("字段批次数量超出工具上限，整批未执行；请分批填写，最后再交接。",
                                  kind="page", state="refused", error_code="invalid_arguments")
        if then_click and then_highlight:
            return LoopToolResult("筛选和表单交接不能同时执行。", kind="page", state="refused",
                                  error_code="invalid_arguments")
        query_filter_pairs = ()
        is_query_submit = bool(query_only or plain(then_click) in {"搜索", "查询"})
        if is_query_submit:
            query_filter_pairs = _current_page_query_filters(s.obs, fields)
            route = getattr(s.obs, "page_route", "") or urlsplit(getattr(s.obs, "url", "") or "").path
            tab = active_view_or_tab(s.obs)
            if is_verified_empty_filter(ctx, policy_turn, route=route, active_view=tab, filter_pairs=query_filter_pairs):
                self._emit_check(s, "verified_empty_filter_repeated")
                return LoopToolResult(
                    "未执行：同一页面筛选条件在本轮已核验为空结果（0条）；请勿重复相同筛选或重置后重查，请更换条件或如实说明查无记录。",
                    kind="page", state="refused", error_code="verified_empty_filter_repeated",
                )
        intent = ctx.write_intent
        form_batch = bool(then_highlight or add_lines) or any(
            _business_scope(e.attrs.get("form_scope"))
            and e.attrs.get("page_control") != "query"
            and e.attrs.get("field_label") in {item.get("label") for item in fields}
            for e in getattr(s.obs, "elements", ()))
        if isinstance(intent, WriteIntent) and intent.mode == "draft" and not then_click and form_batch:
            unrequested = _unrequested_draft_fields(
                fields, ctx.current_request, ctx.earlier_requests,
                other_labels=[str(e.attrs.get("field_label") or "") for e in s.obs.elements
                              if _business_scope(e.attrs.get("form_scope"))
                              and e.attrs.get("page_control") != "query"])
            if unrequested:
                return LoopToolResult(
                    "这些表单字段没有得到用户请求支持，整批未填写：" + "、".join(unrequested)
                    + "。只填写用户明确指定的字段和值；需要复制其它字段时请先询问用户。",
                    kind="page", state="refused", error_code="unrequested_draft_field")
        if add_lines:
            failed = add_draft_lines(s, add_lines["button"], add_lines["count"])
            if failed:
                return failed
        before, expected = s.obs, []
        said, incomplete, failure = [], False, None
        paging_commands, paging_targets = [], []
        batch_applied, batch_rejected, batch_previous, batch_restricted = {}, {}, {}, []

        def _visible_named(target_label: str):
            vis_lines, _ = _page_lines(s.obs)
            vis_shown = {m.group(2) for line in vis_lines if (m := _DEPTH.match(line))}
            matches = [
                e for e in s.obs.elements
                if e.ref[1:] in vis_shown and target_label and target_label in (
                    str(e.attrs.get("field_label") or "").rstrip(":：").strip(),
                    str(e.attrs.get("title") or ""),
                ) and (not query_only or e.attrs.get("page_control") == "query")
            ]
            return vis_shown, matches

        for field_idx, item in enumerate(fields):
            label, value = str(item.get("label", "")).strip().rstrip(":：").strip(), str(item.get("value", ""))
            if label in _PAGING:
                paging = self._paging(s, _PAGING[label], label, value)
                said.append(paging.feedback)
                incomplete = incomplete or paging.state != "completed"
                if paging.state != "completed":
                    failure = failure or paging
                else:
                    paging_targets.append(("page_size" if _PAGING[label] == "page_size" else "page",
                                           str(int(value.strip().rstrip("条页").strip()))))
                paging_commands.append(paging.completion_command)
                if s.terminal_reason:
                    return LoopToolResult("操作没有完成。", kind="page", state="unverified")
                continue
            if locate is not None:
                missing = locate(s, label)
                if missing:
                    return missing
            shown, found = _visible_named(label)
            if not found:
                incomplete = True
                batch_rejected[label] = "missing_control"
                said.append(f"{label}：屏幕上没有这个字段（先 scroll 让它出现，或核对字段名）")
                continue
            kind = _control_kind(found[0])
            anchor = found[0]
            restricted_kind = _is_restricted_control(anchor)
            if restricted_kind:
                incomplete = True
                if label not in batch_restricted:
                    batch_restricted.append(label)
                batch_rejected[label] = f"restricted_{restricted_kind}"
                said.append(f"{label}：属于受限控件（{restricted_kind}），不能由助手自动填入，请由用户手动填写")
                continue
            pair = _date_pair(anchor, s.obs.elements)
            if kind == "〔日期〕" and (pair or re.search(r"[~～至到]", value)):
                if not pair or any(e.ref[1:] not in shown for e in pair) or any(e not in pair for e in found):
                    incomplete = True
                    batch_rejected[label] = "missing_date_range"
                    said.append(f"{label}：屏幕上没有唯一的日期范围两个框（先 scroll 让它们出现，或核对字段名）")
                    continue
                ends = [part.strip() for part in re.split(r"\s*(?:~|～|至|到)\s*", value)]
                if len(ends) != 2 or not all(ends):
                    incomplete = True
                    batch_rejected[label] = "invalid_date_range"
                    said.append(f"{label}：这是日期范围，值写成“开始 ~ 结束”")
                    continue
                steps = list(zip(pair, ends))
                batch_previous.setdefault(
                    label,
                    " ~ ".join(str(e.attrs.get("value") or "") for e in pair).strip(" ~"),
                )
            else:
                if len(found) != 1:
                    incomplete = True
                    batch_rejected[label] = "ambiguous_control"
                    said.append(f"{label}：屏幕上有多个同名字段，尚未填写")
                    continue
                steps = [(found[0], value)]
                batch_previous.setdefault(
                    label,
                    str(found[0].attrs.get("selected_option")
                        if found[0].attrs.get("selected_option") is not None
                        else found[0].attrs.get("value") or ""),
                )
            failed = ""
            dispatched_any = False
            for at, (element, text) in enumerate(steps):
                if pair:
                    current = _date_pair(anchor, s.obs.elements)
                    lines, _hints = _page_lines(s.obs)
                    visible = {m.group(2) for line in lines if (m := _DEPTH.match(line))}
                    if not current or any(e.ref[1:] not in visible for e in current):
                        failed = LoopToolResult("日期范围控件已变化，请根据最新观察重新填写",
                                                kind="page", state="refused", error_code="stale_binding")
                        break
                    element = current[at]
                text = _native_input_text(element, text)
                if kind in ("〔下拉〕", "〔单选〕") and text.strip():
                    text = self._choice_label(s, element, text)
                    if _unavailable_already_matches(element, text):
                        continue
                    failed = (choose or self._select)(s, element, text)
                else:
                    if _unavailable_already_matches(element, text):
                        continue
                    failed = self._act(s, "fill", {"ref": element.ref, "value": text,
                                                   "commit": _commits(element, s.obs.elements)})
                if failed or s.terminal_reason:
                    break
                dispatched_any = True
                expected.append((field_identity(element), text))
                coalesce_plain = False
                if kind == "" and not pair and field_idx + 1 < len(fields):
                    nxt_label = str(fields[field_idx + 1].get("label", "")).strip().rstrip(":：").strip()
                    if nxt_label and nxt_label not in _PAGING:
                        _, nxt_found = _visible_named(nxt_label)
                        if (len(nxt_found) == 1 and _control_kind(nxt_found[0]) == ""
                                and not _date_pair(nxt_found[0], s.obs.elements)):
                            coalesce_plain = True
                if not coalesce_plain:
                    last_rcpt = getattr(self, "_last_action_receipt", None)
                    if not (kind in ("〔下拉〕", "〔单选〕") and _is_settled_action_receipt("select", last_rcpt)):
                        self._settled(s, waits=2)
            if s.terminal_reason:
                detail = "；".join(said)
                detail += ("；" if detail else "") + f"{label}：" + (
                    failed.feedback if failed else "本次处理已停止，当前字段结果未确认。")
                detail = detail[:3500] + " 后续字段未继续处理，没有点击提交按钮，请核对当前草稿。"
                return (replace(failed, feedback=detail,
                                terminal_answer=detail if failed.terminal_answer else "") if failed else LoopToolResult(
                    detail, kind="page", state="unverified", dispatch="uncertain", error_code="interrupted"))
            if failed:
                incomplete = True
                failure = failure or failed
                batch_rejected[label] = failed.error_code or "rejected"
                said.append(f"{label}：{failed.feedback}")
                if failed.error_code == "form_closed_or_readonly":
                    break
            else:
                if not dispatched_any:
                    batch_applied[label] = value
                    said.append(f"{label}：已是「{value}」（只读/禁用字段，保持原值）")
                else:
                    batch_applied[label] = value
                    turn["touched"].append((label, value))
                    turn["trace"].append(f"fill_fields[{label}]({json.dumps(value, ensure_ascii=False)[:60]})")
                    said.append(f"{label}：已{'选' if kind in ('〔下拉〕', '〔单选〕') else '填'}")
        _merge_field_outcomes(
            turn,
            applied=batch_applied,
            rejected=batch_rejected,
            previous=batch_previous,
            restricted=batch_restricted,
        )
        if policy_turn is not None:
            _merge_field_outcomes(
                policy_turn,
                applied=batch_applied,
                rejected=batch_rejected,
                previous=batch_previous,
                restricted=batch_restricted,
            )
        if failure is not None and failure.error_code == "form_closed_or_readonly":
            return replace(failure, feedback="；".join(said) + "。")
        wanted = re.sub(r"\s+", "", then_click or "")
        if wanted and not incomplete and not failure:
            if locate is not None:
                missing = locate(s, then_click, button=True)
                if missing:
                    return missing
            lines, _hints = _page_lines(s.obs)
            shown = {m.group(2) for line in lines if (m := _DEPTH.match(line))}
            buttons = [e for e in s.obs.elements if e.ref[1:] in shown and e.tag == "button"
                       and re.sub(r"\s+", "", e.name) == wanted
                       and (not query_only or e.attrs.get("page_control") == "query")]
            if len(buttons) != 1:
                incomplete = True
                said.append(f"没有找到“{then_click}”按钮，没有点")
            else:
                result = self._act(s, "click", {"ref": buttons[0].ref}, complete_query=True)
                if result.completion_command:
                    read_info = getattr(s.obs, "page_read", None)
                    if is_query_submit and isinstance(read_info, dict) and (
                            read_info.get("total") == 0 or read_info.get("rows") == 0):
                        record_verified_empty_filter(
                            ctx, policy_turn,
                            route=getattr(s.obs, "page_route", "") or urlsplit(getattr(s.obs, "url", "") or "").path,
                            active_view=active_view_or_tab(s.obs),
                            filter_pairs=query_filter_pairs,
                        )
                    return replace(result, feedback="；".join(said) + "；" + result.feedback)
                failed = result
                incomplete = incomplete or bool(failed)
                failure = failure or failed
                said.append(failed.feedback if failed else f"已点“{then_click}”")
        text = "；".join(said) + "。"
        if form_batch and not query_only and expected and not then_click and not incomplete and not failure:
            self.form_fill = {"before": before, "expected": list(expected)}
        if then_highlight and not then_click and not incomplete and not failure:
            if policy_turn is None:
                return LoopToolResult(text + "程序没有本轮状态可核对，不标出提交按钮。",
                                      kind="page", state="refused", error_code="turn_state_missing")
            if policy_turn.get("submit_handoff_blocked"):
                return LoopToolResult(text + "提交已被拒绝，不标出提交按钮。", kind="page",
                                      state="refused", error_code=str(policy_turn["submit_handoff_blocked"]))
            from ..writes.draft_readback import product_price_readback
            expected = product_price_readback(s, before, expected, self.goal_port.data)
            result = handoff_draft(s, before, expected, then_highlight,
                                   blocked=policy_turn.get("unrestorable_forms"))
            if result.marked:
                turn["marked"] = result.marked
                handoff_facts = build_handoff_facts(policy_turn or turn, s.obs)
                if handoff_facts is not None:
                    turn["handoff_facts"] = handoff_facts
                    policy_turn["handoff_facts"] = handoff_facts
            return replace(result, feedback=text + result.feedback)
        if (not incomplete and not failure and not then_click and not then_highlight
                and len(paging_commands) == len(fields) and all(paging_commands)):
            read = s.obs.page_read or {}
            if all(str(read.get(key)) == value for key, value in paging_targets):
                return LoopToolResult(text, kind="page", completion_command=paging_commands[-1])
            return LoopToolResult(text + "最后一次分页使前面的要求发生变化，请核对当前页码和每页条数。",
                                  kind="page", state="unverified")
        return (replace(failure, feedback=text) if failure else
                LoopToolResult(text, kind="page", state="unverified" if incomplete else "completed"))

    def _entity_rules(self, s, target: str, operation: str = "") -> str:
        rules_fn = (getattr(self.goal_port, "rules_index", None)
                    or getattr(getattr(self.goal_port, "data", None), "rules_index", None))
        index = rules_fn(context=s.operation) if callable(rules_fn) else self.goal_port.index(context=s.operation)
        query = (target or "").strip().casefold()
        op_filter = (operation or "").strip().casefold()
        if not query:
            return "请提供要查询的业务对象名或接口前缀。"
        scored = []
        for entity in index.entities:
            if not entity.rules:
                continue
            hay = " ".join((entity.key, entity.label, *entity.aliases)).casefold()
            if query == entity.key.casefold() or query == entity.label.casefold():
                score = 0
            elif query in hay or any(query in (r.subject or "").casefold() for r in entity.rules):
                score = 1
            else:
                continue
            scored.append((score, entity))
        if not scored:
            if not any(e.rules for e in index.entities):
                return "当前部署未附带编译期源码规则。"
            return f"未找到与“{target}”匹配的源码规则。"
        scored.sort(key=lambda item: (item[0], item[1].key))
        lines = []
        kind_order = {"refusal": 0, "cascade": 1, "visibility": 2, "editability": 3}
        for _, entity in scored[:3]:
            rules = sorted(entity.rules, key=lambda r: (kind_order.get(r.kind, 9), r.subject, r.about))
            note = ""
            if op_filter:
                filtered = [r for r in rules if op_filter in r.about.casefold()
                            or op_filter in (r.subject or "").casefold()
                            or op_filter in r.text.casefold()]
                if filtered:
                    rules = filtered
                else:
                    note = f"（未命中操作词“{operation.strip()}”，以下展示该对象全部主要规则）"
            lines.append(f"【{entity.label}（{entity.key}）编译期源码规则】{note}")
            for rule in rules[:15]:
                subj = f" [{rule.subject}]" if rule.subject else ""
                lines.append(f"- ({rule.kind}) {rule.about}{subj}: {rule.text}")
        output = "\n".join(lines)
        if len(output) > 12_000:
            output = output[:11_900].rsplit("\n", 1)[0] + "\n…（其余规则已省略，请指定更具体的 target 或 operation 缩小范围）"
        return output

    def _find_record(self, s, name: str, kind: str = "", *,
                     answer_targets: list[AnswerTarget] | None = None,
                     found_values: list | None = None,
                     preview_commits: list | None = None) -> str:
        """Where the application has a record of this name, through the goal port."""
        ctx = self._ctx()
        if not name:
            return "请给出要找的名称。"
        scope_notes = []
        try:
            with s.measure("business"):
                context = s.operation
                index = getattr(self.goal_port, "outline_index", self.goal_port.index)(context=context)
                among = _of_kind(index, kind)
                diagnostic = getattr(self.goal_port, "records_named_diagnostics", None)
                if diagnostic:
                    hits, failures = diagnostic(name, context=context, entities=among)
                else:
                    hits = self.goal_port.records_named(name, context=context, entities=among)
                    failures = ()
                hits = hits[:4]
                scope_notes.extend(f"{entity} 的列表读取" + ("被应用明确拒绝" if denied else "未完成")
                                   for entity, denied, _read_failed in failures)
                near, lists = [], []
                if not hits:
                    for entity in among or tuple(dict.fromkeys(p.entity for p in index.pages if p.entity)):
                        try:
                            candidates = self.knowledge.outside_actor_scope_records(entity, name)
                        except DataRefused as exc:
                            scope_notes.append(f"{entity} 的普通列表读取被应用拒绝" if exc.permission_denied
                                               else f"{entity} 的普通列表读取未完成")
                            continue
                        readable = []
                        for rid, display in candidates:
                            try:
                                row = self.knowledge.client.get("crm/customer/get", id=rid)
                            except DataRefused as exc:
                                scope_notes.append(f"{entity} 的“{display}”（编号 {rid}）详情读取"
                                                   + ("被应用明确拒绝" if exc.permission_denied else "未完成"))
                                continue
                            if isinstance(row, dict) and str(row.get("id")) == rid:
                                readable.append((rid, display, f"/crm/customer/detail/{rid}"))
                            else:
                                scope_notes.append(f"{entity} 的“{display}”（编号 {rid}）详情结果无法核实")
                        if readable:
                            hits += ((entity, tuple(readable)),)
                if not hits and among:
                    for part in _name_parts(name):
                        near += self.goal_port.records_named(part, context=context, entities=among)
                    for entity in among[:3]:
                        try:
                            if preview_commits is not None:
                                preview, preview_prepared = self.knowledge._prepare_list_preview(entity)
                                if preview_prepared is not None:
                                    preview_commits.append(preview_prepared)
                                    if "error" in preview_prepared:
                                        raise preview_prepared["error"]
                            else:
                                preview = self.knowledge.list_preview(entity)
                        except DataRefused as exc:
                            scope_notes.append(f"{entity} 的列表预览" +
                                               ("被应用明确拒绝" if exc.permission_denied else "未完成"))
                            continue
                        if preview:
                            lists.append((entity, preview))
        except DataRefused as exc:
            if exc.permission_denied or exc.read_failed:
                raise
            return f"查找失败：{redacted(str(exc)[:160], self.knowledge.handoffs)}"
        labels = {e.key: e.label for e in index.entities}
        pages = {}
        for page in index.pages:
            if page.entity:
                pages.setdefault(page.entity, page.breadcrumb or page.label)
        if not hits and among:
            if scope_notes:
                return redacted("；".join(scope_notes) + "。不能凭负责范围列表的空结果判断详情权限或记录状态。",
                                self.knowledge.handoffs)
            where = "、".join(labels.get(e, e) for e in among)
            parts = "”“".join(_name_parts(name))
            looked = f"按名称“{name}”" + (f"和它的一部分（“{parts}”）" if parts else "")
            told = [f"在名称里有“{kind}”的模块（{where}）里，{looked}查列表，都没有名称是或包含“{name}”的记录。"]
            if near:
                names = sorted({display for _entity, records in near for _rid, display, _address in records})[:8]
                told += ["按名称的一部分查到的记录，名称与用户说的不同，不是它：", fenced("、".join(names))]
            for entity, text in lists:
                told += [f"{labels.get(entity, entity)} 的列表第一页（看现有的名称用；不用再读）：", text[:2500]]
            if _new_form_draft_request(ctx.current_request):
                told.append("这是已有记录的查找结果，不是新增草稿的可行性判断。用户本轮要先填新资料；"
                            "不要因为没有同名旧记录而拒绝。现在打开对应页面的新增表单，填入用户给出的值并交接提交。")
            else:
                told.append("以上只核对了名称，不证明原生ID或业务编号不存在；编号需用已发布接口的对应参数读取。"
                            "用户给的是确切名称时，如实说明这些模块没有该名称（cannot），不要再换名字查；"
                            "用户没说清是哪一类，它也可能在别的模块里，那样就不带 kind 再查一次。")
            return redacted("\n".join(told), self.knowledge.handoffs)
        if not hits:
            if scope_notes:
                return redacted("；".join(scope_notes) + "。记录的详情状态尚未全部核实。",
                                self.knowledge.handoffs)
            if _new_form_draft_request(ctx.current_request):
                return ("这是已有记录的查找结果；用户本轮要先填新资料，不应因为查不到旧记录而拒绝。"
                        "现在打开对应页面的新增表单，填入用户给出的值并交接提交。")
            return redacted(f"索引已涵盖的模块里，没有名称是（或以它开头的）“{name}”的记录。"
                    "这不是全部应用模块的不存在证明，也未核对原生ID或业务编号；不要缩短名称反复做全局查找。"
                    "下面只是按文字检索的接口候选，不是记录事实。用户给的是确切名称时，选最贴合对象的模块按名称查一次，"
                    "该模块成功完整读取且无匹配后，立即 cannot 并说明查过的范围，不要再换关键词重查、读无条件列表或打开列表来确认。不存在的记录无需打开页面；不要穷举其它模块或点击不同名称的记录。\n"
                    + self.knowledge.search_api(name), self.knowledge.handoffs)
        told, lists, several = [], [], False
        for entity, records in hits:
            if found_values is not None:
                found_values += [value for rid, display, _address in records
                                 for value in (rid, display) if value]
            page = pages.get(entity, "")
            path = self.knowledge.page_path(page) if page else ""
            head = f"{labels.get(entity, entity)}（{entity}；页面：{page or '无'}{' ' + path if path else ''}）"
            if len(records) == 1:
                rid, display, address = records[0]
                told.append(f"{head}：{display}（编号 {rid}）" + (f"；这条记录的详情页 {address}" if _record_page(address, rid, path) else ""))
            else:
                several = True
                told.append(f"{head}：有 {len(records)} 条可能是它（同名或名称相近）：")
                told += [f"    {display}（编号 {rid}" + (f"；详情页 {address}" if _record_page(address, rid, path) else "") + "）"
                         for rid, display, address in records]
            api = self.knowledge.list_api(entity)
            if api and api not in lists:
                lists.append(api)
        out = ["找到的记录（模块；页面：记录名称）：", fenced(redacted("\n".join(told), self.knowledge.handoffs))]
        if several:
            out.append(
                "同一个模块里有几条可能是它：用户要的是其中一条时，不要自己挑，用 need_input"
                "（调用 finish(status='need_input', answer=..., options=[...])，在 options 提供 2 到 4 个候选"
                " {id, label, prompt}，标明名称和编号）让用户选。"
            )
        if lists:
            out += ["这些模块的列表接口：", *("    " + api.replace("\n", "\n    ") for api in lists)]
        if scope_notes:
            out.append("另有未查完的模块：" + "；".join(scope_notes))
        if answer_targets is not None and len(hits) == 1 and len(hits[0][1]) == 1:
            entity, records = hits[0]
            rid, display, address = records[0]
            page = pages.get(entity, "")
            list_path = self.knowledge.page_path(page) if page else ""
            data = getattr(self.goal_port, "data", None)
            if data is not None and _record_page(address, rid, list_path):
                try:
                    bound = self._bound_reads(data, s.operation, s.page.checkpoint)
                    row = bound.read(entity, rid, ())
                    current_address = bound.record_address(entity, rid)
                    if (row.id == rid and row.display and row.display in display
                            and _record_page(current_address, rid, list_path)):
                        answer_targets.append(AnswerTarget("record", current_address, display, f"{entity}:{rid}"))
                except (DataRefused, BusinessToolPermissionError, BusinessToolUnavailableError, ValueError):
                    pass
        return "\n".join(out)

    def verification_messages(self, prompt, answer, turn, obs, history):
        ctx = self._ctx()
        said = re.sub(r"[\s,，。.：:]", "", " ".join(history))
        stray = [f"{label}←{value}" for label, value in turn["touched"]
                 if value and re.sub(r"[\s,，。.：:]", "", value) not in said]
        list_path = _own_list_path(getattr(self, "goal_port", None), obs)
        read, query = _own_list_facts(obs, list_path)
        state = {
            "path": urlsplit(obs.url).path, "title": obs.title,
            "selected": list(obs.selected_controls),
            "list": ({k: read[k] for k in ("page", "page_size", "rows", "total")} if read is not None else None),
            "applied_query": query, "form": _fields(obs),
            "form_lines": {f"第{row}行": cells for row, cells in sorted(_lines(obs).items())},
            "highlighted": turn["marked"], "messages": list(obs.messages[:4]),
            "page_text": page_text(obs, handoffs=self.knowledge.handoffs, list_path=list_path)[-2500:],
        }
        content = ("用户本轮原话：" + prompt
                   + ("\n此前各轮原话：" + " / ".join(history[:-1]) if len(history) > 1 else "")
                   + "\n助手答复：" + answer
                   + "\n助手本轮的操作轨迹：\n" + ("\n".join(turn["trace"][-14:]) or "无")
                   + "\n助手本轮填写/选择过的控件（程序记录，按先后）："
                   + ("；".join(f"{label}←{value or '(清空)'}" for label, value in turn["touched"][-20:]) or "无")
                   + "\n助手本轮读过的接口：" + ("\n".join(f"call_api({a}) → {r}" for a, r in turn["reads"][-4:]) or "无")
                   + ("\n本轮已由用户批准、程序执行并回读核对的写入（程序记录）：\n"
                      + ("\n".join(turn["written"]) or "无") if turn.get("writes") is not None else "")
                   + ("\n程序检出：助手填入的这些值没有原样出现在用户原话里：" + "；".join(stray)
                      + "。逐个判断：是用户要求的另一种写法（如日期补全年份、名称补全全称）或应用自动联动（合计、天数、自动带出的关联）就不算问题；是助手自己编的值就算问题，要求恢复原值。"
                      if stray else "")
                   + "\n页面真实状态：" + json.dumps(state, ensure_ascii=False)[:7000])
        policy = VERIFY_SYSTEM
        if turn.get("writes") is not None:
            policy = policy.replace("\n只输出一个 JSON", VERIFY_WRITES + "\n只输出一个 JSON", 1)
        elif isinstance(ctx.write_intent, WriteIntent) and ctx.write_intent.mode == "save":
            policy += ("\n本轮没有持久写入工具。办理请求可以降级为交接：须定位用户指定的确切记录，"
                       "标出相应业务控件，并明确尚未实际办理。只打开列表、给手工建议或标出不相干控件，"
                       "都不算完成交接；不得要求执行已被禁用的写入。")
        return [{"role": "system", "content": policy}, {"role": "user", "content": content}]

    def prepare(self, prompt, context):
        bound = self._bound_reads(self.goal_port.data, context.operation, context.page.checkpoint)
        with self.knowledge.client.read_window(bound._check):
            self.knowledge._load()
            suggested = self.knowledge.suggest(prompt)
        dates = _precomputed_dates(prompt, self._ctx().turn_tables)
        if dates:
            suggested = (suggested + "\n\n" + dates) if suggested else dates
        return suggested

    def execute(self, name, arguments, context):
        turn = {"reads": [], "marked": "", "trace": [], "touched": []}
        context.page.checkpoint()
        if name in {"search_menu", "search_api", "entity_rules", "find_record", "call_api"}:
            bound = self._bound_reads(self.goal_port.data, context.operation, context.page.checkpoint)
            with self.knowledge.client.read_window(bound._check):
                result = self._tool(context, name, arguments, turn)
        else:
            result = self._tool(context, name, arguments, turn)
        context.page.checkpoint()
        return replace(result, entered=tuple(turn["touched"]), trace=tuple(turn["trace"]),
                       read_summary=turn["reads"][-1] if turn["reads"] else None, marked=turn["marked"])

    @contextlib.contextmanager
    def _worker_read_scope(self, operation):
        client = getattr(getattr(self, "knowledge", None), "client", None)
        data = getattr(getattr(self, "goal_port", None), "data", None)
        data_client = getattr(data, "_client", None)
        actor_client = data_client or client
        with contextlib.ExitStack() as stack:
            if actor_client is not None and hasattr(actor_client, "read_generation"):
                seed_scope = getattr(self._ctx(), "actor_reads", None)
                stack.enter_context(bound_data_reads.turn_actor_reads(actor_client, seed_scope=seed_scope))
            if (operation is not None and data is not None and data_client is not None
                    and hasattr(data_client, "read_generation")):
                def checkpoint():
                    operation.require_live(application_id="yudao", required_scope="yudao.read")

                bound = bound_data_reads.BoundDataReads(data, operation, checkpoint)
                if hasattr(data_client, "read_window"):
                    stack.enter_context(data_client.read_window(bound._check))
                if client is not None and client is not data_client and hasattr(client, "read_window"):
                    stack.enter_context(client.read_window(bound._check))
            yield

    def prepare_read(self, name: str, arguments: dict, *, observation=None, operation=None):
        """Pure-read worker preparation; never mutates `TurnTables`, `ActorKnowledge`, or `SlimTurnContext`."""
        del observation
        if name == "load_domain_guide":
            from ..rules.skill_packages import domain_playbooks
            domain = str(arguments.get("domain") or "").strip()
            return {"domain": domain, "guide": domain_playbooks().get(domain)}
        with self._worker_read_scope(operation):
            if name == "search_api":
                return {"keyword": arguments["keyword"], "text": self.knowledge.search_api(arguments["keyword"])}
            if name == "entity_rules":
                return {
                    "target": arguments["target"],
                    "text": self._entity_rules(
                        SimpleNamespace(operation=operation),
                        arguments["target"],
                        arguments.get("operation", ""),
                    ),
                }
            if name == "find_record":
                targets: list[AnswerTarget] = []
                found: list = []
                preview_commits: list = []

                def checkpoint():
                    if operation is not None:
                        operation.require_live(application_id="yudao", required_scope="yudao.read")

                worker_state = SimpleNamespace(
                    operation=operation,
                    measure=lambda _name: contextlib.nullcontext(),
                    page=SimpleNamespace(checkpoint=checkpoint),
                )
                try:
                    text = self._find_record(
                        worker_state,
                        arguments["name"].strip(),
                        arguments.get("kind", "").strip(),
                        answer_targets=targets,
                        found_values=found,
                        preview_commits=preview_commits,
                    )
                except QueryParametersError as exc:
                    return {"error": exc, "preview_commits": preview_commits}
                return {
                    "text": text,
                    "targets": targets,
                    "found": found,
                    "preview_commits": preview_commits,
                }
            if name == "call_api":
                params = json.loads(arguments.get("params", "{}"))
                if not isinstance(params, dict):
                    raise ValueError("query parameters must be an object")
                called_path = "/admin-api/" + arguments["path"].removeprefix("/admin-api/").lstrip("/")
                req_filters = {key: value for key, value in params.items() if key not in ("pageNo", "pageSize")}
                ctx = self._ctx()
                if is_verified_empty_filter(ctx, ctx.executor_turn, route=called_path, active_view="", filter_pairs=req_filters):
                    return {
                        "called_path": called_path,
                        "params": params,
                        "verified_empty_blocked": True,
                    }
                use_table_sink = ctx.turn_tables is not None
                actor_digest = (
                    operation.principal.actor_authority_digest
                    if "summary" in arguments and operation is not None
                    else ""
                )
                api_prepared = self.knowledge._prepare_call_api(
                    arguments["path"],
                    params,
                    arguments.get("fields", ()),
                    use_row_sink=use_table_sink,
                    row_offset=arguments.get("row_offset", 0),
                    **({"summary": arguments["summary"]} if "summary" in arguments else {}),
                    **({"actor_digest": actor_digest} if "summary" in arguments else {}),
                )
                return {"called_path": called_path, "params": params, "api": api_prepared}
        raise ValueError(f"unsupported concurrent read tool: {name}")

    def commit_read(self, name: str, prepared: dict, arguments: dict, context) -> LoopToolResult:
        """Main-thread sequential commit for a prepared pure-read result."""
        ctx = self._ctx()
        policy_turn = ctx.executor_turn
        table_context = ctx.turn_tables
        said = {
            key: value for key, value in arguments.items()
            if isinstance(value, (str, int, float)) and not isinstance(value, bool)
        }
        if name == "load_domain_guide":
            domain = prepared["domain"]
            self._note(context, stage="search", what="guide", term=domain, tool=name, arguments=said)
            guide = prepared["guide"]
            if not guide:
                return LoopToolResult(f"未知业务域：{domain}", kind="read", state="refused", error_code="unknown_domain")
            return LoopToolResult(guide, kind="read")
        if name == "search_api":
            self._note(context, stage="search", what="source", term=prepared["keyword"], tool=name, arguments=said)
            return LoopToolResult(prepared["text"])
        if name == "entity_rules":
            self._note(context, stage="search", what="rules", term=prepared["target"], tool=name, arguments=said)
            return LoopToolResult(prepared["text"], kind="read")
        if name == "find_record":
            self._note(context, stage="search", what="record", term=arguments["name"], tool=name, arguments=said)
            for preview_prepared in prepared.get("preview_commits", ()):
                try:
                    self.knowledge._commit_call_api(preview_prepared)
                except DataRefused:
                    pass
            if "error" in prepared:
                exc = prepared["error"]
                return LoopToolResult(
                    "查找未完成：" + str(exc) + "。用 search_api 核对接口，再用 call_api 读取。",
                    kind="read", state="refused", error_code="invalid_arguments",
                )
            if policy_turn is not None:
                note_candidates(policy_turn, (("", value) for value in prepared["found"]))
            return LoopToolResult(prepared["text"], kind="read", answer_targets=tuple(prepared["targets"]))
        if name == "call_api":
            know = self.knowledge
            called_path = prepared["called_path"]
            params = prepared["params"]
            if prepared.get("verified_empty_blocked"):
                self._emit_check(context, "verified_empty_filter_repeated")
                return LoopToolResult(
                    "未执行：同一接口与筛选条件在本轮已核验为空结果（total=0）；请勿重复相同查询，请更换条件或如实说明查无记录。",
                    kind="read", state="refused", error_code="verified_empty_filter_repeated",
                )
            self._note(context, stage="tool", tool=name, arguments=said)
            for page_no in prepared["api"].get("pages_read", ()):
                self._publish_read_page(context, page_no)
            try:
                sink = table_context[0].add if table_context is not None else None
                text = know._commit_call_api(prepared["api"], row_sink=sink)
            except TurnReadError as exc:
                return LoopToolResult(str(exc), kind="read", state="unverified", error_code=exc.code)
            except SummaryRefused as exc:
                return LoopToolResult("程序汇总未能核实：" + exc.detail, kind="read", state="unverified", error_code=exc.code)
            except QueryParametersError as exc:
                return LoopToolResult("未执行：" + str(exc), kind="read", state="refused", error_code="invalid_arguments")
            except DataRefused as exc:
                if not exc.permission_denied:
                    raise
                context.audit("yudao_api_read_refused", path=called_path, reason="permission_denied")
                return LoopToolResult(
                    "此只读接口明确拒绝了当前账号。这次拒绝只说明该接口本次不可读，"
                    "不影响其它已读到的结果；不能据此断定其它接口、整类数据或整个问题都无权限。"
                    "已有的成功读取能回答用户问题时照常作答，只说明这个接口未能读取；"
                    "还需要补充证据时，依据已发布的只读接口和当前账号范围寻找。",
                    kind="read", state="refused", error_code="permission_denied",
                )
            if know.last_read:
                req_filters = {key: value for key, value in params.items() if key not in ("pageNo", "pageSize")}
                if getattr(know, "last_is_list", False) and (
                        know.last_read.get("declared_total") == 0 or know.last_read.get("count") == 0):
                    record_verified_empty_filter(
                        ctx, policy_turn,
                        route=called_path, active_view="",
                        filter_pairs=getattr(know, "last_filters", req_filters),
                    )
                def valid_key(key):
                    return isinstance(key, str) and bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", key))

                published_fields = know._row_schema(called_path)
                paging = {
                    key: int(params[key]) for key in ("pageNo", "pageSize")
                    if key in params and re.fullmatch(r"[1-9][0-9]{0,5}", str(params[key]))
                }
                context.audit(
                    "yudao_api_read",
                    path=called_path,
                    query_keys=sorted(key for key in params if valid_key(key))[:32],
                    paging=paging,
                    field_keys=sorted({
                        key for key in arguments.get("fields") or ()
                        if valid_key(key) and key in published_fields
                    })[:12],
                    read_count=know.last_read["count"],
                    declared_total=know.last_read["declared_total"],
                    complete=know.last_read["complete"],
                )
            review = getattr(self.goal_port, "review", None)
            obs = context.obs
            if know.last_read and review and obs and obs.page_read is not None:
                route = urlsplit(obs.url).path
                observed_path = obs.page_read.get("path")
                if obs.page_route == route and observed_path == called_path:
                    labels = review.labels_on(route)
                    named = [f"{key}→{labels[key]}" for key in know.last_keys if key in labels][:30]
                    if named:
                        text += "\n本页表格列名（接口字段→页面列）：" + "、".join(named)
            if know.last_read and policy_turn is not None:
                note_candidates(policy_turn, know.last_read.get("values") or ())
            if know.last_read:
                self._note(
                    context,
                    stage="read_done",
                    tool=name,
                    **{k: v for k, v in know.last_read.items() if k != "values"},
                )
            return LoopToolResult(
                text,
                kind="read",
                state="completed" if know.last_read else "refused",
                query_answer=getattr(know, "last_query_answer", None) if "summary" in arguments else None,
                read_key=called_path if know.last_read else "",
                read_count=know.last_read["count"] if know.last_read else None,
                read_summary=(json.dumps(arguments, ensure_ascii=False)[:160], text[:1500]),
            )
        raise ValueError(f"unsupported concurrent read tool: {name}")
