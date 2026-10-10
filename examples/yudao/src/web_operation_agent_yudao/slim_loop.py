"""A model-driven turn for the native host: one conversation over a small fixed tool set.

A measured prototype hosted by this adapter, not core architecture. The turn's binding,
its one leave to work the page, the write watch, the application's read-only window,
receipts, budget and settlement are `GoalTurnExecutor`'s, unchanged; only what decides the
next step differs. Here the model chooses it: it finds a page in the actor's own menu,
reads through the application's read-only API as the actor, works the page with the
runtime's fixed actions, and marks a submit control for the person instead of pressing it.
Each result and each fresh observation returns to the same conversation.

When the model says it is done, one separate model request checks the person's words
against what the page then shows, and may send the turn back once.
"""
from __future__ import annotations

from datetime import datetime
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from web_operation_agent.goal.data import DataRefused
from web_operation_agent.goal.executor import GoalTurnExecutor
from web_operation_agent.execution.receipt import MeasuredChat
from web_operation_agent.modeling.egress import AuditedChat
from web_operation_agent.modeling.provider import ProviderError
from web_operation_agent.privacy import SessionHandoffPolicy, redact_sensitive_assignments

#: Requests a turn may make of the model for its steps; the check at the end is extra.
MODEL_STEPS = 24
#: Development only: a directory to which each turn's conversation is written as the model
#: saw it. Unset in any deployment; what a page shows does not belong in a log.
_DEBUG_DIR = os.environ.get("WOA_SLIM_DEBUG_DIR")
_MARK = re.compile(r"^\s*\*?\[(\d+)\]")
_RUN, _HEAD, _TAIL = 60, 3, 30
_PAGE_CHARS = 9_000


# -- what the application already publishes: the actor's menu and its API description ------
def _grams(text: str) -> set[str]:
    text = re.sub(r"\s+", "", text.lower())
    return {text[i:i + 2] for i in range(len(text) - 1)} | set(text)


def _score(query: str, *fields) -> float:
    terms = [t for t in re.split(r"[\s,，、/]+", query.lower()) if t]
    score = 0.0
    for weight, field in fields:
        low = field.lower()
        for term in terms:
            if term in low:
                score += 6 * weight * (1 + len(term) / 8)
        want, have = _grams("".join(terms)), _grams(low)
        if want:
            score += weight * 3 * len(want & have) / len(want)
    return score


def _slim(value, deep: bool = False):
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if item in (None, "", [], {}):
                continue
            if isinstance(item, (dict, list)):
                if deep:
                    out[key] = _slim(item) if isinstance(item, dict) else [_slim(x) for x in item[:20]]
                elif isinstance(item, list) and item and not isinstance(item[0], (dict, list)):
                    out[key] = item[:10]
                continue
            out[key] = item[:80] if isinstance(item, str) else item
        return out
    return value


