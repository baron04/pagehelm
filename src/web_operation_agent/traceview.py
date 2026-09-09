"""Create a small, privacy-safe product replay from an append-only session ledger."""
from __future__ import annotations

import html
import json
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlsplit

from .privacy import (has_unredacted_sensitive_assignment,
                      has_unredacted_session_handoff,
                      redact_sensitive_assignments, redact_session_handoffs)


VISIBLE_EVENTS = frozenset({
    "approval_requested", "approval_decided", "tool_call", "application_entered",
    "skill_app_entered", "skill_app_observed", "skill_route_opened",
    "page_route_rejected", "action_receipt", "write_permitted", "success_claim",
    "run_finished", "product_verification",
})

_TRACE_TEMPLATE = ("web_runtime", "trace_replay.html")
_ASSISTANT_COMPONENT = ("web_runtime", "assistant_panel.js")


def _safe(value):
    clean = redact_sensitive_assignments(redact_session_handoffs(value))
    if (has_unredacted_sensitive_assignment(clean, url_decode_depth=3)
            or has_unredacted_session_handoff(clean, url_decode_depth=3)):
        raise ValueError("trace projection still contains a credential")
    return clean


def _path(value: str) -> str:
    parsed = urlsplit(str(value or ""))
    path = parsed.path or "/"
    return path + (f"?{parsed.query}" if parsed.query else "")


def _detail(event: dict) -> tuple[str, str, str]:
    kind = event["event"]
    if kind == "approval_requested":
        return "approval", "确认业务变更", str(event.get("effect", "等待用户确认"))
    if kind == "approval_decided":
        decision = event.get("decision", "unknown")
        return "approval", "用户已确认" if decision == "approved" else "用户已取消", decision
    if kind == "tool_call":
        tool = str(event.get("tool", "tool"))
        args = event.get("args") if isinstance(event.get("args"), dict) else {}
        if "url" in args:
            detail = _path(args["url"])
        elif "skill_id" in args:
            detail = str(args["skill_id"])
        elif "ref" in args:
            detail = f"页面元素 {args['ref']}"
        else:
            detail = "执行已验证的工具参数"
        return "decision", tool, detail
    if kind in {"application_entered", "skill_app_entered"}:
        return "action", "进入业务应用", str(event.get("skill_id", "通过当前页面入口"))
    if kind == "skill_app_observed":
        absent = event.get("target_visible_link_absent") is True
        links = event.get("visible_link_count", 0)
        return "knowledge", "确认页面知识缺口", (
            f"检查 {links} 个可见链接；目标入口不可见" if absent
            else f"检查 {links} 个可见链接")
    if kind == "skill_route_opened":
        return "knowledge", "采用 App Skill", _path(event.get("url", ""))
    if kind == "page_route_rejected":
        return "guard", "拒绝未观察到的路径", _path(event.get("url", ""))
    if kind == "action_receipt":
        ok = event.get("ok") is True
        return "action" if ok else "guard", (
            "页面动作完成" if ok else "页面动作失败"), (
            f"{event.get('tool', 'action')} · {_path(event.get('url_after', ''))}")
    if kind == "write_permitted":
        return "approval", "已消费一次写许可", "隐藏控制计划匹配本次业务写入"
    if kind == "success_claim":
        return "decision", "Agent 请求结束", str(event.get("claim", "等待产品验证"))
    if kind == "run_finished":
        return "decision", "推理循环结束", str(event.get("terminal_reason", "unknown"))
    if kind == "product_verification":
        passed = event.get("product_success") is True
        return "verify", "产品验证通过" if passed else "产品验证未通过", (
            "页面与业务后置条件成立" if passed else "确定性完成条件未成立")
    return "decision", kind.replace("_", " "), ""


