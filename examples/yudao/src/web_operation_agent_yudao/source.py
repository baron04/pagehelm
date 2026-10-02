"""Rules of a yudao deployment's source, as index entries on its entities.

yudao states its business rules in two places, and this module reads both, citing the exact
lines of the bound release:

- The admin UI's pages say when a control shows: an `<el-button>` carries a `v-if` condition
  on the record and a `v-hasPermi` permission, and its click handler calls an API function
  whose URL names the operation (`/crm/contract/submit`).
- The server's controllers map that URL to a service method, and the service method (or a
  `validate...` method of the same class it calls) refuses with `throw exception(CODE)`
  under an `if`; the module's `ErrorCodeConstants` hold each code's message.

A control's visibility and the refusals of the operation it calls share the operation's URL
as their `subject`, so the core answers them together. Nothing is read at turn time: the
rules are compiled once, and each citation carries the lines' hash.

Only these files are read: `*Controller.java`, `*ServiceImpl.java`, `ErrorCodeConstants.java`
and `*Enum.java` (the names a refusal's message is filled with) under the offered modules'
`src/main/java`, and `.vue` and `.ts` files under the UI's `src/views` and `src/api` for those
modules.
"""
from __future__ import annotations

import hashlib
from collections import OrderedDict
from threading import RLock
import re
from dataclasses import dataclass, field
from pathlib import Path

from web_operation_agent.goal.index import IndexRule

_MAX_CONDITION = 300
_MAX_RULES_PER_ENTITY = 60

_ENUM_CONSTANT = re.compile(r'\b([A-Z][A-Z0-9_]+)\(\s*-?\d+\s*,\s*"([^"]+)"')
_ENUM_NAME = re.compile(r"\b(\w+Enum)\.([A-Z][A-Z0-9_]+)\.getName\(\)")
_ERROR_CODE = re.compile(r'ErrorCode\s+([A-Z0-9_]+)\s*=\s*new\s+ErrorCode\([^,]+,\s*"([^"]*)"')
_CLASS_MAPPING = re.compile(r'@RequestMapping\(\s*(?:value\s*=\s*)?"([^"]+)"\s*\)\s*(?:@\w+(?:\([^)]*\))?\s*)*public\s+class')
_METHOD_MAPPING = re.compile(r'@(Get|Post|Put|Delete)Mapping\(\s*(?:value\s*=\s*)?(?:\{\s*)?"([^"]+)"')
_OPERATION = re.compile(r'@Operation\(\s*summary\s*=\s*"([^"]+)"')
_PERMISSION = re.compile(r"hasPermission\('([^']+)'\)")
_SERVICE_FIELD = re.compile(r"private\s+(\w+Service)\s+(\w+)\s*;")
_SERVICE_CALL = re.compile(r"\b(\w+Service)\.(\w+)\(")
_METHOD_HEAD = re.compile(
    r"(?m)^[ \t]*(?:@\w+(?:\([^)]*\))?\s*)*(public|private|protected)\s+(?:static\s+)?"
    r"(?:<[^>]+>\s*)?[\w<>\[\],.?\s]+?\s+(\w+)\s*\(")
_THROW = re.compile(r"throw\s+exception\(")
_CALL = re.compile(r"(?<![.\w])(\w+)\s*\(")

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


@dataclass(frozen=True)
class EntitySource:
    """What the compiler needs to know of one entity: its key, the UI component directory
    its pages live in ("crm/contract"), the API path its records sit under
    ("crm/contract"), and its fields' labels and choices for reading conditions."""
    key: str
    component_dir: str
    api_base: str
    labels: dict = field(default_factory=dict)
    #: field -> {value: label}
    choices: dict = field(default_factory=dict)


@dataclass
class _Refusal:
    code: str
    message: str
    condition: str
    path: str
    start: int
    end: int