class ActorKnowledge:
    """The signed-in actor's menu pages and the application's read-only API, searchable."""

    def __init__(self, client, api_docs, writing_gets=frozenset()):
        self.client, self._docs, self.writing_gets = client, api_docs, frozenset(writing_gets)
        self._menu = self._ops = self._by_path = self._schemas = None
        self.user_name = ""

    def _load(self):
        if self._ops is not None:
            return
        docs = self._docs() if callable(self._docs) else self._docs
        self._schemas = (docs.get("components") or {}).get("schemas") or {}
        ops = []
        for path, item in (docs.get("paths") or {}).items():
            op = item.get("get") if isinstance(item, dict) else None
            if not path.startswith("/admin-api/") or not isinstance(op, dict):
                continue
            if any(mark in path for mark in ("/export", "/import", "/download")):
                continue
            ops.append({"path": path, "summary": op.get("summary") or "",
                        "tag": (op.get("tags") or [""])[0].replace("管理后台 - ", ""), "op": op})
        self._by_path = {op["path"]: op for op in ops}
        info = self.client.get("system/auth/get-permission-info") or {}
        self.user_name = str((info.get("user") or {}).get("nickname") or "")
        self._menu = []
        self._walk(info.get("menus") or [], [], "")
        self._ops = ops

    def _walk(self, nodes, names, base):
        for node in nodes or []:
            path = node.get("path") or ""
            full = path if path.startswith("/") else f"{base.rstrip('/')}/{path}"
            trail = names + [node.get("name") or ""]
            children = node.get("children") or []
            if children:
                self._walk(children, trail, full)
            elif node.get("visible", True) and node.get("component"):
                self._menu.append({"name": " > ".join(trail), "path": full, "leaf": trail[-1]})

    def has_page(self, path: str) -> bool:
        self._load()
        return any(m["path"] == path for m in self._menu)

    def search_menu(self, keyword: str) -> str:
        self._load()
        ranked = sorted(((_score(keyword, (2, m["leaf"]), (1, m["name"])), m) for m in self._menu),
                        key=lambda pair: -pair[0])
        hits = [m for score, m in ranked[:8] if score > 2]
        if not hits:
            return "没有匹配的菜单页面。换一个更短的业务词再试。"
        return "\n".join(f"{m['path']}  {m['name']}" for m in hits)

    def search_api(self, keyword: str) -> str:
        self._load()
        ranked = sorted(((_score(keyword, (2, o["summary"]), (2, o["tag"]), (1, o["path"])), o)
                         for o in self._ops), key=lambda pair: -pair[0])
        lines = []
        for score, op in ranked[:8]:
            if score <= 2:
                break
            params = []
            for p in op["op"].get("parameters") or []:
                if p.get("in") != "query":
                    continue
                desc = (p.get("description") or "").split("，")[0][:24]
                params.append(f"{p['name']}{'*' if p.get('required') else ''}({desc})")
            lines.append(f"GET {op['path']}  [{op['tag']}] {op['summary']}\n    参数: "
                         + (", ".join(params[:14]) or "无"))
        return "\n".join(lines) or "没有匹配的只读接口。换一个业务词再试。"

    def _deref(self, schema):
        ref = schema.get("$ref") if isinstance(schema, dict) else None
        return self._schemas.get(ref.rsplit("/", 1)[-1], {}) if ref else (schema or {})

    def _row_schema(self, path):
        op = self._by_path.get(path)
        try:
            schema = next(iter(op["op"]["responses"]["200"]["content"].values()))["schema"]
        except (KeyError, StopIteration, TypeError):
            return {}
        for _ in range(6):
            schema = self._deref(schema)
            props = schema.get("properties") or {}
            if "data" in props and "code" in props:
                schema = props["data"]
            elif "list" in props and "total" in props:
                schema = props["list"]
            elif schema.get("type") == "array":
                schema = schema.get("items") or {}
            else:
                return props
        return {}

    def call_api(self, path: str, params: dict) -> str:
        self._load()
        if not path.startswith("/admin-api/"):
            path = "/admin-api/" + path.lstrip("/")
        if path not in self._by_path:
            near = self.search_api(path.replace("/admin-api/", "").replace("/", " "))
            return f"没有这个只读接口：{path}。相近的接口：\n{near}"
        if path in self.writing_gets:
            return "这个接口读取时会写入数据，助手不允许调用。"
        try:
            data = self.client.get(path[len("/admin-api/"):], **{
                str(k): v for k, v in (params or {}).items() if isinstance(k, str)})
        except DataRefused as exc:
            return f"接口拒绝：{str(exc)[:200]}"
        props, out, rows = self._row_schema(path), [], None
        if isinstance(data, dict) and isinstance(data.get("list"), list):
            rows = data["list"]
            out.append(f"total={data.get('total')}（符合条件的总数），本次返回 {len(rows)} 行")
        elif isinstance(data, list):
            rows = data
            out.append(f"返回 {len(rows)} 行")
        if rows is not None:
            out.extend(json.dumps(_slim(row), ensure_ascii=False) for row in rows[:25])
            if len(rows) > 25:
                out.append(f"……其余 {len(rows) - 25} 行未显示；用更具体的参数缩小范围")
            keys = list(rows[0].keys()) if rows and isinstance(rows[0], dict) else []
        else:
            out.append(json.dumps(_slim(data, deep=True), ensure_ascii=False)[:3500])
            keys = list(data.keys()) if isinstance(data, dict) else []
        notes = [f"{k}={(self._deref(props[k]).get('description') or '')[:40]}"
                 for k in keys if k in props and self._deref(props[k]).get("description")]
        if notes:
            out.append("字段说明: " + "; ".join(notes[:30]))
        return "\n".join(out)[:6000]


# -- tools ------------------------------------------------------------------------------
def _tool(name, description, properties, required):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required}}}


