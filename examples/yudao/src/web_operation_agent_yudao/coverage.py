"""How much of an account's menu the goal adapter can answer about.

Read-only and model-free: it signs in as the account, reads every page menu that account may
open, compiles the same index a goal turn resolves against, and reports per module which pages
map to a readable list and which do not, each unmapped page classified by why. A dashboard or a
settings page that lists no records is one the adapter deliberately does not claim; a page whose
own source reads a list endpoint that the adapter did not take is a defect.

Mapping is not readability: a list may require a parent, an account set or an accounting period
before it answers. `--probe` calls each mapped entity's endpoint once and reports what it did.

    PYTHONPATH=src:examples/yudao/src python -m web_operation_agent_yudao.coverage \\
        --account evaluator --ui-source $YUDAO_CACHE/ui
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

from web_operation_agent.goal.data import DataRefused

from .accounts import add_account_arguments
from .client import YudaoAccount, YudaoClient
from .goal import YudaoGoalData, _leaves, _page_item
from .ui_source import UiSource

#: The endpoint shapes the adapter takes as a page's record list.
_LIST_ENDPOINT = re.compile(r"[/-](page|list|tree)$")


def _source_reason(ui: UiSource, paths: dict, schemas: dict, component: str) -> str:
    """Why the page's own source did not give the adapter a list endpoint."""
    view = ui.views.get(component) if component else None
    if view is None:
        return "no-view"
    endpoint = view.endpoint
    if not endpoint:
        return "no-endpoint"
    if f"/admin-api{endpoint}" not in paths:
        return "endpoint-undocumented"
    if not _LIST_ENDPOINT.search(endpoint):
        return "endpoint-not-a-list"
    if not _page_item(paths[f"/admin-api{endpoint}"].get("get", {}), schemas):
        return "endpoint-not-a-page"
    return "unreachable"


def _name_reason(module: str, tail: list[str], component: str, permissions: set,
                 paths: dict) -> str:
    """Why the words of the page's path and component named no documented list."""
    words = [w for w in component.split("/")[1:-1]] if component.endswith("/index") else []
    candidates = ["-".join(tail), "-".join(tail[-2:]), tail[-1] if tail else "",
                  "-".join(words), words[-1] if words else ""]
    named = False
    for resource in dict.fromkeys(c for c in candidates if c):
        named = True
        if f"/admin-api/{module}/{resource}/page" in paths:
            if f"{module}:{resource}:query" in permissions:
                return "unreachable"
            return "no-query-permission"
    return "no-documented-endpoint" if named else "no-candidate"


def page_coverage(client: YudaoClient, docs: dict, ui: UiSource, data: YudaoGoalData, *,
                  modules: tuple[str, ...] = ()) -> dict:
    """Every page menu the account may open, whether the adapter maps it, and why not."""
    info = client.get("system/auth/get-permission-info")
    permissions = {str(p) for p in info.get("permissions") or ()}
    paths = docs.get("paths", {})
    schemas = docs.get("components", {}).get("schemas", {})
    schema = data.schema()
    entities = {page.key: page.entity for page in schema.pages}
    rows = []
    for menu, (names, trail) in _leaves(info.get("menus") or ()):
        module = trail[0] if trail else ""
        if modules and module not in modules:
            continue
        path = "/" + "/".join(trail)
        component = str(menu.get("component") or "")
        row = {"path": path, "name": str(menu.get("name") or ""), "module": module,
               "component": component, "crumbs": " / ".join(names),
               "entity": entities.get(path, "")}
        if not row["entity"]:
            row["source"] = _source_reason(ui, paths, schemas, component)
            row["name_words"] = _name_reason(module, list(trail[1:]), component, permissions,
                                             paths)
        rows.append(row)
    return {"actor": str((info.get("user") or {}).get("username") or ""),
            "tenant": client.tenant, "with_ui_source": ui.views != {},
            "pages": rows, "modules": _modules(rows)}


def _modules(rows: list[dict]) -> list[dict]:
    out = {}
    for row in rows:
        held = out.setdefault(row["module"], {"pages": 0, "mapped": 0, "why": Counter()})
        held["pages"] += 1
        held["mapped"] += 1 if row["entity"] else 0
        if not row["entity"]:
            held["why"][f"{row['source']}/{row['name_words']}"] += 1
    return [{"module": module, **held, "why": dict(held["why"].most_common())}
            for module, held in sorted(out.items(), key=lambda kv: -kv[1]["pages"])]


def _probe(client: YudaoClient, data: YudaoGoalData, rows: list[dict], *,
           limit: int) -> list[dict]:
    """Read each mapped entity once, through the port a question resolves against: whether
    the adapter can answer about the page at all, and how many records it holds."""
    from web_operation_agent.goal.data import All

    seen, out = set(), []
    for row in rows:
        key = row["entity"]
        if not key or key in seen:
            continue
        seen.add(key)
        spec = data._entities.get(key)
        if spec is None:
            continue
        try:
            read = data.rows(key, All())
            out.append({"entity": key, "endpoint": spec.list_path, "ok": True,
                        "rows": len(read)})
        except DataRefused as exc:
            out.append({"entity": key, "endpoint": spec.list_path, "ok": False,
                        "error": str(exc)[:160]})
        if len(out) >= limit:
            break
    return out


def report(doc: dict) -> str:
    """The coverage as a person reads it."""
    rows, lines = doc["pages"], []
    mapped = sum(1 for r in rows if r["entity"])
    lines.append(f"{doc['actor']} in tenant {doc['tenant']}"
                 f"{' with' if doc['with_ui_source'] else ' without'} the UI source: "
                 f"{mapped}/{len(rows)} page menus map to a readable list")
    lines.append("")
    lines.append(f"{'module':<12} {'mapped':>7}  why the rest did not map")
    for held in doc["modules"]:
        why = ", ".join(f"{count}×{reason}" for reason, count in held["why"].items())
        lines.append(f"{held['module']:<12} {held['mapped']:>3}/{held['pages']:<3} {why}")
    lines.append("")
    for row in rows:
        if row["entity"]:
            continue
        lines.append(f"  {row['crumbs']}  [{row['path']}]  {row['component'] or '-'}"
                     f"  <- {row['source']}/{row['name_words']}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--origin", default="http://127.0.0.1:48088")
    add_account_arguments(parser)
    parser.add_argument("--ui-source", type=Path)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--module", action="append", default=[],
                        help="report only these top-level module paths")
    parser.add_argument("--probe", action="store_true",
                        help="read each mapped entity's list once and report what it did")
    parser.add_argument("--probe-limit", type=int, default=400)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    from .demo import read_api_docs, ui_source

    account = YudaoAccount.load(args.account, args.accounts)
    client = YudaoClient(args.origin, account)
    docs = read_api_docs(args.origin)
    data = YudaoGoalData(client, docs, ui=ui_source(args.ui_source, args.source_root))
    ui = data._ui
    doc = page_coverage(client, docs, ui, data, modules=tuple(args.module))
    if args.probe:
        doc["probe"] = _probe(client, data, doc["pages"], limit=args.probe_limit)
        refused = [p for p in doc["probe"] if not p["ok"]]
        doc["probe_summary"] = {"probed": len(doc["probe"]), "answered": len(doc["probe"]) - len(refused),
                                "refused": len(refused)}
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n")
    print(report(doc))
    if args.probe:
        print("")
        print(json.dumps(doc["probe_summary"], ensure_ascii=False))
        for row in doc["probe"]:
            if not row["ok"]:
                print(f"  {row['entity']} ({row['endpoint']}): {row['error']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
