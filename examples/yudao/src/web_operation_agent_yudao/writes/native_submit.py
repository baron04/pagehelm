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
from urllib.parse import parse_qsl

from web_operation_agent.execution.submit_contract import (ActionRequest, SubmitConventions,
                                                           SubmitPermit, same_value)

from ..reads.client import YudaoReadRefused

_WRITE_METHODS = ("post", "put", "patch", "delete")
_LIST_READ = re.compile(r"/(?:page|list|tree)(?:-[a-z0-9-]+)?$")
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


def _enrich_erp_line_totals(prefix: str, data: dict) -> dict:
    """Populate Yudao ERP ItemForm client-side deterministic line display totals when absent from GET."""
    if not isinstance(prefix, str) or not prefix.startswith("/admin-api/erp/"):
        return data
    items = data.get("items")
    if not isinstance(items, list):
        return data
    out_items = []
    changed = False
    for raw_item in items:
        if not isinstance(raw_item, dict):
            out_items.append(raw_item)
            continue
        price = raw_item.get("productPrice")
        count = raw_item.get("count")
        if price is None or count is None or isinstance(price, bool) or isinstance(count, bool):
            out_items.append(raw_item)
            continue
        try:
            base = round(float(price) * float(count), 2)
            item = dict(raw_item)
            if "taxPercent" in item or "taxPrice" in item:
                if item.get("totalProductPrice") is None:
                    item["totalProductPrice"] = base
                    changed = True
                total_prod = float(item["totalProductPrice"])
                if item.get("taxPrice") is None:
                    tax_pct = float(item.get("taxPercent") or 0)
                    item["taxPrice"] = round(total_prod * (tax_pct / 100.0), 2)
                    changed = True
                if item.get("totalPrice") is None:
                    item["totalPrice"] = round(total_prod + float(item.get("taxPrice") or 0), 2)
                    changed = True
            else:
                if item.get("totalPrice") is None:
                    item["totalPrice"] = base
                    changed = True
        except (TypeError, ValueError):
            out_items.append(raw_item)
            continue
        out_items.append(item)
    return {**data, "items": out_items} if changed else data


YUDAO_SUBMIT_CONVENTIONS = SubmitConventions(
    volatile_keys=frozenset({"updateTime", "updater", "updaterName", "modifyTime", "version"}),
    record_key="id", line_key="id", name_key="name",
    epoch_zone=timezone(timedelta(hours=8)),
    decode_write=_decode_write)


