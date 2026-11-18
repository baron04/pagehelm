"""Turn-local loop guards, observation/handoff caches, and completion proofs."""
from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlsplit

from ..schemas import PageObservation
from .checks import ProgramCheckSpec

FAILED_READ_REPEATED_SPEC = ProgramCheckSpec(
    name="failed_read_repeated",
    checks_what="a non-transiently failed read/metadata tool call is not re-executed with identical arguments",
    retry_guidance="skip re-execution on first repeat and instruct the model to fix parameters or switch tools",
    terminal_delivery="stop on repeated unchanged failure and deliver already-read complete actor-bound results or status note",
)

SAME_STEP_STALLED_SPEC = ProgramCheckSpec(
    name="same_step_stalled",
    checks_what="the model does not repeat the identical page action with unchanged arguments on an unchanged observation",
    retry_guidance="skip duplicate page action up to three times and tell the model to try a different step or report cannot",
    terminal_delivery="stop turn as agent_refused once stalled count exceeds three and notify user the repeated step had no effect",
)

STABLE_CONTROL_REPEATED_SPEC = ProgramCheckSpec(
    name="stable_control_repeated",
    checks_what="a failed page control action is not repeatedly dispatched with the same stable control identity",
    retry_guidance="skip the 2nd identical failed dispatch and return the exact failure reason and candidate labels to try another option or control",
    terminal_delivery="stop cleanly as agent_refused once repeated identical failed control dispatches stall",
)

VERIFIED_EMPTY_FILTER_REPEATED_SPEC = ProgramCheckSpec(
    name="verified_empty_filter_repeated",
    checks_what="filter queries or page resets already verified empty in this turn are not re-dispatched",
    retry_guidance="block duplicate empty filter dispatches before hitting the page or API and instruct the model to change filter criteria or report empty",
    terminal_delivery="block duplicate empty filter dispatch and preserve already-verified empty findings",
)

#: Read failures that may not recur on an identical call; every other code does.
_TRANSIENT_FAILURES = frozenset({
    "application_unavailable", "stale_binding", "observation_unavailable",
    "runtime_unavailable", "stale_observation", "action_unavailable",
    "runtime_error", "transport_error", "interrupted",
})
#: Page steps a read-only deployment declined, said after the answer in the person's words.
_NOT_EXECUTED = {
    "unreviewed": "本部署未开放此操作",
    "permission_denied": "当前账号无权限",
    "invalid_target": "目标未通过核对",
    "unrequested_draft_field": "用户未要求该字段",
}
_NOT_EXECUTED_EN = {
    "unreviewed": "not offered by this deployment",
    "permission_denied": "not permitted for this account",
    "invalid_target": "target not confirmed",
    "unrequested_draft_field": "field not requested",
}
_ACTION_WORDS = {
    "goto": "打开",
    "click": "点击",
    "input": "填写",
    "select": "选择",
    "fill_fields": "填写",
    "highlight": "标出",
    "scroll": "滚动",
}


def page_fingerprint(observation) -> str:
    """Stable content fingerprint of a page observation, ignoring epoch and active marker."""
    if observation is None:
        return "no-page-observation"
    value = observation.to_json()
    value.pop("epoch", None)
    for element in value.get("elements", ()):
        element.pop("epoch", None)
    value["content"] = re.sub(r"(?m)^(\s*)\*(?=\[\d+\])", r"\1", value.get("content", ""))
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def target_name(element) -> str:
    """Short label for an element target used in step traces and labels."""
    return (element.name.splitlines() or [element.attrs.get("field_label") or element.role])[0][:80]


def action_label(name: str, args: dict, target) -> str:
    """Human-readable label describing a declined page action."""
    if name == "fill_fields":
        labels = [str(item.get("label") or "") for item in args.get("fields") or () if isinstance(item, dict)]
        return "、".join(label for label in labels[:3] if label)[:60]
    if name == "goto":
        return str(args.get("path") or "")[:80]
    return target_name(target)[:40] if target is not None else ""


