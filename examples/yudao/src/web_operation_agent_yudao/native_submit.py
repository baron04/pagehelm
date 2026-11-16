"""Yudao's side of the native submit contract.

The core decides what is shown and what is released; this port answers the questions only
the application can: which interfaces belong to the record the page is working on, how to
read that record as the signed-in person, and where the guard that holds the permit lives.

Nothing here is per page or per entity. The interfaces come from what the page itself
read (the list or the record it loaded) and from the application's published API
documentation, which names every write interface under that prefix.
"""
from __future__ import annotations

import json
import re
from datetime import timedelta, timezone

from web_operation_agent.execution.submit_contract import (ActionRequest, SubmitConventions,
                                                           SubmitPermit, same_value)

from .client import YudaoReadRefused

_WRITE_METHODS = ("post", "put", "patch", "delete")
_LIST_READ = re.compile(r"/(?:page|list)(?:-[a-z0-9-]+)?$")
_PREFIX = re.compile(r"^/admin-api(?:/[A-Za-z0-9_-]+)+/$")
_MISSING = re.compile(r"不存在|未找到|找不到")
_DANGER = re.compile(r"删除|移除|清除|作废|关闭|撤销|取消|注销|清空|反审|退款|终止|重置密码")


def _decode_write(status, body):
    """Yudao's response envelope: a 2xx whose `code` is not 0 refused inside the 200;
    the created record's id arrives as a scalar `data`."""
    answer = None
    if body and status is not None and 200 <= status < 300:
        try:
            envelope = json.loads(body.decode("utf-8"))
            answer = envelope.get("data") if isinstance(envelope, dict) else None
            if isinstance(envelope, dict) and envelope.get("code") not in (0, None):
                status = 500       # the application refused inside a 200
            if isinstance(answer, (dict, list)):
                answer = None
        except (UnicodeDecodeError, ValueError):
            pass
    return status, answer


YUDAO_SUBMIT_CONVENTIONS = SubmitConventions(
    volatile_keys=frozenset({"updateTime", "updater", "updaterName", "modifyTime", "version"}),
    record_key="id", line_key="id", name_key="name",
    epoch_zone=timezone(timedelta(hours=8)),
    decode_write=_decode_write)


