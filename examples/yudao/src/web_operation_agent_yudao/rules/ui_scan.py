"""Admin UI (.vue and .ts) scanning primitives for yudao source rules."""
from __future__ import annotations

from pathlib import Path
import re

from .java_scan import _balanced

_MAX_CONDITION = 300

_BUTTON = re.compile(r"<el-button\b((?:[^>\"']|\"[^\"]*\"|'[^']*')*)>(.*?)</el-button>", re.S)
_ATTR = re.compile(r"""([@:\w-]+)(?:\s*=\s*"([^"]*)")?""")
_API_IMPORT = re.compile(r"import\s+(?:\*\s+as\s+(\w+)|\{([^}]*)\})\s+from\s+'@/api/([^']+)'")
_FORM_IMPORT = re.compile(r"import\s+(\w+)\s+from\s+'(\./[^']+\.vue)'")
_API_CALL = re.compile(r"\b(\w+)\.(\w+)\(")
#: The API function `name` and the URL it calls. The shape is the one this release's
#: modules are written in -- `export const updateJob = (data: JobVO) => { return
#: request.put({ url: '/infra/job/update', data }) }` -- which is neither `async` nor
#: `await`; requiring both found no URL in any of the 538 modules, so every button's
#: operation was unknown and its rules never joined the operation's refusals.
_API_URL = (r"\b{name}\s*[:=]\s*(?:async\s*)?\([^)]*\)\s*(?::[^=>{{]+)?=>\s*\{{?\s*"
            r"(?:return\s+)?(?:await\s+)?request\.\w+\(\{{\s*url:\s*[`']([^`'?$]+)")
_FORM_ITEM = re.compile(r"<el-form-item\b([^>]*)>(.*?)</el-form-item>", re.S)
_DISABLED = re.compile(r':disabled="([^"]+)"')
#: Conditions about the page's own state (loading, a selection), not about the record.
_UI_STATE = re.compile(r"^(?:formLoading|disabled|loading)$|selection|currentRow|Loading|isManagerUser|hasPermi")
_ROW_FIELD = re.compile(r"(?:scope\.row|row|\w+(?:\.value)?)\.(\w+)\s*(===|!==|==|!=|>=|<=|>|<)\s*(-?\d+|'[^']*'|true|false)")


def _imported_apis(page: str) -> dict[str, str]:
    """Names a page imports from `@/api/...` (a namespace or named exports) -> the module."""
    out = {}
    for star, names, module_path in _API_IMPORT.findall(page):
        if star:
            out[star] = module_path
        for name in names.split(","):
            name = name.strip().split(" as ")[-1].strip()
            if name:
                out[name] = module_path
    return out


def _function_body(page: str, name: str) -> str | None:
    match = re.search(rf"\b(?:const|function)\s+{re.escape(name)}\b[^{{]*?=>\s*\{{|\bfunction\s+{re.escape(name)}\s*\([^)]*\)\s*\{{", page)
    if match is None:
        return None
    end = _balanced(page, match.end() - 1, "{", "}")
    return page[match.end():end] if end is not None else None


def _readable(condition: str, entity) -> str:
    """A condition with the record's fields and coded values named as the page names them
    ("scope.row.auditStatus === 0" -> "审批状态 === 0（未提交）"), the rest as written."""
    def name(match):
        key, op, value = match.groups()
        label = entity.labels.get(key)
        if not label:
            return match.group(0)
        choice = entity.choices.get(key, {}).get(value.strip("'"))
        return f"{label} {op} {value}" + (f"（{choice}）" if choice else "")
    text = _ROW_FIELD.sub(name, " ".join(condition.split()))
    # How the form was opened, and a field left empty, as the page means them.
    text = re.sub(r"formType\s*!==?\s*'create'", "修改已有记录时（只在新建时可填）", text)
    text = re.sub(r"formType\s*===?\s*'update'", "修改已有记录时", text)
    # The CRM pages' team-permission flags (`PermissionList.vue`): the record's team, with
    # the owner as OWNER and members as READ or WRITE.
    text = re.sub(r"(?:\w+(?:\?)?\.)?validateOwnerUser\b",
                  "当前用户是这条记录的负责人（validateOwnerUser）", text)
    text = re.sub(r"(?:\w+(?:\?)?\.)?validateWrite\b",
                  "当前用户对这条记录有写权限：负责人，或团队中有读写权限的成员（validateWrite）", text)
    return re.sub(r"!\s*(?:formData|scope\.row|row)\.(\w+)",
                  lambda m: f"未填{entity.labels.get(m.group(1), m.group(1))}", text)


