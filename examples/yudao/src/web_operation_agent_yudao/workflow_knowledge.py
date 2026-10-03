"""Offline Vue workflow evidence packs, optional and application-owned.

No question/answer catalogue and no model calls. Literal template labels and
bounded original rule/handler bodies form a draft guide. Dynamic conditions,
backend enforcement and actor permissions are never inferred by this extractor.
"""
from __future__ import annotations

from pathlib import PurePosixPath
import re

from web_operation_agent.planning.product_knowledge import KnowledgeGuide, KnowledgeSection
from web_operation_agent.planning.sourcetree import AccessDenied, SourceTree
from .source_forms import _Template, _walk, _dynamic


MODULE_LABELS = {
    "mall": "商城", "erp": "ERP", "crm": "CRM", "wms": "WMS", "mes": "MES",
    "pms": "项目", "system": "系统", "bpm": "工作流", "infra": "基础设施",
    "member": "会员", "pay": "支付", "hrm": "人事", "oa": "OA", "iot": "物联网",
    "fms": "财务", "ai": "AI", "mp": "公众号",
}
_IMPORT = re.compile(r"import\s+([A-Za-z]\w*)\s+from\s+['\"]([^'\"]+\.vue)['\"]")
_FUNCTION = re.compile(r"^(?:export\s+)?(?:const\s+(\w+)\s*=\s*(?:async\s*)?\([^\n]*?\)\s*=>|(?:async\s+)?function\s+(\w+)\([\s\S]{0,1000}?\))\s*\{", re.M)
_RULES = re.compile(r"^const\s+\w*[Rr]ules\s*=\s*(?:reactive|ref)(?:<[^\n]*?>)?\s*\(", re.M)


def _resolve(path, target, files):
    base = "ui/src/" + target[2:] if target.startswith("@/") else str(PurePosixPath(path).parent) + "/" + target
    parts = []
    for p in base.split("/"):
        if p == "..":
            if not parts:
                return None
            parts.pop()
        elif p != ".":
            parts.append(p)
    candidate = "/".join(parts)
    return candidate if candidate in files else None


def _parse(text):
    parser = _Template()
    try:
        parser.feed(text)
        parser.close()
    except (ValueError, RecursionError):
        return None
    roots = [n for n in parser.roots if n.tag == "template"]
    return roots[0] if parser.valid and not parser.stack and len(roots) == 1 else None


