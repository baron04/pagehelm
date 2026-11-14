"""Yudao's side of the native submit contract.

The core decides what is shown and what is released; this port answers the questions only
the application can: which interfaces belong to the record the page is working on, how to
read that record as the signed-in person, and where the guard that holds the permit lives.

Nothing here is per page or per entity. The interfaces come from what the page itself
read (the list or the record it loaded) and from the application's published API
documentation, which names every write interface under that prefix.
"""
from __future__ import annotations

import re

from web_operation_agent.execution.submit_contract import SubmitPermit

from .client import YudaoReadRefused

_WRITE_METHODS = ("post", "put", "patch", "delete")
_LIST_READ = re.compile(r"/(?:page|list)(?:-[a-z0-9-]+)?$")
_PREFIX = re.compile(r"^/admin-api(?:/[A-Za-z0-9_-]+)+/$")
_MISSING = re.compile(r"不存在|未找到|找不到")
_DANGER = re.compile(r"删除|移除|清除|作废|关闭|撤销|取消|注销|清空|反审|退款|终止|重置密码")


class YudaoSubmitPort:
    """`SubmitPort` for one signed-in person's session."""

    def __init__(self, client, proxy, api_docs):
        self.client, self.proxy, self._api_docs = client, proxy, api_docs

    # -- who ---------------------------------------------------------------------------
    def _actor(self) -> str:
        return self.proxy.resolve_actor(self.client._login(), self.client.tenant)

    # -- what the page itself read -----------------------------------------------------
    def recent_reads(self) -> tuple:
        return self.proxy.guard.recent_reads(self._actor())

    def prefixes(self, reads, *, mode, record_id):
        """Entity prefixes the page reached the record or its list through."""
        found = []
        if mode == "update" and record_id:
            for path, query in reversed(reads):
                head, _, last = path.rpartition("/")
                if last.startswith("get") and f"id={record_id}" in query.split("&"):
                    found.append(head + "/")
        if not found:
            # The page's own table first, newest first; the option lists a dialog loads for its
            # selects come after it and must not push it out.
            for kind in ("page", "list"):
                for path, _query in reversed(reads):
                    last = path.rpartition("/")[2]
                    if _LIST_READ.search(path) and last.split("-")[0] == kind:
                        found.append(path.rpartition("/")[0] + "/")
        out = []
        for prefix in found:
            if _PREFIX.fullmatch(prefix) and prefix not in out:
                out.append(prefix)
        return tuple(out[:4])

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
        self.proxy.guard.arm_permit(actor, permit)

    def disarm(self) -> None:
        self.proxy.guard.disarm_permit(self._actor())

    def settled(self) -> None:
        self.proxy.guard.wait_idle(self._actor(), 5.0)