def _place(relative: str) -> str:
    """Which of an entity's pages a view file is, as a person tells them apart."""
    name = relative.rsplit("/", 1)[-1]
    if relative.endswith("/detail/index.vue") or "/detail/" in relative:
        return "详情页"
    if name == "index.vue":
        return "列表页"
    if name.endswith("Form.vue"):
        return "表单"
    return ""


def _short(text: str) -> str:
    return text if len(text) <= _MAX_CONDITION else text[:_MAX_CONDITION - 1] + "…"


def scan_buttons(source):
    """(page path, label, condition, permission, operation URL, start, end) of each
    button of the offered modules' pages."""
    for module in source.modules:
        base = source.root / "ui" / "src" / "views" / module
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.vue")):
            relative = source._relative(path)
            page = source._read(relative)
            previous = ""
            for match in _BUTTON.finditer(page):
                attrs = dict((k, v or "") for k, v in _ATTR.findall(match.group(1)))
                label = re.sub(r"<[^>]+>|\s+", "", match.group(2))
                if not label or "{{" in label:
                    continue
                if "v-if" in attrs:
                    condition = previous = attrs["v-if"]
                elif "v-else-if" in attrs:
                    condition = f"!({previous}) && ({attrs['v-else-if']})"
                    previous = attrs["v-else-if"]
                elif "v-else" in attrs:
                    condition = f"!({previous})" if previous else ""
                else:
                    condition = ""
                permission = ", ".join(re.findall(r"'([^']+)'", attrs.get("v-hasPermi", "")))
                url = source._handler_url(page, path, attrs.get("@click", ""))
                start = page.count("\n", 0, match.start()) + 1
                end = page.count("\n", 0, match.end()) + 1
                yield relative, label, condition, permission, url, start, end


def scan_form_fields(source):
    """(page path, field label, condition, start, end) of each form field whose input
    is disabled under a condition on the record or on how the form was opened."""
    for module in source.modules:
        base = source.root / "ui" / "src" / "views" / module
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.vue")):
            relative = source._relative(path)
            page = source._read(relative)
            for match in _FORM_ITEM.finditer(page):
                label = re.search(r'\blabel="([^"]+)"', match.group(1))
                disabled = _DISABLED.search(match.group(2))
                if label is None or disabled is None or _UI_STATE.search(disabled.group(1)):
                    continue
                start = page.count("\n", 0, match.start()) + 1
                end = page.count("\n", 0, match.end()) + 1
                yield relative, label.group(1), disabled.group(1), start, end


def resolve_handler_url(source, page: str, page_path: Path, click: str) -> str:
    """The URL of the operation a click handler ends in: an API call in the handler's
    body, or, for a form it opens, the form's create or update call."""
    match = re.match(r"\s*(\w+)\s*\(([^)]*)\)?", click)
    if match is None:
        return ""
    handler, args = match.group(1), match.group(2) or ""
    imports = _imported_apis(page)
    body = _function_body(page, handler)
    if body is None:
        return ""
    for obj, function in _API_CALL.findall(body):
        if obj in imports:
            url = source._api_url(imports[obj], function)
            if url:
                return url
    kind = re.search(r"'(create|update)'", args)
    if kind is not None:
        # A form it opens: the form's own call for that kind.
        for _name, form in _FORM_IMPORT.findall(page):
            form_path = (page_path.parent / form).resolve()
            if not form_path.is_file() or not form_path.is_relative_to(source.root / "ui" / "src" / "views"):
                continue
            form_text = source._read(source._relative(form_path))
            form_imports = _imported_apis(form_text)
            for obj, function in _API_CALL.findall(form_text):
                if obj in form_imports and function.lower().startswith(kind.group(1)):
                    url = source._api_url(form_imports[obj], function)
                    if url:
                        return url
    return ""
