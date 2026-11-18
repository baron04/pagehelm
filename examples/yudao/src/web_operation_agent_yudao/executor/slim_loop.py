"""A model-driven turn for the native host: one conversation over a small fixed tool set.

The shared model-driven core owns the decision loop. The turn's binding,
its one leave to work the page, the write watch, the application's read-only window,
receipts, budget and settlement are `GoalTurnExecutor`'s, unchanged; only what decides the
next step differs. Here the model chooses it: it finds a page in the actor's own menu,
reads through the application's read-only API as the actor, works the page with the
runtime's fixed actions, and marks a submit control for the person instead of pressing it.
Each result and each fresh observation returns to the same conversation.

Where the deployment enables business writes, the same conversation may also load an
installed Skill and propose one of its typed operations (`goal.skill_writes`). A proposal
is prepared read-only, shown to the person in full, dispatched once after their approval
and read back by the application; its outcome returns to the conversation like any other
result. The page's own submit controls stay unpressed either way.

Read-only deployments review completion claims for draft and business requests against
all the person's requirements. This uses the shared bounded review, as do unresolved
record-tab targets. Typed values absent from the person's words may also prompt a
review; untraced business numbers are fed back once to the same decision conversation.
These model readings can miss intent; native receipts still prove only their own effects.

A turn's conversation with the model lasts that turn. What later turns keep of it is the
executor's bounded conversation state: the person's words, the answer each turn gave and
the writes it verified, persisted by the host under the session's identifier. A new turn
starts from those and reads the page and the data again.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import re
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlsplit

from web_operation_agent.business.errors import (
    BusinessToolContractError,
    BusinessToolIdentityError,
    BusinessToolUnavailableError,
)
from web_operation_agent.execution.checks import ProgramCheckSpec, record_check_fired
from web_operation_agent.execution.draft_handoff import field_identity, handoff_draft, unrestorable_form
from web_operation_agent.execution.model_loop import ModelDrivenTurnExecutor, history_messages
from web_operation_agent.execution.model_loop_contracts import (
    DeliveryFeedback,
    LoopProcedure,
    LoopToolResult,
    ModelLoopProfile,
)
from web_operation_agent.execution.native_submit import _confirm_words
from web_operation_agent.execution.value_source import note_receipt
from web_operation_agent.goal.data import DataRefused
from web_operation_agent.privacy import BASELINE_SESSION_HANDOFFS, SessionHandoffPolicy
from web_operation_agent.schemas.browser import FORM_INPUT_CONTROLS

from ..reads.answer_numbers import untraced as untraced_answer_numbers
from ..reads.api_content import redacted
from ..reads.bound_data_reads import turn_actor_reads
from ..reads.record_access import contact_can_edit, customer_can_edit
from ..reads.turn_tables import TurnReadError, TurnTables
from ..writes.draft_readback import product_price_readback
from ..writes.live_forms import SUBMIT_WORDS, plain
from ..writes.write_intent import WriteIntent, classify_write_intent, preceding_draft_handoff
from .actor_knowledge import ActorKnowledge, QueryParametersError
from .page_prompts import (
    MODEL_STEPS,
    NATIVE_SUBMIT_RULES,
    PAGE_DELIVERY_RULES,
    SYSTEM,
    SYSTEM_READ,
    TABLE_READ_RULES,
    TABLE_TOOLS,
    TOOLS,
    VERIFY_SYSTEM,
    SlimTurnContext,
    _DEPTH,
    _LINE,
    _PAGING,
    _SCROLL,
    _TABLE_TOOLS,
    _control_name,
    _native_input_text,
    _own_list_path,
    _precomputed_dates,
    _scroll_anchor,
    _table_tool_schemas,
    _tabs_not_shown,
    _today,
    _tool,
    _unavailable_already_matches,
    _words,
    conventions_text,
    page_scope_notice,
    page_text,
    scoped_page_tools,
    skills_text,
    system_text,
)
from .procedures import (
    YudaoProceduresBase,
    _ConcurrentReadProcedure,
    _Procedure,
    _business_scope,
    _draft_field_named,
    _draft_only_request,
    _new_form_draft_request,
    _unrequested_draft_fields,
    build_handoff_facts,
)

_STATUS_READ_TOOLS = frozenset({"find_record", "search_api", "call_api", "entity_rules", "load_domain_guide", "finish"})
_CONCURRENT_READ_TOOLS = frozenset({"call_api", "find_record", "search_api", "entity_rules", "load_domain_guide"})

YUDAO_CHECK_SPECS: dict[str, ProgramCheckSpec] = {
    "read_only_question": ProgramCheckSpec(
        name="read_only_question",
        checks_what="Status-only questions refuse page mutation or navigation tools.",
        retry_guidance="本轮只询问状态；请根据当前账号的只读接口结果直接回答，不操作页面。",
        terminal_delivery="Only verified read-only API status facts are delivered; page actions remain refused.",
    ),
    "draft_only": ProgramCheckSpec(
        name="draft_only",
        checks_what="Draft-only form prefill requests block Skill write tools and persistent proposals.",
        retry_guidance="用户本轮只要求预填表单；请改用页面表单步骤，停在提交前。",
        terminal_delivery="No persistent write is proposed or dispatched; only transient form prefill is allowed.",
    ),
    "dangerous_unconfirmed": ProgramCheckSpec(
        name="dangerous_unconfirmed",
        checks_what="Irreversible or reverse status actions require explicit user confirmation from an earlier turn.",
        retry_guidance="请先在对话里说明动作对目标记录及关联库存/活动的具体影响，并用 finish(need_input) 请用户确认。",
        terminal_delivery="The turn stops with need_input before creating an approval card until the user confirms.",
    ),
    "unrequested_draft_field": ProgramCheckSpec(
        name="unrequested_draft_field",
        checks_what="Draft form entries may only set fields explicitly named or valued by the user.",
        retry_guidance="这些表单字段没有得到用户请求支持，未填写；请只填用户指定的字段。",
        terminal_delivery="Unrequested draft values are refused before touching page controls.",
    ),
    "draft_handoff_required": ProgramCheckSpec(
        name="draft_handoff_required",
        checks_what="Entered draft business forms must complete same-form readback and submit-control handoff before done.",
        retry_guidance="已填写的业务表单尚未完成同一实时表单的回读与提交控件交接；请用 then_highlight 完成交接或如实说明 cannot。",
        terminal_delivery="After a failed retry, the reply is preserved and annotated as undelivered with status=cannot.",
    ),
    "delivery_sensitive": ProgramCheckSpec(
        name="delivery_sensitive",
        checks_what="Delivery answers and item descriptions must not contain unredacted credentials.",
        retry_guidance="交付内容含凭据；删去后重新交付。",
        terminal_delivery="After a failed retry, the reply is annotated as undelivered with status=cannot.",
    ),
    "delivery_missing_receipt": ProgramCheckSpec(
        name="delivery_missing_receipt",
        checks_what="Delivered non-page items must cite at least one current-turn receipt.",
        retry_guidance="已交付项必须引用至少一份本轮回执。",
        terminal_delivery="After a failed retry, the affected item is marked undelivered with status=cannot.",
    ),
    "delivery_foreign_receipt": ProgramCheckSpec(
        name="delivery_foreign_receipt",
        checks_what="Every cited delivery receipt must exist in this turn's TurnTables namespace.",
        retry_guidance="有回执不存在或不属于本轮；请用本轮工具返回的编号。",
        terminal_delivery="After a failed retry, the affected item is marked undelivered with status=cannot.",
    ),
    "delivery_page_handoff": ProgramCheckSpec(
        name="delivery_page_handoff",
        checks_what="Answers claiming form handoff must match a handoff still active on the current page.",
        retry_guidance="交接的表单或提交按钮已不在用户页上；核对当前页面并必要时重新交接。",
        terminal_delivery="After a failed retry, the page item is marked undelivered with status=cannot.",
    ),
    "delivery_page_home": ProgramCheckSpec(
        name="delivery_page_home",
        checks_what="Delivered page items must not remain on the application home route.",
        retry_guidance="用户页仍在首页；先到达交付要求的页面再标为已交付。",
        terminal_delivery="After a failed retry, the page item is marked undelivered with status=cannot.",
    ),
    "delivery_dialog_missing": ProgramCheckSpec(
        name="delivery_dialog_missing",
        checks_what="Page items declaring container=dialog require an active dialog on the current page.",
        retry_guidance="交付声明为弹窗，但当前用户页没有这个弹窗。",
        terminal_delivery="After a failed retry, the page item is marked undelivered with status=cannot.",
    ),
    "delivery_page_masked": ProgramCheckSpec(
        name="delivery_page_masked",
        checks_what="Non-dialog page delivery requires no unexpected blocking dialog mask on the current page.",
        retry_guidance="当前页面仍有弹窗遮罩未关闭；等页面落定后再标为已交付。",
        terminal_delivery="After a failed retry, the page item is marked undelivered with status=cannot.",
    ),
    "delivery_write_unverified": ProgramCheckSpec(
        name="delivery_write_unverified",
        checks_what="Delivered kind=write items require a verified write outcome, handoff, or verified satisfaction.",
        retry_guidance="交付项声明为已完成写入（kind=write），但本轮没有已核验的写入结果或表单交接；请先完成或如实标为 undelivered / cannot。",
        terminal_delivery="After a failed retry, the write item is marked undelivered with status=cannot.",
    ),
    "delivery_page_obligation": ProgramCheckSpec(
        name="delivery_page_obligation",
        checks_what="Requests asking to open, pull up, or filter a page cannot be satisfied by API text alone.",
        retry_guidance="请在用户页完成对应导航与筛选并加入 kind=page 交付项，或标为 undelivered 并以 cannot 说明。",
        terminal_delivery="After a failed retry, the reply is annotated as undelivered with status=cannot.",
    ),
    "delivery_incomplete": ProgramCheckSpec(
        name="delivery_incomplete",
        checks_what="finish(status=done) requires every declared delivery item to have status=delivered.",
        retry_guidance="仍有交付项未交付或在等澄清、批准；本轮如实标为未完成。",
        terminal_delivery="After a failed retry, undelivered items close the turn as status=cannot.",
    ),
}


class YudaoLoopProcedures(YudaoProceduresBase):
    """Application knowledge and bounded page procedures with actor-bound handoff checks."""

    def _record_access(self, s, path):
        ctx = self._ctx()
        customer = re.fullmatch(r"/crm/customer/detail/([1-9][0-9]{0,19})", path)
        contact = re.fullmatch(r"/crm/contact/detail/([1-9][0-9]{0,19})", path)
        if customer and "联系人" in ctx.current_request:
            return ""
        intent = ctx.write_intent
        if contact and (not isinstance(intent, WriteIntent) or intent.mode not in {"draft", "save"}):
            return ""
        if not customer and not contact:
            return ""
        record = customer or contact
        biz_type, can_edit_record = (2, customer_can_edit) if customer else (3, contact_can_edit)
        bound = self._bound_reads(self.goal_port.data, s.operation, s.page.checkpoint)
        identity = bound._invoke(self.knowledge.client.get, "system/auth/get-permission-info")
        user = identity.get("user") if isinstance(identity, dict) else None
        principal = s.operation.principal
        if (not isinstance(user, dict) or str(user.get("id")) != principal.actor_id
                or str(user.get("deptId") or "none") != principal.workspace_id):
            raise BusinessToolIdentityError("the customer permission actor changed")
        team = bound._invoke(self.knowledge.client.get, "crm/permission/list", bizType=biz_type, bizId=record[1])
        try:
            can_edit = can_edit_record(identity, team, record[1], principal.actor_id)
        except BusinessToolContractError:
            raise BusinessToolUnavailableError("the application edit permission could not be verified") from None
        if contact:
            if can_edit:
                return "\n应用实时权限：当前账号有此联系人记录本身的编辑资格；页面动作仍须单独审核，写入仍须新的批准。"
            return "\n应用实时权限：当前账号没有此联系人记录本身的编辑权限，不能修改该联系人；这是联系人权限限制，不是客户记录不存在。无需为编辑该联系人而滚动或更换入口重试。"
        if can_edit:
            return "\n应用实时权限：当前账号有此客户记录本身的编辑资格；不代表可编辑其联系人。页面动作仍须单独审核，写入仍须新的批准。"
        return "\n应用实时权限：当前账号没有此客户记录本身的编辑权限；不代表其联系人不能修改。这是客户记录的权限限制，不是记录不存在。无需为编辑客户而滚动或更换入口重试。"

    def _detail_handoff_target(self, s, request: str) -> bool:
        """Bind a detail button to the actor-read record the person actually named."""
        observation = s.obs
        path = urlsplit(observation.url).path
        if (observation.render_state != "ready" or observation.page_route != path
                or observation.record_route != path):
            return False
        data = self.goal_port.data
        candidates = []
        for page in self.goal_port.index(context=s.operation).pages:
            template = data._ui.records.get(page.key, "")
            if not page.entity or template.count("{id}") != 1:
                continue
            match = re.fullmatch(re.escape(template).replace(r"\{id\}", r"([1-9][0-9]*)"), path)
            if match:
                candidates.append((page.entity, match[1]))
        if len(candidates) != 1:
            return False
        entity, identifier = candidates[0]
        spec = data._entities.get(entity)
        if spec is None or not spec.has_get:
            return False
        bound = self._bound_reads(data, s.operation, s.page.checkpoint)
        try:
            record = bound._invoke(data._client.get, spec.get_path, id=identifier)
            if (not isinstance(record, dict) or str(record.get("id")) != identifier
                    or bound.record_address(entity, identifier) != path):
                return False
            if identifier in getattr(self.knowledge, "turn_targets", {}).get(
                    "/admin-api/" + spec.get_path.rpartition("/")[0], set()):
                return True
            display = data._display(entity, record).strip()
            if not display:
                return False
            if re.search(r"(?:编号|ID)\s*[=:：#]?\s*" + re.escape(identifier) + r"(?![0-9])",
                         request, re.I):
                return True
            if display.isdecimal() or display not in request:
                return False
            named = [ref for ref in bound.find(entity, display, limit=2) if ref.name == display]
            return len(named) == 1 and named[0].id == identifier
        except DataRefused as exc:
            if exc.permission_denied or exc.read_failed:
                raise
            return False
        except ValueError:
            return False

    def _list_handoff_target(self, s, element, request: str) -> bool:
        """Bind the clicked row to the exact applied page GET and a fresh same-actor query."""
        obs, read = s.obs, s.obs.page_read
        recent = getattr(self, "page_reads", None)
        if (not callable(recent) or not read or read["page"] != 1 or read["total"] != 1
                or read["rows"] != 1 or obs.render_state != "ready"
                or obs.page_route != urlsplit(obs.url).path):
            return False
        matching = []
        for path, query in recent():
            if path != read["path"]:
                continue
            pairs = parse_qsl(query, keep_blank_values=True)
            groups = {}
            for key, value in pairs:
                groups.setdefault(key, []).append(value)
            if groups.get("pageNo") != ["1"] or groups.get("pageSize") != [str(read["page_size"])]:
                continue
            filters = {k: v for k, v in groups.items() if k not in {"pageNo", "pageSize"}}
            hashes = {key: hashlib.sha256(json.dumps(values, ensure_ascii=False,
                separators=(",", ":")).encode()).hexdigest() for key, values in filters.items()}
            if hashes == read["query_fields"]:
                matching.append((path, groups))
        if not matching:
            return False
        path, groups = matching[-1]
        params = {key: values[0] if len(values) == 1 else values for key, values in groups.items()}
        self.knowledge._load()
        if path in getattr(self.knowledge, "writing_gets", ()):
            return False
        validation_params, indexed = {}, {}
        for key, value in params.items():
            match = re.fullmatch(r"(.+)\[([0-9]+)\]", key)
            if match:
                indexed.setdefault(match[1], {})[int(match[2])] = value
            else:
                validation_params[key] = value
        for key, values in indexed.items():
            if key in validation_params or sorted(values) != list(range(len(values))):
                return False
            validation_params[key] = [values[i] for i in range(len(values))]
        try:
            self.knowledge._validate_params(path, validation_params)
        except (QueryParametersError, KeyError):
            return False
        data = self.goal_port.data
        bound = self._bound_reads(data, s.operation, s.page.checkpoint)
        outcome = s.page.execute("form_model", {"ref": element.ref, "kind": "row"})
        row = getattr(getattr(outcome, "receipt", None), "form_data", None)
        fresh = next((e for e in s.obs.elements if e.ref == element.ref), None)
        if (outcome.state != "completed" or not isinstance(row, dict) or row.get("source") != path
                or not str(row.get("record_id") or "") or s.obs.page_read != read
                or element.attrs.get("record_id") not in {None, "", str(row.get("record_id"))}
                or s.obs.page_route != obs.page_route or s.obs.render_state != "ready"
                or fresh is None or (fresh.role, fresh.name, fresh.tag) != (element.role, element.name, element.tag)):
            return False
        listed = bound._invoke(data._client.get, path.removeprefix("/admin-api/"), **params)
        if (not isinstance(listed, dict) or type(listed.get("total")) is not int or listed["total"] != 1
                or not isinstance(listed.get("list"), list) or len(listed["list"]) != 1
                or not isinstance(listed["list"][0], dict)
                or str(listed["list"][0].get("id")) != str(row["record_id"])):
            return False
        record, identifier = listed["list"][0], str(row["record_id"])
        prefix = path.rpartition("/")[0]
        if identifier in getattr(self.knowledge, "turn_targets", {}).get(prefix, set()):
            return True
        if re.search(r"(?:编号|ID)\s*[=:：#]?\s*" + re.escape(identifier) + r"(?![0-9])", request, re.I):
            return True
        return any(isinstance(value := record.get(field), str) and value.strip() and (
            bool(re.search(r"(?<![A-Za-z0-9_-])" + re.escape(value) + r"(?![A-Za-z0-9_-])", request))
            if field == "no" else value in request) for field in ("name", "no"))

    def _user_status_target(self, s, element, request: str) -> bool:
        obs = s.obs
        match = re.fullmatch(r"woa-user-status-([1-9][0-9]{0,19})", element.attrs.get("id", ""))
        read = obs.page_read
        if (not match or obs.page_route != "/system/user" or urlsplit(obs.url).path != obs.page_route
                or obs.render_state != "ready" or not read or read["path"] != "/admin-api/system/user/page"
                or read["page"] != 1 or read["rows"] != 1 or read["total"] != 1
                or not obs.page_query or obs.page_query["path"] != read["path"]
                or len([e for e in obs.elements if e.role == "switch"]) != 1):
            return False
        data = self.goal_port.data
        bound = self._bound_reads(data, s.operation, s.page.checkpoint)
        record = bound._invoke(data._client.get, "system/user/get", id=match[1])
        if (not isinstance(record, dict) or str(record.get("id")) != match[1]
                or type(record.get("status")) is not int or record["status"] not in (0, 1)):
            return False
        username, nickname = record.get("username"), record.get("nickname")
        if (not isinstance(username, str) or not username or not isinstance(nickname, str) or not nickname
                or element.name != f"状态开关：{nickname}（{username}）"
                or element.attrs.get("aria-checked") != str(record["status"] == 0).lower()):
            return False
        params = {"pageNo": 1, "pageSize": read["page_size"]}
        for field, digest in read["query_fields"].items():
            values = {value for e in obs.elements if e.attrs.get("page_control") == "query"
                      and e.attrs.get("query_field") == field
                      and isinstance(value := e.attrs.get("query_value", e.attrs.get("value")), str)}
            matched = [value for value in values if hashlib.sha256(
                json.dumps([value], ensure_ascii=False, separators=(",", ":")).encode()).hexdigest() == digest]
            if len(matched) != 1:
                return False
            params[field] = matched[0]
        self.knowledge._load()
        try:
            self.knowledge._validate_params("/admin-api/system/user/page", params)
        except QueryParametersError:
            return False
        listed = bound._invoke(data._client.get, "system/user/page", **params)
        if (not isinstance(listed, dict) or type(listed.get("total")) is not int or listed["total"] != 1
                or not isinstance(listed.get("list"), list) or len(listed["list"]) != 1
                or not isinstance(listed["list"][0], dict)
                or str(listed["list"][0].get("id")) != match[1]
                or any(listed["list"][0].get(key) != record[key] for key in ("username", "nickname", "status"))):
            return False
        if re.search(r"(?:编号|ID)\s*[=:：#]?\s*" + match[1] + r"(?![0-9])", request, re.I):
            return True
        if username in request:
            return True
        if not nickname or nickname not in request:
            return False
        named = [r for r in bound.find("system/user", nickname, limit=2) if r.name == nickname]
        return len(named) == 1 and named[0].id == match[1]


def _requirements(name, arguments, observation, review=None):
    if name == "filter_page":
        return frozenset({"facets"}) | (frozenset({"sections"}) if arguments.get("tab") else frozenset())
    if name == "page_section":
        return frozenset({"sections"})
    if name == "page_number":
        return frozenset({"paging"})
    if name == "highlight":
        return frozenset({"handoffs"})
    if name == "goto" and arguments.get("open"):
        return (frozenset() if {"fields", "lines"} <= getattr(review, "disabled_handlers", frozenset())
                else frozenset({"fill"}))
    if name == "fill_fields":
        return frozenset({"paging" if item["label"] in _PAGING else
            "lines" if _LINE.match(item["label"]) else
            "facets" if any(e.attrs.get("page_control") == "query" and
                item["label"] in {e.attrs.get("field_label"), e.attrs.get("title")} for e in observation.elements)
            else "fill" for item in arguments["fields"]}) | (frozenset({"handoffs"})
                if arguments.get("then_highlight") else frozenset()) | (frozenset({"lines"})
                if arguments.get("add_lines") else frozenset()) | (frozenset({"facets"})
                if arguments.get("then_click") else frozenset())
    if name in {"input", "select", "click"}:
        element = next((e for e in observation.elements if e.ref == "e" + str(arguments["index"])), None)
        if element:
            if element.role == "tab":
                return frozenset({"sections"})
            if element.attrs.get("page_control") == "query":
                return frozenset({"facets"})
            if element.attrs.get("page_control") in {"page_size", "pagination"} or element.role == "option":
                return frozenset({"paging"})
            if _LINE.match(element.attrs.get("title", "")):
                return frozenset({"lines"})
            if str(element.attrs.get("form_scope", "")).startswith("dialog"):
                if review is not None and element.tag == "button" and review.permits(
                        "lines", urlsplit(observation.url).path, element.name.strip()):
                    return frozenset({"lines"})
                return frozenset({"fill"})
    return frozenset()


def _read_only_status_question(text: str) -> bool:
    """Only unmistakable status questions; mixed page, money and write requests retain tools."""
    return (bool(re.search(r"状态|进度|情况", text))
            and bool(re.search(r"[?？]|什么|如何|怎样|目前|现在|当前", text))
            and not re.search(r"金额|价格|单价|费用|余额|打开|进入|跳转|页面|筛选|过滤|点击|选择|选中|填写|切换|翻页|定位|显示在|修改|改成|改为|变更|调整|设为|置为|标记|更新|设置|保存|提交|创建|新建|删除|取消|执行|办理", text))


def _normalize_input_options(options) -> list[dict[str, str]]:
    if not isinstance(options, list) or not (2 <= len(options) <= 4):
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for idx, opt in enumerate(options, start=1):
        if not isinstance(opt, dict):
            return []
        label = str(opt.get("label") or "").strip()
        if not label or label in seen:
            return []
        seen.add(label)
        item: dict[str, str] = {
            "id": str(opt.get("id") or f"opt_{idx}").strip()[:64],
            "label": label[:120],
        }
        prompt = str(opt.get("prompt") or "").strip()
        if prompt:
            item["prompt"] = prompt[:500]
        out.append(item)
    return out


def _draft_form_binding(observation, element):
    attrs = element.attrs
    scope = attrs.get("form_scope")
    instance = attrs.get("form_lifetime") or attrs.get("form_instance")
    if (isinstance(scope, str) and _business_scope(scope)
            and isinstance(instance, str) and instance.strip() and attrs.get("page_control") != "query"):
        return (observation.url, observation.page_route, scope,
                "lifetime" if attrs.get("form_lifetime") else "instance", instance)
    return None


def _handoff_key(observation, marked: str):
    """The (scope, instance) key of the form holding the marked control, else "row"."""
    for element in getattr(observation, "elements", ()):
        if _control_name(element) == marked and _business_scope(element.attrs.get("form_scope")):
            return (element.attrs.get("form_scope"),
                    element.attrs.get("form_lifetime") or element.attrs.get("form_instance") or "")
    return "row"


def _emit_yudao_check_fired(executor, s, check: str, *, outcome: str = "blocked",
                            attempt: int = 1, answer_preserved: bool = False) -> None:
    model_state = s if getattr(s, "ledger", None) is not None else getattr(executor, "_active_model_state", s)
    ledger = getattr(model_state, "ledger", None)
    if ledger is None:
        return
    record_check_fired(
        ledger,
        s=model_state,
        trace=getattr(executor, "_trace", None),
        check=check,
        outcome=outcome,
        attempt=attempt,
        answer_preserved=answer_preserved,
    )


class YudaoTurnExecutor(ModelDrivenTurnExecutor):
    """Canonical Yudao composition of the application-neutral model-driven executor."""

    program_read_delivery = False
    direct_skill_operations = True

    def _ctx(self) -> SlimTurnContext:
        ctx = getattr(self, "turn_context", None)
        if ctx is None:
            ctx = SlimTurnContext()
            self.turn_context = ctx
        return ctx

    def _run(self, state):
        ctx = self._ctx()
        prev_request = ctx.current_request
        prev_earlier = ctx.earlier_requests
        prev_intent = ctx.write_intent
        prev_tables = ctx.turn_tables
        prev_model_state = getattr(self, "_active_model_state", None)
        ctx.current_request = state.message.text
        self._active_model_state = state
        self.knowledge.turn_targets = {}
        context = self.conversation_state.model_context() or {}
        ctx.earlier_requests = tuple(str(turn["user"]) for turn in context.get("prior_turns", ()))
        prior_draft = preceding_draft_handoff(context.get("prior_turns"))
        intent = classify_write_intent(state.message.text, prior_draft=prior_draft)
        if intent.mode == "save" and not state.approvals.mutations_enabled:
            # Nothing can be saved here; words like 审核 or 改成 only describe data.
            intent = WriteIntent("neutral")
        ctx.write_intent = intent
        if getattr(self, "procedures", None) is not None:
            self.procedures.form_fill = None
            self.procedures.turn_context = ctx
        handoffs = getattr(self.knowledge, "handoffs", BASELINE_SESSION_HANDOFFS)
        date_override = getattr(self, "_date_override", getattr(self, "today", ""))
        time_zone = getattr(self, "_time_zone", "Asia/Shanghai")
        tables = TurnTables(handoffs)
        ctx.turn_tables = (tables, _today(date_override, time_zone), time_zone)
        try:
            with turn_actor_reads(self.knowledge.client, turn_context=ctx):
                return super()._run(state)
        finally:
            ctx.turn_tables = prev_tables
            tables.close()
            self._active_model_state = prev_model_state
            ctx.write_intent = prev_intent
            ctx.earlier_requests = prev_earlier
            ctx.current_request = prev_request

    def _tools(self, turn):
        ctx = self._ctx()
        intent = ctx.write_intent
        page_request = re.search(
            r"打开|进入|跳转|切到|切换.{0,20}(?:页签|视图)|翻页|翻到|标出|高亮|"
            r"(?:查看|看看|看一下).{0,30}(?:页面|详情)|"
            r"(?:在|把).{0,30}(?:页面|列表).{0,20}(?:筛选|过滤)", ctx.current_request)
        if (turn.get("writes") is None and not turn.get("read_only") and (page_request and turn.get("query_answers") or
                isinstance(intent, WriteIntent) and intent.mode in {"draft", "save"})):
            turn["review_completion"] = True
        offered = super()._tools(turn)
        if _read_only_status_question(ctx.current_request):
            return [tool for tool in offered
                    if tool["function"]["name"] in (_STATUS_READ_TOOLS | (_TABLE_TOOLS if ctx.turn_tables else frozenset()))]
        if not isinstance(intent, WriteIntent) or intent.mode != "draft":
            return offered
        names = {"load_skill", "read_skill_resource", "propose_operation", "request_submit"}
        writes = turn.get("writes")
        if writes is not None:
            avail = getattr(writes, "available_definitions", writes.definitions)
            names.update(definition.name for definition in avail())
        return [tool for tool in offered if tool["function"]["name"] not in names]

    def _publish_step(self, facts: dict, turn_id: str = "") -> None:
        if getattr(self, "harness", None) is not None:
            super()._publish_step(facts, turn_id)
            return
        channel = getattr(self, "channel", None)
        if channel is not None and hasattr(channel, "publish"):
            from web_operation_agent.transport.panel_view import describe_step, step_facts
            text = describe_step(facts)
            if text:
                channel.publish(
                    "progress",
                    {"state": "step", "message": text, "step": step_facts(facts)},
                    turn_id=turn_id,
                )

    def _submit_allowed(self, turn):
        ctx = self._ctx()
        intent = ctx.write_intent
        return not (isinstance(intent, WriteIntent) and intent.mode == "draft") \
            and not _draft_only_request(ctx.current_request or "")

    def _emit_check_fired(self, s, check: str, *, outcome: str = "blocked",
                          attempt: int = 1, answer_preserved: bool = False) -> None:
        _emit_yudao_check_fired(self, s, check, outcome=outcome, attempt=attempt, answer_preserved=answer_preserved)

    def _concurrent_read_commit(self, s, name: str, args: dict, prepared, turn) -> LoopToolResult:
        ctx = self._ctx()
        prev_turn = ctx.executor_turn
        ctx.executor_turn = turn
        try:
            return super()._concurrent_read_commit(s, name, args, prepared, turn)
        finally:
            ctx.executor_turn = prev_turn

    def _invoke_tool(self, s, name, args, turn):
        ctx = self._ctx()
        if _read_only_status_question(ctx.current_request):
            if name not in (_STATUS_READ_TOOLS | (_TABLE_TOOLS if ctx.turn_tables else frozenset())):
                self._emit_check_fired(s, "read_only_question")
                return LoopToolResult("本轮只询问状态；请根据当前账号的只读接口结果直接回答，不操作页面。",
                                      kind="read", state="refused", error_code="read_only_question")
        intent = ctx.write_intent
        if isinstance(intent, WriteIntent) and intent.mode == "draft":
            writes = turn.get("writes")
            avail = getattr(writes, "available_definitions", writes.definitions) if writes is not None else (lambda: ())
            operations = {definition.name for definition in avail()}
            if name in {"load_skill", "read_skill_resource", "propose_operation"} | operations:
                self._emit_check_fired(s, "draft_only")
                return LoopToolResult("用户本轮只要求预填表单；Skill 写入工具不可用。",
                                      kind="write", state="refused", error_code="draft_only")
        before = getattr(s, "obs", None)
        receipt_start = len(getattr(self, "_tool_receipts", ()))
        if self.native_submit is not None and name in {"fill_fields", "input", "select"} and before is not None:
            anchor = next((e for e in before.elements
                           if _business_scope(e.attrs.get("form_scope"))
                           and e.attrs.get("page_control") != "query"), None)
            if anchor is not None:
                self.capture_form_baseline(s, turn, anchor)
            else:
                s.ledger.append("native_submit_baseline", captured=False, reason="no_business_form_on_the_page",
                                sample=[{k: str(v)[:40] for k, v in e.attrs.items() if k.startswith(("form", "field", "page_c"))}
                                        for e in before.elements if e.attrs.get("field_label")][:4])
        marked_before = str(turn.get("marked") or "")
        prev_turn = ctx.executor_turn
        ctx.executor_turn = turn
        try:
            result = super()._invoke_tool(s, name, args, turn)
        finally:
            ctx.executor_turn = prev_turn
        if result.state == "refused" and result.error_code == "unrequested_draft_field":
            self._emit_check_fired(s, "unrequested_draft_field")
        if self.native_submit is not None:
            marked_now = str(turn.get("marked") or getattr(result, "marked", None) or "")
            if marked_now and marked_now != marked_before:
                key = _handoff_key(getattr(s, "obs", None) or before, marked_now)
                target = (turn.get("submit_targets") or {}).get(key) or {}
                if target.get("kind") == "row" or plain(marked_now) in SUBMIT_WORDS:
                    turn["handoff_button"] = marked_now
                    turn["handoff_target_key"] = key
                    mark_fn = getattr(self.native_submit, "user_write_mark", None)
                    if callable(mark_fn):
                        turn["handoff_mark"] = mark_fn()
        if self.native_submit is not None and result.entered:
            table_ctx = ctx.turn_tables
            if table_ctx is not None and not turn.get("_seeded_precomputed_dates"):
                turn["_seeded_precomputed_dates"] = True
                for val in getattr(table_ctx[0], "precomputed_date_values", ()):
                    note_receipt(turn, "", val)
            self.note_entries(s, turn, result.entered, before,
                              receipts=self._tool_receipts[receipt_start:])
            ledger = turn.get("source_ledger")
            observed = before
            for receipt in self._tool_receipts[receipt_start:]:
                target = next((e for e in getattr(observed, "elements", ())
                               if e.ref == receipt.args_echo.get("ref")), None)
                if (receipt.ok and receipt.action == "select" and target is not None
                        and target.attrs.get("form_scope") and target.attrs.get("page_control") != "query"):
                    label = _control_name(target)
                    selected = [e for e in getattr(receipt.observation, "elements", ())
                                if e.attrs.get("form_scope") == target.attrs.get("form_scope")
                                and _control_name(e) == label and e.attrs.get("selected_option")]
                    if len(selected) == 1:
                        value = selected[0].attrs["selected_option"]
                        ledger.entries = [replace(entry, candidate=True) if entry.field == label
                                          and entry.value == value else entry for entry in ledger.entries]
                observed = receipt.observation or observed
        if result.entered and name in {"input", "select"} and getattr(self, "procedures", None) is not None:
            entered = dict(result.entered)
            target = self.procedures._element(SimpleNamespace(obs=before), args["index"])
            if (target is not None and target.role in FORM_INPUT_CONTROLS
                    and _business_scope(target.attrs.get("form_scope"))
                    and target.attrs.get("page_control") != "query" and _control_name(target) in entered):
                fill = self.procedures.form_fill
                identity = field_identity(target)
                if fill is None:
                    self.procedures.form_fill = {"before": before,
                        "expected": [(identity, entered[_control_name(target)])]}
                elif any(key[:2] == identity[:2] for key, _ in fill["expected"]):
                    fill["expected"] = [(key, value) for key, value in fill["expected"] if key != identity] + [
                        (identity, entered[_control_name(target)])]
        if isinstance(intent, WriteIntent) and intent.mode == "draft":
            if result.entered:
                observed = before
                for receipt in self._tool_receipts:
                    if receipt.ok and receipt.action in {"fill", "select"}:
                        target = next((e for e in getattr(observed, "elements", ())
                                       if e.ref == receipt.args_echo.get("ref") and e.role in FORM_INPUT_CONTROLS), None)
                        if target is not None and target.attrs.get("form_scope") and target.attrs.get("page_control") != "query":
                            turn.setdefault("draft_form_entries", set()).add(_draft_form_binding(observed, target))
                    observed = receipt.observation or observed
            if (result.marked and result.state == "completed" and result.completion_command
                    and self._completion_proved(s, name, args, result)):
                receipt = next((r for r in self._tool_receipts
                    if r.action == "handoff_form" and r.command_id == result.completion_command), None)
                if receipt is not None:
                    turn.pop("draft_handoff_form", None)
                    submit = receipt.args_echo.get("submit", {})
                    targets = [e for e in receipt.observation.elements if isinstance(submit, dict) and e.role == "button"
                               and plain(e.name) == plain(str(submit.get("label") or ""))
                               and e.attrs.get("form_scope") == submit.get("scope")]
                    if len(targets) == 1:
                        turn["draft_handoff_form"] = self._handoff_form_binding(receipt.observation, targets[0])
        return result

    def _handoff_form_binding(self, observation, target):
        scope = target.attrs.get("form_scope")
        instance = target.attrs.get("form_instance")
        carrier = next(
            (e for e in observation.elements
             if e.role in FORM_INPUT_CONTROLS
             and e.attrs.get("form_scope") == scope
             and e.attrs.get("form_instance") == instance
             and e.attrs.get("form_lifetime")),
            None,
        )
        if carrier is None and not target.attrs.get("form_lifetime"):
            for past in reversed(getattr(self, "_tool_receipts", ())):
                past_obs = getattr(past, "observation", None)
                if past_obs is None:
                    continue
                carrier = next(
                    (e for e in past_obs.elements
                     if e.role in FORM_INPUT_CONTROLS
                     and e.attrs.get("form_scope") == scope
                     and e.attrs.get("form_instance") == instance
                     and e.attrs.get("form_lifetime")),
                    None,
                )
                if carrier is not None:
                    return _draft_form_binding(past_obs, carrier)
        return _draft_form_binding(observation, carrier or target)

    def _default_handoff(self, s, turn) -> str:
        """A filled business form that nothing submitted gets its submit control marked by the program."""
        fill = getattr(getattr(self, "procedures", None), "form_fill", None)
        if not fill or s.obs is None or turn.get("submit_handoff_blocked"):
            return ""
        handoff = turn.get("handoff")
        if handoff and handoff[0] == self._page_fingerprint(s.obs):
            return str(handoff[1] or "")
        if turn.get("default_handoff_tried") == self._page_fingerprint(s.obs):
            return ""
        turn["default_handoff_tried"] = self._page_fingerprint(s.obs)
        scopes = {(identity[0], identity[1]) for identity, _value in fill["expected"]}
        if len(scopes) != 1:
            return ""
        scope, _instance = next(iter(scopes))
        buttons = [e for e in s.obs.elements if e.role == "button" and e.attrs.get("form_scope") == scope
                   and plain(e.name) in SUBMIT_WORDS]
        labels = {e.name.strip() for e in buttons}
        if len(labels) != 1:
            return ""
        context = self._procedure_context(s, "default_handoff", page_actions=True)
        before = fill["before"]
        restored = turn.get("restored_form_labels", ())
        if restored:
            now = {field_identity(e): e for e in s.obs.elements}
            before = replace(before, elements=[replace(e, attrs=now[field_identity(e)].attrs)
                if _control_name(e) in restored and field_identity(e) in now else e for e in before.elements])
        try:
            expected = product_price_readback(context, before, fill["expected"], self.goal_port.data)
            result = handoff_draft(context, before, expected, next(iter(labels)),
                                   blocked=turn.get("unrestorable_forms"))
        except Exception as exc:  # noqa: BLE001 -- the reply then says it was not done
            s.ledger.append("default_handoff_failed", error_type=type(exc).__name__)
            return ""
        s.ledger.append("default_handoff", marked=result.marked, state=result.state,
                        command=result.completion_command, code=result.error_code)
        if result.marked and result.state == "completed":
            turn["handoff"] = (self._page_fingerprint(s.obs), result.marked)
            turn["marked"] = result.marked
            if self.native_submit is not None:
                turn["handoff_button"] = str(result.marked)
                turn["handoff_target_key"] = _handoff_key(s.obs, str(result.marked))
                mark_fn = getattr(self.native_submit, "user_write_mark", None)
                if callable(mark_fn):
                    turn["handoff_mark"] = mark_fn()
            return result.marked
        return ""

    def export_table(self, ref: str) -> dict | None:
        table_ctx = self._ctx().turn_tables
        if table_ctx is None:
            return None
        try:
            return table_ctx[0].export_table(ref)
        except TurnReadError:
            return None

    def _resolve_finish_table(self, s, arguments: dict) -> dict | None:
        del s
        ref = str((arguments or {}).get("table_ref") or "").strip()
        if not ref or str((arguments or {}).get("status") or "") != "done":
            return None
        return self.export_table(ref)

    def _settle(self, s, current, text):
        table_payload = getattr(s, "finish_table", None)
        input_options = getattr(s, "input_options", None)
        handoff_facts = getattr(s, "handoff_payload", None) or getattr(s, "handoff_facts", None)
        if handoff_facts is not None and getattr(s, "handoff_payload", None) is None:
            setattr(s, "handoff_payload", handoff_facts)
        channel = getattr(self, "channel", None)
        if channel is None or not hasattr(channel, "publish") or (
                table_payload is None and input_options is None and handoff_facts is None):
            return super()._settle(s, current, text)
        orig_publish = channel.publish

        def enriched_publish(kind, payload, **kw):
            if kind == "progress" and isinstance(payload, dict) and "terminal_reason" in payload:
                merged = dict(payload)
                if table_payload is not None:
                    merged.setdefault("table", table_payload)
                if input_options is not None:
                    merged.setdefault("input_options", list(input_options))
                if handoff_facts is not None:
                    merged.setdefault("handoff", dict(handoff_facts))
                return orig_publish(kind, merged, **kw)
            return orig_publish(kind, payload, **kw)

        channel.publish = enriched_publish
        try:
            return super()._settle(s, current, text)
        finally:
            channel.publish = orig_publish

    def _closing(self, s, turn, args):
        args = super()._closing(s, turn, args)
        table_ref = str(args.get("table_ref") or "").strip()
        if table_ref:
            exported = self._resolve_finish_table(s, args)
            if exported is not None:
                turn["finish_table"] = exported
                setattr(s, "finish_table", exported)
                trace = getattr(self, "_trace", None)
                if trace is not None and hasattr(trace, "record_delivery") and not turn.get("finish_table_trace_recorded"):
                    try:
                        trace.record_delivery(
                            reference=table_ref,
                            projection=exported,
                            rendered_text=str(args.get("answer") or ""),
                            presented_records=len(exported["rows"]),
                            presentation_complete=len(exported["rows"]) >= exported["total_rows"],
                            source_reference=table_ref,
                            structured_table=exported,
                        )
                        turn["finish_table_trace_recorded"] = True
                    except Exception:
                        pass
                receipt = self._ctx().turn_tables[0].receipt_for_table_ref(table_ref) if self._ctx().turn_tables else ""
                s.ledger.append("finish_table_attached", ref=table_ref, rows=len(exported["rows"]),
                                total_rows=exported["total_rows"], receipt=receipt)
        if args.get("status") == "need_input" and args.get("options"):
            normalized = _normalize_input_options(args.get("options"))
            if normalized:
                turn["input_options"] = normalized
                setattr(s, "input_options", normalized)
        handoff_facts = getattr(self._ctx(), "handoff_facts", None) or turn.get("handoff_facts")
        if handoff_facts is None and (turn.get("applied_fields") or turn.get("rejected_fields") or turn.get("restricted_fields")
                                      or (turn.get("handoff") and getattr(s, "obs", None) is not None)):
            handoff_facts = build_handoff_facts(turn, getattr(s, "obs", None))
        if handoff_facts is not None:
            turn["handoff_facts"] = handoff_facts
            setattr(s, "handoff_facts", handoff_facts)
            setattr(s, "handoff_payload", handoff_facts)
        if (getattr(getattr(self, "procedures", None), "form_fill", None) and not turn.get("submit_started")
                and not turn.get("written") and not turn.get("submit_handoff_blocked")):
            label = self._default_handoff(s, turn)
            said = str(args.get("answer") or "")
            if not ("尚未提交" in said or "还没有提交" in said or "没有提交" in said):
                note = (f"表单已按你的要求填好并核验，「{label}」已标出；尚未提交，请核对后自己点击。" if label else
                        "表单已按你的要求填写，但程序没能核验并标出提交按钮；尚未提交，请核对表单后自己提交。")
                args = {**args, "answer": (said.rstrip() + "\n\n" + note).strip()}
        return self._close_with_notes(s, turn, args)

    def _close_with_notes(self, s, turn, args):
        """The two additions a closing answer may still need."""
        unrestorable = next(iter((turn.get("unrestorable_forms") or {}).values()), None)
        said = str(args.get("answer") or "")
        if unrestorable is not None and "无法还原" not in said:
            effects = "；".join(str(e) for e in (unrestorable.get("effects") or ())[:8]) \
                      or str(unrestorable.get("detail") or "")
            args = {**args, "answer": (said.rstrip()
                    + "\n\n未标出提交按钮：表单打开时页面自动修改了字段且无法还原，"
                    + (f"直接保存会把 {effects}；" if effects else "直接保存会带入未核对的变化；")
                    + "请核对后自己提交。").strip()}
        pressed = (self._await_handoff_press(s, turn)
                   if args.get("status") in ("done", "cannot") and self._submit_allowed(turn) else "")
        if pressed:
            args = {**args, "answer": (str(args.get("answer") or "").rstrip() + "\n\n" + pressed).strip()}
        return args

    def _finish_tool(self, turn):
        tool = super()._finish_tool(turn)
        parameters = tool["function"]["parameters"]
        parameters["properties"]["items"] = {"type": "array", "minItems": 1, "maxItems": 16, "items": {
            "type": "object", "properties": {
                "item": {"type": "string", "minLength": 1, "maxLength": 240},
                "kind": {"type": "string", "enum": ["data", "page", "write"]},
                "status": {"type": "string", "enum": ["delivered", "undelivered"]},
                "container": {"type": "string", "enum": ["page", "dialog"]},
                "receipts": {"type": "array", "maxItems": 12,
                             "items": {"type": "string", "minLength": 1, "maxLength": 48}}},
            "required": ["item", "kind", "status", "receipts"], "additionalProperties": False}}
        parameters["properties"]["table_ref"] = {
            "type": "string", "minLength": 2, "maxLength": 12,
            "description": "可选：引用 sql 返回的 q1..qN 或 t1..tN 表句柄，由服务端直接渲染完整数据表格。",
        }
        parameters["properties"]["options"] = {
            "type": "array", "minItems": 2, "maxItems": 4,
            "description": "仅 status=need_input 时可选：2 到 4 个让用户直接点选的候选或确认选项。",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "minLength": 1, "maxLength": 64},
                    "label": {"type": "string", "minLength": 1, "maxLength": 120},
                    "prompt": {"type": "string", "minLength": 1, "maxLength": 500},
                },
                "required": ["label"],
                "additionalProperties": False,
            },
        }
        if turn.get("sources") is None or turn.get("runtime_used"):
            parameters["required"].append("items")
        return tool

    def _completion_proved(self, s, name, args, result=None, *, target=None):
        if args.get("answer") and result is not None and result.kind == "page":
            if not any(receipt.action == "handoff_form" and receipt.command_id == result.completion_command
                       for receipt in self._tool_receipts):
                return False
        return super()._completion_proved(s, name, args, result, target=target)

    def _finish_feedback(self, s, turn, args):
        if not turn.get("submit_started") and not turn.get("written"):
            self._default_handoff(s, turn)
        entered = turn.get("draft_form_entries")
        if entered and turn.get("unrestorable_forms"):
            entered = {binding for binding in entered
                       if not (isinstance(binding, tuple) and len(binding) >= 5
                               and unrestorable_form(turn["unrestorable_forms"], binding[2], binding[4]))}
        if args.get("status") == "done" and entered and not turn.get("submit_handoff_blocked"):
            handoff = turn.get("handoff")
            if (not handoff or handoff[0] != self._page_fingerprint(s.obs) or not turn.get("draft_handoff_form")
                    or entered != {turn.get("draft_handoff_form")}):
                if self._default_handoff(s, turn):
                    receipt = next((r for r in reversed(self._tool_receipts) if r.action == "handoff_form"), None)
                    if receipt is not None:
                        submit = receipt.args_echo.get("submit", {})
                        targets = [e for e in receipt.observation.elements if isinstance(submit, dict)
                                   and e.role == "button" and plain(e.name) == plain(str(submit.get("label") or ""))
                                   and e.attrs.get("form_scope") == submit.get("scope")]
                        if len(targets) == 1:
                            turn["draft_handoff_form"] = self._handoff_form_binding(receipt.observation, targets[0])
                if (not turn.get("handoff") or turn["handoff"][0] != self._page_fingerprint(s.obs)
                        or entered != {turn.get("draft_handoff_form")}):
                    return DeliveryFeedback("draft_handoff_required",
                        "已填写的业务表单尚未完成同一实时表单的回读与提交控件交接。"
                        "请核对当前表单，用 fill_fields 的 then_highlight 完成审核交接；"
                        "不能交接时用 cannot 如实说明，不能仅凭填写或普通高亮声称完成。")
        feedback = super()._finish_feedback(s, turn, args)
        if feedback:
            return feedback
        if args.get("table_ref"):
            if args.get("status") != "done":
                return DeliveryFeedback("invalid_table_ref", "table_ref 仅可在 status=done 时使用。")
            if self.export_table(str(args["table_ref"])) is None:
                return DeliveryFeedback("invalid_table_ref",
                    f"表句柄 {args['table_ref']} 不存在或未生成；请引用 sql 返回的 q1..qN / t1..tN。")
        if args.get("options"):
            if args.get("status") != "need_input":
                return DeliveryFeedback("invalid_input_options", "options 仅可在 status=need_input 时使用。")
            if not _normalize_input_options(args.get("options")):
                return DeliveryFeedback("invalid_input_options", "options 必须包含 2 到 4 个非空且不重复 label 的候选项。")
        if "items" not in args:
            return ""
        try:
            self._ctx().turn_tables[0].validate_delivery(args["answer"], args["items"])
            page_state = {}

            def page_facts():
                if not page_state:
                    route = getattr(s.obs, "page_route", "") or urlsplit(
                        getattr(s.obs, "url", "") or "").path
                    page_state["home"] = route.rstrip("/") in {"", "/index"}
                    page_state["masked"] = any(
                        str(getattr(e, "attrs", {}).get("form_scope", "")).startswith("dialog")
                        or getattr(e, "role", "") == "dialog"
                        for e in getattr(s.obs, "elements", ()) or ())
                return page_state

            for item in args["items"]:
                if item["kind"] == "page" and item["status"] == "delivered":
                    facts = page_facts()
                    handoff_made = turn.get("handoff")
                    if (handoff_made and handoff_made[0] != self._page_fingerprint(s.obs)
                            and re.search(r"交接|标出|高亮|已填|提交按钮", str(args.get("answer") or ""))):
                        raise TurnReadError("delivery_page_handoff",
                                            "答复说已交接，但交接的表单或提交按钮已不在用户页上；"
                                            "核对当前页面，必要时重新交接。")
                    if facts["home"]:
                        raise TurnReadError("delivery_page_home",
                                            "用户页仍在首页；先到达交付要求的页面再标为已交付。")
                    handoff = turn.get("handoff")
                    delivered_dialog = (item.get("container") == "dialog" or
                        bool(handoff and handoff[0] == self._page_fingerprint(s.obs)))
                    if item.get("container") == "dialog" and not facts["masked"]:
                        raise TurnReadError("delivery_dialog_missing",
                                            "交付声明为弹窗，但当前用户页没有这个弹窗。")
                    if facts["masked"] and not delivered_dialog:
                        raise TurnReadError("delivery_page_masked",
                                            "当前页面仍有弹窗遮罩未关闭；等页面落定后再标为已交付。")
                elif item["kind"] == "write" and item["status"] == "delivered":
                    ans = str(args.get("answer") or "")
                    satisfied = bool(_ALREADY_SATISFIED.search(ans) and not _WRITE_UNEXECUTED.search(ans))
                    if not turn.get("written") and not turn.get("handoff") and not satisfied:
                        raise TurnReadError("delivery_write_unverified",
                                            "交付项声明为已完成写入（kind=write），但本轮没有已核验的写入结果或表单交接；"
                                            "请先完成批准提交或如实标为 undelivered / cannot。")
            if (args.get("status") == "done"
                    and "goto" not in getattr(getattr(self, "config", None), "disabled_actions", frozenset())
                    and not turn.get("written") and not turn.get("handoff")
                    and _requires_page_delivery(getattr(getattr(s, "message", None), "text", ""))):
                facts = page_facts()
                has_page_item = any(item.get("kind") == "page" and item.get("status") == "delivered"
                                    for item in args["items"])
                page_acted = (
                    not facts["home"]
                    and not facts["masked"]
                    and any(r.ok and r.action in {"goto", "click", "fill", "select"}
                            for r in self._tool_receipts)
                )
                if facts["home"] or (not has_page_item and not page_acted):
                    raise TurnReadError("delivery_page_obligation",
                                        "用户的请求要求在页面上调出、打开或筛选，不能仅用接口文字代替页面落点。"
                                        "请在用户页完成对应导航与筛选并加入 kind=page 的交付项；"
                                        "若页面本身无对应入口或筛选控件，将该页面项标为 undelivered 并以 status=cannot 如实说明。")
            if (args.get("status") == "done"
                    and any(item["status"] != "delivered" for item in args["items"])):
                raise TurnReadError("delivery_incomplete",
                                    "仍有交付项未交付或在等澄清、批准；本轮如实标为未完成。")
        except TurnReadError as exc:
            if exc.code.startswith(("delivery_page_", "delivery_dialog_")):
                affected = tuple(i for i, item in enumerate(args["items"]) if item["kind"] == "page")
            elif exc.code == "delivery_write_unverified":
                affected = tuple(i for i, item in enumerate(args["items"]) if item["kind"] == "write")
            elif exc.code == "delivery_incomplete":
                affected = tuple(i for i, item in enumerate(args["items"]) if item["status"] != "delivered")
            else:
                affected = ()
            return DeliveryFeedback(exc.code, str(exc), affected)
        s.ledger.append("model_delivery_validated", items=[{
            "kind": item["kind"], "status": item["status"], "receipts": item["receipts"]}
            for item in args["items"]])
        return ""

    def _tool_feedback(self, s, turn, name, result, text):
        if name in {"call_api", "tables", "sql"}:
            turn["read_tables_used"] = True
        if result.state == "completed" and result.kind in {"read", "page", "write"} and name != "tables":
            reference = self._ctx().turn_tables[0].issue_receipt()
            s.ledger.append("model_delivery_receipt", tool=name, reference=reference)
            if result.kind == "page":
                observed = [receipt for receipt in self._tool_receipts
                            if receipt.ok and receipt.observation is not None]
                if observed and self._page_fingerprint(observed[-1].observation) == self._page_fingerprint(s.obs):
                    digest = hashlib.sha256(self._page_fingerprint(s.obs).encode()).hexdigest()
                    turn.setdefault("page_delivery_receipts", {})[reference] = digest
                    s.ledger.append("model_page_delivery_receipt", reference=reference,
                                    command_id=observed[-1].command_id, state_sha256=digest)
                    return text + f"\n本轮页面状态回执：{reference}。核对当前页面后用于kind=page的交付。"
            return text + f"\n本轮工具回执：{reference}"
        return text

    def _untraced_numbers(self, prompt, answer, turn):
        stripped = _TABLE_TOKEN.sub("", _BARE_RECEIPT.sub("", answer or ""))
        return super()._untraced_numbers(prompt, stripped, turn)

    def _clean_answer(self, answer: str) -> str:
        handoffs = getattr(getattr(self, "knowledge", None), "handoffs", BASELINE_SESSION_HANDOFFS)
        return _clean_user_answer(answer, handoffs)

    def _with_not_executed(self, s, turn, answer):
        return self._clean_answer(super()._with_not_executed(s, turn, answer))

    def _propose(self, s, writes, operation: str, inputs: dict, turn: dict) -> LoopToolResult:
        ctx = self._ctx() if hasattr(self, "_ctx") else getattr(self, "turn_context", None)
        intent = ctx.write_intent if ctx is not None else None
        if (isinstance(intent, WriteIntent) and intent.mode == "draft") or _draft_only_request(s.message.text):
            _emit_yudao_check_fired(self, s, "draft_only")
            return LoopToolResult(
                "用户本轮只要求预填表单。未提出写入提案，也未执行写入；请改用页面表单步骤，停在提交前。",
                kind="write", state="refused", dispatch="not_dispatched", error_code="draft_only")
        dangerous_op = (
            operation == "promotion_seckill_activity_close"
            or (operation == "erp_document_status" and isinstance(inputs, dict) and inputs.get("action") == "反审批")
        )
        if dangerous_op and (not self._earlier_words or not _confirm_words(s.message.text)):
            action_label = ("关闭秒杀活动（不可逆）" if operation == "promotion_seckill_activity_close"
                            else "反审批单据（回退库存或核销状态）")
            s.ledger.append("skill_operation_refused", step=s.step + 1, tool=operation,
                            error_code="dangerous_unconfirmed", detail=action_label, side_effect_possible=False)
            _emit_yudao_check_fired(self, s, "dangerous_unconfirmed")
            return LoopToolResult(
                f"「{action_label}」属于不可逆或逆向状态变更。请先在对话里向用户说明该动作对目标记录及关联库存/活动的具体影响，"
                "并用 finish(status='need_input', answer=..., options=[{'id': 'confirm', 'label': '确认执行', 'prompt': '确认执行该不可逆操作'}, "
                "{'id': 'cancel', 'label': '取消操作', 'prompt': '暂不执行，取消'}]) 请用户明确确认；用户确认后下一轮再提出写入提案。",
                kind="write", state="needs_input", dispatch="not_dispatched", error_code="dangerous_unconfirmed",
            )
        prefill_port = getattr(getattr(self, "profile", None), "mutation_prefill", None)
        if prefill_port is not None and hasattr(prefill_port, "last_handoff_facts"):
            prefill_port.last_handoff_facts = None
        result = super()._propose(s, writes, operation, inputs, turn)
        handoff_facts = getattr(prefill_port, "last_handoff_facts", None) or (
            getattr(ctx, "handoff_facts", None) if ctx is not None else None
        )
        if handoff_facts is not None:
            turn["handoff_facts"] = handoff_facts
            setattr(s, "handoff_facts", handoff_facts)
            setattr(s, "handoff_payload", handoff_facts)
        if result.state == "needs_input" and "options" not in result.feedback:
            result = replace(
                result,
                feedback=result.feedback + "\n多个候选需要用户选择时，请在 finish(status='need_input', answer=..., options=[...]) 中提供 2 到 4 个候选 {id, label, prompt}。",
            )
        return result

    def __init__(self, *, knowledge, actor_name="", today="", time_zone="Asia/Shanghai", conventions=(),
                 draft_steps=None, mutation_prefill=None, location_bindings=False, native_submit=None, page_reads=None, **kwargs):
        self._date_override, self._time_zone = today, time_zone
        kwargs.setdefault("model_steps", MODEL_STEPS)
        application, goal_port = kwargs["application"], kwargs["goal_port"]
        self.knowledge = knowledge
        self.turn_context = SlimTurnContext()
        knowledge.handoffs = SessionHandoffPolicy.extending(application.ports.routes.session_handoff_query_keys)
        procedure_type = YudaoLoopProcedures
        if location_bindings:
            from ..reads.location import LocationProcedures
            procedure_type = LocationProcedures
        self.procedures = owner = procedure_type(
            knowledge, goal_port, application.descriptor.origin,
            turn_context=self.turn_context,
            publish_step=lambda facts, tid="": self._publish_step(facts, tid),
            check_fired=self._emit_check_fired,
        )
        owner.page_reads = page_reads
        review = getattr(goal_port, "review", None)
        schemas = _table_tool_schemas(review)
        if location_bindings:
            from ..reads.location import location_schemas
            schemas = location_schemas(schemas)
        procedures = tuple(LoopProcedure(json.dumps(schema, ensure_ascii=False),
            (_ConcurrentReadProcedure if schema["function"]["name"] in _CONCURRENT_READ_TOOLS else _Procedure)(
                owner, schema["function"]["name"]),
            "read" if schema["function"]["name"] in {"call_api", "find_record", "entity_rules", "load_domain_guide"} | _TABLE_TOOLS else
            "metadata" if schema["function"]["name"] in {"search_menu", "search_api"} else "page",
            "yudao.read", frozenset({"handoffs"}) if schema["function"]["name"] == "highlight" else frozenset(),
            lambda arguments, observation, name=schema["function"]["name"]:
                _requirements(name, arguments, observation, getattr(goal_port, "review", None)))
                for schema in schemas if schema["function"]["name"] != "finish")
        has_compiled_rules = "source" in getattr(goal_port, "capabilities", lambda: frozenset())()
        profile = ModelLoopProfile(
            system_text(user=actor_name or "当前用户", date=_today(today, time_zone), tz=time_zone,
                        conventions=conventions,
                        runtime_source=bool(getattr(getattr(kwargs.get("config"), "source", None),
                                                    "runtime_source", False)),
                        compiled_rules=has_compiled_rules)
                + page_scope_notice(review) + ("\n当前仅定位：filter_page一次导航、切页签和筛选；"
                    "open_record按当前行的record_id或精确列值打开详情；page_section/page_number切页签和分页。"
                    "新增绑定工具只操作查询区；原普通动作完整保留，沿用同一准入及业务填表值来源边界。"
                    if location_bindings else ""), procedures,
            lambda observation, marked: page_text(observation, handoffs=knowledge.handoffs, marked=marked,
                                                  list_path=_own_list_path(goal_port, observation)),
            owner.prepare, lambda observation, answer: tuple(
                _tabs_not_shown(observation, answer, request=self.turn_context.current_request)),
            owner.verification_messages, draft_steps, mutation_prefill,
            untraced_numbers=untraced_answer_numbers)
        super().__init__(profile=profile, **kwargs)
        if native_submit is not None:
            self.native_submit = native_submit
            if hasattr(self.channel, "read_only_mode"):
                self.channel.read_only_mode = False
            updated_instructions = (
                self.profile.instructions
                .replace(
                    "只有下面“已安装的 Skill”覆盖的操作能办：你提出写入提案，用户在批准卡片上确认后，程序执行一次并回读核对。\n"
                    "   - 本轮提供了 Skill 写入工具且用户要求办理其覆盖的事时，先 load_skill，再用已安装操作提出提案；没有对应 Skill 时把表单填好交接。",
                    "优先用下面“已安装的 Skill”覆盖的操作提出提案；Skill 未覆盖的新增、修改或行内状态动作，"
                    "在用户页用原生表单或行内按钮配合 request_submit 经批准卡提交（不得因无对应 Skill 而拒绝，唯独涉及密码或本地文件上传时填好其余字段交接）。\n"
                    "   - 本轮提供了 Skill 写入工具且用户要求办理其覆盖的事时，先 load_skill，再用已安装操作提出提案；没有对应 Skill 且非纯预填时，打开对应页面表单填好后用 request_submit 请求批准提交。",
                    1,
                )
                .replace(
                    "用户要的事里有 Skill 办不到的部分（参数里没有的字段、另一种业务动作）：先如实说明办不到的是哪部分（cannot），不要只办能办的那部分就说完成。",
                    "用户要的事里若既有 Skill 参数没覆盖某个字段或动作，改走用户页原生表单配合 request_submit 办理；若应用页面本身也不支持，再如实说明办不到的是哪部分（cannot），不要只办能办的那部分就说完成。",
                    1,
                )
                + NATIVE_SUBMIT_RULES
            )
            self.profile = replace(self.profile, instructions=updated_instructions)
        instructions = self.profile.instructions.replace(
            "先 load_skill，再用已安装操作提出提案",
            "直接用 propose_operation(operation, inputs) 提出提案（程序自动加载对应 Skill，需要查完整领域规则时仍可调 load_skill；"
            "扁平单表记录字段修改优先用 update_record，两态启停/上下架优先用 toggle_record_status，专用新建或状态变更用对应操作）",
        )
        self.profile = replace(self.profile, instructions=instructions + PAGE_DELIVERY_RULES)


SlimTurnExecutor = YudaoTurnExecutor
TableReadTurnExecutor = YudaoTurnExecutor


_RECEIPT_PAREN = re.compile(
    r"[（(][^（）()\n]{0,32}?(?:ev_[0-9a-f]{12}_\d+|(?:本轮)?行表\s*`?t\d{1,2}`?)[^（）()\n]{0,120}?[）)]"
)
_RECEIPT_INLINE = re.compile(
    r"(?:[，,；;、]\s*)?(?:依据|来源|引用|参考|见)?(?:本轮)?(?:读取|工具|页面状态)?回执"
    r"(?:号|编号)?[:：=\s]*`?ev_[0-9a-f]{12}_\d+`?(?:[、,，/\s]+`?ev_[0-9a-f]{12}_\d+`?)*"
)
_BARE_RECEIPT = re.compile(r"`?ev_[0-9a-f]{12}_\d+`?")
_RECEIPT_STUB = re.compile(r"[（(]\s*(?:依据|来源|引用|参考|见)?(?:本轮)?(?:读取|工具)?回执[，,、\s]*(?:详见)?(?:本轮读取数据)?\s*[）)]")
_EMPTY_PARENS = re.compile(r"[（(]\s*[）)]")
_TABLE_TOKEN = re.compile(r"(?:(?:本轮)?行表|数据表|临时表|表)\s*`?t\d{1,2}`?|`t\d{1,2}`")
_PAGE_OBLIGATION = re.compile(
    r"调出|(?:帮我|请)?(?:打开|进入|跳转到?|定位到?|切到|切换到)[^，。；！？?\n]{0,24}"
    r"(?:页面|列表页?|详情|台账|看板|页签|栏目|菜单|视图|界面)"
    r"|在[^，。；！？?\n]{0,12}(?:页面|列表页?|台账)(?:上|里|中)?(?:筛选|过滤|按条件筛)"
)
_META_PAGE_QUESTION = re.compile(r"(?:怎么|如何|怎样|能否|能不能|是否|为什么)[^，。；！？?\n]{0,12}(?:打开|调出|进入|跳转|切到)")
_NEGATED_PAGE_OBLIGATION = re.compile(
    r"(?:不要|不用|无需|无须|不必|不需|别|勿)[^，。；！？?\n]{0,8}(?:打开|调出|进入|跳转|定位|切到|切换|筛选)"
)
_ALREADY_SATISFIED = re.compile(r"已是|已经为|已经是|无需修改|原本就是|保持一致|未发生变化")
_WRITE_UNEXECUTED = re.compile(r"未(?:执行|完成|成功|提交|保存|修改|写入)|尚未|未能")


def _requires_page_delivery(prompt: str) -> bool:
    said = _words(prompt or "").strip()
    if not said:
        return False
    for clause in re.split(r"[，,；;。！？!\n]+", said):
        if not clause or not _PAGE_OBLIGATION.search(clause):
            continue
        if _META_PAGE_QUESTION.search(clause) or _NEGATED_PAGE_OBLIGATION.search(clause):
            continue
        return True
    return False


def _clean_user_answer(answer: str, handoffs) -> str:
    if not answer:
        return answer
    cleaned = redacted(answer, handoffs)
    cleaned = _RECEIPT_PAREN.sub("", cleaned)
    cleaned = _RECEIPT_INLINE.sub("", cleaned)
    cleaned = _BARE_RECEIPT.sub("", cleaned)
    cleaned = _TABLE_TOKEN.sub("本轮读取数据", cleaned)
    cleaned = _RECEIPT_STUB.sub("", cleaned)
    cleaned = _EMPTY_PARENS.sub("", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\s+([，。；：！？])", r"\1", cleaned)
    return cleaned.strip()


__all__ = [
    "ActorKnowledge",
    "SlimTurnContext",
    "SlimTurnExecutor",
    "TableReadTurnExecutor",
    "YUDAO_CHECK_SPECS",
    "YudaoLoopProcedures",
    "YudaoTurnExecutor",
    "conventions_text",
    "history_messages",
    "page_text",
    "skills_text",
    "system_text",
]