def _obs_route(observation) -> str:
    if observation is None:
        return ""
    route = str(getattr(observation, "page_route", "") or "").strip()
    if route:
        return route
    url = str(getattr(observation, "url", "") or "").strip()
    return (urlsplit(url).path or "") if url else ""


def _normalize_control_value(value) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())


def stable_control_identity(name: str, arguments: dict, target, observation=None):
    """Stable control identity `(action, route, form_scope, label_or_name, normalized_value)`."""
    if name not in {"click", "input", "select", "fill_fields"}:
        return None
    route = _obs_route(observation)
    args = arguments if isinstance(arguments, dict) else {}
    if name == "fill_fields":
        fields = args.get("fields") or ()
        labels = tuple(str(item.get("label") or "").strip() for item in fields if isinstance(item, dict))
        values = tuple(_normalize_control_value(item.get("value")) for item in fields if isinstance(item, dict))
        form_scope = str(args.get("form_scope") or "").strip()
        if not form_scope and observation is not None and labels:
            for elem in getattr(observation, "elements", ()):
                elem_label = str(
                    elem.attrs.get("field_label")
                    or elem.attrs.get("field")
                    or elem.attrs.get("title")
                    or elem.name
                    or ""
                ).strip()
                if elem_label in labels and elem.attrs.get("form_scope"):
                    form_scope = str(elem.attrs.get("form_scope") or "").strip()
                    break
        return ("fill_fields", route, form_scope, labels, values)
    form_scope = (
        str(target.attrs.get("form_scope") or "").strip()
        if target is not None
        else str(args.get("form_scope") or "").strip()
    )
    if target is not None:
        label_or_name = str(
            target.attrs.get("field_label")
            or target.attrs.get("field")
            or target.attrs.get("title")
            or target.name
            or target.role
            or ""
        ).strip()
    else:
        label_or_name = str(
            args.get("label")
            or args.get("path")
            or args.get("url")
            or f"index:{args.get('index')}"
        ).strip()
    raw_val = (
        args.get("value")
        if args.get("value") is not None
        else args.get("option_label")
        if args.get("option_label") is not None
        else args.get("option")
    )
    return (name, route, form_scope, label_or_name, _normalize_control_value(raw_val))


def failure_base(name: str, arguments: dict, target, observation=None):
    """A turn-local stable failure key without volatile element index or epoch."""
    identity = stable_control_identity(name, arguments, target, observation)
    if identity is not None:
        return identity
    if name == "goto":
        args = arguments if isinstance(arguments, dict) else {}
        dest = str(args.get("path") or args.get("url") or "").strip()
        return ("goto", _obs_route(observation), "", dest, "")
    return None


def canonical_filter_key(
    route: str = "",
    active_view_or_tab: str = "",
    filter_pairs=(),
) -> tuple[str, str, tuple[tuple[str, str], ...]]:
    """Canonical `(route, active_view_or_tab, canonical_filter_pairs)` key for empty filter dedup."""
    clean_route = str(route or "").strip()
    clean_view = " ".join(str(active_view_or_tab or "").split())
    items: list[tuple[str, str]] = []
    raw_items = filter_pairs.items() if isinstance(filter_pairs, dict) else (filter_pairs or ())
    for entry in raw_items:
        if not isinstance(entry, (tuple, list)) or len(entry) != 2:
            continue
        k = str(entry[0] or "").strip()
        v = _normalize_control_value(entry[1])
        if k and v:
            items.append((k, v))
    items.sort()
    return (clean_route, clean_view, tuple(items))


def _verified_empty_filter_set(turn, *, create: bool) -> set | None:
    if turn is None:
        return None
    if hasattr(turn, "get") and hasattr(turn, "setdefault"):
        return turn.setdefault("verified_empty_filters", set()) if create else turn.get("verified_empty_filters")
    bucket = getattr(turn, "verified_empty_filters", None)
    if bucket is None and create:
        bucket = set()
        setattr(turn, "verified_empty_filters", bucket)
    return bucket