TOOLS = [
    _tool("search_menu", "按业务词查找后台菜单页面，返回页面路径。", {
        "keyword": {"type": "string", "description": "业务词，如 采购入库、会员等级"}}, ["keyword"]),
    _tool("goto", "打开一个页面路径（来自 search_menu 或页面里出现的详情路径）。", {
        "path": {"type": "string"}}, ["path"]),
    _tool("observe", "重新观察当前页面。", {}, []),
    _tool("click", "点击当前观察里带序号的元素。", {
        "index": {"type": "integer"}}, ["index"]),
    _tool("input", "向带序号的输入框填入文字（会先清空原值）。日期框填 2026-10-20 这样的格式。", {
        "index": {"type": "integer"}, "text": {"type": "string"}}, ["index", "text"]),
    _tool("select", "在带序号的下拉框或单选组里选中一个选项，option 写选项显示的文字。", {
        "index": {"type": "integer"}, "option": {"type": "string"}}, ["index", "option"]),
    _tool("scroll", "滚动页面；给出 index 时滚动那个元素所在的可滚动区域（例如弹窗）。", {
        "down": {"type": "boolean"}, "index": {"type": "integer"}}, ["down"]),
    _tool("search_api", "按业务词查找应用的只读数据接口。", {
        "keyword": {"type": "string"}}, ["keyword"]),
    _tool("call_api", "以当前用户身份调用一个只读接口（GET）。分页接口必须带 pageNo 和 pageSize。", {
        "path": {"type": "string"},
        "params": {"type": "object", "description": "查询参数"}}, ["path"]),
    _tool("highlight", "把一个控件标出来交给用户自己点（例如表单的确定按钮）。不会点击它。", {
        "index": {"type": "integer"}}, ["index"]),
    _tool("finish", "结束本轮。", {
        "status": {"type": "string", "enum": ["done", "need_input", "cannot"],
                   "description": "done=全部完成；need_input=需要用户补充或在多个候选中选择；cannot=做不到（记录不存在、无权限、应用不支持）"},
        "answer": {"type": "string", "description": "给用户的简短答复，包含查到的事实"}},
        ["status", "answer"]),
]

SYSTEM = """你是嵌入在企业管理后台里的助手，替当前登录用户完成一句话的业务请求。
当前用户：{user}。今天是 {date}（{tz}）。请求里没写年份的日期按今年理解。

你能做三类事，一句话里可能同时包含几类，每一类都要做完：
1. 回答业务数据问题。先用 search_api 找到接口，再用 call_api 读真实数据。
   - 数量、合计必须来自接口的 total 或把全部行读完后计算，不能只数页面上的一页。
   - 先把名称解析成记录：用名称参数查列表，确认只有一条匹配；匹配多条就 need_input 列出候选，没有匹配就 cannot，不要换成名字相近的另一条。
   - 金额、单价这类值：接口返回的是原始数，字段说明里的单位（分/元）不一定准确。回答前必须打开对应的列表或详情页，以页面上显示的数字为准。
2. 把页面带到用户要的状态：打开某个列表或详情、切换页签、筛选、翻页、改每页条数。
   - 用 search_menu 找页面，goto 打开；同一个业务词常常在几个模块里都有页面，先看全部结果再选最贴合用户说法的那个，并在答复里说明用的是哪个模块的页面。
   - 筛选要在页面自己的筛选控件上操作，再点“搜索”，然后从“页面生效的查询”确认参数已生效。
   - 下拉框用 select 选；select 不行时先点击它，再从新观察里点击选项。打开某条记录的详情：在列表里找到那一行再点它的名称或“详情”。
   - 用户要打开或修改“某一条”记录时：按用户给的名称或条件筛选后，只有一条才继续；有多条就 need_input 并列出全部候选让用户选，不要自己挑；一条也没有就 cannot。
   - 用户说“只看…”“翻到下一页”而没说哪个页面时，指的是当前页面。
3. 替用户把表单填好但不提交：打开“新增”或那条记录的“编辑”表单，只填用户明确给出的字段，其它字段保持原样。
   - 填完后重新观察，确认每个值都显示正确；然后用 highlight 标出表单的“确定/保存/提交”按钮，再 finish。
   - 绝对不要点击确定、保存、提交、删除等会写入数据的按钮；这一步留给用户。
   - 用户没提到的字段一律不填，包括必填项：留空，在答复里提醒用户还有哪些必填项要自己补，然后照常以 done 结束。
   - 只有用户一个具体值都没给（比如只说“新增一家供应商”）时，才用 need_input 问，不要自己编。

通用规则：
- 每次只根据最新的观察行动；序号只在最新观察里有效。观察只包含当前屏幕上看得见的部分，要找的控件不在里面时先 scroll。
- 不编造任何值。读不到、找不到、没有权限就如实说（cannot）。
- 全部做完后调用 finish，答复用一两句中文，写出查到的具体值和页面/表单现在的状态。"""

