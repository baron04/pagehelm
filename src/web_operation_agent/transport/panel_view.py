"""Server-owned projection of session events onto the assistant panel's timeline.

The embedded panel is a view over server-owned session state. Everything it
renders in the timeline is produced here, so that a full page navigation replays the same
timeline from the server instead of losing it with the previous document (F8a), and so that
no user-visible string or bound is written down twice (F8b).

Navigation targets are separately issued from actor-read application results. They are
links for a user's click, never an assistant action envelope or an approval capability.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from importlib.resources import files
from typing import Any
from urllib.parse import urlsplit

from ..privacy import redact_sensitive_assignments, redact_session_handoffs
from ..schemas import TaskEvent

_CONTRACT_RESOURCE = ("web_runtime", "panel_contract.json")


@lru_cache(maxsize=1)
def panel_contract() -> dict:
    """The single contract shared by the server, the Runtime and the React panel."""
    raw = files("web_operation_agent").joinpath(*_CONTRACT_RESOURCE).read_text(encoding="utf-8")
    contract = json.loads(raw)
    _validate_contract(contract)
    return contract


def _validate_contract(contract: dict) -> None:
    states, tones = contract["panel_states"], set(contract["tones"])
    unknown_tone = sorted(set(states.values()) - tones)
    if unknown_tone:
        raise ValueError(f"panel state tone is not in the contract: {unknown_tone[0]}")
    for name, mapping in contract["progress_states"].items():
        if mapping["panel_state"] not in states:
            raise ValueError(f"progress state {name} maps to an unknown panel state")
        if mapping["tone"] not in tones:
            raise ValueError(f"progress state {name} carries an unknown tone")
    for code, state in contract["terminal_panel_state_by_code"].items():
        if state not in states:
            raise ValueError(f"terminal code {code} maps to an unknown panel state")
        if code not in contract["terminal_error_codes"]:
            raise ValueError(f"terminal code {code} is not listed as terminal")
    for code, state in contract["nonterminal_error_states"].items():
        if (state not in states or code in contract["terminal_error_codes"]
                or code not in contract["notices"]):
            raise ValueError(f"nonterminal code {code} needs a valid state and guidance")
    if not all(isinstance(code, str) and code for code in contract["lapse_codes"]):
        raise ValueError("lapse codes must be non-empty strings")
    if not set(contract["notices"]) >= set(contract["terminal_error_codes"]):
        raise ValueError("every terminal transport code needs user-visible guidance")
    # The panel falls back to this one when a code arrives that nothing anticipated. It
    # used to fall back to `reconnecting`, which told a person their connection had
    # dropped when what had actually happened was a server-side failure.
    if "unexpected" not in contract["notices"]:
        raise ValueError("the panel needs a notice for an unanticipated failure code")
    unknown_message = sorted(
        set(contract["progress_messages"]) - {"comment"} - set(contract["progress_states"]))
    if unknown_message:
        raise ValueError(f"progress message names an unknown state: {unknown_message[0]}")


def progress_message(state: str) -> str:
    """The server-owned prose for a progress state that publishes none of its own."""
    return str(panel_contract()["progress_messages"].get(state) or "")


def panel_limits() -> dict[str, int]:
    return dict(panel_contract()["limits"])


def panel_labels(overrides: dict[str, str] | None = None) -> dict[str, str]:
    labels = dict(panel_contract()["label_keys"])
    if overrides:
        labels.update(overrides)
    return labels


def label_keys() -> frozenset[str]:
    return frozenset(panel_contract()["label_keys"])


def _clip(value: Any, limit: int) -> str:
    text = redact_sensitive_assignments(redact_session_handoffs(str(value or "")))
    text = "".join(character for character in text
                   if character == "\n" or ord(character) >= 32)
    return text[:limit]


_HEX_SHA256_LEN = 64


def _is_hex_sha256(value: object) -> bool:
    return (isinstance(value, str) and len(value) == _HEX_SHA256_LEN
            and all(ch in "0123456789abcdef" for ch in value))


def _reconciliation_projection(payload: dict, limits: dict[str, int]) -> dict | None:
    raw = payload.get("reconciliation")
    if not isinstance(raw, dict):
        return None
    outcome = raw.get("outcome")
    settled = raw.get("settled")
    if outcome not in {"completed", "failed", "unknown"} or type(settled) is not bool:
        return None
    entry: dict[str, Any] = {"outcome": outcome, "settled": settled}
    message = raw.get("message")
    if isinstance(message, str) and message.strip():
        entry["message"] = _clip(message, limits["notice_max_chars"])
    return entry


def _nonneg_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _debug_trace_projection(payload: dict) -> dict[str, Any] | None:
    """Bound and redact an optional local-dev ARC-08 trace summary onto a terminal entry."""
    raw = payload.get("debug_trace")
    if not isinstance(raw, dict):
        return None
    projected: dict[str, Any] = {}
    for key in ("first_route_kind", "stop_reason"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            projected[key] = _clip(value.strip(), 120)
    for key in ("wall_ms", "model_calls"):
        num = _nonneg_int(raw.get(key))
        if num is not None:
            projected[key] = num
    raw_stages = raw.get("stage_ms")
    if isinstance(raw_stages, dict):
        stages: dict[str, int] = {}
        for k, v in list(raw_stages.items())[:24]:
            num = _nonneg_int(v)
            if isinstance(k, str) and k.strip() and num is not None:
                stages[_clip(k.strip(), 64)] = num
        if stages:
            projected["stage_ms"] = stages
    raw_tokens = raw.get("tokens")
    if isinstance(raw_tokens, dict):
        tokens: dict[str, int] = {}
        for tk in ("input", "output", "total"):
            num = _nonneg_int(raw_tokens.get(tk))
            if num is not None:
                tokens[tk] = num
        if tokens:
            projected["tokens"] = tokens
    raw_chain = raw.get("tool_chain")
    if isinstance(raw_chain, list):
        chain: list[dict[str, Any]] = []
        for item in raw_chain[:40]:
            if not isinstance(item, dict):
                continue
            tool = item.get("tool")
            elapsed = _nonneg_int(item.get("elapsed_ms"))
            if not isinstance(tool, str) or not tool.strip() or elapsed is None:
                continue
            entry: dict[str, Any] = {"tool": _clip(tool.strip(), 80), "elapsed_ms": elapsed}
            step_num = _nonneg_int(item.get("step"))
            if step_num is not None:
                entry["step"] = step_num
            if isinstance(item.get("ok"), bool):
                entry["ok"] = item["ok"]
            err = item.get("error_code")
            if isinstance(err, str) and err.strip():
                entry["error_code"] = _clip(err.strip(), 80)
            chain.append(entry)
        if chain:
            projected["tool_chain"] = chain
    raw_checks = raw.get("checks")
    if isinstance(raw_checks, list):
        checks: list[dict[str, Any]] = []
        for item in raw_checks[:20]:
            if not isinstance(item, dict):
                continue
            check = item.get("check")
            outcome = item.get("outcome")
            if not (isinstance(check, str) and check.strip()
                    and isinstance(outcome, str) and outcome.strip()):
                continue
            entry: dict[str, Any] = {
                "check": _clip(check.strip(), 80),
                "outcome": _clip(outcome.strip(), 40),
            }
            attempt = _nonneg_int(item.get("attempt"))
            if attempt is not None:
                entry["attempt"] = attempt
            if isinstance(item.get("answer_preserved"), bool):
                entry["answer_preserved"] = item["answer_preserved"]
            checks.append(entry)
        if checks:
            projected["checks"] = checks
    return projected or None


_TABLE_MAX_COLS = 24
_TABLE_MAX_ROWS = 2000
_TABLE_CELL_CHARS = 300
_INPUT_MAX_OPTIONS = 12
_HANDOFF_MAX_FIELDS = 24
_HANDOFF_MAX_RESTRICTED = 12
_SECRET_FIELD_RE = re.compile(r"(?:password|passwd|secret|token|密码|口令)", re.IGNORECASE)


def _table_projection(payload: dict) -> dict[str, Any] | None:
    """Project a bounded structured table payload directly onto a terminal result entry."""
    raw = payload.get("table")
    if not isinstance(raw, dict):
        return None
    raw_cols = raw.get("columns")
    raw_rows = raw.get("rows")
    if not isinstance(raw_cols, (list, tuple)) or not isinstance(raw_rows, (list, tuple)):
        return None
    columns = [" ".join(_clip(col, 80).split()) for col in raw_cols[:_TABLE_MAX_COLS]]
    if not columns or any(not col for col in columns):
        return None
    width = len(columns)
    rows: list[list[str]] = []
    for row in raw_rows[:_TABLE_MAX_ROWS]:
        if not isinstance(row, (list, tuple)):
            continue
        cells = [" ".join(_clip(row[idx] if idx < len(row) else "", _TABLE_CELL_CHARS).split())
                 for idx in range(width)]
        rows.append(cells)
    total_raw = raw.get("total_rows")
    total_rows = (total_raw if isinstance(total_raw, int) and not isinstance(total_raw, bool)
                  and total_raw >= len(rows) else len(rows))
    projected: dict[str, Any] = {"columns": columns, "rows": rows, "total_rows": total_rows}
    title = " ".join(_clip(raw.get("title"), 120).split())
    if title:
        projected["title"] = title
    return projected


def _input_options_projection(payload: dict, limits: dict[str, int]) -> list[dict[str, str]]:
    """Project bounded clickable clarification options for ``needs_input`` turns."""
    raw = payload.get("input_options")
    if not isinstance(raw, (list, tuple)) or not raw:
        return []
    options: list[dict[str, str]] = []
    for idx, item in enumerate(raw[:_INPUT_MAX_OPTIONS], start=1):
        if isinstance(item, str):
            label = " ".join(_clip(item, limits["label_max_chars"]).split())
            if label:
                options.append({"id": f"opt-{idx}", "label": label})
            continue
        if not isinstance(item, dict):
            continue
        label = " ".join(_clip(item.get("label"), limits["label_max_chars"]).split())
        if not label:
            continue
        opt_id = " ".join(_clip(item.get("id"), 60).split()) or f"opt-{idx}"
        entry: dict[str, str] = {"id": opt_id, "label": label}
        prompt = _clip(item.get("prompt"), limits["goal_max_chars"]).strip()
        if prompt:
            entry["prompt"] = prompt
        options.append(entry)
    return options


def _handoff_fields(raw_items: object, limits: dict[str, int]) -> list[dict[str, str]]:
    if not isinstance(raw_items, (list, tuple)):
        return []
    result: list[dict[str, str]] = []
    for item in raw_items[:_HANDOFF_MAX_FIELDS]:
        if isinstance(item, str):
            name = " ".join(_clip(item, limits["label_max_chars"]).split())
            if name:
                result.append({"name": name})
            continue
        if not isinstance(item, dict):
            continue
        name = " ".join(_clip(item.get("name"), limits["label_max_chars"]).split())
        if not name:
            continue
        entry: dict[str, str] = {"name": name}
        raw_val = item.get("value")
        kind = str(item.get("kind") or "")
        if raw_val not in (None, "") and not _SECRET_FIELD_RE.search(f"{name} {kind}"):
            val = _clip(raw_val, limits["target_max_chars"]).strip()
            if val:
                entry["value"] = val
        result.append(entry)
    return result


def _handoff_projection(payload: dict, limits: dict[str, int]) -> dict[str, Any] | None:
    """Project bounded form-handoff summaries and restricted control pointers."""
    raw = payload.get("handoff")
    if not isinstance(raw, dict):
        return None
    completed = _handoff_fields(raw.get("completed_fields"), limits)
    pending = _handoff_fields(raw.get("pending_fields"), limits)
    restricted: list[dict[str, str]] = []
    raw_restricted = raw.get("restricted_controls")
    if isinstance(raw_restricted, (list, tuple)):
        for item in raw_restricted[:_HANDOFF_MAX_RESTRICTED]:
            if not isinstance(item, dict):
                continue
            label = " ".join(_clip(item.get("label"), limits["label_max_chars"]).split())
            if not label:
                continue
            ctrl: dict[str, str] = {
                "label": label,
                "kind": " ".join(_clip(item.get("kind") or "restricted", 32).split()) or "restricted",
            }
            ref = " ".join(_clip(item.get("ref"), 32).split())
            if ref:
                ctrl["ref"] = ref
            restricted.append(ctrl)
    if not (completed or pending or restricted
            or isinstance(raw.get("can_undo"), bool)
            or isinstance(raw.get("undone"), bool)):
        return None
    out: dict[str, Any] = {"completed_fields": completed, "pending_fields": pending}
    if restricted:
        out["restricted_controls"] = restricted
    if isinstance(raw.get("can_undo"), bool):
        out["can_undo"] = raw["can_undo"]
    if isinstance(raw.get("undone"), bool):
        out["undone"] = raw["undone"]
    return out


def _terminal_extras(payload: dict, limits: dict[str, int], *, state: str = "") -> dict[str, Any]:
    extras: dict[str, Any] = {}
    if payload.get("retryable") is True:
        extras["retryable"] = True
    proposal_digest = payload.get("proposal_digest")
    if _is_hex_sha256(proposal_digest):
        extras["proposal_digest"] = proposal_digest
    reconciliation = _reconciliation_projection(payload, limits)
    if reconciliation is not None:
        extras["reconciliation"] = reconciliation
    debug_trace = _debug_trace_projection(payload)
    if debug_trace is not None:
        extras["debug_trace"] = debug_trace
    if state == "finished":
        table = _table_projection(payload)
        if table is not None:
            extras["table"] = table
    if state == "needs_input":
        options = _input_options_projection(payload, limits)
        if options:
            extras["input_options"] = options
    if state in {"finished", "needs_input"}:
        handoff = _handoff_projection(payload, limits)
        if handoff is not None:
            extras["handoff"] = handoff
    return extras


def _answer_targets(payload: dict, answer: str) -> list[dict]:
    """Project only bounded relative links from a completed server answer."""
    raw = payload.get("answer_targets")
    if not isinstance(raw, list) or len(raw) > 4:
        return []
    projected = []
    for item in raw:
        if not isinstance(item, dict):
            return []
        kind, address = item.get("kind"), item.get("address")
        label, identity, read_ref = item.get("label"), item.get("identity"), item.get("read_ref")
        if (kind not in {"page", "record"} or not isinstance(address, str)
                or not 1 <= len(address) <= 1000 or not address.startswith("/")
                or address.startswith("//") or any(ord(c) < 33 or ord(c) == 127 or c == "\\" for c in address)
                or urlsplit(address).scheme or urlsplit(address).netloc
                or urlsplit(address).query or urlsplit(address).fragment
                or not isinstance(label, str) or not label.strip() or len(label) > 160
                or any(ord(c) < 32 or ord(c) == 127 for c in label) or label not in answer
                or not isinstance(identity, str) or not identity or len(identity) > 160
                or any(ord(c) < 33 or ord(c) == 127 for c in identity)
                or not isinstance(read_ref, str) or not read_ref.startswith("read#")
                or len(read_ref) > 20 or not read_ref[5:].isdigit()):
            return []
        projected.append({"kind": kind, "address": address, "label": _clip(label, 160),
                          "identity": identity, "read_ref": read_ref})
    return projected


#: Page commands already reach the timeline as `action` events; a second line for the same
#: dispatch would say everything twice.
_PAGE_TOOLS = frozenset({
    "goto", "back", "click", "fill", "select", "submit", "scroll", "wait_for", "observe",
    "apply_facet", "reveal", "form_model", "form_submit",
    "prefill_form", "finish", "ask_user"})


#: What a turn may say it is looking for, and how much of the word it looks for is shown.
_SEARCHED = {"page": "页面", "source": "数据来源", "record": "记录", "rules": "业务规则", "guide": "业务域说明"}
_SEARCH_TERM_CHARS = 24


#: What a step node may carry beyond its sentence, so the panel can show the reader what
#: the assistant actually called and what came back without the panel inventing any of it.
_STEP_DETAIL_CHARS = 200


def step_facts(facts: dict) -> dict:
    """The public, bounded projection of one step, for the panel's timeline node.

    Only what a reader can check: which tool ran, the arguments it ran with, and what came
    back. Everything is clipped and redacted here rather than in the panel, which has no
    business deciding what is safe to show.
    """
    stage = str(facts.get("stage") or "")
    node: dict = {"stage": stage}
    tool = str(facts.get("tool") or "")
    if tool:
        node["tool"] = _clip(tool, 60)
    arguments = facts.get("arguments")
    if isinstance(arguments, dict) and arguments:
        pairs = []
        for name in sorted(arguments)[:4]:
            value = arguments[name]
            if isinstance(value, (dict, list, tuple)):
                continue
            pairs.append(f"{_clip(name, 40)}={_clip(value, 60)}")
        if pairs:
            node["arguments"] = _clip(" · ".join(pairs), _STEP_DETAIL_CHARS)
    if stage == "read_done":
        count = int(facts.get("count") or 0)
        scope = _clip(facts.get("scope"), 80)
        node["result"] = f"{count} 条" + (f" · {scope}" if scope else "")
    if stage == "write_done":
        node["result"] = "已执行，正在核对"
    return node


def describe_step(facts: dict) -> str:
    """User-visible prose for one thing the turn is doing, between its page actions.

    A turn that reads business data spends most of its wall clock inside model calls and
    typed reads, neither of which dispatches a page command.  The panel therefore stood on
    `正在理解你的需求` for as long as that took -- the product was working and said nothing.
    The session reports *facts* (a stage, a tool name, a count); the sentence is written
    here, with the rest of the panel's prose.
    """
    stage = str(facts.get("stage") or "")
    if stage == "thinking":
        step = int(facts.get("step") or 0)
        return f"正在思考（第 {step} 步）" if step > 1 else "正在思考"
    if stage == "tool":
        tool = str(facts.get("tool") or "")
        if tool in _PAGE_TOOLS:
            return ""
        if tool == "load_skill":
            return "正在选择操作方式"
        if tool == "compute":
            return "正在计算"
        if tool == "describe_form":
            return "正在核对当前表单"
        if tool == "prepare_form":
            return "正在准备表单内容"
        if facts.get("mutation"):
            return "正在准备这次更改"
        return "正在读取业务数据"
    if stage == "read_done":
        count = int(facts.get("count") or 0)
        scope = _clip(facts.get("scope"), 60)
        return f"已读取 {count} 条：{scope}" if scope else f"已读取 {count} 条"
    if stage == "write_done":
        return "更改已执行，正在核对结果"
    if stage == "search":
        # Looking for where something is, before anything is read or opened: a page of the
        # application, a source of its data, or a record by its name.
        what = _SEARCHED.get(str(facts.get("what") or ""))
        if not what:
            return ""
        term = " ".join(_clip(facts.get("term"), _SEARCH_TERM_CHARS).split())
        return f"正在查找{what}：{term}" if term else f"正在查找{what}"
    if stage == "read_page":
        base = panel_contract()["label_keys"]["progressPaging"]
        page = facts.get("page")
        if isinstance(page, int) and not isinstance(page, bool) and 1 <= page <= 10_000:
            return f"{base}（第 {page} 页）"
        return base
    if stage == "settle":
        return panel_contract()["label_keys"]["progressSettling"]
    if stage == "awaiting_approval":
        return panel_contract()["label_keys"]["progressAwaitingApproval"]
    if stage == "check":
        return "正在核对结果"
    return ""


#: How a control is called when the page gives it a name. A role with no word here is named
#: by its name alone.
_ROLE_WORDS = {
    "button": "按钮", "link": "链接", "menuitem": "菜单项", "tab": "标签页",
    "checkbox": "复选框", "radio": "单选项", "switch": "开关", "option": "选项",
    "textbox": "输入框", "searchbox": "搜索框", "combobox": "下拉框", "select": "下拉框",
    "listbox": "列表", "article": "卡片",
}
#: Controls that are a row of a list or a cell of one.
_ROW_ROLES = frozenset({"row", "tr", "td", "cell", "gridcell"})

#: A control's name is the page's text for it, which for a list row is the whole row.
_TARGET_NAME_CHARS = 24


def _path(url: str) -> str:
    parts = urlsplit(url)
    return parts.path.rstrip("/") + (f"?{parts.query}" if parts.query else "")


def action_target(command: dict | None, observation: dict | None) -> dict:
    """What the person would call the control one command acts on, from the page it acts on.

    The command names a control by the observation's ref (`e12`), which means nothing to
    the person reading along. The observation the command was based on knows the control's
    role and visible name; for a page opened by path, the link on that page that leads
    there names it. Nothing here comes back from the browser after the fact.
    """
    args = (command or {}).get("args") or {}
    elements = [element for element in ((observation or {}).get("elements") or [])
                if isinstance(element, dict)]
    found: dict | None = None
    if (command or {}).get("action") == "goto":
        wanted = _path(str(args.get("url") or ""))
        found = next((element for element in elements
                      if wanted and _path(str((element.get("attrs") or {}).get("href") or ""))
                      == wanted and str(element.get("name") or "").strip()), None)
    elif args.get("ref"):
        found = next((element for element in elements
                      if element.get("ref") == args.get("ref")), None)
    if found is None:
        return {}
    name = " ".join(_clip(found.get("name"), 200).split())
    if len(name) > _TARGET_NAME_CHARS:
        name = name[:_TARGET_NAME_CHARS - 1] + "…"
    return {"name": name, "role": _clip(found.get("role"), 20)}


def describe_action(command: dict | None, target: dict | None = None) -> str:
    """User-visible prose for one dispatched browser command.

    This is deliberately server-side.  The Runtime used to author it, which meant the
    timeline could not survive the document that produced it.  A control is named as the
    page names it (`target`, from `action_target`), never by its ref or a path.
    """
    action = str((command or {}).get("action") or "")
    args = (command or {}).get("args") or {}
    name = str((target or {}).get("name") or "")
    raw_role = str((target or {}).get("role") or "")
    role = _ROLE_WORDS.get(raw_role, "")
    quoted = f"“{name}”" if name else ""
    if action == "goto":
        return f"打开“{name}”" if name else "打开页面"
    if action == "back":
        return "返回上一页"
    if action == "click":
        if quoted and raw_role in _ROW_ROLES:
            # A row is named by its first cell; what the click does with it is the page's.
            return f"点击{quoted}所在行"
        if quoted:
            return f"点击{quoted}{role}"
        return f"点击页面上的{role}" if role else "点击页面上的控件"
    if action == "fill":
        return f"填写{quoted}" if quoted else "填写页面上的输入框"
    if action == "select":
        return f"在{quoted}中选择" if quoted else "在页面上选择选项"
    if action == "apply_facet":
        return "切换页面筛选或分组"
    if action == "reveal":
        return f"展开{quoted}菜单" if quoted else "展开页面上的菜单"
    if action == "form_model":
        return "读取表单里的完整数据"
    if action == "form_submit":
        return f"按已批准的内容点击{quoted}" if quoted else "按已批准的内容点击提交"
    if action == "submit":
        return f"提交{quoted}" if quoted else "提交页面表单"
    if action == "wait_for":
        return "等待页面状态更新"
    if action == "scroll":
        return "向下滚动页面" if args.get("direction") == "down" else "向上滚动页面"
    if action == "observe":
        return "重新检查当前页面"
    return "执行页面动作"


#: Why the assistant's session ended, as the person at the panel is told. The reason codes
#: are the channel's own and were shown raw -- "任务结束（delegated_authority_changed）" -- which
#: is the bare error code J6.03 rules out of a Chinese conversation.
_SESSION_ENDED = {
    "principal_revoked": "已退出登录，助手会话已结束",
    "principal_expired": "登录凭证已过期，助手会话已结束",
    "principal_validation_failed": "无法核验当前登录身份，助手会话已结束",
    "session_expired": "助手会话已过期",
    "browser_inactive": "长时间没有连接，助手会话已结束",
    "native_bootstrap_replaced": "页面重新连接了助手，这个会话已结束",
    "delegated_authority_changed": "账号的授权已变更，助手会话已结束",
    "identity_authority_mismatch": "登录身份与授权不一致，助手会话已结束",
    "host_stopped": "助手服务已停止",
    "worker_failed": "助手服务出错，这个会话已结束，请重新开始",
}


def session_ended_message(reason: str) -> str:
    """The line a closed session leaves in the timeline; never the raw reason code."""
    if reason.endswith("_capacity_exhausted"):
        return "助手会话达到容量上限，已结束"
    return _SESSION_ENDED.get(reason, "助手会话已结束")


def _ending_text(payload: dict, text: str) -> str:
    # Keep prior verified effects and the reason supplied by the executor. Expiry also
    # needs an explanation and a next step, live and when older records are replayed.
    if payload.get("terminal_reason") == "approval_expired":
        guidance = panel_contract()["notices"]["approval_expired"]
        return _clip("\n\n".join(dict.fromkeys(part for part in (guidance, text) if part)),
                     panel_limits()["result_max_chars"])
    return text


#: Steps that say only that something is under way. Between two things the assistant did,
#: "正在思考" is what it was doing while nothing happened; once the next thing has happened,
#: or the turn has ended, it says nothing more and the panel stops showing it.
_TRANSIENT_STAGES = frozenset({"thinking", "settle", "awaiting_approval"})


def project_event(event: TaskEvent, *, at: int, target: dict | None = None) -> dict | None:
    """Project one session event onto a panel timeline entry, or ``None`` if invisible.

    An ``approval_required`` event projects only its *presence*.  The card's content and the
    witness that makes a decision acceptable travel on the panel's own channel, never on the
    stream the host page can read.
    """
    contract = panel_contract()
    limits = contract["limits"]
    payload = event.payload
    base = {"id": event.event_id, "sequence": event.sequence,
            "turn_id": event.turn_id, "at": at}
    if event.kind == "session_started":
        return None
    if event.kind == "action":
        return {**base, "kind": "action", "tone": "active",
                "text": describe_action(payload.get("command"), target)}
    if event.kind == "approval_required":
        text = ("需要确认后才能操作页面" if payload.get("kind") == "operate"
                else "需要确认后才能继续")
        return {**base, "kind": "approval", "tone": "warning",
                "text": text, "approval_id": str(payload.get("approval_id") or "")}
    if event.kind == "terminal":
        if str(payload.get("reason") or "") in panel_contract()["lapse_codes"]:
            return None
        return {**base, "kind": "result", "tone": "neutral",
                "text": session_ended_message(str(payload.get("reason") or ""))}
    if event.kind != "progress":
        return None
    state = str(payload.get("state") or "")
    mapping = contract["progress_states"].get(state)
    if mapping is None:
        # An unmapped state is a contract violation on the server side, not something the
        # panel should paper over with a generic line. test_panel_contract pins the set.
        raise ValueError(f"progress state {state!r} has no panel projection")
    if state in {"new_conversation", "session_recovered"}:
        # Recorded in the event log because it happened, and because it is what ended the
        # model's reference history or re-established the durable connection. Neither is a
        # user turn: rendering either alone creates a misleading empty turn and "用时 0s".
        return None
    if state == "message_accepted":
        return {**base, "kind": "goal", "tone": "neutral",
                "text": _clip(payload.get("text"), limits["goal_max_chars"])}
    text = (_clip(payload.get("message"), limits["result_max_chars"])
            or progress_message(state))
    if mapping["terminal"]:
        text = _ending_text(payload, text)
    if not text:
        text = "正在执行" if not mapping["terminal"] else "任务结束"
    node = payload.get("step")
    node = node if state == "step" and isinstance(node, dict) and node else None
    targets = _answer_targets(payload, text) if mapping["terminal"] and state == "finished" else []
    terminal_extras = _terminal_extras(payload, limits, state=state) if mapping["terminal"] else {}
    return {**base, "kind": "result" if mapping["terminal"] else "progress",
            "tone": mapping["tone"], "text": text,
            "panel_state": mapping["panel_state"],
            **({"answer_targets": targets} if targets else {}),
            **terminal_extras,
            **({"step": node} if node else {}),
            **({"transient": True} if node and node.get("stage") in _TRANSIENT_STAGES else {})}


def project_history_record(record: dict, *, index: int) -> dict | None:
    """Project one durable outbox record from a previous incarnation of this session.

    Only progress survives a restart (`durable.append` stores nothing else), so this is the
    portion of the timeline that outlives the process, not only the document.
    """
    if record.get("kind") != "progress":
        return None
    payload = record.get("payload") or {}
    contract = panel_contract()
    state = str(payload.get("state") or "")
    mapping = contract["progress_states"].get(state)
    if mapping is None:
        return None
    if state in {"new_conversation", "session_recovered"}:
        return None
    limits = contract["limits"]
    base = {"id": f"h{index:08d}", "sequence": 0, "replayed": True,
            "turn_id": str(record.get("turn_id") or ""), "at": int(record.get("at") or 0)}
    if state == "message_accepted":
        return {**base, "kind": "goal", "tone": "neutral",
                "text": _clip(payload.get("text"), limits["goal_max_chars"])}
    text = (_clip(payload.get("message"), limits["result_max_chars"])
            or progress_message(state))
    if mapping["terminal"]:
        text = _ending_text(payload, text)
    if not text:
        return None
    targets = _answer_targets(payload, text) if mapping["terminal"] and state == "finished" else []
    terminal_extras = _terminal_extras(payload, limits, state=state) if mapping["terminal"] else {}
    return {**base, "kind": "result" if mapping["terminal"] else "progress",
            "tone": mapping["tone"], "text": text, "panel_state": mapping["panel_state"],
            **({"answer_targets": targets} if targets else {}),
            **terminal_extras}


def collapse_timeline(entries: list[dict]) -> list[dict]:
    """Collapse repetition once, on the server, for every consumer.

    Consecutive identical progress or action lines collapse onto the latest occurrence, and
    the repeated "re-check the page" line appears once per user turn.  This is de-noising,
    not truncation: nothing a user asked for is discarded here.  How much of the result a
    client holds in the DOM is the client's paging decision, bounded by the session's own
    ``max_events``.
    """
    collapsed: list[dict] = []
    observed_turn: set[str] = set()
    for entry in entries:
        append_timeline_entry(collapsed, entry, observed_turn)
    return collapsed


def append_timeline_entry(entries: list[dict], entry: dict, observed_turn: set[str]) -> None:
    """Apply the same de-noising incrementally as new live events arrive."""
    if entry["kind"] == "action" and entry["text"] == "重新检查当前页面":
        if entry["turn_id"] in observed_turn:
            return
        observed_turn.add(entry["turn_id"])
    previous = entries[-1] if entries else None
    if (previous is not None and previous["kind"] == entry["kind"]
            and previous["text"] == entry["text"]
            and previous["turn_id"] == entry["turn_id"]
            and entry["kind"] in {"progress", "action"}):
        entries[-1] = entry
    else:
        entries.append(entry)
