"""Component-backed location procedures over the existing admitted page actions."""
from __future__ import annotations

import json
import hashlib
import re
from urllib.parse import urljoin, urlsplit

from web_operation_agent.execution.model_loop_contracts import LoopToolResult
from ..executor.page_prompts import _DEPTH, _SCROLL, _scroll_anchor, _tool
from ..executor.procedures import (
    _current_page_query_filters,
    is_verified_empty_filter,
    record_verified_empty_filter,
)
from ..executor.slim_loop import YudaoLoopProcedures
from .live_filters import active_view_or_tab
from web_operation_agent.schemas.browser import MAX_SCROLL_PAGES


LOCATION_TOOLS = [
    _tool("filter_page", "一次完成页面定位和筛选。可先给path导航、tab切页签，再按最新页面标签填筛选项并搜索；"
          "只填查询区，不填业务新建或编辑表单。日期范围写成“开始 ~ 结束”。"
          "页面未提供的字段如实返回；结果必须以应用本次请求和当前行核验。", {
        "path": {"type": "string"}, "tab": {"type": "string"},
        "fields": {"type": "array", "maxItems": 20, "items": {"type": "object", "properties": {
            "label": {"type": "string"}, "value": {"type": "string"}},
            "required": ["label", "value"], "additionalProperties": False}},
        "search": {"type": "string", "description": "当前查询区搜索按钮的精确文字，默认搜索"}}, ["fields"]),
    _tool("open_record", "在当前表格按同账号读取的记录编号或可见业务值唯一认行，打开这一行的详情。"
          "不在当前页时先用filter_page或page_number，不猜行号；同值多条须补充条件。", {
        "record_id": {"type": "string"},
        "column": {"type": "string", "description": "按可见业务值认行时的列标题"},
        "value": {"type": "string", "description": "按可见业务值认行时的精确值"},
        "button": {"type": "string", "description": "行内详情按钮精确文字；不提供时也可用column/value的记录名链接"}}, []),
    _tool("page_section", "切换当前页面的一个精确页签，等待页面自己的加载完成。", {
        "label": {"type": "string"}}, ["label"]),
    _tool("page_number", "通过当前页面的分页控件翻页或改每页条数，保留筛选条件并核对本次请求。", {
        "page": {"type": "integer", "minimum": 1},
        "page_size": {"type": "integer", "minimum": 1}}, []),
]


def refused(text, code="invalid_target"):
    return LoopToolResult(text, kind="page", state="refused", error_code=code)


def _label(element):
    return str(element.attrs.get("field_label") or element.attrs.get("title") or "").strip().rstrip(":：")



