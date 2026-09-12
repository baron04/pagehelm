"""Server-owned projection of session events onto the assistant panel's timeline.

The panel is a cross-origin document with no session state of its own.  Everything it
renders in the timeline is produced here, so that a full page navigation replays the same
timeline from the server instead of losing it with the previous document (F8a), and so that
no user-visible string or bound is written down twice (F8b).

Nothing in this module may emit an action envelope, an approval capability, a page
observation or a URL the panel could act on: the panel shows prose, it does not drive the
page.
"""
from __future__ import annotations

import json
from functools import lru_cache
from importlib.resources import files
from typing import Any

from .privacy import redact_sensitive_assignments, redact_session_handoffs
from .schemas import TaskEvent

_CONTRACT_RESOURCE = ("web_runtime", "panel_contract.json")
_TOKENS_RESOURCE = ("web_runtime", "design_tokens.json")


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


@lru_cache(maxsize=1)
def design_tokens() -> dict:
    """The one palette. The panel compiles it into CSS; traceview writes the same values."""
    raw = files("web_operation_agent").joinpath(*_TOKENS_RESOURCE).read_text(encoding="utf-8")
    return json.loads(raw)


def token_custom_properties() -> str:
    """Render the palette as CSS custom properties for a renderer that has no build step."""
    tokens = design_tokens()

    def block(values: dict[str, str], indent: str = "      ") -> str:
        return "\n".join(f"{indent}--woa-{name}: {value};" for name, value in values.items())

    return (
        ":root {\n"
        f"{block(tokens['scale'])}\n{block(tokens['themes']['light'])}\n"
        "      color-scheme: light;\n    }\n"
        "    @media (prefers-color-scheme: dark) {\n      :root {\n"
        f"{block(tokens['themes']['dark'], '        ')}\n"
        "        color-scheme: dark;\n      }\n    }"
    )


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


def describe_action(command: dict | None) -> str:
    """User-visible prose for one dispatched browser command.

    This is deliberately server-side.  The Runtime used to author it, which meant the
    timeline could not survive the document that produced it.
    """
    action = str((command or {}).get("action") or "")
    args = (command or {}).get("args") or {}
    limits = panel_limits()
    ref = _clip(args.get("ref"), 40)
    if action == "goto":
        return f"打开页面：{_clip(args.get('url'), limits['result_max_chars'])}"
    if action == "back":
        return "返回上一页"
    if action == "click":
        return f"点击页面控件：{ref}"
    if action == "fill":
        return f"填写页面字段：{ref}"
    if action == "select":
        return f"选择页面选项：{ref}"
    if action == "submit":
        return f"提交页面表单：{ref}"
    if action == "wait_for":
        return "等待页面状态更新"
    if action == "observe":
        return "重新检查当前页面"
    return f"执行页面动作：{_clip(action, 40)}" if action else "执行页面动作"


def project_event(event: TaskEvent, *, at: int) -> dict | None:
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
                "text": describe_action(payload.get("command"))}
    if event.kind == "approval_required":
        return {**base, "kind": "approval", "tone": "warning",
                "text": "需要确认后才能继续", "approval_id": str(payload.get("approval_id") or "")}
    if event.kind == "terminal":
        reason = _clip(payload.get("reason"), 100)
        return {**base, "kind": "result", "tone": "neutral",
                "text": f"任务结束（{reason}）" if reason else "任务结束"}
    if event.kind != "progress":
        return None
    state = str(payload.get("state") or "")
    mapping = contract["progress_states"].get(state)
    if mapping is None:
        # An unmapped state is a contract violation on the server side, not something the
        # panel should paper over with a generic line. test_panel_contract pins the set.
        raise ValueError(f"progress state {state!r} has no panel projection")
    if state == "new_conversation":
        # Recorded in the event log because it happened, and because it is what ended the
        # model's reference history. Not rendered: the panel clears its view instead, so a
        # divider would be a line above nothing.
        return None
    if state == "message_accepted":
        return {**base, "kind": "goal", "tone": "neutral",
                "text": _clip(payload.get("text"), limits["goal_max_chars"])}
    text = (_clip(payload.get("message"), limits["result_max_chars"])
            or progress_message(state))
    if not text:
        text = "正在执行" if not mapping["terminal"] else "任务结束"
    return {**base, "kind": "result" if mapping["terminal"] else "progress",
            "tone": mapping["tone"], "text": text,
            "panel_state": mapping["panel_state"]}


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
    limits = contract["limits"]
    base = {"id": f"h{index:08d}", "sequence": 0, "replayed": True,
            "turn_id": str(record.get("turn_id") or ""), "at": int(record.get("at") or 0)}
    if state == "message_accepted":
        return {**base, "kind": "goal", "tone": "neutral",
                "text": _clip(payload.get("text"), limits["goal_max_chars"])}
    text = (_clip(payload.get("message"), limits["result_max_chars"])
            or progress_message(state))
    if not text:
        return None
    return {**base, "kind": "result" if mapping["terminal"] else "progress",
            "tone": mapping["tone"], "text": text, "panel_state": mapping["panel_state"]}


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
        if entry["kind"] == "action" and entry["text"] == "重新检查当前页面":
            if entry["turn_id"] in observed_turn:
                continue
            observed_turn.add(entry["turn_id"])
        previous = collapsed[-1] if collapsed else None
        if (previous is not None and previous["kind"] == entry["kind"]
                and previous["text"] == entry["text"]
                and previous["turn_id"] == entry["turn_id"]
                and entry["kind"] in {"progress", "action"}):
            collapsed[-1] = entry
            continue
        collapsed.append(entry)
    return collapsed