def build_trace_case(*, case_id: str, label: str, result_path: str | Path,
                     ledger_path: str | Path, mode: str) -> dict:
    """Project one real run into a compact replay; reject incomplete ledger identity."""
    result = json.loads(Path(result_path).read_text(encoding="utf-8"))
    events = [json.loads(line) for line in Path(ledger_path).read_text(encoding="utf-8").splitlines()
              if line.strip()]
    if not events:
        raise ValueError("trace ledger is empty")
    sequences = [event.get("seq") for event in events]
    if sequences != list(range(1, len(events) + 1)):
        raise ValueError("trace ledger sequence is not contiguous")
    run_id = result.get("product_run_id")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("result has no product_run_id")
    if any(event.get("run_id") != run_id for event in events):
        raise ValueError("trace ledger and product result belong to different runs")

    started = float(result.get("started_at", events[0].get("t", 0)))
    projected = []
    for event in events:
        if event.get("event") not in VISIBLE_EVENTS:
            continue
        tone, title, detail = _detail(event)
        projected.append({
            "seq": event["seq"],
            "at_ms": max(0, round((float(event.get("t", started)) - started) * 1000)),
            "kind": event["event"],
            "tone": tone,
            "title": title,
            "detail": detail,
        })

    agent = result.get("agent") if isinstance(result.get("agent"), dict) else {}
    spend = agent.get("spend") if isinstance(agent.get("spend"), dict) else {}
    control = agent.get("control") if isinstance(agent.get("control"), dict) else {}
    duration = max(0, float(result.get("finished_at", started)) - started)
    case = {
        "id": case_id,
        "label": label,
        "mode": mode,
        "task": result.get("task", ""),
        "success": result.get("product_success") is True,
        "summary": {
            "duration_s": round(duration, 1),
            "model_calls": int(spend.get("provider_calls", 0)),
            "page_actions": int(agent.get("page_actions", 0)),
            "cost_cny": float(spend.get("cost_cny", 0)),
            "permit_consumed": int(control.get("permit_consumed", 0)),
            "write_blocked": int(control.get("write_blocked", 0)),
            "terminal_reason": str(agent.get("terminal_reason", "unknown")),
        },
        "answer": str(agent.get("agent_said", "")),
        "events": projected,
    }
    return _safe(case)


def write_trace_bundle(cases: list[dict], output: str | Path) -> dict:
    if not 1 <= len(cases) <= 8:
        raise ValueError("trace bundle needs from 1 to 8 cases")
    identifiers = [case.get("id") for case in cases]
    if len(set(identifiers)) != len(identifiers) or not all(identifiers):
        raise ValueError("trace case ids must be unique and non-empty")
    bundle = _safe({"schema_version": 1, "product": "Web Operation Agent", "cases": cases})
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(bundle, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                    encoding="utf-8")
    return bundle


def render_trace_html(bundle: dict, output: str | Path) -> None:
    """Render a dependency-free replay that also works from a local file URL."""
    safe_bundle = _safe(bundle)
    data = json.dumps(safe_bundle, ensure_ascii=False, sort_keys=True).replace("</", "<\\/")
    template = files("web_operation_agent").joinpath(*_TRACE_TEMPLATE).read_text(encoding="utf-8")
    marker = "__WOA_TRACE_BUNDLE__"
    if template.count(marker) != 1:
        raise ValueError("trace replay template must contain exactly one data marker")
    rendered = template.replace(marker, data)
    component_marker = "/*__WOA_ASSISTANT_COMPONENT__*/"
    if rendered.count(component_marker) != 1:
        raise ValueError("trace replay template must contain exactly one assistant marker")
    component = files("web_operation_agent").joinpath(*_ASSISTANT_COMPONENT).read_text(
        encoding="utf-8")
    rendered = rendered.replace(component_marker, component)
    # The data is inserted into application/json, then parsed via textContent. Escape any
    # accidental HTML-special characters in the static title only; JSON stays machine-readable.
    if not safe_bundle.get("product"):
        raise ValueError("trace bundle has no product name")
    rendered = rendered.replace(
        "__WOA_PRODUCT_NAME__", html.escape(str(safe_bundle["product"])))
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered, encoding="utf-8")