VERIFY_SYSTEM = """你是验收员。下面是用户的原话、助手的答复，以及页面此刻的真实状态（由程序读取，不是助手自述）。
逐项核对用户原话里的每一个要求是否真的满足：
- 要求打开/筛选/翻页/切页签的：页面路径、弹窗、页签、页面生效的查询是否就是用户要的那个对象和条件；打开的记录名称是否与用户说的完全一致。
- 要求填表的：用户给的每个值是否出现在对应字段；是否标出了提交按钮；是否改动了用户没要求的字段。
- 要求回答的：答复里是否有具体值；这个值是否能在“助手本轮读过的接口”结果或页面状态里找到；有矛盾才算问题。只提问、没要求打开页面的请求，不需要页面有任何变化。
- 助手说“已完成”的事，状态里有没有证据。
- 用户指的是单独一条记录（“那张”“这个”“它”或直接给名称），而操作轨迹里按这个名称或条件查到多条（total 大于 1），助手却自己挑了一条打开或修改：算问题，应当列出候选让用户选。
- 答复里的金额、单价与页面状态里显示的数字不一致，或只来自接口原始值而页面上没有出现：算问题。
只输出一个 JSON：{"ok": true 或 false, "problems": ["具体缺了什么或哪里不一致"]}。没有问题时 problems 为空数组。不要因为措辞、礼貌或你自己的偏好判不通过。"""


def _today(today: str, time_zone: str) -> str:
    """The date the model is told: the application's reference day when it has one,
    otherwise today where the application's clock is."""
    return today or datetime.now(ZoneInfo(time_zone)).date().isoformat()


def _number(ref: str) -> str:
    return ref[1:] if re.fullmatch(r"e\d+", ref or "") else ref


def _fields(observation) -> dict[str, str]:
    """What each labelled form control on the page holds, as the runtime reports it."""
    found = {}
    for element in observation.elements:
        label = element.attrs.get("field_label")
        if not label:
            continue
        value = element.attrs.get("selected_option") or element.attrs.get("value") or ""
        found.setdefault(str(label).rstrip(":：").strip(), str(value))
    return found


_DEPTH = re.compile(r"^(\t*)\*?\[(\d+)\]")
_SCROLL = re.compile(r'data-scrollable="([^"]*)"')


def _page_lines(observation) -> tuple[list[str], list[str]]:
    """The page's text without what is not the page: the runtime's own preamble and the
    application's navigation menu (a container whose marked children are menu entries; a
    page is reached through the actor's menu index, not by reading its several hundred
    lines). Also says which regions hold more than the screen shows."""
    lines = observation.content.splitlines()
    if "[Start of page]" in lines:
        lines = lines[lines.index("[Start of page]") + 1:]
    lines = [line for line in lines if line.strip() != "[End of page]"]
    roles = {e.ref: e.role for e in observation.elements}
    marks = [(i, len(m.group(1)), m.group(2)) for i, line in enumerate(lines)
             if (m := _DEPTH.match(line))]
    # A bare wrapper with no name of its own tells the reader nothing; what it holds stays.
    blank = {e.ref for e in observation.elements
             if e.tag in ("div", "span", "i") and not e.name.strip()
             and not any(e.attrs.get(k) for k in ("title", "placeholder", "aria-label", "field_label"))}
    drop: set[int] = {i for i, _depth, number in marks
                      if f"e{number}" in blank and _SCROLL.search(lines[i]) is None
                      and lines[i].rstrip().endswith("/>")}
    for position, (i, depth, _number_) in enumerate(marks):
        if i in drop:
            continue
        inside = []
        end = len(lines)
        for j, deeper, number in marks[position + 1:]:
            if deeper <= depth:
                end = j
                break
            inside.append(number)
        menu = [n for n in inside if roles.get(f"e{n}") == "menuitem"]
        if len(inside) >= 5 and len(menu) >= 0.8 * len(inside):
            drop.update(range(i, end))
    hints = []
    for i, depth, number in marks:
        if i in drop:
            continue
        found = _SCROLL.search(lines[i])
        if not found:
            continue
        for side, word, down in (("bottom", "下方", "true"), ("top", "上方", "false")):
            amount = re.search(rf"{side}=(\d+)", found.group(1))
            if amount and int(amount.group(1)) >= 40:
                hints.append(f"[{number}] {word}还有约 {amount.group(1)} 像素没显示，"
                             f"用 scroll(down={down}, index={number}) 查看")
    return [line for i, line in enumerate(lines) if i not in drop], hints