class YudaoSource:
    """The rules of one deployed release of yudao's server and admin UI."""

    # Pure Java parsing only. Actor schemas, permissions, reads and UI observations
    # are never cached here. Every access rehashes the exact current source inputs.
    _server_cache = OrderedDict()
    _server_cache_lock = RLock()
    _ui_cache = OrderedDict()

    def __init__(self, root: Path | str, *, release: str, modules: tuple[str, ...] = ()):
        # Resolved once: this class compares the tree it was handed with paths it resolves
        # itself, and a root that passes through a symlink (macOS `/tmp`, a `current`
        # release link) would make every such comparison fail.
        self.root = Path(root).resolve()
        self.release = release
        # Every module the source holds, unless named: rules are the whole application's.
        self.modules = tuple(modules) or tuple(sorted(
            p.name.removeprefix("yudao-module-") for p in (self.root / "server").glob("yudao-module-*")
            if p.is_dir()))
        self._lines: dict[str, list[bytes]] = {}
        self._server_snapshot = None
        self._ui_cache_complete = True

    # -- citations ------------------------------------------------------------------------
    def _read(self, relative: str) -> str:
        path = self.root / relative
        if path.resolve() != path or not path.is_relative_to(self.root):
            raise ValueError("source files must stay inside the supplied tree without links")
        if self._server_snapshot is not None and relative not in self._server_snapshot:
            self._ui_cache_complete = False
        raw = (self._server_snapshot[relative] if self._server_snapshot is not None
               and relative in self._server_snapshot else path.read_bytes())
        self._lines[relative] = raw.splitlines(keepends=True)
        return raw.decode("utf-8")

    def _cite(self, relative: str, start: int, end: int) -> str:
        lines = self._lines[relative][start - 1:end]
        digest = hashlib.sha256(b"".join(lines)).hexdigest()
        return f"{relative}:{start}-{end}@{self.release} sha256:{digest}"

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    # -- server ---------------------------------------------------------------------------
    def _java_files(self, suffix: str):
        if self._server_snapshot is not None:
            yield from (self.root / p for p in self._server_snapshot
                        if p.startswith("server/") and p.endswith(suffix))
            return
        for module in self.modules:
            base = self.root / "server" / f"yudao-module-{module}" / "src" / "main" / "java"
            if base.is_dir():
                yield from sorted(base.rglob(f"*{suffix}"))

    def _messages(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for path in self._java_files("ErrorCodeConstants.java"):
            out.update(_ERROR_CODE.findall(self._read(self._relative(path))))
        return out

    def _enum_names(self) -> dict[tuple[str, str], str]:
        """(enum class, constant) -> the name it carries: `CRM_CONTACT(3, "联系人")`."""
        out: dict[tuple[str, str], str] = {}
        for path in self._java_files("Enum.java"):
            for constant, name in _ENUM_CONSTANT.findall(self._read(self._relative(path))):
                out[(path.stem, constant)] = name
        return out

    def _operations(self) -> dict[str, tuple[str, str, str, str]]:
        """URL path ("crm/contract/submit") -> (service type, service method, permission,
        operation summary), from the admin controllers."""
        out = {}
        for path in self._java_files("Controller.java"):
            text = self._read(self._relative(path))
            base = _CLASS_MAPPING.search(text)
            if base is None:
                continue
            fields = {name: kind for kind, name in _SERVICE_FIELD.findall(text)}
            for name, start, body_start, body_end in _methods(text):
                head = text[max(0, start - 600):body_start]
                head = head[head.rfind(";") + 1:] if ";" in head else head
                head = head[head.rfind("}") + 1:] if "}" in head else head
                mapping = _METHOD_MAPPING.search(head)
                if mapping is None:
                    continue
                call = _SERVICE_CALL.search(text[body_start:body_end])
                if call is None or call.group(1) not in fields:
                    continue
                url = (base.group(1).strip("/") + "/" + mapping.group(2).strip("/")).strip("/")
                permission = _PERMISSION.search(head)
                summary = _OPERATION.search(head)
                out[url] = (fields[call.group(1)], call.group(2),
                            permission.group(1) if permission else "",
                            summary.group(1) if summary else "")
        return out

    def _refusals(self, messages: dict[str, str]) -> dict[tuple[str, str], list[_Refusal]]:
        """(service interface, method) -> the refusals it raises itself or through a method
        of its own class it calls."""
        out: dict[tuple[str, str], list[_Refusal]] = {}
        names = self._enum_names()
        for path in self._java_files("ServiceImpl.java"):
            relative = self._relative(path)
            text = self._read(relative)
            interface = path.stem[:-len("Impl")]
            methods = {name: (start, body_start, body_end)
                       for name, start, body_start, body_end in _methods(text)}
            own = {name: _throws(text, body_start, body_end, messages, relative, names)
                   for name, (_s, body_start, body_end) in methods.items()}
            for name, (_s, body_start, body_end) in methods.items():
                found = list(own[name])
                for called in dict.fromkeys(_CALL.findall(text[body_start:body_end])):
                    if called != name and called in own:
                        found += own[called]
                if found:
                    out[(interface, name)] = found
        return out

    # -- admin UI -------------------------------------------------------------------------
    def _api_url(self, module_path: str, function: str) -> str:
        if module_path.split("/", 1)[0] not in self.modules:
            self._ui_cache_complete = False
        for candidate in (f"ui/src/api/{module_path}.ts", f"ui/src/api/{module_path}/index.ts"):
            if (self.root / candidate).is_file():
                found = re.search(_API_URL.format(name=re.escape(function)), self._read(candidate))
                return found.group(1).strip("/") if found else ""
        return ""

    def _handler_url(self, page: str, page_path: Path, click: str) -> str:
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
                url = self._api_url(imports[obj], function)
                if url:
                    return url
        kind = re.search(r"'(create|update)'", args)
        if kind is not None:
            # A form it opens: the form's own call for that kind.
            for _name, form in _FORM_IMPORT.findall(page):
                form_path = (page_path.parent / form).resolve()
                if not form_path.is_file() or not form_path.is_relative_to(self.root / "ui" / "src" / "views"):
                    continue
                form_text = self._read(self._relative(form_path))
                form_imports = _imported_apis(form_text)
                for obj, function in _API_CALL.findall(form_text):
                    if obj in form_imports and function.lower().startswith(kind.group(1)):
                        url = self._api_url(form_imports[obj], function)
                        if url:
                            return url
        return ""

    def _buttons(self):
        """(page path, label, condition, permission, operation URL, start, end) of each
        button of the offered modules' pages."""
        for module in self.modules:
            base = self.root / "ui" / "src" / "views" / module
            if not base.is_dir():
                continue
            for path in sorted(base.rglob("*.vue")):
                relative = self._relative(path)
                page = self._read(relative)
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
                    url = self._handler_url(page, path, attrs.get("@click", ""))
                    start = page.count("\n", 0, match.start()) + 1
                    end = page.count("\n", 0, match.end()) + 1
                    yield relative, label, condition, permission, url, start, end

    def _form_fields(self):
        """(page path, field label, condition, start, end) of each form field whose input
        is disabled under a condition on the record or on how the form was opened."""
        for module in self.modules:
            base = self.root / "ui" / "src" / "views" / module
            if not base.is_dir():
                continue
            for path in sorted(base.rglob("*.vue")):
                relative = self._relative(path)
                page = self._read(relative)
                for match in _FORM_ITEM.finditer(page):
                    label = re.search(r'\blabel="([^"]+)"', match.group(1))
                    disabled = _DISABLED.search(match.group(2))
                    if label is None or disabled is None or _UI_STATE.search(disabled.group(1)):
                        continue
                    start = page.count("\n", 0, match.start()) + 1
                    end = page.count("\n", 0, match.end()) + 1
                    yield relative, label.group(1), disabled.group(1), start, end

    def _server_rules(self):
        paths = set()
        suffixes = ("ErrorCodeConstants.java", "Enum.java", "Controller.java", "ServiceImpl.java")
        for module in self.modules:
            base = self.root / "server" / f"yudao-module-{module}" / "src" / "main" / "java"
            if base.is_dir():
                paths.update(p for p in base.rglob("*.java") if p.name.endswith(suffixes))
        paths = sorted(paths)
        snapshot = {}
        for path in paths:
            if path.resolve() != path or not path.is_relative_to(self.root):
                raise ValueError("source files must stay inside the supplied tree without links")
            snapshot[self._relative(path)] = path.read_bytes()
        inputs = tuple((path, hashlib.sha256(raw).hexdigest()) for path, raw in snapshot.items())
        key = (str(self.root), self.release, self.modules, inputs)
        with self._server_cache_lock:
            cached = self._server_cache.get(key)
            if cached is not None:
                self._server_cache.move_to_end(key)
                # Service citation hashes are still made from current, checked bytes.
                self._lines.update({p: raw.splitlines(keepends=True) for p, raw in snapshot.items()})
                return cached
            self._server_snapshot = snapshot
            try:
                operations = self._operations()
                refusals = {k: tuple(v) for k, v in self._refusals(self._messages()).items()}
            finally:
                self._server_snapshot = None
            result = operations, refusals
            self._server_cache[key] = result
            while len(self._server_cache) > 4:
                self._server_cache.popitem(last=False)
            return result

    def _ui_rules(self):
        paths = set()
        for module in self.modules:
            for directory, suffix in (("views", ".vue"), ("api", ".ts")):
                base = self.root / "ui" / "src" / directory / module
                if base.is_dir():
                    paths.update(base.rglob("*" + suffix))
                flat = base.with_suffix(suffix)
                if flat.is_file():
                    paths.add(flat)
        snapshot = {}
        for path in sorted(paths):
            if path.resolve() != path or not path.is_relative_to(self.root):
                raise ValueError("source files must stay inside the supplied tree without links")
            snapshot[self._relative(path)] = path.read_bytes()
        inputs = tuple((path, hashlib.sha256(raw).hexdigest()) for path, raw in snapshot.items())
        key = (str(self.root), self.release, self.modules, inputs)
        with self._server_cache_lock:
            cached = self._ui_cache.get(key)
            if cached is not None:
                self._ui_cache.move_to_end(key)
                self._lines.update({p: raw.splitlines(keepends=True) for p, raw in snapshot.items()})
                return cached
            self._server_snapshot = snapshot
            self._ui_cache_complete = True
            try:
                result = tuple(self._buttons()), tuple(self._form_fields())
            finally:
                self._server_snapshot = None
            # Imports outside the offered modules do not have a complete change
            # manifest (including previously absent candidates). Do not cache them.
            if self._ui_cache_complete:
                self._ui_cache[key] = result
                while len(self._ui_cache) > 4:
                    self._ui_cache.popitem(last=False)
            return result

    # -- rules ----------------------------------------------------------------------------
    def rules(self, entities: tuple[EntitySource, ...]) -> tuple[dict[str, tuple[IndexRule, ...]],
                                                                  dict[str, tuple[str, ...]]]:
        """(entity key -> its rules, entity key -> the controls its pages show)."""
        operations, refusals = self._server_rules()
        buttons, form_fields = self._ui_rules()
        by_dir = sorted(entities, key=lambda e: -len(e.component_dir))
        out: dict[str, list[IndexRule]] = {e.key: [] for e in entities}
        controls: dict[str, list[str]] = {e.key: [] for e in entities}
        labels_by_url: dict[tuple[str, str], str] = {}
        counter = 0

        def next_id() -> str:
            nonlocal counter
            counter += 1
            return f"r{counter}"

        for relative, label, condition, permission, url, start, end in buttons:
            page_dir = relative.removeprefix("ui/src/views/").rsplit("/", 1)[0]
            owner = next((e for e in by_dir if page_dir == e.component_dir
                          or page_dir.startswith(e.component_dir + "/")), None)
            if owner is None:
                continue
            if (url or permission) and label not in controls[owner.key]:
                controls[owner.key].append(label)
            if url:
                labels_by_url.setdefault((owner.key, url), label)
            if not condition and not permission:
                continue
            parts = []
            if condition:
                parts.append("显示条件：" + _short(_readable(condition, owner)))
            if permission:
                parts.append("需要权限 " + permission)
            out[owner.key].append(IndexRule(
                id=next_id(), kind="visibility", about=label, text="；".join(parts),
                citation=self._cite(relative, start, end), subject=url, place=_place(relative)))
        for relative, label, condition, start, end in form_fields:
            page_dir = relative.removeprefix("ui/src/views/").rsplit("/", 1)[0]
            owner = next((e for e in by_dir if page_dir == e.component_dir
                          or page_dir.startswith(e.component_dir + "/")), None)
            if owner is None:
                continue
            out[owner.key].append(IndexRule(
                id=next_id(), kind="editability", about=label,
                text="不可修改条件：" + _short(_readable(condition, owner)),
                citation=self._cite(relative, start, end)))
        for url, (service, method, permission, summary) in sorted(operations.items()):
            base = url.rsplit("/", 1)[0]
            owners = [e for e in entities if e.api_base == base]
            for owner in owners:
                about = labels_by_url.get((owner.key, url)) or summary or url.rsplit("/", 1)[-1]
                for refusal in refusals.get((service, method), ()):
                    if refusal.code.endswith("_NOT_EXISTS"):
                        continue  # a record that is not there, not a rule of the operation
                    text = refusal.message or refusal.code
                    if refusal.condition:
                        text += "（条件：" + _short(_readable(refusal.condition, owner)) + "）"
                    out[owner.key].append(IndexRule(
                        id=next_id(), kind="refusal", about=about, text=text,
                        citation=self._cite(refusal.path, refusal.start, refusal.end), subject=url))
        rules = {k: tuple(v[:_MAX_RULES_PER_ENTITY]) for k, v in out.items() if v}
        return rules, {k: tuple(v) for k, v in controls.items() if v}


# -- Java reading -----------------------------------------------------------------------------
def _methods(text: str):
    """(name, header start, body start, body end) of each method with a body."""
    for match in _METHOD_HEAD.finditer(text):
        open_paren = match.end() - 1
        close = _balanced(text, open_paren, "(", ")")
        if close is None:
            continue
        brace = text.find("{", close)
        semicolon = text.find(";", close)
        if brace < 0 or (0 <= semicolon < brace):
            continue
        end = _balanced(text, brace, "{", "}")
        if end is None:
            continue
        yield match.group(2), match.start(), brace + 1, end


def _balanced(text: str, start: int, opening: str, closing: str) -> int | None:
    """The index of the bracket closing the one at `start`, skipping strings."""
    depth, i, quote = 0, start, ""
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == opening:
            depth += 1
        elif ch == closing:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _throws(text: str, body_start: int, body_end: int, messages: dict, relative: str,
            names: dict | None = None) -> list[_Refusal]:
    """Each `throw exception(CODE)` in a method body with the `if` conditions it is under.
    A message's `{}` is filled with what the throw passes it, when that is a word the source
    states: a string, or an enum constant's name."""
    out = []
    body = text[body_start:body_end]
    for match in _THROW.finditer(body):
        at = body_start + match.start()
        close = _balanced(text, at + match.end() - match.start() - 1, "(", ")")
        codes = re.findall(r"\b([A-Z][A-Z0-9_]{3,})\b", text[at:close or at])
        codes = [c for c in codes if c in messages]
        if not codes:
            continue
        conditions, first = _conditions(text, body_start, at)
        start_line = text.count("\n", 0, first if first is not None else at) + 1
        end_line = text.count("\n", 0, close or at) + 1
        message = " / ".join(dict.fromkeys(messages[c] for c in codes))
        if "{}" in message:
            passed = text[at:close or at].split(codes[0], 1)[-1]
            words = [m.group(1) or (names or {}).get((m.group(2), m.group(3)), "")
                     for m in re.finditer(r'"([^"]*)"|\b(\w+Enum)\.([A-Z][A-Z0-9_]+)\.getName\(\)',
                                          passed)]
            for word in words:
                if not word or "{}" not in message:
                    break
                message = message.replace("{}", word, 1)
        out.append(_Refusal(code=codes[0], message=message, condition=" && ".join(conditions),
                            path=relative, start=start_line, end=end_line))
    return out


def _conditions(text: str, body_start: int, at: int) -> tuple[list[str], int | None]:
    """The `if` conditions enclosing position `at`, outermost first, and where the
    innermost `if` starts."""
    found: list[tuple[int, str]] = []
    for match in re.finditer(r"\b(else\s+)?if\s*\(", text[body_start:at]):
        start = body_start + match.start()
        paren = body_start + match.end() - 1
        close = _balanced(text, paren, "(", ")")
        if close is None or close > at:
            continue
        after = text[close + 1:at].lstrip()
        condition = " ".join(text[paren + 1:close].split())
        if after.startswith("{"):
            block_end = _balanced(text, close + 1 + (len(text[close + 1:at]) - len(after)), "{", "}")
            if block_end is not None and block_end < at:
                continue
        elif not text[close + 1:at].strip().startswith("throw") and ";" in text[close + 1:at]:
            continue
        found.append((start, condition))
    return [c for _s, c in found], (found[-1][0] if found else None)


# -- UI reading -------------------------------------------------------------------------------
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


def _readable(condition: str, entity: EntitySource) -> str:
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


__all__ = ["EntitySource", "YudaoSource"]
