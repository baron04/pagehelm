"""Generic, entity-agnostic scripted turn templates and evaluators for the 5 read/page families.

All turns run through the real product stack (`FullStack`, `YudaoHost`, `YudaoTurnExecutor`,
`Panel`) with `ScriptedModel` answering strictly from tool outputs and compiled metadata.
Zero entity names or page route literals appear anywhere in this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import itertools
import json
import re
import time
from typing import Callable
from urllib.parse import parse_qsl, urlsplit

from evals.yudao.write_contract.fullstack import (
    observation_text,
    reply,
)

from .ledger import CellResult
from .oracle import IndependentApiOracle

_CALL_SEQ = itertools.count(1)
_RECEIPT_RE = re.compile(r"\b(ev_[0-9a-f]{6,32}_\d+)\b")
_PAGESIZE_MAX_RE = re.compile(r"pageSize\*?\([^)]*?最大(?:值)?\s*[为:=]?\s*(\d+)")
_PRECOMPUTED_MONTH_RE = re.compile(r"本月[^：\n]*：(\d{4}-\d{2}-\d{2})\s*~\s*(\d{4}-\d{2}-\d{2})")
_TABLE_HANDLE_RE = re.compile(r"本轮行表\s+(t\d+)\b|\"name\"\s*:\s*\"(t\d+)\"")
_SQL_HANDLE_RE = re.compile(r"\"table_ref\"\s*:\s*\"(q\d+)\"")
_CURRENT_PAGE_RE = re.compile(r"^当前页面:\s*(\S+)", re.M)
_LIST_LINE_RE = re.compile(r"^列表:\s*第\s*(\d+)\s*页，每页\s*(\d+)\s*条，本页\s*(\d+)\s*行，共\s*(\d+)\s*条", re.M)
_APPLIED_QUERY_RE = re.compile(r"^页面生效的查询[^:：]*:\s*GET\s+(\S+)\s+(\{.*\})", re.M)
_VERIFIED_COND_RE = re.compile(r"^已核验的列表查询条件:\s*(.+)$", re.M)
_UNTRACED_BLOCK_RE = re.compile(r"以下值在本轮读取中找不到|没有结束：|找不到出处")
_FENCED_JSON_RE = re.compile(r"BEGIN_UNTRUSTED_APPLICATION_DATA\n(.*?)\nEND_UNTRUSTED_APPLICATION_DATA", re.S)
_FIND_HEADER_RE = re.compile(r"^[^\s][^\n]*?（([A-Za-z0-9_/-]+)；页面：[^\n]*?）：(.*)$")
_FIND_ID_RE = re.compile(r"(?:编号\s+|ID=)([^\s））；;)]+)|\(([0-9A-Za-z_-]+)\)")


def _parse_find_record_blocks(out: str) -> dict[str, tuple[str, set[str]]]:
    """Parse entity-scoped candidate blocks and exact bounded IDs from find_record output."""
    m = _FENCED_JSON_RE.search(out or "")
    body = m.group(1) if m else (out or "")
    grouped: dict[str, list[str]] = {}
    current_entity = ""
    for line in body.splitlines():
        hm = _FIND_HEADER_RE.match(line)
        if hm:
            current_entity = hm.group(1).strip("/")
            grouped.setdefault(current_entity, []).append(hm.group(2))
        elif current_entity and line.startswith((" ", "\t")):
            grouped[current_entity].append(line)
        else:
            current_entity = ""
    blocks: dict[str, tuple[str, set[str]]] = {}
    for entity, lines in grouped.items():
        text = "\n".join(lines)
        ids = {g1 or g2 for g1, g2 in _FIND_ID_RE.findall(text) if (g1 or g2)}
        blocks[entity] = (text, ids)
    return blocks


def _date_token_matches(sent_val: str, token: str) -> bool:
    """Match an emitted date/datetime/month param against the requested date or YYYY-MM token."""
    sv = str(sent_val or "").strip()
    tok = str(token or "").strip()
    if not sv or not tok:
        return False
    if sv == tok or sv.startswith(tok + " ") or sv.startswith(tok + "T"):
        return True
    if re.fullmatch(r"\d{4}-\d{2}", tok) and sv.startswith(tok + "-"):
        return True
    return False


def _split_year_month_matches(sent_filters: dict, token: str) -> bool:
    """Accept split year + month filter params only for pure YYYY-MM month targets."""
    if not re.fullmatch(r"\d{4}-\d{2}", token):
        return False
    year_tok, month_tok = token.split("-")
    month_num = str(int(month_tok))
    non_empty = [(str(k), str(v).strip()) for k, v in sent_filters.items() if v is not None and str(v).strip() != ""]
    year_keys = {k for k, v in non_empty if v == year_tok}
    month_keys = {k for k, v in non_empty if v in {month_tok, month_num}}
    return bool(any(yk != mk for yk in year_keys for mk in month_keys))


def _filter_sent_matches_requested(cell: dict, bag: dict, pre_filters: dict, sent_filters: dict) -> tuple[bool, str]:
    """Verify page-emitted GET filter parameters genuinely reflect the requested filter value."""
    non_empty = [
        (str(k), str(v).strip())
        for k, v in sent_filters.items()
        if v is not None and str(v).strip() != ""
    ]
    if not non_empty:
        return False, f"page GET carried no non-empty filter params: before={pre_filters} after={sent_filters}"

    changed = {
        k: v for k, v in sent_filters.items()
        if v is not None and str(v).strip() != "" and str(pre_filters.get(k, "")).strip() != str(v).strip()
    }
    used = str(bag.get("used_value") or cell.get("value") or "").strip()
    mode = str(cell.get("value_mode") or "")
    ctrl = str(cell.get("control_kind") or "")
    qp = str(cell.get("query_param") or "").strip()

    if " ~ " in used or ctrl in {"daterange", "datetimerange", "monthrange"}:
        parts = [p.strip() for p in used.split(" ~ ", 1)]
        if len(parts) != 2 or not all(parts):
            return False, f"invalid requested date range {used!r}"
        start_tok, end_tok = parts
        start_hits = {i for i, (_k, v) in enumerate(non_empty) if _date_token_matches(v, start_tok)}
        end_hits = {i for i, (_k, v) in enumerate(non_empty) if _date_token_matches(v, end_tok)}
        if any(i != j for i in start_hits for j in end_hits):
            return True, ""
        return False, f"page GET missing requested date range {used!r}: sent={sent_filters}"

    if mode in {"explicit_date", "relative_date"}:
        if any(_date_token_matches(v, used) for _k, v in non_empty) or _split_year_month_matches(sent_filters, used):
            return True, ""
        return False, f"page GET missing requested date {used!r}: sent={sent_filters}"

    if mode == "option_label":
        if changed or (qp and any(k == qp or k.startswith(qp + "[") for k, _v in non_empty)):
            return True, ""
        return False, f"page GET did not emit requested option param {qp!r}: before={pre_filters} after={sent_filters}"

    if any(v == used for _k, v in non_empty) and (changed or (qp and any(k == qp and v == used for k, v in non_empty))):
        return True, ""
    return False, f"page GET missing requested filter value {used!r}: before={pre_filters} after={sent_filters}"


def _call(tool_name: str, args: dict) -> dict:
    """Build an OpenAI tool-call dict without kwargs collision (e.g. `find_record(name=...)`)."""
    return {
        "id": f"call_{next(_CALL_SEQ)}",
        "type": "function",
        "function": {"name": tool_name, "arguments": json.dumps(args, ensure_ascii=False)},
    }


def _finish_cannot(answer: str, *, kind: str = "data") -> dict:
    return reply(_call("finish", {
        "status": "cannot",
        "answer": answer,
        "items": [{"item": answer, "kind": kind, "status": "undelivered", "receipts": []}],
    }))


def _finish_done(answer: str, *, kind: str, rcpts: list[str]) -> dict:
    if not rcpts:
        return _finish_cannot(answer, kind=kind)
    return reply(_call("finish", {
        "status": "done",
        "answer": answer,
        "items": [{"item": answer, "kind": kind, "status": "delivered", "receipts": rcpts[-1:]}],
    }))


def _fmt_num(val) -> str:
    """Format numbers >= 1000 with thousands separators while preserving decimals cleanly."""
    if val is None or isinstance(val, bool):
        return str(val)
    d = _parse_decimal(val)
    if d is None:
        return str(val)
    if d == d.to_integral_value():
        iv = int(d)
        return f"{iv:,}" if abs(iv) >= 1000 else str(iv)
    text = format(d.normalize(), "f")
    whole, dot, frac = text.partition(".")
    iv = int(whole)
    whole_fmt = f"{iv:,}" if abs(iv) >= 1000 else whole
    return f"{whole_fmt}.{frac}" if dot else whole_fmt


def _all_receipts(text: str) -> list[str]:
    return list(dict.fromkeys(_RECEIPT_RE.findall(str(text or ""))))


def _all_tool_messages(request: dict) -> list[str]:
    return [str(m.get("content") or "") for m in request.get("messages", ()) if m.get("role") == "tool"]


def _all_user_messages(request: dict) -> list[str]:
    return [str(m.get("content") or "") for m in request.get("messages", ()) if m.get("role") == "user"]


def _is_verifier_request(request: dict) -> bool:
    msgs = request.get("messages", ())
    if not msgs or msgs[0].get("role") != "system":
        return False
    sys_txt = str(msgs[0].get("content") or "")
    return "只输出一个 JSON" in sys_txt or '"ok"' in sys_txt and "problems" in sys_txt


def _normalize_label(text: str) -> str:
    txt = re.sub(r"\s+", "", str(text or ""))
    txt = re.sub(r"[（(][^（）()]{1,12}[）)]$", "", txt)
    return txt.casefold()


def _parse_decimal(val) -> Decimal | None:
    if val is None or isinstance(val, bool) or val == "":
        return None
    s = re.sub(r"[,，\s元分%]", "", str(val))
    try:
        d = Decimal(s)
        return d if d.is_finite() else None
    except InvalidOperation:
        return None


def _extract_vocabulary(tool_text: str) -> tuple[dict, dict, dict]:
    """Parse `columns`, `choices`, and inline `代码列的取值和名称` from `call_api` feedback."""
    columns: dict[str, str] = {}
    choices: dict[str, dict[str, str]] = {}
    inline_codes: dict[str, dict[str, str]] = {}
    for block in _FENCED_JSON_RE.findall(tool_text):
        try:
            obj = json.loads(block)
        except ValueError:
            continue
        if isinstance(obj, dict):
            if isinstance(obj.get("columns"), dict):
                columns.update({str(k): str(v) for k, v in obj["columns"].items()})
            if isinstance(obj.get("choices"), dict):
                for ck, cmap in obj["choices"].items():
                    if isinstance(cmap, dict):
                        choices[str(ck)] = {str(k): str(v) for k, v in cmap.items()}
    m = re.search(r"代码列的取值和名称[^:：]*:\nBEGIN_UNTRUSTED_APPLICATION_DATA\n(.*?)\nEND_UNTRUSTED_APPLICATION_DATA", tool_text, re.S)
    if m:
        for seg in m.group(1).split("；"):
            if ":" not in seg:
                continue
            k, rest = seg.split(":", 1)
            k = k.strip()
            pairs = {}
            for item in rest.split("、"):
                if "=" in item:
                    code, lbl = item.split("=", 1)
                    pairs[code.strip()] = lbl.strip()
            if pairs:
                inline_codes[k] = pairs
    m_page = re.search(r"本页表格列名（接口字段→页面列）：([^\n]+)", tool_text)
    if m_page:
        for pair in m_page.group(1).split("、"):
            if "→" in pair:
                k, lbl = pair.split("→", 1)
                columns[k.strip()] = lbl.strip()
    return columns, choices, inline_codes


def _extract_table_info(tool_text: str) -> dict | None:
    for line in tool_text.splitlines():
        line = line.strip()
        if line.startswith("{") and '"row_count"' in line and '"complete"' in line:
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    return obj
            except ValueError:
                pass
    return None


def _extract_sql_result(tool_text: str) -> dict | None:
    for block in _FENCED_JSON_RE.findall(tool_text):
        try:
            obj = json.loads(block)
            if isinstance(obj, dict) and "columns" in obj and "rows" in obj:
                return obj
        except ValueError:
            continue
    return None


@dataclass
class TurnExecution:
    prompt: str
    reply: str
    timed_out: bool
    final_url: str
    requests: list[dict]
    model_errors: list[str]
    cards: list[dict]
    api_gets_during_turn: list[dict]
    nav_seq_before: int
    nav_seq_after: int


def make_scripted_turn(cell: dict, *, today_str: str) -> tuple[str, Callable[[dict], dict], dict]:
    """Build `(prompt, script_fn, state_bag)` for one census cell."""
    fam = cell["family"]
    bag: dict = {
        "step": 0,
        "finish_attempts": 0,
        "answer_check_blocked": False,
        "answer_check_detail": "",
        "unexpected_error": "",
    }

    if fam == "find":
        prompt = f"查找名称或编号为「{cell['query_text']}」的记录。"

        def script(body: dict) -> dict:
            if _is_verifier_request(body):
                return {"role": "assistant", "content": '{"ok": true, "problems": []}'}
            bag["step"] += 1
            if bag["step"] == 1:
                return reply(_call("find_record", {"name": cell["query_text"]}))
            tool_msgs = _all_tool_messages(body)
            last = tool_msgs[-1] if tool_msgs else ""
            if bag["finish_attempts"] >= 1 and _UNTRACED_BLOCK_RE.search(last):
                bag["answer_check_blocked"] = True
                bag["answer_check_detail"] = last[:300]
                return _finish_cannot("答复检查阻断。")
            bag["find_output"] = last
            rcpts = _all_receipts(last)
            bag["finish_attempts"] += 1
            ans = f"已查找 {cell['query_text']}。"
            if "查找未完成" in last or "查找失败" in last or not rcpts:
                return _finish_cannot(ans)
            return _finish_done(ans, kind="data", rcpts=rcpts)

        return prompt, script, bag

    if fam == "detail":
        prompt = f"读取记录详情并核对字段：{cell.get('record_name') or cell['record_id']}。"

        def script(body: dict) -> dict:
            if _is_verifier_request(body):
                return {"role": "assistant", "content": '{"ok": true, "problems": []}'}
            bag["step"] += 1
            if bag["step"] == 1:
                if cell.get("has_get") and cell.get("get_path"):
                    return reply(_call("call_api", {
                        "path": cell["get_path"],
                        "params": json.dumps({"id": cell["record_id"]}),
                    }))
                params = {"pageNo": 1, "pageSize": 20, **(cell.get("params") or {})}
                return reply(_call("call_api", {
                    "path": cell["list_path"],
                    "params": json.dumps(params, ensure_ascii=False),
                    "fields": cell["fields"],
                }))
            tool_msgs = _all_tool_messages(body)
            last = tool_msgs[-1] if tool_msgs else ""
            if bag["finish_attempts"] >= 1 and _UNTRACED_BLOCK_RE.search(last):
                bag["answer_check_blocked"] = True
                bag["answer_check_detail"] = last[:300]
                return _finish_cannot("答复检查阻断。")
            if bag["step"] == 2:
                bag["api_output"] = last
                bag["table_info"] = _extract_table_info(last)
                if not bag["table_info"]:
                    bag["finish_attempts"] += 1
                    return _finish_cannot("接口读取失败。")
                tname = bag["table_info"]["name"]
                cols_sql = ", ".join(f'"{c}"' for c in cell["fields"])
                if int(bag["table_info"].get("row_count") or 0) <= 1:
                    return reply(_call("sql", {"query": f"SELECT {cols_sql} FROM {tname} LIMIT 1"}))
                rid = str(cell["record_id"]).replace("'", "''")
                return reply(_call("sql", {"query": f"SELECT {cols_sql} FROM {tname} WHERE CAST(id AS TEXT) = '{rid}' LIMIT 1"}))
            sql_res = _extract_sql_result(last) if bag["step"] >= 3 else None
            bag["sql_output"] = last if sql_res else ""
            bag["sql_result"] = sql_res
            rcpt_all = _all_receipts("\n".join(tool_msgs))
            cols, choices, inline_codes = _extract_vocabulary(bag.get("api_output", ""))
            parts = []
            if sql_res and sql_res.get("rows"):
                row_vals = sql_res["rows"][0]
                for fname, fval in zip(cell["fields"], row_vals):
                    flabel = cols.get(fname, fname)
                    shown_val = "" if fval is None else str(fval)
                    cmap = choices.get(fname) or inline_codes.get(fname) or {}
                    if shown_val in cmap:
                        shown_val = f"{cmap[shown_val]}"
                    parts.append(f"{flabel}={shown_val}")
            else:
                parts.append(f"编号={cell['record_id']}")
            ans = "记录详情：" + "，".join(parts) + "。"
            bag["finish_attempts"] += 1
            return _finish_done(ans, kind="data", rcpts=rcpt_all)

        return prompt, script, bag

    if fam == "aggregate":
        col = cell["metric_col"]
        label = cell.get("entity_label") or ""
        prompt = f"统计「{label}」记录行数，并计算 {col} 的合计、最大值和最小值。"

        def script(body: dict) -> dict:
            if _is_verifier_request(body):
                return {"role": "assistant", "content": '{"ok": true, "problems": []}'}
            bag["step"] += 1
            if bag["step"] == 1:
                kw = cell["list_path"].removeprefix("/admin-api/")
                return reply(_call("search_api", {"keyword": kw}))
            tool_msgs = _all_tool_messages(body)
            last = tool_msgs[-1] if tool_msgs else ""
            if bag["finish_attempts"] >= 1 and _UNTRACED_BLOCK_RE.search(last):
                bag["answer_check_blocked"] = True
                bag["answer_check_detail"] = last[:300]
                return _finish_cannot("答复检查阻断。")
            if bag["step"] == 2:
                bag["search_api_output"] = last
                # Determine max pageSize permitted by the model-visible endpoint description.
                max_ps = 500
                matched = False
                for line in last.splitlines():
                    if cell["list_path"] in line:
                        matched = True
                        m = _PAGESIZE_MAX_RE.search(line)
                        if m:
                            max_ps = int(m.group(1))
                            break
                    elif matched and "参数:" in line:
                        m = _PAGESIZE_MAX_RE.search(line)
                        if m:
                            max_ps = int(m.group(1))
                        break
                    elif matched and line.startswith("GET "):
                        matched = False
                bag["chosen_page_size"] = max_ps
                params = dict(cell.get("params") or {})
                if cell.get("paged", True):
                    params["pageNo"] = 1
                    params["pageSize"] = (
                        min(max_ps, 100) if cell.get("field_or_control_type") == "large_table" else max_ps
                    )
                return reply(_call("call_api", {
                    "path": cell["list_path"],
                    "params": json.dumps(params, ensure_ascii=False),
                    "fields": [col],
                }))
            if bag["step"] == 3:
                bag["call_api_output"] = last
                tinfo = _extract_table_info(last)
                bag["table_info"] = tinfo
                if not tinfo:
                    bag["finish_attempts"] += 1
                    return _finish_cannot("接口读取失败。")
                tname = tinfo["name"]
                return reply(_call("sql", {"query": (
                    f'SELECT COUNT(*) AS cnt, COUNT("{col}") AS non_null_cnt, '
                    f'SUM("{col}") AS total_val, MAX("{col}") AS max_val, MIN("{col}") AS min_val '
                    f'FROM {tname}'
                )}))
            if bag["step"] == 4:
                bag["sql_agg_output"] = last
                bag["sql_agg_result"] = _extract_sql_result(last)
                tname = bag["table_info"]["name"]
                return reply(_call("sql", {"query": f'SELECT MAX("{col}") AS max_val FROM {tname}'}))
            bag["sql_tie_output"] = last
            rcpts = _all_receipts("\n".join(tool_msgs))
            sql_res = bag.get("sql_agg_result") or {}
            rows = sql_res.get("rows") or []
            if rows:
                cnt, _nn, total_val, max_val, min_val = rows[0]
                ans = f"共 {_fmt_num(cnt)} 条，最大 {max_val}，最小 {min_val}，{col} 合计 {_fmt_num(total_val)}。"
                if "并列" in last:
                    ans += " 最大值存在并列。"
                bag["finish_attempts"] += 1
                return _finish_done(ans, kind="data", rcpts=rcpts)
            bag["finish_attempts"] += 1
            return _finish_cannot("无法计算聚合统计。")

        return prompt, script, bag

    if fam == "open_page":
        is_record = cell["operation"] == "open_record_page"
        prompt = (
            f"查找「{cell['record_name']}」并打开它的详情页面。"
            if is_record and cell.get("record_name")
            else f"打开「{cell['menu_label']}」页面。"
        )

        def script(body: dict) -> dict:
            if _is_verifier_request(body):
                return {"role": "assistant", "content": '{"ok": true, "problems": []}'}
            bag["step"] += 1
            tool_msgs = _all_tool_messages(body)
            last = tool_msgs[-1] if tool_msgs else ""
            if bag["finish_attempts"] >= 1 and _UNTRACED_BLOCK_RE.search(last):
                bag["answer_check_blocked"] = True
                bag["answer_check_detail"] = last[:300]
                return _finish_cannot("答复检查阻断。", kind="page")
            if bag["step"] == 1:
                if is_record and cell.get("record_name"):
                    return reply(_call("find_record", {"name": cell["record_name"]}))
                return reply(_call("search_menu", {"keyword": cell["menu_label"]}))
            if bag["step"] == 2:
                bag["lookup_output"] = last
                target = cell["route"]
                if is_record and cell.get("record_name"):
                    m_rec = re.search(r"详情页\s+(/\S+?)(?:[）\s]|$)", last)
                    if m_rec:
                        target = m_rec.group(1)
                    else:
                        bag["record_link_missing"] = True
                        bag["finish_attempts"] += 1
                        return _finish_cannot("未找到详情页地址。", kind="page")
                bag["target_path"] = target
                return reply(_call("goto", {"path": target}))
            if bag["step"] == 3:
                bag["first_goto_output"] = last
                bag["first_obs_text"] = observation_text(body)
                # Re-open the exact same page to verify no full reload (Section 3.1 & Step 0).
                return reply(_call("goto", {"path": bag.get("target_path", cell["route"])}))
            bag["second_goto_output"] = last
            bag["second_obs_text"] = observation_text(body)
            rcpts = _all_receipts(last)
            ans = f"已打开「{cell.get('menu_label') or cell['route']}」页面。"
            bag["finish_attempts"] += 1
            return _finish_done(ans, kind="page", rcpts=rcpts)

        return prompt, script, bag

    if fam == "filter_page":
        val_phrase = "本月" if cell["value_mode"] == "relative_date" else cell["value"]
        prompt = f"在「{cell['route']}」页面按「{cell['control_label']}」筛选「{val_phrase}」并调出列表。"

        def script(body: dict) -> dict:
            if _is_verifier_request(body):
                return {"role": "assistant", "content": '{"ok": true, "problems": []}'}
            bag["step"] += 1
            tool_msgs = _all_tool_messages(body)
            last = tool_msgs[-1] if tool_msgs else ""
            if bag["finish_attempts"] >= 1 and _UNTRACED_BLOCK_RE.search(last):
                bag["answer_check_blocked"] = True
                bag["answer_check_detail"] = last[:300]
                return _finish_cannot("答复检查阻断。", kind="page")
            if bag["step"] == 1:
                return reply(_call("goto", {"path": cell["route"]}))
            if bag["step"] == 2:
                bag["goto_output"] = last
                fill_val = cell["value"]
                if cell["value_mode"] == "relative_date":
                    user_text = "\n".join(_all_user_messages(body))
                    m = _PRECOMPUTED_MONTH_RE.search(user_text)
                    if not m:
                        bag["precomputed_missing"] = True
                        bag["finish_attempts"] += 1
                        return _finish_cannot("未找到预换算月份范围。", kind="page")
                    if cell.get("is_month_control"):
                        fill_val = f"{m.group(1)[:7]} ~ {m.group(2)[:7]}" if cell["control_kind"] == "daterange" else m.group(1)[:7]
                    else:
                        fill_val = f"{m.group(1)} ~ {m.group(2)}" if cell["control_kind"] == "daterange" else m.group(1)
                bag["used_value"] = fill_val
                bag["fill_sent_ts"] = time.time()
                return reply(_call("fill_fields", {
                    "fields": [{"label": cell["control_label"], "value": fill_val}],
                    "then_click": cell["search_button"],
                }))
            bag["fill_output"] = last
            bag["after_obs_text"] = observation_text(body)
            rcpts = _all_receipts(last)
            ans = f"已按「{cell['control_label']}={bag.get('used_value', cell['value'])}」调出列表。"
            bag["finish_attempts"] += 1
            return _finish_done(ans, kind="page", rcpts=rcpts)

        return prompt, script, bag

    raise ValueError(f"unsupported family: {fam}")


def _fail(cell: dict, reason_code: str, detail: str, elapsed_s: float, *, attempts: int = 1, flaky: bool = False) -> CellResult:
    return CellResult(
        cell_id=cell["cell_id"],
        family=cell["family"],
        entity=cell["entity"],
        domain=cell["domain"],
        operation=cell["operation"],
        field_or_control=cell["field_or_control"],
        field_or_control_type=cell["field_or_control_type"],
        status="fail",
        reason_code=reason_code,
        detail=detail[:400],
        elapsed_s=round(elapsed_s, 3),
        attempts=attempts,
        flaky=flaky,
    )


def _pass(cell: dict, detail: str, elapsed_s: float, *, attempts: int = 1, flaky: bool = False) -> CellResult:
    return CellResult(
        cell_id=cell["cell_id"],
        family=cell["family"],
        entity=cell["entity"],
        domain=cell["domain"],
        operation=cell["operation"],
        field_or_control=cell["field_or_control"],
        field_or_control_type=cell["field_or_control_type"],
        status="pass",
        detail=detail[:300],
        elapsed_s=round(elapsed_s, 3),
        attempts=attempts,
        flaky=flaky,
    )


def _unsupported(cell: dict, reason_code: str, detail: str, elapsed_s: float) -> CellResult:
    return CellResult(
        cell_id=cell["cell_id"],
        family=cell["family"],
        entity=cell["entity"],
        domain=cell["domain"],
        operation=cell["operation"],
        field_or_control=cell["field_or_control"],
        field_or_control_type=cell["field_or_control_type"],
        status="unsupported",
        reason_code=reason_code,
        detail=detail[:300],
        elapsed_s=round(elapsed_s, 3),
    )


def _untested(cell: dict, reason: str, elapsed_s: float = 0.0) -> CellResult:
    return CellResult(
        cell_id=cell["cell_id"],
        family=cell["family"],
        entity=cell["entity"],
        domain=cell["domain"],
        operation=cell["operation"],
        field_or_control=cell["field_or_control"],
        field_or_control_type=cell["field_or_control_type"],
        status="untested",
        reason_code=reason,
        detail=reason,
        elapsed_s=round(elapsed_s, 3),
    )


def _parse_page_receipt(obs_text: str) -> dict:
    route_m = _CURRENT_PAGE_RE.search(obs_text)
    list_m = _LIST_LINE_RE.search(obs_text)
    query_m = _APPLIED_QUERY_RE.search(obs_text)
    cond_m = _VERIFIED_COND_RE.search(obs_text)
    qparams = {}
    if query_m:
        try:
            qparams = json.loads(query_m.group(2))
        except ValueError:
            qparams = {}
    return {
        "route": route_m.group(1) if route_m else "",
        "list": ({
            "page": int(list_m.group(1)),
            "page_size": int(list_m.group(2)),
            "rows": int(list_m.group(3)),
            "total": int(list_m.group(4)),
        } if list_m else None),
        "query_path": query_m.group(1) if query_m else "",
        "query_params": qparams,
        "verified_conds": cond_m.group(1) if cond_m else "",
    }


def _check_delivery_accepted(exe: TurnExecution, bag: dict) -> tuple[bool, str]:
    if bag.get("answer_check_blocked"):
        return False, "answer_check_false_block:" + bag.get("answer_check_detail", "")
    for req in exe.requests:
        for m in req.get("messages", ()):
            if m.get("role") == "tool":
                txt = str(m.get("content") or "")
                if _UNTRACED_BLOCK_RE.search(txt):
                    return False, "delivery_blocked:" + txt[:200]
    if exe.timed_out:
        return False, "turn_timed_out"
    if ("没有通过交付检查" in exe.reply or "找不到出处" in exe.reply
            or "未在本轮读取中找到出处" in exe.reply or "标为未交付" in exe.reply):
        return False, "reply_delivery_rejected:" + exe.reply[:200]
    if any(c.get("kind") == "write" for c in exe.cards):
        return False, "unexpected_write_approval_card"
    return True, ""


def evaluate_cell(
    cell: dict,
    exe: TurnExecution,
    bag: dict,
    *,
    oracle: IndependentApiOracle,
    route_snapshot: dict | None,
    elapsed_s: float,
) -> CellResult:
    """Judge one completed turn against independent oracle truth."""
    fam = cell["family"]
    if exe.timed_out:
        return _fail(cell, "tool_error", "turn_timed_out", elapsed_s)
    if any(c.get("kind") == "write" for c in exe.cards):
        return _fail(cell, "tool_error", "unexpected_write_approval_card", elapsed_s)
    if exe.model_errors:
        return _fail(cell, "tool_error", "; ".join(exe.model_errors)[:300], elapsed_s)

    if fam == "find":
        out = bag.get("find_output", "")
        if not out:
            return _fail(cell, "tool_error", "missing find_record output", elapsed_s)
        if "查找未完成" in out or "查找失败" in out:
            return _fail(cell, "api_rejected", out[:240], elapsed_s)
        # Check entity-scoped candidates and exact multi-entity headers.
        target_entity = str(cell.get("entity") or "").strip("/")
        expected_id = str(cell.get("expected_id") or "").strip()
        blocks = _parse_find_record_blocks(out)
        block_text, entity_ids = blocks.get(target_entity, ("", set()))
        if expected_id:
            if target_entity not in blocks or expected_id not in entity_ids:
                # Verify on live oracle whether the record still exists under this actor.
                live = oracle.fetch_all(cell["list_path"], cell.get("params") or {}, paged=True, max_rows=500)
                if not any(str(r.get("id")) == expected_id for r in live.get("rows") or ()):
                    return _untested(cell, "record_absent_on_env", elapsed_s)
                return _fail(
                    cell,
                    "candidate_missing",
                    f"expected id {expected_id} for {target_entity} not in find_record output: {out[:200]}",
                    elapsed_s,
                )
        else:
            query_text = str(cell.get("query_text") or "").strip()
            if target_entity not in blocks or (query_text and query_text not in block_text):
                return _fail(
                    cell,
                    "candidate_missing",
                    f"expected entity {target_entity} match for {query_text!r} not in find_record output: {out[:200]}",
                    elapsed_s,
                )
        multi = [str(e).strip("/") for e in (cell.get("expected_multi_entities") or ()) if str(e).strip("/")]
        if len(multi) >= 2:
            shown_multi = [e for e in multi if e in blocks]
            if len(shown_multi) < min(len(multi), 4):
                missing_ents = [e for e in multi if e not in blocks]
                return _fail(cell, "candidate_missing", f"same-name entities missing: {missing_ents}", elapsed_s)
        ok_deliv, deliv_err = _check_delivery_accepted(exe, bag)
        if not ok_deliv:
            return _fail(cell, "answer_check_false_block", deliv_err, elapsed_s)
        return _pass(cell, f"matched id {expected_id}", elapsed_s)

    if fam == "detail":
        out = bag.get("api_output", "")
        if not out:
            return _fail(cell, "tool_error", "missing call_api output", elapsed_s)
        if "接口拒绝：" in out or "此只读接口明确拒绝了当前账号" in out:
            return _fail(cell, "api_rejected", out[:240], elapsed_s)
        if "这个接口读取时会写入数据，助手不允许调用" in out:
            return _unsupported(cell, "writing_get_fenced", out[:240], elapsed_s)
        if "没有这个只读接口" in out:
            return _unsupported(cell, "endpoint_not_published", out[:240], elapsed_s)

        # Fetch independent raw record from the same environment.
        raw_row = None
        if cell.get("has_get") and cell.get("get_path"):
            raw_row = oracle.fetch_detail(cell["get_path"], cell["record_id"])
        if raw_row is None:
            listed = oracle.fetch_all(cell["list_path"], cell.get("params") or {}, paged=True, max_rows=2000)
            raw_row = next((r for r in listed.get("rows") or () if str(r.get("id")) == str(cell["record_id"])), None)
        if raw_row is None:
            return _untested(cell, "oracle_record_unavailable", elapsed_s)

        dom = (route_snapshot or {}).get("dom") or {}
        headers = list(dom.get("headers") or ())
        rendered_rows = list(dom.get("rows") or ())
        live_gets = [g for g in (route_snapshot or {}).get("api_gets") or () if g.get("path") == cell["list_path"]]
        if not headers or not rendered_rows:
            return _untested(cell, "page_missing_rendered_table", elapsed_s)

        # Match rendered row with its raw API row from the page's own GET response.
        matched_rendered = None
        if live_gets and live_gets[0].get("sample_rows"):
            for raw_page_row, dom_row in zip(live_gets[0]["sample_rows"], rendered_rows):
                if str(raw_page_row.get("id")) == str(cell["record_id"]):
                    matched_rendered = (raw_page_row, dom_row)
                    break
            if matched_rendered is None and live_gets[0]["sample_rows"] and rendered_rows:
                matched_rendered = (live_gets[0]["sample_rows"][0], rendered_rows[0])

        columns, choices, inline_codes = _extract_vocabulary(out)
        sql_res = bag.get("sql_result") or {}
        sql_row = dict(zip(sql_res.get("columns") or (), (sql_res.get("rows") or [[]])[0]))
        dicts = oracle.dict_map()

        # 1) Check coded field label against live rendered cell & dicts.
        coded = cell.get("coded_field") or ""
        if coded and coded in raw_row and raw_row[coded] not in (None, ""):
            raw_code = str(raw_row[coded])
            prod_map = choices.get(coded) or inline_codes.get(coded) or {}
            prod_label = prod_map.get(raw_code, "")
            # Locate rendered cell if the column is visible on the page.
            rendered_label = ""
            if matched_rendered is not None:
                raw_p, dom_r = matched_rendered
                if str(raw_p.get(coded)) == raw_code and coded in columns:
                    hdr = columns[coded]
                    if hdr in dom_r.get("by_header", {}):
                        rendered_label = str(dom_r["by_header"][hdr] or "").strip()
            if rendered_label in {"__SWITCH_TRUE__", "__SWITCH_FALSE__"}:
                rendered_label = ""
            if rendered_label and rendered_label != raw_code and not rendered_label.isdigit():
                if not prod_label:
                    return _fail(cell, "undecoded", f"{coded}={raw_code} undecoded; page shows {rendered_label!r}", elapsed_s)
                if prod_label != rendered_label and rendered_label not in prod_label and prod_label not in rendered_label:
                    return _fail(cell, "label_mismatch", f"{coded}={raw_code} product={prod_label!r} vs page={rendered_label!r}", elapsed_s)

        # 2) Check numeric / money field scale & value against raw / rendered cell.
        num_f = cell.get("numeric_field") or ""
        if num_f and num_f in sql_row and num_f in raw_row:
            prod_dec = _parse_decimal(sql_row[num_f])
            raw_dec = _parse_decimal(raw_row[num_f])
            if prod_dec is not None and raw_dec is not None:
                rendered_num = None
                if matched_rendered is not None and num_f in columns:
                    hdr = columns[num_f]
                    cell_txt = str((matched_rendered[1].get("by_header") or {}).get(hdr) or "")
                    rendered_num = _parse_decimal(cell_txt)
                if rendered_num is not None and prod_dec != rendered_num:
                    if rendered_num != 0 and (prod_dec / Decimal(100) == rendered_num or prod_dec * Decimal(100) == rendered_num):
                        return _fail(cell, "unit_mismatch", f"{num_f} product={prod_dec} vs page={rendered_num}", elapsed_s)
                    return _fail(cell, "value_mismatch", f"{num_f} product={prod_dec} vs page={rendered_num}", elapsed_s)

        # 3) Check column labels against rendered page headers where mapped.
        norm_headers = {_normalize_label(h): h for h in headers if h}
        verified_cols = 0
        for fname in cell["fields"]:
            if fname in columns:
                lbl = columns[fname]
                if _normalize_label(lbl) in norm_headers:
                    verified_cols += 1
        if not verified_cols and not coded:
            return _untested(cell, "tested_columns_not_in_rendered_headers", elapsed_s)

        # 4) Check scalar equality for SQL row vs raw_row on non-numeric/non-coded fields.
        for fname in cell["fields"]:
            if fname in {coded, num_f} or fname not in sql_row or fname not in raw_row:
                continue
            sv, rv = sql_row[fname], raw_row[fname]
            if isinstance(rv, (int, float, str)) and sv is not None and str(sv) != str(rv):
                # Allow formatted timestamp string vs epoch ms
                if not (isinstance(rv, int) and re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", str(sv))):
                    return _fail(cell, "value_mismatch", f"{fname}: product={sv!r} vs raw={rv!r}", elapsed_s)

        del dicts
        ok_deliv, deliv_err = _check_delivery_accepted(exe, bag)
        if not ok_deliv:
            return _fail(cell, "answer_check_false_block", deliv_err, elapsed_s)
        return _pass(cell, "detail values, labels, and delivery check match", elapsed_s)

    if fam == "aggregate":
        call_out = bag.get("call_api_output", "")
        if not call_out:
            return _fail(cell, "tool_error", "missing call_api output", elapsed_s)
        if "pageSize: maximum" in call_out or "pageSize:" in call_out or "每页条数最大" in call_out:
            return _fail(cell, "page_size_rejected", call_out[:240], elapsed_s)
        if "接口拒绝：" in call_out or "此只读接口明确拒绝了当前账号" in call_out:
            return _fail(cell, "api_rejected", call_out[:240], elapsed_s)
        if "这个接口读取时会写入数据，助手不允许调用" in call_out:
            return _unsupported(cell, "writing_get_fenced", call_out[:240], elapsed_s)
        tinfo = bag.get("table_info")
        if not tinfo:
            return _fail(cell, "tool_error", f"no TurnTables payload: {call_out[:200]}", elapsed_s)

        oracle_res = oracle.fetch_all(cell["list_path"], cell.get("params") or {}, paged=cell.get("paged", True), max_rows=30000)
        if oracle_res.get("error"):
            return _untested(cell, f"oracle_error:{oracle_res['error'][:80]}", elapsed_s)
        if not tinfo.get("complete") or int(tinfo.get("row_count") or 0) < int(oracle_res["total"]):
            return _fail(
                cell,
                "incomplete_read",
                f"complete={tinfo.get('complete')} row_count={tinfo.get('row_count')} oracle_total={oracle_res['total']}",
                elapsed_s,
            )

        stats = oracle.aggregate_stats(oracle_res["rows"], cell["metric_col"])
        if stats is None:
            return _untested(cell, "oracle_no_numeric_values", elapsed_s)
        sql_res = bag.get("sql_agg_result") or {}
        rows = sql_res.get("rows") or []
        if not rows:
            return _fail(cell, "tool_error", f"sql aggregate failed: {bag.get('sql_agg_output', '')[:200]}", elapsed_s)
        cnt, _nn, total_val, max_val, min_val = rows[0]
        if int(cnt) != int(stats["row_count"]):
            return _fail(cell, "value_mismatch", f"count={cnt} vs oracle={stats['row_count']}", elapsed_s)

        prod_sum = _parse_decimal(total_val)
        ora_sum = Decimal(stats["sum"])
        prod_max = _parse_decimal(max_val)
        ora_max = Decimal(stats["max"])
        prod_min = _parse_decimal(min_val)
        ora_min = Decimal(stats["min"])
        if prod_sum is None or prod_max is None or prod_min is None:
            return _fail(cell, "value_mismatch", f"non-numeric sql aggregate result: {rows[0]}", elapsed_s)
        # Allow exact /100 conversion when product's VerifiedMoneyReview converted fen -> yuan.
        sum_ok = prod_sum == ora_sum or (ora_sum != 0 and prod_sum == (ora_sum / Decimal(100)).normalize())
        max_ok = prod_max == ora_max or (ora_max != 0 and prod_max == (ora_max / Decimal(100)).normalize())
        min_ok = prod_min == ora_min or (ora_min != 0 and prod_min == (ora_min / Decimal(100)).normalize())
        if not (sum_ok and max_ok and min_ok):
            return _fail(
                cell,
                "value_mismatch",
                f"sql=({prod_sum},{max_val},{min_val}) vs oracle=({ora_sum},{ora_max},{ora_min})",
                elapsed_s,
            )

        if stats["max_ties"] >= 2 and "并列" not in bag.get("sql_tie_output", ""):
            return _fail(cell, "missing_tie_note", f"oracle max_ties={stats['max_ties']} without tie_note", elapsed_s)

        ok_deliv, deliv_err = _check_delivery_accepted(exe, bag)
        if not ok_deliv:
            return _fail(cell, "answer_check_false_block", deliv_err, elapsed_s)
        return _pass(cell, f"count={cnt}, sum={prod_sum}, max={prod_max}, min={prod_min}", elapsed_s)

    if fam == "open_page":
        if bag.get("record_link_missing"):
            return _fail(cell, "candidate_missing", bag.get("lookup_output", "")[:240], elapsed_s)
        target_path = bag.get("target_path") or cell["route"]
        final_path = urlsplit(exe.final_url).path
        if final_path != target_path:
            return _fail(cell, "page_not_reached", f"final={final_path!r} expected={target_path!r}", elapsed_s)
        if exe.nav_seq_after != exe.nav_seq_before:
            return _fail(cell, "page_reloaded", f"nav_seq changed {exe.nav_seq_before}->{exe.nav_seq_after} on same-path reopen", elapsed_s)

        # Verify model-facing page receipt against page's own GET request.
        obs = _parse_page_receipt(bag.get("second_obs_text") or bag.get("first_obs_text") or "")
        if obs["route"] and obs["route"] != target_path:
            return _fail(cell, "receipt_mismatch", f"obs route={obs['route']!r} vs final={target_path!r}", elapsed_s)
        list_path = cell.get("list_path") or ""
        matching_gets = [g for g in exe.api_gets_during_turn if g.get("path") == list_path and g.get("code") == 0]
        if obs["list"] and matching_gets:
            last_g = matching_gets[-1]
            if obs["list"]["total"] != last_g.get("total") or obs["list"]["rows"] != last_g.get("row_count"):
                return _fail(
                    cell,
                    "receipt_mismatch",
                    f"obs list={obs['list']} vs page GET total={last_g.get('total')} rows={last_g.get('row_count')}",
                    elapsed_s,
                )
        ok_deliv, deliv_err = _check_delivery_accepted(exe, bag)
        if not ok_deliv:
            return _fail(cell, "answer_check_false_block", deliv_err, elapsed_s)
        return _pass(cell, f"reached {target_path}, no reload, receipt matched", elapsed_s)

    if fam == "filter_page":
        if bag.get("precomputed_missing"):
            return _fail(cell, "tool_error", "precomputed date range missing from turn prompt", elapsed_s)
        fill_out = bag.get("fill_output", "")
        if not fill_out:
            return _fail(cell, "tool_error", "missing fill_fields output", elapsed_s)
        if "没有这个字段" in fill_out or "没有找到" in fill_out or "屏幕上有多个同名字段" in fill_out:
            return _fail(cell, "control_unavailable", fill_out[:240], elapsed_s)
        if "未执行：" in fill_out or "未选择：" in fill_out or "值写成" in fill_out:
            return _fail(cell, "value_rejected", fill_out[:240], elapsed_s)

        list_path = cell["list_path"]
        fill_ts = float(bag.get("fill_sent_ts") or 0.0)
        pre_gets = [g for g in exe.api_gets_during_turn if g.get("path") == list_path and g.get("code") == 0 and g.get("t", 0.0) < fill_ts]
        post_gets = [g for g in exe.api_gets_during_turn if g.get("path") == list_path and g.get("code") == 0 and g.get("t", 0.0) >= fill_ts]
        if not post_gets:
            return _fail(cell, "filter_ineffective", f"no page list GET to {list_path} emitted after fill_fields", elapsed_s)
        last_g = post_gets[-1]
        pre_filters = {k: v for k, v in ((pre_gets[-1].get("params") if pre_gets else {}) or {}).items() if k not in {"pageNo", "pageSize"}}
        sent_filters = {k: v for k, v in (last_g.get("params") or {}).items() if k not in {"pageNo", "pageSize"}}
        ok_filter, filter_err = _filter_sent_matches_requested(cell, bag, pre_filters, sent_filters)
        if not ok_filter:
            return _fail(cell, "filter_ineffective", filter_err, elapsed_s)

        # Independent API read using the exact filter params the page sent.
        raw_params: dict = {}
        for k, v in sent_filters.items():
            m_arr = re.fullmatch(r"(.+)\[(\d+)\]", k)
            if m_arr:
                raw_params.setdefault(m_arr.group(1), []).append(v)
            else:
                raw_params[k] = v
        ora = oracle.fetch_all(list_path, raw_params, paged=True, max_rows=5000)
        if ora.get("error"):
            return _untested(cell, f"oracle_error:{ora['error'][:80]}", elapsed_s)
        if last_g.get("total") is not None and int(last_g["total"]) != int(ora["total"]):
            return _fail(
                cell,
                "filter_ineffective",
                f"page total={last_g.get('total')} vs oracle={ora['total']}",
                elapsed_s,
            )
        ora_ids = {str(r.get("id")) for r in ora.get("rows") or () if r.get("id") is not None}
        page_ids = [str(x) for x in last_g.get("row_ids") or ()]
        if page_ids and ora_ids and any(pid not in ora_ids for pid in page_ids):
            return _fail(
                cell,
                "filter_ineffective",
                f"page returned rows not in independent oracle filtered set",
                elapsed_s,
            )
        # Verify product's page receipt agrees with the page's own GET request.
        obs = _parse_page_receipt(bag.get("after_obs_text") or "")
        if obs["route"] != cell["route"]:
            return _fail(cell, "receipt_mismatch", f"obs route={obs['route']!r} vs expected={cell['route']!r}", elapsed_s)
        expects_list_receipt = (
            "pageNo" in (last_g.get("params") or {})
            and int(last_g.get("total") or 0) > 0
        )
        if obs["list"]:
            if obs["list"]["total"] != last_g.get("total") or obs["list"]["rows"] != last_g.get("row_count"):
                return _fail(
                    cell,
                    "receipt_mismatch",
                    f"obs list={obs['list']} vs page GET total={last_g.get('total')} rows={last_g.get('row_count')}",
                    elapsed_s,
                )
        elif expects_list_receipt:
            return _fail(cell, "receipt_mismatch", "product observation missing list receipt after filter", elapsed_s)
        if not obs["query_path"] or obs["query_path"] != list_path:
            return _fail(cell, "receipt_mismatch", f"obs query_path={obs['query_path']!r} vs {list_path!r}", elapsed_s)
        obs_filters = {k: str(v) for k, v in (obs["query_params"] or {}).items() if k not in {"pageNo", "pageSize"}}
        exp_filters = {k: str(v) for k, v in sent_filters.items() if v != ""}
        for k, v in exp_filters.items():
            if obs_filters.get(k) != v:
                return _fail(
                    cell,
                    "receipt_mismatch",
                    f"obs query_params[{k!r}]={obs_filters.get(k)!r} vs page GET {v!r}",
                    elapsed_s,
                )

        ok_deliv, deliv_err = _check_delivery_accepted(exe, bag)
        if not ok_deliv:
            return _fail(cell, "answer_check_false_block", deliv_err, elapsed_s)
        return _pass(cell, f"filtered {cell['control_label']} -> total={last_g.get('total')}", elapsed_s)

    raise ValueError(f"unknown family: {fam}")