def page_text(observation, *, handoffs, marked: str = "") -> str:
    """The page as the loop's model reads it; what it may act on is marked [n]."""
    head = [f"当前页面: {urlsplit(observation.url).path}  {observation.title}"]
    if observation.render_state in {"pending", "blocked"}:
        head.append(f"页面状态: {observation.render_state}")
    if observation.selected_controls:
        head.append("选中的页签/视图: " + " | ".join(observation.selected_controls))
    if observation.page_read is not None:
        read = observation.page_read
        head.append(f"列表: 第 {read['page']} 页，每页 {read['page_size']} 条，本页 {read['rows']} 行，共 {read['total']} 条")
    if observation.page_query is not None:
        query = observation.page_query
        head.append("页面生效的查询（页面自己最近一次成功读取的参数，即当前生效的筛选）: GET "
                    f"{query['path']} {json.dumps(query['parameters'], ensure_ascii=False)[:400]}")
    fields = _fields(observation)
    if fields:
        head.append("表单当前值: " + "; ".join(f"{k}={v or '(空)'}" for k, v in list(fields.items())[:40]))
    if observation.messages:
        head.append("页面提示: " + " | ".join(observation.messages[:4])[:300])
    if marked:
        head.append("已标出的控件: " + marked)
    lines, hints = _page_lines(observation)
    if hints:
        head.append("可滚动区域: " + "；".join(hints[:3]))
    body, run = [], []

    def flush():
        if len(run) > _RUN:
            body.extend(run[:_HEAD])
            body.append(f"……（中间 {len(run) - _HEAD - _TAIL} 行没有可操作元素的文字已省略）")
            body.extend(run[-_TAIL:])
        else:
            body.extend(run)
        run.clear()

    if lines:
        for line in lines:
            if _MARK.match(line):
                flush()
                body.append(line)
            else:
                run.append(line)
        flush()
    else:
        body = [f'[{_number(e.ref)}] {e.role} "{e.name}"' for e in observation.elements]
    text = "\n".join(head + ["页面元素（只含当前屏幕；*[序号] 或 [序号] 可操作）:"] + body)
    text = redact_sensitive_assignments(handoffs.redact(text))
    if len(text) > _PAGE_CHARS:
        text = text[:_PAGE_CHARS] + "\n……（页面较长，已截断；可 scroll 后再观察）"
    return text