def compile_workflow_guides(tree: SourceTree) -> tuple[KnowledgeGuide, ...]:
    """Build page/form packs from an allowlisted snapshot, not from test prompts.

    Original evidence stays below 4000 characters. Unsupported large bodies are
    split by whole lines. Related components require actual imports and template
    usage; mentioning a product in a module does not create a workflow there.
    """
    files = set(tree.files())
    pages = {}
    for path in sorted(files):
        if not path.startswith("ui/src/views/") or not path.endswith(".vue"):
            continue
        try:
            text = tree.read_text(path)
        except (AccessDenied, OSError, UnicodeError):
            continue
        template = _parse(text)
        if template is not None:
            pages[path] = (text, template)
    sources = {path: text for path, (text, _) in pages.items()}
    router_path = "ui/src/router/modules/remaining.ts"
    if router_path in files:
        try:
            sources[router_path] = tree.read_text(router_path)
        except (AccessDenied, OSError, UnicodeError):
            pass

    def section(path, summary, start, end):
        lines = sources[path].splitlines(keepends=True)
        end = min(end, len(lines))
        out, first, size = [], start, 0
        for number in range(start, end + 1):
            length = len(lines[number - 1])
            if length > 4000:
                return []  # No truncated citation can substitute for original bytes.
            if size + length > 3900 or number - first >= 44:
                ref = tree.cite(path, first, number - 1, excerpt_chars=4000)
                if ref.excerpt:
                    out.append(KnowledgeSection(summary[:1800], (ref,)))
                first, size = number, 0
            size += length
        if size:
            ref = tree.cite(path, first, end, excerpt_chars=4000)
            if ref.excerpt:
                out.append(KnowledgeSection(summary[:1800], (ref,)))
        return out

    def relatives(path, depth=0, visited=None):
        visited = set() if visited is None else visited
        if path in visited or path not in pages or depth > 2:
            return []
        visited.add(path)
        text, template = pages[path]
        used = {node.tag for node, _ in _walk(template)}
        children = []
        for name, target in _IMPORT.findall(text):
            related = _resolve(path, target, files)
            if name.lower() in used and related in pages:
                children.extend(relatives(related, depth + 1, visited))
        return [path, *children]

    # A parent list belongs to a form only through an actual import and usage.
    parents = {}
    for path in pages:
        for child in relatives(path)[1:]:
            parents.setdefault(child, []).append(path)
    # Literal named routes connect separate creation pages to their caller. These
    # are explanatory relationships only, never addresses authorized for goto.
    routes = {}
    if router_path in sources:
        for match in re.finditer(r"component:\s*\(\)\s*=>\s*import\(['\"]@/views/([^'\"]+\.vue)['\"]\),\s*name:\s*['\"]([^'\"]+)['\"]", sources[router_path]):
            target = "ui/src/views/" + match[1]
            if target in pages:
                routes[match[2]] = target
        for parent, (source, _) in pages.items():
            for name in re.findall(r"\bpush\(\s*\{\s*name:\s*['\"]([^'\"]+)['\"]", source):
                if name in routes:
                    parents.setdefault(routes[name], []).append(parent)

    guides = []
    for path, (text, template) in pages.items():
        # Include create-capable forms. Read/detail selectors alone are not roots.
        if not re.search(r"\b\w+(?:Api\.)?create[A-Z]\w*\s*\(|\bcreate[A-Z]\w*\s*\(", text):
            continue
        if not any(node.tag == "el-form" and (node.attrs.get(":model", "").startswith("formData")
                   or not node.attrs.get(":model") and any(
                       child.tag == "el-button" and child.attrs.get("@click") == "submitForm"
                       for child, _ in _walk(node))) for node, _ in _walk(template)):
            continue
        related = relatives(path)
        sections = []
        labels = []
        # Parent entry and its handler must remain evidence, not guessed routing.
        for parent in sorted(set(parents.get(path, [])))[:2]:
            source, root = pages[parent]
            for node, _ in _walk(root):
                handler = node.attrs.get("@click", "") or ""
                if node.tag == "el-button" and re.search(r"create|[Aa]dd", handler):
                    sections.extend(section(parent, "父页面的新增入口声明；是否可见和可执行需以当前页面与账号为准。",
                                            node.start, node.end))
            for match in _FUNCTION.finditer(source):
                if (match[1] or match[2]) in ("openForm", "handleAdd", "handleCreate"):
                    first = source[:match.start()].count("\n") + 1
                    tail = source[match.end():]
                    ending = re.search(r"^}\s*$", tail, re.M)
                    if ending:
                        last = source[:match.end() + ending.end()].count("\n") + 1
                        sections.extend(section(parent, "父页面打开表单的实际处理函数。", first, last))
        for component in related:
            source, root = pages[component]
            nodes = list(_walk(root))
            fields, tabs, controls = [], [], []
            field_labels = {}
            for node, ancestors in nodes:
                attrs = node.attrs
                label = attrs.get("label", "") or ""
                if node.tag == "el-tab-pane" and label and not _dynamic(attrs, "label"):
                    tabs.append(label)
                if node.tag == "el-form-item" and label and not _dynamic(attrs, "label", "prop"):
                    prop = attrs.get("prop", "") or ""
                    conditional = any(set(p.attrs) & {"v-if", "v-show", "v-for", "v-else", "v-else-if"}
                                      for p in (*ancestors, node))
                    fields.append(f"{label}({prop})" + ("[条件展示]" if conditional else ""))
                    labels.append(label)
                    if prop:
                        field_labels[prop] = label
                if node.tag == "el-button" and attrs.get("@click") == "submitForm":
                    controls.append(node)
            if tabs:
                sections.extend(section(component, "模板字面量页签：" + "、".join(tabs) + "。不能用组件名替换这些页签名。",
                                        root.start, min(root.end, root.start + 60)))
            if fields:
                sections.extend(section(component, "表单声明字段：" + "、".join(fields) + "。显示字段不等于必填，条件展示需运行时确认。",
                                        root.start, root.end))
            validators = set()
            for match in _RULES.finditer(source):
                first = source[:match.start()].count("\n") + 1
                tail = source[match.end():]
                closing = re.search(r"^\}\)\s*;?\s*(?://[^\n]*)?$", tail, re.M)
                if closing:
                    last = source[:match.end() + closing.end()].count("\n") + 1
                    rules = tail[:closing.end()]
                    required = re.findall(r"^\s*([A-Za-z]\w*):\s*\[\s*\{\s*required:\s*true\b", rules, re.M)
                    names = [field_labels.get(key, key) + "(" + key + ")" for key in required]
                    validators.update(re.findall(r"\bvalidator:\s*([A-Za-z]\w*)", rules))
                    note = "前端校验规则原文；已提取字面量 required=true：" + ("、".join(names) or "本提取器未识别")
                    note += "。展示条件不等于必填；未识别不证明前端或后端可选。"
                    sections.extend(section(component, note, first, last))
            for match in _FUNCTION.finditer(source):
                if (match[1] or match[2]) not in {"submitForm", "validate", "validateForm", *validators}:
                    continue
                first = source[:match.start()].count("\n") + 1
                tail = source[match.end():]
                closing = re.search(r"^}\s*$", tail, re.M)
                if closing:
                    last = source[:match.end() + closing.end()].count("\n") + 1
                    sections.extend(section(component, "表单校验/保存处理函数原文；只说明这条前端路径，未证明全部服务端规则或执行成功。", first, last))
            for node in controls:
                sections.extend(section(component, "实际保存/确定控件声明；知识问答不执行点击或提交。", node.start, node.end))
        if sections:
            module = path.split("/")[3]
            descriptions = []
            for source_path in [path, *parents.get(path, [])[:2]]:
                found = re.search(r"/\*\*\s*([^*]{1,120})\s*\*/\s*defineOptions", pages[source_path][0])
                if found:
                    descriptions.append(" ".join(found[1].split()))
            component_name = re.search(r"defineOptions\(\{\s*name:\s*['\"]([^'\"]+)", text)
            subject = component_name[1] if component_name else PurePosixPath(path).stem
            subject = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", subject)
            title = MODULE_LABELS.get(module, module) + " " + " ".join(descriptions) + " " + subject + " 表单流程"
            if not descriptions:
                title += " " + "、".join(dict.fromkeys(labels))[:120]
            # No guide generated from an unrelated concept-only page is added.
            priority = {"父页面的新增": 0, "模板字面量": 1, "前端校验": 2,
                        "表单校验/保存": 3, "实际保存": 4, "父页面打开": 5}
            sections.sort(key=lambda s: next((p for prefix, p in priority.items()
                                               if s.summary.startswith(prefix)), 6))
            guides.append(KnowledgeGuide(path, title[:300], tuple(sections[:64])))
    return tuple(guides)


__all__ = ["compile_workflow_guides"]