class YudaoSubmitPort:
    """`SubmitPort` for one signed-in person's session."""

    def contract(self):
        return YUDAO_SUBMIT_CONVENTIONS

    def __init__(self, client, proxy, api_docs, *, private_evidence=None, window="", write_tracker=None):
        if private_evidence is not None and not callable(private_evidence):
            raise TypeError("private_evidence must be callable")
        self.client, self.proxy, self._api_docs = client, proxy, api_docs
        self._private_evidence = private_evidence
        self._write_tracker = write_tracker
        self._active_write_cm = None
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
        """Resolve the write prefix from an explicit form source or unambiguous reads."""
        if not isinstance(source, str):
            return ()
        source_clean = source.strip()
        if source_clean:
            prefix, _, leaf = source_clean.rpartition("/")
            if not _PREFIX.fullmatch(prefix + "/"):
                return ()
            if mode == "update":
                if not leaf.startswith("get") or not any(
                    path == source_clean and f"id={record_id}" in query.split("&")
                    for path, query in reads
                ):
                    return ()
            elif mode == "any" and leaf.startswith("get"):
                if not any(path == source_clean and "id=" in query for path, query in reads):
                    return ()
            elif not _LIST_READ.search(source_clean) or not any(path == source_clean for path, _ in reads):
                return ()
            return (prefix + "/",)
        if not reads:
            return ()
        candidates: list[str] = []
        seen: set[str] = set()
        if mode == "update":
            if not record_id:
                return ()
            for path, query in reads:
                prefix, _, leaf = path.rpartition("/")
                cand = prefix + "/"
                if cand in seen or not _PREFIX.fullmatch(cand):
                    continue
                if leaf.startswith("get") and f"id={record_id}" in query.split("&"):
                    seen.add(cand)
                    candidates.append(cand)
            if len(candidates) > 1:
                return ()
            return tuple(candidates) if len(candidates) == 1 else ()
        if mode == "any":
            for path, _query in reads:
                if not _LIST_READ.search(path):
                    continue
                prefix = path.rpartition("/")[0] + "/"
                if prefix in seen or not _PREFIX.fullmatch(prefix):
                    continue
                if any(
                    (m == "POST" and p == prefix + "create") or (m in {"PUT", "PATCH"} and p == prefix + "update")
                    for m, p in self.catalog((prefix,))
                ):
                    seen.add(prefix)
                    candidates.append(prefix)
            if len(candidates) == 1:
                return tuple(candidates)
            if candidates:
                return ()
            for path, query in reads:
                prefix, _, leaf = path.rpartition("/")
                cand = prefix + "/"
                if cand in seen or not _PREFIX.fullmatch(cand):
                    continue
                if leaf.startswith("get") and "id=" in query and any(
                    m in {"PUT", "PATCH"} and p == cand + "update" for m, p in self.catalog((cand,))
                ):
                    seen.add(cand)
                    candidates.append(cand)
            return tuple(candidates) if len(candidates) == 1 else ()
        for path, _query in reads:
            if not _LIST_READ.search(path):
                continue
            prefix = path.rpartition("/")[0] + "/"
            if prefix in seen or not _PREFIX.fullmatch(prefix):
                continue
            if any(m == "POST" and p == prefix + "create" for m, p in self.catalog((prefix,))):
                seen.add(prefix)
                candidates.append(prefix)
        return tuple(candidates) if len(candidates) == 1 else ()

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
    def _api_paths(self) -> dict:
        docs = self._api_docs() if callable(self._api_docs) else self._api_docs
        if isinstance(docs, dict) and isinstance(docs.get("paths"), dict):
            return docs["paths"]
        return {}

    @staticmethod
    def _required_query_params(paths: dict, api_path: str, *, exclude=("id",)) -> tuple[str, ...]:
        op = paths.get(api_path)
        if not isinstance(op, dict):
            return ()
        get_op = op.get("get")
        if not isinstance(get_op, dict):
            return ()
        out: list[str] = []
        for param in get_op.get("parameters") or ():
            if not isinstance(param, dict):
                continue
            if param.get("in") != "query" or not param.get("required"):
                continue
            name = param.get("name")
            if isinstance(name, str) and name not in exclude and name not in out:
                out.append(name)
        return tuple(out)

    def _resolve_context_params(
        self, prefix: str, required_names: tuple[str, ...], context_params: dict | None
    ) -> dict[str, str]:
        if not required_names:
            return {}
        resolved: dict[str, str] = {}
        if isinstance(context_params, dict):
            for name in required_names:
                val = context_params.get(name)
                if isinstance(val, (str, int)) and not isinstance(val, bool) and str(val).strip():
                    resolved[name] = str(val).strip()
        missing = [n for n in required_names if n not in resolved]
        if not missing:
            return resolved
        try:
            reads = self.recent_reads() or ()
        except Exception:  # noqa: BLE001
            reads = ()
        for path, query in reversed(reads):
            if not path.startswith(prefix) or not query:
                continue
            qmap = dict(parse_qsl(query, keep_blank_values=False))
            for name in list(missing):
                val = qmap.get(name, "").strip()
                if val:
                    resolved[name] = val
                    missing.remove(name)
            if not missing:
                return resolved
        if missing:
            for name in missing:
                vals: set[str] = set()
                for _path, query in reads:
                    if not query:
                        continue
                    for k, v in parse_qsl(query, keep_blank_values=False):
                        if k == name and v.strip():
                            vals.add(v.strip())
                if len(vals) == 1:
                    resolved[name] = next(iter(vals))
        return resolved

    def read_record(self, prefixes, record_id, *, context_params: dict | None = None):
        """The record as the signed-in person reads it; None when it is not there."""
        if not record_id:
            return None
        paths = self._api_paths()
        failure = None
        for prefix in prefixes:
            doc_single = tuple(s for s in ("get", "get-detail") if prefix + s in paths)
            doc_lists = tuple(s for s in ("list", "simple-list", "page") if prefix + s in paths)
            single_suffixes = doc_single if (doc_single or doc_lists) else ("get", "get-detail")
            for suffix in single_suffixes:
                extra = self._resolve_context_params(
                    prefix,
                    self._required_query_params(paths, prefix + suffix),
                    context_params,
                )
                try:
                    data = self.client.get(
                        (prefix + suffix).removeprefix("/admin-api/"),
                        id=record_id,
                        **extra,
                    )
                except YudaoReadRefused as exc:
                    code = getattr(exc, "code", None)
                    if (suffix == "get" or doc_lists) and code in {404, 405}:
                        failure = exc
                        continue
                    if _MISSING.search(str(exc)):
                        return None
                    failure = exc
                    break
                if isinstance(data, dict):
                    return _enrich_erp_line_totals(prefix, data)
                if data is None and suffix == single_suffixes[-1] and not doc_lists:
                    return None
            for list_suffix in doc_lists:
                extra = self._resolve_context_params(
                    prefix,
                    self._required_query_params(paths, prefix + list_suffix, exclude=()),
                    context_params,
                )
                if list_suffix == "page":
                    extra.setdefault("pageNo", 1)
                    extra.setdefault("pageSize", 100)
                try:
                    data = self.client.get(
                        (prefix + list_suffix).removeprefix("/admin-api/"),
                        **extra,
                    )
                except YudaoReadRefused as exc:
                    failure = exc
                    continue
                rows = (
                    data
                    if isinstance(data, list)
                    else data.get("list")
                    if isinstance(data, dict)
                    else None
                )
                if isinstance(rows, list):
                    for row in rows:
                        if isinstance(row, dict) and str(row.get("id")) == str(record_id):
                            return _enrich_erp_line_totals(prefix, row)
                    if isinstance(data, list) or not isinstance(data.get("total"), int) or data.get("total", 0) <= len(rows):
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
        if self._write_tracker is not None and self._active_write_cm is None:
            cm = self._write_tracker()
            cm.__enter__()
            self._active_write_cm = cm
        try:
            self.proxy.guard.arm_permit(actor, permit, owner=self._window_name())
        except BaseException:
            self._release_write_tracker()
            raise

    def _release_write_tracker(self) -> None:
        cm = self._active_write_cm
        self._active_write_cm = None
        if cm is not None:
            cm.__exit__(None, None, None)

    def disarm(self) -> None:
        try:
            self.proxy.guard.disarm_permit(self._actor())
        finally:
            self._release_write_tracker()

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
                                                     create_only=bool(create),
                                                     include_rejected=True)
        except Exception:  # noqa: BLE001 -- a missing observation degrades to no event
            return None
