"""Build a release-pinned menu route catalog without copying actor data.

Input may be get-permission-info JSON or an offline capture containing it.
The output retains manually reviewed handler entries and stores only route paths.
"""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
from web_operation_agent_yudao.ui_source import UiSource  # noqa: E402


def document(path: Path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as source:
        value = json.load(source)
    if "responses" in value:
        value = value["responses"]["system/auth/get-permission-info"]
    return value.get("data", value)


def menu_routes(nodes):
    routes = {}
    def walk(entries, parent=""):
        for entry in entries:
            name = entry.get("path") or ""
            path = name if name.startswith("/") else parent.rstrip("/") + "/" + name
            if entry.get("children"):
                walk(entry["children"], path)
            elif entry.get("visible", True) and entry.get("component"):
                routes[path] = entry["component"]
    walk(nodes)
    return routes


def table_columns(components, ui_root: Path):
    columns = {}
    sensitive = re.compile(r"password|token|secret|credential|api.?key|密码|密钥|令牌", re.I)
    for route, component in components.items():
        source = ui_root / "src/views" / (component if component.endswith(".vue") else component + ".vue")
        if not source.is_file():
            continue
        mappings, conflicts = {}, set()
        for tag in re.findall(r"<el-table-column\b[^>]*>", source.read_text(encoding="utf-8"), re.S):
            field = re.search(r'\bprop="([A-Za-z][A-Za-z0-9]{0,79})"', tag)
            label = re.search(r'\blabel="([^"<]{1,40})"', tag)
            if not field or not label or sensitive.search(field[1] + label[1]):
                continue
            if field[1] in mappings and mappings[field[1]] != label[1]:
                conflicts.add(field[1])
            mappings[field[1]] = label[1]
        for field in conflicts:
            mappings.pop(field, None)
        if mappings:
            columns[route] = dict(sorted(mappings.items()))
    return dict(sorted(columns.items()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--permission", type=Path, required=True)
    parser.add_argument("--fence", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--ui-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    policy = json.loads(args.policy.read_text(encoding="utf-8"))
    components = menu_routes(document(args.permission).get("menus") or ())
    routes = sorted(components)
    if not routes:
        raise SystemExit("permission response has no visible menu pages")
    prior = policy.get("generated") or {}
    columns = table_columns(components, args.ui_root)
    ui = UiSource.read(args.ui_root)
    router = args.ui_root / "src/router/modules/remaining.ts"
    router_digest = hashlib.sha256(router.read_bytes()).hexdigest()
    policy["handlers"] = [item for item in policy["handlers"]
                          if item["evidence"] != "src/router/modules/remaining.ts"]
    installed = {(item["kind"], item["route"]) for item in policy["handlers"]}
    for menu_route, record_route in sorted(ui.records.items()):
        if menu_route not in components:
            continue
        for kind in ("navigation", "tabs", "query", "paging", "form_entry", "fields", "lines"):
            if (kind, record_route) not in installed:
                policy["handlers"].append({"kind": kind, "route": record_route,
                    "evidence": "src/router/modules/remaining.ts", "sha256": router_digest,
                    "controls": []})
                installed.add((kind, record_route))
    policy["schema_version"] = 2
    policy["generated"] = {
        "routes": routes,
        "excluded": prior.get("excluded", []),
        "menu_sha256": hashlib.sha256(json.dumps(tuple(routes), ensure_ascii=False,
            separators=(",", ":")).encode()).hexdigest(),
        "fence_sha256": hashlib.sha256(args.fence.read_bytes()).hexdigest(),
        "columns": columns,
        "columns_sha256": hashlib.sha256(json.dumps(columns, ensure_ascii=False,
            sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
    }
    args.output.write_text(json.dumps(policy, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(routes)} menu routes to {args.output}")


if __name__ == "__main__":
    main()
