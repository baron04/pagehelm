"""Every editable native form of the deployed admin UI, found from its source, and a fixed sample.

An editable native form is a list page whose edit button opens an `XxxForm.vue` dialog with
`openForm('update', id)`. The inventory is read from the UI source tree, never from a question
set, and is sorted so that the same tree always gives the same list; the sample is drawn from
that list with a fixed seed and is never redrawn after a failure.
"""
from __future__ import annotations

import json
from pathlib import Path
import random
import re

SEED = 20261115
SAMPLE = 30

_IMPORT = re.compile(r"import\s+(\w+Form)\s+from\s+'(\./[^']+)'")
_OPEN = re.compile(r"""<el-button\b[^>]*?@click="(?:open\w*|handle\w*)\(\s*'(update|create)'\s*(?:,[^)]*)?\)"[^>]*>\s*([^<]*?)\s*(?:<|$)""", re.S)
_OPEN_ANY = re.compile(r"""@click="(\w+)\(\s*'(update|create)'""")
_DIALOG = re.compile(r"<el-form\b")


def editor_bindings(text: str) -> dict[str, dict]:
    """Resolve list edit handlers to template refs. Unknown syntax stays unknown."""
    refs = {}
    for component, ref in re.findall(r"<(\w+Form)\b[^>]*\bref=[\"'](\w+)[\"']", text):
        refs.setdefault(ref, set()).add(component)
    handlers = sorted({name for name, mode in _OPEN_ANY.findall(text) if mode == "update"})
    # Preserve source offsets while removing inert comments and quoted text *before*
    # locating/scanning handler bodies. Comment braces cannot terminate a handler.
    code = re.sub(r""""(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`|//[^\n]*|/\*[\s\S]*?\*/""",
                  lambda match: re.sub(r"[^\n]", " ", match[0]), text)
    resolved, unknown = {}, []
    for handler in handlers:
        name = re.escape(handler)
        head = re.search(rf"(?:function\s+{name}\s*\([^)]*\)\s*(?::[^{{\n]+)?|(?:const|let)\s+{name}\s*=\s*(?:async\s*)?\([^)]*\)\s*(?::[^=\n]+)?=>)\s*", code)
        if not head or head.end() >= len(code) or code[head.end()] != "{":
            unknown.append(handler); continue
        start, depth, stop = head.end(), 0, None
        for index in range(start, len(code)):
            char = code[index]
            if char == "{": depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0: stop = index; break
        body = code[start:stop] if stop is not None else ""
        targets = set(re.findall(r"\b(\w+)(?:\.value)?(?:\?\.|\.)open\s*\(", body))
        components = set().union(*(refs.get(ref, set()) for ref in targets)) if targets else set()
        if len(targets) != 1 or len(components) != 1:
            unknown.append(handler); continue
        component = next(iter(components))
        resolved.setdefault(component, []).append({"handler": handler, "ref": next(iter(targets))})
    imports = {name for name, _ in _IMPORT.findall(text)}
    return {component: {"state": "confirmed" if component in resolved else "unknown" if unknown or not resolved else "different_editor",
                        "bindings": resolved.get(component, []), "unresolved_handlers": unknown}
            for component in imports}


def scan(views: Path) -> list[dict]:
    found = []
    for index in sorted(views.rglob("index.vue")):
        text = index.read_text(encoding="utf-8")
        forms = {name: path for name, path in _IMPORT.findall(text)}
        bindings = editor_bindings(text)
        if not forms:
            continue
        opens = {(kind) for _name, kind in _OPEN_ANY.findall(text)}
        if "update" not in opens:
            continue
        labels = {}
        for kind, label in _OPEN.findall(text):
            labels.setdefault(kind, label.strip())
        for name, path in sorted(forms.items()):
            file = (index.parent / path).with_suffix(".vue") if not path.endswith(".vue") else index.parent / path
            if not file.exists() or not _DIALOG.search(file.read_text(encoding="utf-8")):
                continue
            body = file.read_text(encoding="utf-8")
            # A form that is opened with a record id and loads it: it edits a record. Assignment,
            # send and configuration dialogs that never load one by id are not editable forms.
            if "formType" not in body or not re.search(r"if\s*\(\s*id\s*\)", body):
                continue
            route = "/" + index.parent.relative_to(views).as_posix()
            found.append({"route": route, "page": index.relative_to(views).as_posix(), "form": file.relative_to(views).as_posix(),
                          "component": name, "edit_label": labels.get("update", ""),
                          "create_label": labels.get("create", ""), "can_create": "create" in opens,
                          "has_lines": bool(re.search(r"<el-table\b", body)), "edit_binding": bindings[name]})
    return sorted(found, key=lambda row: (row["route"], row["form"]))


def menu_routes(menus: list[dict]) -> dict[str, str]:
    """Menu component -> the address the application shows it at, from the menu tree itself."""
    by_parent: dict = {}
    for menu in menus:
        by_parent.setdefault(menu.get("parentId"), []).append(menu)
    routes: dict[str, str] = {}

    def walk(parent, prefix):
        for menu in by_parent.get(parent, ()):
            path = str(menu.get("path") or "")
            full = path if path.startswith("/") else (prefix.rstrip("/") + "/" + path if path else prefix)
            component = str(menu.get("component") or "")
            if component:
                routes.setdefault(component, full)
            walk(menu.get("id"), full)
    walk(0, "")
    return routes


def deployed(inventory: list[dict], routes: dict[str, str]) -> list[dict]:
    """The forms whose list page the application, as deployed, actually offers its signed-in
    person, at the address its menu gives. Decided before any sample is drawn."""
    def name(value: str) -> str:
        return value.removeprefix("../views/").removeprefix("views/").removesuffix(".vue").strip("/")
    offered = {name(component): route for component, route in routes.items()}
    return [{**row, "route": offered[name(row["page"])]} for row in inventory if name(row["page"]) in offered]


def sample(inventory: list[dict], seed: int = SEED, size: int = SAMPLE) -> list[dict]:
    return random.Random(seed).sample(inventory, min(size, len(inventory)))


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("views", type=Path)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()
    inventory = scan(args.views)
    chosen = sample(inventory)
    payload = {"seed": SEED, "forms": len(inventory), "sample": chosen}
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    print(len(inventory), "forms;", len(chosen), "sampled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