def record_verified_empty_filter(
    turn,
    route: str = "",
    active_view_or_tab: str = "",
    filter_pairs=(),
) -> tuple[str, str, tuple[tuple[str, str], ...]]:
    """Record a verified empty `(route, active_view_or_tab, filter_pairs)` monotonically per turn."""
    key = canonical_filter_key(route, active_view_or_tab, filter_pairs)
    bucket = _verified_empty_filter_set(turn, create=True)
    if bucket is not None:
        bucket.add(key)
    return key


def is_verified_empty_filter(
    turn,
    route: str = "",
    active_view_or_tab: str = "",
    filter_pairs=(),
) -> bool:
    """Return whether `(route, active_view_or_tab, filter_pairs)` was already verified empty this turn."""
    bucket = _verified_empty_filter_set(turn, create=False)
    if not bucket:
        return False
    return canonical_filter_key(route, active_view_or_tab, filter_pairs) in bucket


def format_observation_summary(summary: dict | None, *, max_chars: int = 600) -> str:
    """Render a compact bounded observation_summary payload for tool feedback."""
    if not isinstance(summary, dict) or not summary:
        return ""
    encoded = json.dumps(summary, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return encoded[:max_chars] + ("…" if len(encoded) > max_chars else "")


def settled_action_observation_valid(turn, obs, channel) -> bool:
    """Return True if `obs` is still the fresh settled observation from a recent click/select/goto."""
    if obs is None or (not isinstance(turn, dict) and not hasattr(turn, "get")):
        return False
    settled = turn.get("settled_action_observation")
    if not isinstance(settled, dict):
        return False
    if settled.get("epoch") != obs.epoch or settled.get("fingerprint") != page_fingerprint(obs):
        return False
    latest = channel.latest_observation_envelope if channel is not None else None
    if latest is not None and page_fingerprint(latest.observation) != settled.get("fingerprint"):
        return False
    return True


def merge_receipt_form_outcomes(outcomes: dict, receipt) -> bool:
    """Merge per-field form outcomes from an `ActionReceipt` into turn `outcomes`."""
    if receipt is None:
        return False
    touched = False
    applied = getattr(receipt, "applied_fields", None)
    if isinstance(applied, dict) and applied:
        bucket = outcomes.setdefault("applied_fields", {})
        for k, v in applied.items():
            if isinstance(k, str) and k.strip():
                bucket[k.strip()] = v
                touched = True
    rejected = getattr(receipt, "rejected_fields", None)
    if isinstance(rejected, dict) and rejected:
        bucket = outcomes.setdefault("rejected_fields", {})
        for k, v in rejected.items():
            if isinstance(k, str) and k.strip():
                bucket[k.strip()] = v
                touched = True
    previous = getattr(receipt, "previous_fields", None)
    if isinstance(previous, dict) and previous:
        bucket = outcomes.setdefault("previous_fields", {})
        for k, v in previous.items():
            if isinstance(k, str) and k.strip():
                bucket.setdefault(k.strip(), v)
                touched = True
    restricted = getattr(receipt, "restricted_fields", None)
    if restricted:
        bucket = outcomes.setdefault("restricted_fields", [])
        for item in restricted:
            if isinstance(item, str) and item.strip() and item.strip() not in bucket:
                bucket.append(item.strip())
                touched = True
    return touched


def format_receipt_field_details(receipt) -> str:
    """Render concise per-field outcome hints from a single `ActionReceipt`."""
    if receipt is None:
        return ""
    parts: list[str] = []
    applied = getattr(receipt, "applied_fields", None)
    if isinstance(applied, dict) and applied:
        parts.append("applied_fields=" + json.dumps(applied, ensure_ascii=False, separators=(",", ":"))[:400])
    rejected = getattr(receipt, "rejected_fields", None)
    if isinstance(rejected, dict) and rejected:
        parts.append("rejected_fields=" + json.dumps(rejected, ensure_ascii=False, separators=(",", ":"))[:400])
    previous = getattr(receipt, "previous_fields", None)
    if isinstance(previous, dict) and previous:
        parts.append("previous_fields=" + json.dumps(previous, ensure_ascii=False, separators=(",", ":"))[:300])
    restricted = getattr(receipt, "restricted_fields", None)
    if restricted:
        parts.append("restricted_fields=" + "、".join(str(v) for v in restricted[:12]))
    candidates = getattr(receipt, "candidate_labels", None)
    if candidates:
        parts.append("candidates=" + "、".join(str(v) for v in candidates[:12]))
    return "\n".join(parts)


def _infer_restricted_kind(label: str, element=None) -> str:
    if element is not None:
        attrs = getattr(element, "attrs", {}) or {}
        input_type = str(attrs.get("type") or attrs.get("control_kind") or "").casefold()
        if input_type in {"password", "file"}:
            return input_type
        if getattr(element, "role", "") == "file":
            return "file"
    lowered = label.casefold()
    if any(tok in lowered for tok in ("password", "passwd", "密码", "口令")):
        return "password"
    if any(tok in lowered for tok in ("file", "upload", "attachment", "附件", "上传", "文件")):
        return "file"
    return "restricted"


def build_handoff_payload(
    outcomes: dict | None,
    *,
    observation=None,
    fallback_entered=(),
) -> dict | None:
    """Build a bounded terminal `handoff` payload for `panel_view._handoff_projection`."""
    data = outcomes if isinstance(outcomes, dict) else {}
    completed: list[dict[str, str]] = []
    applied = data.get("applied_fields")
    if isinstance(applied, dict) and applied:
        for name, raw_val in list(applied.items())[:24]:
            label = str(name).strip()[:200]
            if not label:
                continue
            entry: dict[str, str] = {"name": label}
            if isinstance(raw_val, dict):
                val = raw_val.get("value")
                kind = raw_val.get("kind")
                if isinstance(kind, str) and kind.strip():
                    entry["kind"] = kind.strip()[:32]
                if val not in (None, ""):
                    entry["value"] = str(val).strip()[:300]
            elif raw_val not in (None, ""):
                entry["value"] = str(raw_val).strip()[:300]
            completed.append(entry)
    elif fallback_entered:
        seen_names: dict[str, str] = {}
        for pair in fallback_entered:
            if isinstance(pair, (tuple, list)) and len(pair) == 2:
                k = str(pair[0] or "").strip()[:200]
                if k:
                    seen_names[k] = str(pair[1] or "").strip()[:300]
        for k, v in list(seen_names.items())[:24]:
            entry = {"name": k}
            if v:
                entry["value"] = v
            completed.append(entry)

    pending: list[dict[str, str]] = []
    rejected = data.get("rejected_fields")
    if isinstance(rejected, dict) and rejected:
        for name, raw_val in list(rejected.items())[:24]:
            label = str(name).strip()[:200]
            if not label:
                continue
            entry = {"name": label}
            if isinstance(raw_val, dict):
                reason = raw_val.get("reason") or raw_val.get("value")
                if reason not in (None, ""):
                    entry["value"] = str(reason).strip()[:300]
            elif raw_val not in (None, ""):
                entry["value"] = str(raw_val).strip()[:300]
            pending.append(entry)

    restricted_controls: list[dict[str, str]] = []
    elements = getattr(observation, "elements", ()) if observation is not None else ()
    for raw_item in (data.get("restricted_fields") or ())[:12]:
        text = str(raw_item or "").strip()
        if not text:
            continue
        label = text
        kind = ""
        if ":" in text:
            head, tail = text.rsplit(":", 1)
            if tail.strip().casefold() in {"password", "file", "restricted", "captcha"}:
                label = head.strip()
                kind = tail.strip().casefold()
        matched = next(
            (
                elem for elem in elements
                if label
                and (
                    str(elem.attrs.get("field_label") or "").strip() == label
                    or str(elem.attrs.get("field") or "").strip() == label
                    or str(elem.name or "").strip() == label
                )
            ),
            None,
        )
        ctrl: dict[str, str] = {
            "label": label[:200],
            "kind": (kind or _infer_restricted_kind(label, matched))[:32],
        }
        if matched is not None and getattr(matched, "ref", ""):
            ctrl["ref"] = str(matched.ref)[:32]
        restricted_controls.append(ctrl)

    can_undo = bool(isinstance(data.get("previous_fields"), dict) and data.get("previous_fields"))
    if not (completed or pending or restricted_controls or can_undo):
        return None
    payload: dict[str, object] = {
        "completed_fields": completed,
        "pending_fields": pending,
        "can_undo": can_undo,
    }
    if restricted_controls:
        payload["restricted_controls"] = restricted_controls
    return payload


def action_target(observation, arguments: dict) -> dict:
    """Bound structural diagnostics, without labels, field values or URLs."""
    target = {
        "observation_epoch": observation.epoch,
        "observation_digest": observation.content_digest,
    }
    element = next((e for e in observation.elements if e.ref == arguments.get("ref")), None)
    if element is not None:
        target.update(
            ref=element.ref,
            tag=element.tag if re.fullmatch(r"[a-z][a-z0-9-]{0,39}", element.tag) else "other",
            role=element.role if re.fullmatch(r"[a-z][a-z0-9-]{0,39}", element.role) else "other",
            name_digest=hashlib.sha256(element.name.encode()).hexdigest(),
        )
    return target


def program_summary(content: str) -> str:
    """Retain only bounded program text outside the application data fence."""
    # Never leave an opening fence whose closing marker was truncated away.
    trusted, inside = [], False
    for line in content.splitlines():
        if line == "BEGIN_UNTRUSTED_APPLICATION_DATA":
            inside = True
        elif line == "END_UNTRUSTED_APPLICATION_DATA":
            inside = False
        elif not inside:
            trusted.append(line)
    return next(iter(trusted), "")[:200]


def forget_old_observations(messages: list[dict], *, api_history_chars: int) -> None:
    """Elide older observations and API payloads from the decision message history."""
    seen = kept = 0
    for message in reversed(messages):
        if message.get("_observation"):
            seen += 1
            if seen > 2:
                message["content"] = (
                    message.get("_user_text", message.get("_tool_feedback", ""))
                    + "\n（更早的页面观察已省略）"
                )
                message["_observation"] = False
        elif message.get("_api"):
            kept += len(message["content"])
            if kept > api_history_chars:
                message["content"] = (
                    program_summary(message["content"])
                    + "\n（更早的接口结果已省略；还要用就带 fields 重新读）"
                )
                message["_api"] = False


def observation_cache_valid(obs, turn, channel) -> bool:
    """Return whether a prior fresh page can be cited for another read.

    The cache is turn-local and only covers read/metadata results. The browser
    observer may publish a newer page between model calls, so compare its page
    fingerprint before reusing the cached observation. Epoch-only changes do not
    invalidate the page facts; element refs are still checked by page dispatch.
    """
    cache = turn.get("observation_cache")
    if not isinstance(cache, dict):
        return False
    fingerprint = cache.get("fingerprint")
    if not isinstance(fingerprint, str) or fingerprint != page_fingerprint(obs):
        return False
    message = cache.get("message")
    if not isinstance(message, dict) or not message.get("_observation"):
        return False
    latest = channel.latest_observation_envelope if channel is not None else None
    if latest is not None and page_fingerprint(latest.observation) != fingerprint:
        return False
    return True


def remember_observation(obs, turn, message: dict) -> None:
    """Remember the exact full-page message that backs a reusable observation."""
    if message.get("_observation"):
        turn["observation_cache"] = {
            "fingerprint": page_fingerprint(obs),
            "message": message,
        }


def hold_handoff(executor, s, action: str, args: dict, receipt) -> None:
    """Remember a handoff the page now shows, and drop the memory when anything changes it."""
    if receipt is not None and receipt.ok and action in {"handoff_form", "highlight"} and s.obs is not None:
        executor._held_handoff = {
            "run": s.run_id,
            "fingerprint": executor._page_fingerprint(s.obs),
            "action": action,
            "args": json.dumps(args, sort_keys=True, default=str),
            "receipt": receipt,
        }
    elif action not in {"observe", "wait_for", "form_model"}:
        executor._held_handoff = None


def kept_handoff(executor, s, action: str, args: dict):
    """Return a previously recorded handoff receipt if the page and args are unchanged."""
    held = getattr(executor, "_held_handoff", None)
    if (held is None or action not in {"handoff_form", "highlight"} or held["run"] != s.run_id
            or s.obs is None or held["fingerprint"] != executor._page_fingerprint(s.obs)):
        return None
    if action == held["action"] and json.dumps(args, sort_keys=True, default=str) == held["args"]:
        return held["receipt"]
    return None


def completion_proved(executor, s, name: str, args: dict, result=None, *, target=None) -> bool:
    """Verify via executor receipts that a page step proved completion."""
    # Only receipts produced by this executor's dispatch may justify ending on
    # an unread result. Adapter feedback and model prose never provide proof.
    receipts = getattr(executor, "_tool_receipts", ())
    if result is not None and result.completion_command:
        receipt = next((r for r in receipts if r.command_id == result.completion_command), None)
        if (receipt is None or not receipt.ok or receipt.error_code or receipt.observation is None
                or receipt.url_after != s.obs.url or s.obs.render_state != "ready"):
            return False
        if receipt.action == "handoff_form":
            return receipt.observation.epoch == s.obs.epoch
        admission = getattr(executor, "_admission", None)
        return (receipt.action == "click" and receipt.write_guard_held
                and receipt.command_id in getattr(executor, "_verified_action_commands", ())
                and admission is not None and admission.query_verified(s.obs, command_id=receipt.command_id))
    action = {"goto": "goto", "highlight": "highlight", "click": "click"}.get(name)
    receipt = next((r for r in receipts if r.action == action), None)
    if receipt is None or not receipt.ok or receipt.error_code or receipt.observation is None:
        return False
    if name == "goto":
        if args.get("open"):
            return False  # entering a form still has a fresh result for the model to read
        expected = "/" + str(args["path"]).lstrip("/")
        from urllib.parse import urlsplit
        return (receipt.command_id in getattr(executor, "_verified_action_commands", ())
                and receipt.args_echo.get("url") == receipt.url_after
                and receipt.write_guard_held and s.obs.render_state == "ready"
                and urlsplit(s.obs.url).path == expected and s.obs.page_route == expected
                and urlsplit(receipt.url_after).path == expected)
    if name == "highlight":
        return (receipt.args_echo.get("ref") == "e" + str(args["index"])
                and receipt.observation.epoch <= s.obs.epoch)
    if name == "click" and target is not None and target.role == "tab":
        # A selected section can end on its actual, application-verified click.
        # Bind the original numbered target to a stable live tab identity;
        # a generic button, a same-name replacement or adapter prose cannot do so.
        identity = {key: target.attrs[key] for key in ("id", "aria-controls") if target.attrs.get(key)}
        proof = getattr(executor, "_verified_action_commands", {}).get(receipt.command_id)
        if proof is None:
            return False
        verified = PageObservation.from_json(json.loads(proof))
        selected = [e for e in s.obs.elements if e.role == "tab" and e.name == target.name
                    and all(e.attrs.get(k) == v for k, v in identity.items())]
        return (bool(identity) and len(selected) == 1
                and selected[0].attrs.get("aria-selected") == "true"
                and receipt.args_echo.get("ref") == target.ref
                and receipt.args_echo.get("epoch") == target.epoch
                and receipt.command_id in getattr(executor, "_verified_action_commands", ())
                and receipt.write_guard_held and s.obs.render_state == "ready"
                and receipt.url_before == receipt.url_after == s.obs.url
                and verified.url == s.obs.url and verified.page_route == s.obs.page_route
                and verified.record_route == s.obs.record_route
                and target.epoch < verified.epoch <= s.obs.epoch)
    return False