class SlimTurnExecutor(GoalTurnExecutor):
    """`GoalTurnExecutor` with its decision replaced by a model-driven tool loop."""

    def __init__(self, *, knowledge: ActorKnowledge, actor_name: str = "", today: str = "",
                 time_zone: str = "Asia/Shanghai", verify: bool = True,
                 model_steps: int = MODEL_STEPS, **kw):
        super().__init__(**kw)
        self.knowledge, self.actor_name = knowledge, actor_name
        self.today, self.time_zone = today, time_zone
        self.verify, self.model_steps = bool(verify), int(model_steps)
        #: This session's conversation as the model saw it; a new conversation starts it over.
        self._messages: list[dict] = []

    # -- the page, through the executor's own dispatch ------------------------------------
    def _settled(self, s, *, path: str = "", waits: int = 8) -> None:
        """Let the page finish what the last step started before it is read again."""
        for _ in range(waits):
            obs = s.obs
            arrived = not path or urlsplit(obs.url).path == path
            if arrived and obs.render_state != "pending":
                break
            if self._dispatch(s, "wait_for", {"ms": 400}) is None:
                return
        self._dispatch(s, "wait_for", {"ms": 400})

    def _element(self, s, index):
        """The element the model named, if the page text it was last shown listed it."""
        number = str(int(index))
        lines, _hints = _page_lines(s.obs)
        shown = {m.group(2) for line in lines if (m := _DEPTH.match(line))}
        if number not in shown:
            return None
        return next((e for e in s.obs.elements if e.ref == f"e{number}"), None)

    def _act(self, s, action: str, args: dict, *, navigating: bool = False) -> str:
        receipt = self._dispatch(s, action, args, navigating=navigating, admitted=True)
        if receipt is None:
            return "未执行：本轮已结束。"
        if not receipt.ok:
            return f"没有成功：{receipt.error_code} {str(receipt.error_detail)[:160]}".strip()
        return ""

    def _tool(self, s, name: str, args: dict, turn: dict) -> tuple[str, bool]:
        know = self.knowledge
        if name == "search_menu":
            return know.search_menu(str(args.get("keyword", ""))), False
        if name == "search_api":
            return know.search_api(str(args.get("keyword", ""))), False
        if name == "call_api":
            params = args.get("params")
            if isinstance(params, str):
                try:
                    params = json.loads(params)
                except ValueError:
                    params = {}
            with s.receipt.stage("business"):
                text = know.call_api(str(args.get("path", "")), params if isinstance(params, dict) else {})
            turn["reads"].append((json.dumps(args, ensure_ascii=False)[:160], text[:1500]))
            return text, False
        if name == "observe":
            self._dispatch(s, "observe", {})
            return "观察如下。", True
        if name == "goto":
            path = "/" + str(args.get("path", "")).lstrip("/")
            if not re.fullmatch(r"/[A-Za-z0-9_\-/]{1,200}", path):
                return "路径不合法。用 search_menu 查找。", False
            failed = self._act(s, "goto", {"url": self.application.descriptor.origin + path},
                               navigating=True)
            if failed:
                return failed, True
            self._settled(s, path=path)
            return "已打开。", True
        if name in ("click", "input", "select", "highlight") or (name == "scroll" and args.get("index") is not None):
            element = self._element(s, args.get("index", -1))
            if element is None:
                return "没有这个序号的元素，请根据最新观察重新选择。", True
        if name == "click":
            failed = self._act(s, "click", {"ref": element.ref})
            self._settled(s)
            return failed or "已点击。", True
        if name == "input":
            failed = self._act(s, "fill", {"ref": element.ref, "value": str(args.get("text", "")),
                                           "commit": True})
            self._settled(s, waits=2)
            return failed or "已填入。", True
        if name == "select":
            option = str(args.get("option", ""))
            failed = self._act(s, "select", {"ref": element.ref, "value": option, "option_label": option})
            self._settled(s, waits=2)
            return failed or "已选中。", True
        if name == "scroll":
            command = {"direction": "down" if args.get("down", True) else "up", "pages": 1}
            if args.get("index") is not None:
                command["ref"] = element.ref
            failed = self._act(s, "scroll", command)
            return failed or "已滚动。", True
        if name == "highlight":
            failed = self._act(s, "highlight", {"ref": element.ref, "epoch": s.obs.epoch})
            if failed:
                return failed, False
            turn["marked"] = element.name.splitlines()[0][:60] if element.name else "(控件)"
            s.ledger.append("page_handoff_ready", step=s.step, observation_epoch=s.obs.epoch,
                            submitted=False, persisted=False)
            return f"已标出：{turn['marked']}", False
        return f"没有这个工具：{name}", False

    # -- the loop ----------------------------------------------------------------------
    def _run(self, s) -> str:
        handoffs = SessionHandoffPolicy.extending(
            self.application.ports.routes.session_handoff_query_keys)
        s.approvals.start_turn(int((s.started_at + self.config.wall_budget_s) * 1000))
        try:
            s.writes.begin_control()
            s.writes.start_observing()
        except Exception as exc:  # ControlPlaneError; typed by the control plane module
            s.terminal_reason, s.detail = "control_plane_error", str(exc)[:200]
            return "无法开始这次操作。"
        s.ledger.append("observation", epoch=s.obs.epoch, url=s.obs.url,
                        elements=len(s.obs.elements), digest=s.obs.content_digest)
        s.chat = MeasuredChat(AuditedChat(self.chat, s.ledger), s.receipt,
                              remaining_s=s.budget.remaining, budget=s.budget,
                              stopped=s.model_stop.is_set)
        try:
            with s.receipt.stage("business"):
                self.knowledge._load()
        except DataRefused as exc:
            s.terminal_reason, s.detail = "agent_refused", str(exc)[:200]
            return "读取当前账号的菜单或接口说明失败，本次没有执行。"
        if self.conversation_state.turn_count == 0 or not self._messages:
            self._messages = [{"role": "system", "content": SYSTEM.format(
                user=self.actor_name or self.knowledge.user_name or "当前用户",
                date=_today(self.today, self.time_zone), tz=self.time_zone)}]
        turn = {"reads": [], "marked": "", "trace": [], "baseline": _fields(s.obs), "handoffs": handoffs,
                "shown": []}
        prompt = s.message.text
        self._messages.append({"role": "user", "_observation": True,
                               "content": prompt + "\n" + self._page(s, turn)})
        try:
            return self._loop(s, prompt, turn, verified=not self.verify)
        finally:
            if _DEBUG_DIR:
                Path(_DEBUG_DIR).mkdir(parents=True, exist_ok=True)
                (Path(_DEBUG_DIR) / f"{s.run_id}.json").write_text(json.dumps(
                    {"messages": self._messages, "trace": turn["trace"], "shown": turn["shown"],
                     "elements": [[e.ref, e.role, e.name[:40], e.tag, dict(e.attrs)] for e in s.obs.elements]},
                    ensure_ascii=False, indent=1))

    def _page(self, s, turn: dict) -> str:
        text = page_text(s.obs, handoffs=turn["handoffs"], marked=turn["marked"])
        if _DEBUG_DIR:
            turn["shown"].append(text)
        return text

    def _forget_old_observations(self) -> None:
        seen = 0
        for message in reversed(self._messages):
            if message.get("_observation"):
                seen += 1
                if seen > 2:
                    message["content"] = message["content"].split("\n当前页面:")[0] \
                        + "\n（更早的页面观察已省略）"
                    message["_observation"] = False

    def _loop(self, s, prompt: str, turn: dict, *, verified: bool) -> str:
        nudges, taken = 0, 0
        while taken < self.model_steps and not s.terminal_reason:
            taken += 1
            self._forget_old_observations()
            wire = [{k: v for k, v in m.items() if not k.startswith("_")} for m in self._messages]
            try:
                message = s.chat.complete_cancellable(
                    wire, TOOLS, s.run_id, cancelled=s.cancelled, max_tokens=1500,
                    timeout_s=max(1.0, s.budget.remaining()))
            except ProviderError as exc:
                s.ledger.append("slim_model_failed", error_type=type(exc).__name__)
                s.terminal_reason, s.detail = "agent_refused", "the model request failed"
                return "模型请求失败，本次没有完成。"
            calls = message.get("tool_calls") or []
            keep = {"role": "assistant", "content": message.get("content") or ""}
            if calls:
                keep["tool_calls"] = calls
            self._messages.append(keep)
            if not calls:
                nudges += 1
                if nudges > 2:
                    s.terminal_reason = "agent_refused"
                    return (message.get("content") or "").strip() or "本次没有完成。"
                self._messages.append({"role": "user", "content":
                                       "请用工具继续；全部完成或确实做不到时调用 finish。"})
                continue
            finish, changed_page = None, False
            for position, call in enumerate(calls):
                name = (call.get("function") or {}).get("name") or ""
                try:
                    args = json.loads((call.get("function") or {}).get("arguments") or "{}")
                except ValueError:
                    args = {}
                if not isinstance(args, dict):
                    args = {}
                if name == "finish":
                    finish = args
                    self._messages.append({"role": "tool", "tool_call_id": call.get("id"), "content": "已收到。"})
                    continue
                if finish is not None or (position > 0 and changed_page):
                    self._messages.append({"role": "tool", "tool_call_id": call.get("id"),
                                           "content": "未执行：页面已变化，请根据新的观察重新决定。"})
                    continue
                try:
                    text, changed = self._tool(s, name, args, turn)
                except (ValueError, TypeError) as exc:
                    text, changed = f"工具参数有误：{str(exc)[:160]}", False
                s.ledger.append("slim_tool", step=s.step, tool=name, changed=changed, ok=not text.startswith("没有成功"))
                turn["trace"].append(f"{name}({json.dumps(args, ensure_ascii=False)[:120]})"
                                     + (f" 页面随后: {self._query_line(s)}" if changed and name != "observe" else ""))
                changed_page = changed and name != "observe"
                entry = {"role": "tool", "tool_call_id": call.get("id"), "content": text}
                if changed:
                    entry["content"] = text + "\n" + self._page(s, turn)
                    entry["_observation"] = True
                self._messages.append(entry)
                if s.terminal_reason:
                    return "操作没有完成。"
            if finish is None:
                continue
            status, answer = str(finish.get("status") or "done"), str(finish.get("answer") or "").strip()
            if status == "done" and not verified:
                problems = self._verify(s, prompt, answer, turn)
                if problems:
                    s.ledger.append("slim_verify_sent_back", problems=len(problems))
                    self._messages.append({"role": "user", "_observation": True, "content":
                                           "验收没有通过：" + "；".join(problems)
                                           + "。请修正后再 finish；如果确实做不到，用 cannot 或 need_input 如实说明。\n"
                                           + page_text(s.obs, handoffs=turn["handoffs"], marked=turn["marked"])})
                    return self._loop(s, prompt, turn, verified=True)
            if status == "need_input":
                s.terminal_reason = "needs_clarification"
                s.clarification = {"schema_version": 1, "clarification_id": f"{s.run_id}-ask"[:100],
                                   "task_id": s.run_id[:100], "question": (answer or "请补充信息。")[:500],
                                   "response_kind": "free_text", "choices": []}
            elif status == "cannot":
                s.terminal_reason = "agent_refused"
            return answer or "已完成。"
        if not s.terminal_reason:
            s.terminal_reason, s.detail = "agent_refused", "the turn's model steps were used up"
        return "步骤用完，没有完成。"

    @staticmethod
    def _query_line(s) -> str:
        obs = s.obs
        parts = [urlsplit(obs.url).path]
        if obs.page_query is not None:
            parts.append(json.dumps(obs.page_query["parameters"], ensure_ascii=False)[:160])
        if obs.page_read is not None:
            parts.append(f"total={obs.page_read['total']}")
        return " ".join(parts)

    def _verify(self, s, prompt: str, answer: str, turn: dict) -> list[str]:
        obs = s.obs
        now = _fields(obs)
        changed = {k: {"from": turn["baseline"].get(k), "to": v} for k, v in now.items()
                   if k in turn["baseline"] and turn["baseline"][k] != v}
        history = [m["content"].split("\n当前页面:")[0] for m in self._messages
                   if m["role"] == "user" and not m["content"].startswith(("验收", "请用工具"))]
        said = re.sub(r"[\s,，。.：:]", "", " ".join(history))
        stray = [f"{label}：{c['from'] or '(空)'}→{c['to'] or '(空)'}" for label, c in changed.items()
                 if c["to"] and re.sub(r"[\s,，。.：:]", "", str(c["to"])) not in said]
        state = {"path": urlsplit(obs.url).path, "title": obs.title,
                 "selected": list(obs.selected_controls),
                 "list": ({k: obs.page_read[k] for k in ("page", "page_size", "rows", "total")}
                          if obs.page_read is not None else None),
                 "applied_query": obs.page_query, "form": now, "form_changed": changed,
                 "highlighted": turn["marked"], "messages": list(obs.messages[:4]),
                 "page_text": page_text(obs, handoffs=turn["handoffs"])[-2500:]}
        content = ("用户本轮原话：" + prompt
                   + ("\n此前各轮原话：" + " / ".join(history[:-1]) if len(history) > 1 else "")
                   + "\n助手答复：" + answer
                   + "\n助手本轮的操作轨迹：\n" + ("\n".join(turn["trace"][-14:]) or "无")
                   + "\n助手本轮读过的接口：" + ("\n".join(f"call_api({a}) → {r}" for a, r in turn["reads"][-4:]) or "无")
                   + ("\n程序检出：这些被改动字段的新值没有原样出现在用户原话里：" + "；".join(stray)
                      + "。逐个判断：是用户要求的另一种写法（如日期补全年份、名称补全全称）或应用自动联动（合计、天数、自动带出的关联）就不算问题；是助手自己编的值就算问题，要求恢复原值。"
                      if stray else "")
                   + "\n页面真实状态：" + json.dumps(state, ensure_ascii=False)[:7000])
        try:
            message = s.chat.complete_cancellable(
                [{"role": "system", "content": VERIFY_SYSTEM}, {"role": "user", "content": content}],
                None, s.run_id, cancelled=s.cancelled, max_tokens=600,
                timeout_s=max(1.0, s.budget.remaining()))
        except ProviderError:
            return []
        match = re.search(r"\{.*\}", message.get("content") or "", re.S)
        try:
            verdict = json.loads(match.group(0)) if match else {}
        except ValueError:
            verdict = {}
        if verdict.get("ok") is False and verdict.get("problems"):
            return [str(p)[:200] for p in verdict["problems"][:4]]
        return []


__all__ = ["ActorKnowledge", "SlimTurnExecutor", "page_text"]