class YudaoSubmitPort:
    """`SubmitPort` for one signed-in person's session."""

    def contract(self):
        return YUDAO_SUBMIT_CONVENTIONS

    def __init__(self, client, proxy, api_docs, *, private_evidence=None, window=""):
        if private_evidence is not None and not callable(private_evidence):
            raise TypeError("private_evidence must be callable")
        self.client, self.proxy, self._api_docs = client, proxy, api_docs
        self._private_evidence = private_evidence
        #: Which of the actor's guard windows the page the current turn runs on works
        #: through. Read evidence is scoped to it, so two pages' reads never explain each
        #: other's. A callable resolves it when the guard is asked -- a shared
        #: conversation names the page the turn is running on, which can change per turn.
        self._window = window

    def _window_name(self) -> str:
        return str(self._window() if callable(self._window) else self._window or "")

    def record_private(self, event, fields):
        """Explicit, integration-owned local collector; disabled in ordinary deployments."""
        if self._private_evidence is not None:
            self._private_evidence(event, fields)

    # -- who ---------------------------------------------------------------------------
    def _actor(self) -> str:
        return self.proxy.resolve_actor(self.client._login(), self.client.tenant)

    # -- what the page itself read -----------------------------------------------------
    def recent_reads(self) -> tuple:
        return self.proxy.guard.recent_reads(self._actor(), window=self._window_name() or None)

    def prefixes(self, reads, *, mode, record_id, source=""):
        """One observed source, identified by the mounted form's own route generation."""
        if not isinstance(source, str): return ()
        prefix, _, leaf = source.rpartition("/")
        if not _PREFIX.fullmatch(prefix + "/"): return ()
        if mode == "update":
            if not leaf.startswith("get") or not any(path == source and f"id={record_id}" in query.split("&") for path, query in reads):
                return ()
        elif not _LIST_READ.search(source) or not any(path == source for path, _ in reads):
            return ()
        return (prefix + "/",)

    def row_prefixes(self, reads, row):
        """Only the list rendered by the target row's own table can name its entity."""
        source = row.get("source")
        if not isinstance(source, str) or not _LIST_READ.search(source):
            return ()
        prefix = source.rpartition("/")[0] + "/"
        if not _PREFIX.fullmatch(prefix) or not any(path == source for path, _ in reads):
            return ()
        return (prefix,)

    def row_request(self, prefixes, row, element, record):
        """Bind native scalar toggles to a documented field update, never a whole entity."""
        toggle = row.get("toggle")
        if isinstance(toggle, dict):
            field, before, after = toggle.get("field"), toggle.get("before"), toggle.get("after")
            if not isinstance(field, str) or not re.fullmatch(r"[a-z][A-Za-z0-9]{0,59}", field):
                return None
            if not same_value(record.get(field), before,
                              zone=YUDAO_SUBMIT_CONVENTIONS.epoch_zone) \
                    or same_value(before, after, zone=YUDAO_SUBMIT_CONVENTIONS.epoch_zone):
                return None
        else:
            field, before = "status", record.get("status")
            label = element.name
            if before not in (0, 1) or isinstance(before, bool):
                return None
            if element.role == "switch" and label.startswith("状态开关："):
                checked = element.attrs.get("aria-checked")
                if checked != str(before == 0).lower(): return None
                after = 1 - before
            elif re.search(r"停用|禁用", label): after = 1
            elif "启用" in label: after = 0
            else: return None
        if not isinstance(after, (str, int, float, bool)) or isinstance(after, str) and len(after) > 100:
            return None
        slug = re.sub(r"([A-Z])", lambda m: "-" + m[1].lower(), field)
        candidates = [(method, path) for method, path in self.catalog(prefixes)
                      if method in {"PUT", "PATCH"} and path in {p + "update-" + slug for p in prefixes}]
        if len(candidates) != 1:
            return None
        method, path = candidates[0]
        return ActionRequest(method, path, {"id": record["id"], field: after})

    def catalog(self, prefixes):
        """Every documented write interface under those prefixes."""
        paths = (self._api_docs().get("paths") or {})
        found = set()
        for path, operations in paths.items():
            if not isinstance(operations, dict) or not any(path.startswith(p) for p in prefixes):
                continue
            for method in _WRITE_METHODS:
                if method in operations:
                    found.add((method.upper(), path))
        return frozenset(found)

    # -- the record, as the same person -------------------------------------------------
    def read_record(self, prefixes, record_id):
        """The record as the signed-in person reads it; None when it is not there."""
        if not record_id:
            return None
        failure = None
        for prefix in prefixes:
            try:
                data = self.client.get((prefix + "get").removeprefix("/admin-api/"), id=record_id)
            except YudaoReadRefused as exc:
                if _MISSING.search(str(exc)):
                    return None
                failure = exc
                continue
            if isinstance(data, dict):
                return data
            if data is None:
                return None
        if failure is not None:
            raise failure
        return None

    # -- what the person may not be asked to approve ------------------------------------
    def dangerous(self, element) -> bool:
        """Destructive or irreversible by its label; a control with no label or a trash icon, and
        anything not clearly harmless, counts as dangerous too."""
        labels = " ".join(str(value) for value in (element.name, element.attrs.get("title"),
            element.attrs.get("aria-label"), element.attrs.get("action_name")) if value)
        icon = str(element.attrs.get("action_icon") or "")
        return bool(_DANGER.search(labels) or re.search(r"delete|remove|trash|clear", icon)
                    or not labels.strip() and not icon)

    # -- the guard -----------------------------------------------------------------------
    def arm(self, permit: SubmitPermit) -> None:
        actor = self._actor()
        permit.actor = actor
        self.proxy.guard.arm_permit(actor, permit, owner=self._window_name())

    def disarm(self) -> None:
        self.proxy.guard.disarm_permit(self._actor())

    def settled(self) -> None:
        self.proxy.guard.wait_idle(self._actor(), 5.0)

    # -- the person's own press after a handoff -------------------------------------------
    def user_write_mark(self):
        try:
            return self.proxy.guard.user_write_mark(self._actor())
        except Exception:  # noqa: BLE001 -- a missing observation degrades to no wait
            return None

    def user_write_event(self, prefixes, record_id, mark, create=False):
        if mark is None:
            return None
        try:
            return self.proxy.guard.user_write_after(self._actor(), int(mark),
                                                     tuple(prefixes), str(record_id or ""),
                                                     create_only=bool(create))
        except Exception:  # noqa: BLE001 -- a missing observation degrades to no event
            return None