class LocationProcedures(YudaoLoopProcedures):
    """Batches choose from fresh observations; they never grant their own effects."""

    def _act(self, s, action, args, **kwargs):
        if action == "click":
            element = next((e for e in s.obs.elements if e.ref == args.get("ref")), None)
            if element is not None and element.role in {"treeitem", "radio", "option"}:
                operational = (s.operation.session_id, s.operation.turn_id,
                               re.sub(r"\s+", "", element.name)) == getattr(self, "_paging_choice", None)
                if operational and element not in self._paging_options(s):
                    return refused("每页条数选项不属于刚展开的分页控件。", "stale_binding")
            opening = (element is not None and element.attrs.get("page_control") == "page_size"
                       and getattr(self, "_paging_choice", None) is not None)
            if opening:
                self._paging_popup = (s.operation.session_id, s.operation.turn_id, s.obs.url,
                    element.attrs.get("aria-controls", ""), s.obs.content_digest,
                    json.dumps(s.obs.page_read, sort_keys=True))
        else:
            opening = False
        result = super()._act(s, action, args, **kwargs)
        if opening and (result is None or result.state == "completed"
                        or result.state == "unverified" and result.dispatch == "acknowledged"):
            # Opening is an intermediate action, not a page-size change. Do not
            # replay it: a fresh, unchanged list plus this exact live popup can
            # prove that selecting its option is now a distinct legal next step.
            self._settled(s, waits=2)
            if self._paging_options(s):
                return None
            return result or LoopToolResult("分页下拉没有新鲜且唯一关联的选项。", kind="page",
                                            state="unverified", dispatch="acknowledged")
        return result

    def _paging_options(self, s):
        opened = getattr(self, "_paging_popup", None)
        if (not opened or opened[:3] != (s.operation.session_id, s.operation.turn_id, s.obs.url)
                or not opened[3] or s.obs.content_digest == opened[4]
                or json.dumps(s.obs.page_read, sort_keys=True) != opened[5]
                or s.obs.render_state != "ready" or s.obs.page_read is None):
            return []
        owners = [e for e in s.obs.elements if e.role == "combobox"
                  and e.attrs.get("page_control") == "page_size"
                  and e.attrs.get("aria-controls") == opened[3]
                  and e.attrs.get("aria-expanded") == "true"
                  and e.attrs.get("disabled") != "true" and e.attrs.get("aria-disabled") != "true"]
        return ([e for e in s.obs.elements if e.role == "option"
                 and e.attrs.get("listbox_owner") == opened[3]] if len(owners) == 1 else [])

    def _paging(self, s, control, label, value):
        self._paging_choice = (s.operation.session_id, s.operation.turn_id,
                               str(int(value)) + "条/页") if control == "page_size" and value.isdigit() else None
        try:
            return super()._paging(s, control, label, value)
        finally:
            self._paging_choice = None
            self._paging_popup = None

    def _section(self, s, label):
        targets = [e for e in s.obs.elements if e.role == "tab" and e.name.strip() == label
                   and e.attrs.get("aria-disabled") != "true"]
        if len(targets) != 1:
            return refused("当前页面没有唯一可用的这个页签。")
        if targets[0].attrs.get("aria-selected") == "true":
            return LoopToolResult("该页签已选中。", kind="page")
        result = self._act(s, "click", {"ref": targets[0].ref})
        if result:
            return result
        settled = self._settled(s)
        selected = [e for e in s.obs.elements if e.role == "tab" and e.name.strip() == label
                    and e.attrs.get("aria-selected") == "true"]
        return (LoopToolResult("页签已核验。", kind="page") if len(selected) == 1 and settled == "still"
                else LoopToolResult("页签结果未确认。", kind="page", state="unverified", dispatch="acknowledged"))

    @staticmethod
    def _query_scroll_region(observation):
        marks = [(len(m[1]), m[2], line) for line in observation.content.splitlines()
                 if (m := _DEPTH.match(line))]
        query = {e.ref[1:] for e in observation.elements if e.attrs.get("page_control") == "query"}
        paging = {e.ref[1:] for e in observation.elements
                  if e.attrs.get("page_control") in {"page_size", "pagination"}}
        records = {e.ref[1:] for e in observation.elements
                   if e.attrs.get("record_id") and e.attrs.get("record_table")}
        candidates = []
        for at, (depth, number, line) in enumerate(marks):
            match = _SCROLL.search(line)
            if match is None or not re.search(r"top=[1-9]", match[1]):
                continue
            children = set()
            for deeper, child, _ in marks[at + 1:]:
                if deeper <= depth:
                    break
                children.add(child)
            if children & (query | paging | records):
                candidates.append((bool(children & paging), bool(children & query),
                                   bool(children & records), depth, number))
        return max(candidates)[-1] if candidates else None

    @staticmethod
    def _query_probe(s, label):
        """Private, bounded metadata only; collection never changes tool behavior."""
        try:
            audit = getattr(s, "audit", None)
            if not callable(audit):
                return
            controls = [e for e in s.obs.elements if e.role in {
                "textbox", "combobox", "select", "button", "searchbox"}]
            controls.sort(key=lambda e: (_label(e) != label, e.attrs.get("page_control") != "query"))
            audit("location_query_unobserved", observation_epoch=s.obs.epoch,
                observation_digest=s.obs.content_digest, truncated=s.obs.truncated,
                render_state=s.obs.render_state, element_count=len(s.obs.elements),
                target_label_sha256=hashlib.sha256(label.encode()).hexdigest(),
                query_count=sum(e.attrs.get("page_control") == "query" for e in s.obs.elements),
                paging_count=sum(e.attrs.get("page_control") in {"page_size", "pagination"}
                                 for e in s.obs.elements),
                record_count=sum(bool(e.attrs.get("record_id") and e.attrs.get("record_table"))
                                 for e in s.obs.elements),
                scroll_region=LocationProcedures._query_scroll_region(s.obs),
                controls=[{"ref": e.ref, "role": e.role,
                    "query": e.attrs.get("page_control") == "query",
                    "matches_label": _label(e) == label,
                    "label_sha256": hashlib.sha256(_label(e).encode()).hexdigest(),
                    "has_form_scope": bool(e.attrs.get("form_scope")),
                    "has_form_instance": bool(e.attrs.get("form_instance"))}
                    for e in controls[:32]], controls_truncated=len(controls) > 32)
        except Exception:
            pass

    def _locate_query_control(self, s, label, *, button=False):
        def found():
            return [e for e in s.obs.elements if e.attrs.get("page_control") == "query"
                and (e.tag == "button" and e.name.strip() == label if button else _label(e) == label)]
        identity = (s.operation.session_id, s.operation.turn_id, s.obs.url)
        for look in range(4):
            s.page.checkpoint()
            if found():
                return None
            region = self._query_scroll_region(s.obs)
            if region is None:
                if look == 0:
                    self._settled(s, waits=2)
                    s.page.checkpoint()
                    if identity != (s.operation.session_id, s.operation.turn_id, s.obs.url):
                        return refused("定位查询区时用户页或本轮身份已改变。", "stale_binding")
                    continue
                break
            before = s.obs.content_digest
            result = self._act(s,"scroll",{"direction":"up","pages":MAX_SCROLL_PAGES,
                                           "ref":"e" + _scroll_anchor(s.obs,region)})
            if result:
                return result
            self._settled(s,waits=2)
            s.page.checkpoint()
            if identity != (s.operation.session_id,s.operation.turn_id,s.obs.url):
                return refused("定位查询区时用户页或本轮身份已改变。","stale_binding")
            if s.obs.content_digest == before:
                break
        if found():
            return None
        self._query_probe(s, label)
        return LoopToolResult("本次可观察的查询区未找到这个" + ("按钮：" if button else "字段：")
            + label + "；未改填其它字段，尚不能确认屏外或未挂载部分是否支持。",
            kind="page",state="unverified",dispatch="not_dispatched",error_code="target_unobserved")

    def _filter(self, s, args, turn):
        if args.get("path"):
            result = super()._tool(s, "goto", {"path": args["path"]}, turn)
            if result.state != "completed":
                return result
        if args.get("tab"):
            result = self._section(s, args["tab"])
            if result.state != "completed":
                return result
        fields = args["fields"]
        # Check the live query scope of the complete batch first.
        # Each control is then resolved afresh by the existing field procedure.
        for item in fields:
            missing = self._locate_query_control(s,item["label"].strip().rstrip(":："))
            if missing:
                return missing
        search = args.get("search", "搜索")
        missing = self._locate_query_control(s,search,button=True)
        if missing:
            return missing
        buttons = [e for e in s.obs.elements if e.tag == "button" and e.name.strip() == search
                   and e.attrs.get("page_control") == "query"]
        if len(buttons) != 1 or re.search(r"删除|保存|提交|审核|确认|导出|重置", search):
            return refused("没有唯一可用的查询按钮，未填写。")
        ctx = self._ctx()
        policy_turn = ctx.executor_turn
        route = getattr(s.obs, "page_route", "") or urlsplit(getattr(s.obs, "url", "") or "").path
        tab = active_view_or_tab(s.obs)
        query_filters = _current_page_query_filters(s.obs, fields)
        if query_filters is not None and is_verified_empty_filter(
                ctx, policy_turn, route=route, active_view=tab, filter_pairs=query_filters):
            self._emit_check(s, "verified_empty_filter_repeated")
            return LoopToolResult(
                "未执行：当前筛选条件在本轮已核验为空结果（0条）；请勿重复相同查询，请更换条件或如实说明查无记录。",
                kind="page", state="refused", error_code="verified_empty_filter_repeated",
            )
        if fields:
            return self._fill_fields(s, fields, search, turn, query_only=True,
                                     locate=self._locate_query_control, choose=self._choose_query)
        result = self._act(s, "click", {"ref": buttons[0].ref}, complete_query=True)
        if not result:
            read_info = getattr(s.obs, "page_read", None)
            if isinstance(read_info, dict) and (read_info.get("total") == 0 or read_info.get("rows") == 0):
                record_verified_empty_filter(
                    ctx, policy_turn,
                    route=getattr(s.obs, "page_route", "") or urlsplit(getattr(s.obs, "url", "") or "").path,
                    active_view=active_view_or_tab(s.obs),
                    filter_pairs=_current_page_query_filters(s.obs),
                )
        return result or LoopToolResult("查询动作已核验。", kind="page")

    def _choose_query(self, s, element, value):
        """Use two admitted clicks for a native, exactly owned query listbox."""
        attrs = element.attrs
        if (element.role != "combobox" or attrs.get("page_control") != "query"
                or attrs.get("aria-haspopup") != "listbox" or not attrs.get("aria-controls")
                or not attrs.get("form_scope") or not attrs.get("form_instance")):
            return self._select(s, element, value)
        keys = ("aria-controls", "form_scope", "form_instance", "query_field", "field_label")
        binding = tuple(attrs.get(key, "") for key in keys)
        identity = (s.operation.session_id, s.operation.turn_id, s.obs.url)

        def owner():
            if identity != (s.operation.session_id, s.operation.turn_id, s.obs.url):
                return None
            matches = [e for e in s.obs.elements if e.role == "combobox"
                and e.attrs.get("page_control") == "query"
                and tuple(e.attrs.get(key, "") for key in keys) == binding]
            return matches[0] if len(matches) == 1 else None

        current = owner()
        if current is None:
            return refused("查询下拉框已变化，未展开。", "stale_binding")
        if current.attrs.get("selected_option") == value:
            return None
        if current.attrs.get("aria-expanded") != "true":
            result = self._act(s, "click", {"ref": current.ref})
            if result is not None and result.state != "completed":
                return result
        self._settled(s, waits=2)
        current = owner()
        if current is None or current.attrs.get("aria-expanded") != "true":
            return LoopToolResult("下拉展开后的归属已变化，未选择选项。", kind="page",
                state="unverified", dispatch="acknowledged", error_code="stale_binding")
        options = [e for e in s.obs.elements if e.role == "option"
            and e.attrs.get("listbox_owner") == binding[0] and e.name.strip() == value.strip()
            and e.attrs.get("disabled") != "true" and e.attrs.get("aria-disabled") != "true"]
        if len(options) != 1:
            return LoopToolResult("当前所属下拉列表没有唯一可用的精确选项；未选择其它名称。", kind="page",
                state="unverified", dispatch="acknowledged", error_code="option_unavailable")
        result = self._act(s, "click", {"ref": options[0].ref})
        if result is not None and result.state != "completed":
            return result
        self._settled(s, waits=2)
        current = owner()
        if current is None or current.attrs.get("selected_option") != value:
            return LoopToolResult("已点击精确选项，但当前查询框未读回该选项；不自动重选。", kind="page",
                state="unverified", dispatch="acknowledged", error_code="stale_binding")
        return None

    @staticmethod
    def _record_rows(s, args):
        identifier, column, value = (args.get(key, "") for key in ("record_id", "column", "value"))
        targets = {}
        for element in s.obs.elements:
            attrs = element.attrs
            if not attrs.get("record_id") or not attrs.get("record_table"):
                continue
            try:
                values = json.loads(attrs.get("record_values", "[]"))
            except (ValueError, TypeError):
                continue
            if (identifier and identifier != attrs["record_id"] or column and
                    (sum(1 for pair in values if isinstance(pair, list) and pair and pair[0] == column) != 1
                     or [column, value] not in values)):
                continue
            targets.setdefault((attrs["record_table"], attrs["record_id"]), []).append(element)
        return targets

    def _open_record(self, s, args):
        identifier, column, value = (args.get(key, "") for key in ("record_id", "column", "value"))
        if not identifier and not (column and value) or bool(column) != bool(value):
            return refused("请给记录编号，或一对精确列名和业务值。")
        button = args.get("button", "")
        if button and button not in {"详情", "查看", "查看详情"}:
            return refused("定位工具只打开详情，不执行业务变更。")
        if self._settled(s) != "still" or s.obs.render_state != "ready":
            return refused("当前列表尚无稳定的原生页面证明。", "stale_binding")
        targets = self._record_rows(s, args)
        if len(targets) != 1:
            return refused("当前页没有唯一匹配的记录；先筛选或翻页，多条时补充业务条件。")
        selected = next(iter(targets))
        source = s.obs.url
        if (self._settled(s) != "still" or s.obs.url != source or s.obs.render_state != "ready"
                or set(self._record_rows(s,args)) != {selected}):
            return refused("重读时目标行已变化；没有派发点击。","stale_binding")
        row=self._record_rows(s,args)[selected]
        controls=[e for e in row if e.tag in {"a","button"} and e.role in {"link","button"}]
        if button:
            choices=[e for e in controls if e.name.strip()==button]
        else:
            choices=[e for e in controls if column and e.name.strip()==value]
            if not choices:
                choices=[e for e in controls if e.name.strip() in {"详情","查看","查看详情"}]
        if len(choices)!=1:
            return refused("这一行没有唯一可用的记录名链接或详情控件；未点击其它行或按钮。")
        target=choices[0]
        href=target.attrs.get("href","")
        expected_route=""
        if href and urlsplit(href).scheme in {"","http","https"} and href!="#":
            address=urlsplit(urljoin(source,href))
            if (address.scheme,address.netloc)==urlsplit(source)[:2]:
                expected_route=address.path
        result=self._act(s,"click",{"ref":target.ref})
        if result is not None and result.state!="completed":
            return result
        settled=self._settled(s)
        route=s.obs.record_route or ""
        route_identity=(bool(route) and selected[1] in urlsplit(route).path.split("/")
                        and (not expected_route or urlsplit(route).path==expected_route))
        dialog_identity=any(e.attrs.get("form_record_id")==selected[1]
            and e.attrs.get("form_scope","").startswith("dialog") for e in s.obs.elements)
        visible=(not value or re.sub(r"\s+"," ",value).strip() in re.sub(r"\s+"," ",s.obs.content))
        verified=settled=="still" and s.obs.render_state=="ready" and visible and (route_identity or dialog_identity)
        return LoopToolResult("已点击当前唯一记录的原生详情控件，请以用户页上的记录身份核对交付。",kind="page",
            state="completed" if verified else "unverified",dispatch="acknowledged",
            completion_command=result.completion_command if result is not None else "")

    def _tool(self, s, name, args, turn):
        if name == "filter_page":
            return self._filter(s, args, turn)
        if name == "open_record":
            return self._open_record(s, args)
        if name == "page_section":
            return self._section(s, args["label"])
        if name == "page_number":
            fields = [{"label": label, "value": str(args[key])} for key, label in (
                ("page_size", "每页条数"), ("page", "页码")) if key in args]
            return self._fill_fields(s, fields, "", turn) if fields else refused("请给页码或每页条数。")
        # Existing ordinary tools keep exactly the shared control-group boundary.
        return super()._tool(s, name, args, turn)


def location_schemas(schemas):
    return list(schemas) + LOCATION_TOOLS
