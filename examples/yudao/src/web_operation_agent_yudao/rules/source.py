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
from dataclasses import dataclass, field
from pathlib import Path
import re
from threading import RLock

from web_operation_agent.goal.index import IndexRule

from .java_scan import (
    _CALL,
    _CASCADE_VERBS,
    _CLASS_MAPPING,
    _DICT_CONST_DECL,
    _ENUM_CODE_VALUE,
    _ENUM_CONSTANT,
    _ENUM_STR_CODE_VALUE,
    _ERROR_CODE,
    _MAPPER_CALL,
    _MAPPER_FIELD,
    _METHOD_MAPPING,
    _METHOD_REF,
    _OPERATION,
    _PERMISSION,
    _SERVICE_CALL,
    _SERVICE_FIELD,
    _UNTRACEABLE,
    _Refusal,
    _call_arity,
    _call_first_arg,
    _callsite_codes,
    _conditions,
    _mask,
    _methods,
    _resolve_overloads,
    _signature_param_types,
    _signature_params,
    _throws,
    _throws_param,
    parse_resp_vo_code_bindings,
)
from .ui_scan import (
    _API_URL,
    _place,
    _readable,
    _short,
    resolve_handler_url,
    scan_buttons,
    scan_form_fields,
)

_MAX_RULES_PER_ENTITY = 60


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


class YudaoSource:
    """The rules of one deployed release of yudao's server and admin UI."""

    # Pure Java parsing only. Actor schemas, permissions, reads and UI observations
    # are never cached here. Every access rehashes the exact current source inputs.
    _server_cache = OrderedDict()
    _server_cache_lock = RLock()
    _ui_cache = OrderedDict()
    _CASCADE_VERBS = _CASCADE_VERBS
    _UNTRACEABLE = _UNTRACEABLE

    def __init__(self, root: Path | str, *, release: str, modules: tuple[str, ...] = ()):
        # Resolved once: this class compares the tree it was handed with paths it resolves
        # itself, and a root that passes through a symlink (macOS `/tmp`, a `current`
        # release link) would make every such comparison fail.
        self.root = Path(root).resolve()
        self.release = release
        # Every module the source holds, unless named: rules are the whole application's.
        # Discovery recurses -- module groups like mall carry nested members
        # (server/yudao-module-mall/yudao-module-product -> "mall/product"), and
        # a group root without Java of its own is no module on its own.
        self.modules = tuple(modules) or tuple(sorted(
            "/".join(seg.removeprefix("yudao-module-") for seg in p.relative_to(self.root / "server").parts)
            for p in (self.root / "server").rglob("yudao-module-*")
            if p.is_dir() and (p / "src" / "main" / "java").is_dir()))
        self._lines: dict[str, list[bytes]] = {}
        self._server_snapshot = None
        self._ui_cache_complete = True
        self._instance_lock = RLock()

    def warm(self) -> None:
        """Precompile server and UI source caches outside live turns."""
        with self._instance_lock:
            self._server_rules()
            self._ui_rules()
            self.dict_type_constants()
            self.enum_value_labels()
            self.code_binding_tables()

    # -- citations ------------------------------------------------------------------------
    def _read(self, relative: str) -> str:
        with self._instance_lock:
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
    def _module_base(self, module: str) -> Path:
        base = self.root / "server"
        for seg in module.split("/"):
            base = base / f"yudao-module-{seg}"
        return base / "src" / "main" / "java"

    def _java_files(self, suffix: str):
        with self._instance_lock:
            if self._server_snapshot is not None:
                return tuple(
                    self.root / p for p in self._server_snapshot
                    if p.startswith("server/") and p.endswith(suffix)
                )
            found: list[Path] = []
            for module in self.modules:
                base = self._module_base(module)
                if base.is_dir():
                    found.extend(sorted(base.rglob(f"*{suffix}")))
            return tuple(found)

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

    def enum_value_labels(self) -> dict[str, dict[str, str]]:
        """Compile-time ``EnumClass -> {code_str: label}`` across ``*Enum.java`` files."""
        with self._instance_lock:
            cached = getattr(self, "_enum_value_labels_cache", None)
            if cached is not None:
                return cached
            out: dict[str, dict[str, str]] = {}
            paths_to_scan = list(self._java_files("Enum.java"))
            fw_dir = self.root / "server" / "yudao-framework"
            if self._server_snapshot is None and fw_dir.is_dir():
                paths_to_scan.extend(sorted(fw_dir.rglob("*Enum.java")))
            for path in paths_to_scan:
                text = self._read(self._relative(path))
                items = _ENUM_CODE_VALUE.findall(text)
                if not items and "ArrayValuable<String>" in text:
                    items = _ENUM_STR_CODE_VALUE.findall(text)
                if items:
                    out[path.stem] = {str(code): label for code, label in items}
            self._enum_value_labels_cache = out
            return out

    def dict_type_constants(self) -> dict[str, str]:
        """Compile-time ``CONST_NAME -> dict_type`` from ``*DictTypeConstants.java``."""
        with self._instance_lock:
            cached = getattr(self, "_dict_const_cache", None)
            if cached is not None:
                return cached
            out: dict[str, str] = {}
            for path in self._java_files("DictTypeConstants.java"):
                out.update(_DICT_CONST_DECL.findall(self._read(self._relative(path))))
            self._dict_const_cache = out
            return out

    def resp_vo_code_bindings(self) -> dict[str, dict[str, tuple[str, str]]]:
        """Compile-time ``VOClass -> {field: ('dict', dict_type) | ('enum', enum_class)}``."""
        return self.code_binding_tables()["resp_by_name"]

    def code_binding_tables(self) -> dict[str, dict]:
        """Compile-time RespVO (with BaseVO inheritance), DO, Save/Update/CreateReqVO, and Controller GET mappings."""
        with self._instance_lock:
            cached = getattr(self, "_code_binding_tables_cache", None)
            if cached is not None:
                return cached
            consts = self.dict_type_constants()
            pkg_mod_re = re.compile(r"^\s*package\s+[\w.]*\.module\.([a-z0-9_]+)\b", re.M)
            extends_re = re.compile(r"\bclass\s+[A-Z]\w*\s+extends\s+([A-Za-z_]\w*)")
            resp_inner_re = re.compile(r"CommonResult<\s*(?:(?:PageResult|List)<\s*)?([A-Za-z_]\w*)\b")

            def _mod_of(text: str, rel: str) -> str:
                m = pkg_mod_re.search(text)
                if m:
                    return m.group(1)
                parts = rel.split("/")
                for tok in parts:
                    if tok.startswith("yudao-module-"):
                        return tok[len("yudao-module-"):]
                return ""

            base_by_mod: dict[tuple[str, str], dict[str, tuple[str, str]]] = {}
            for path in self._java_files("BaseVO.java"):
                rel = self._relative(path)
                txt = self._read(rel)
                b = parse_resp_vo_code_bindings(txt, consts)
                if b:
                    base_by_mod[(_mod_of(txt, rel), path.stem)] = b

            resp_by_mod: dict[tuple[str, str], dict[str, tuple[str, str]]] = {}
            resp_by_name: dict[str, dict[str, tuple[str, str]]] = {}
            for path in self._java_files("RespVO.java"):
                rel = self._relative(path)
                txt = self._read(rel)
                mod = _mod_of(txt, rel)
                own = parse_resp_vo_code_bindings(txt, consts)
                merged: dict[str, tuple[str, str]] = {}
                m_ext = extends_re.search(txt)
                if m_ext:
                    parent = base_by_mod.get((mod, m_ext.group(1)))
                    if parent:
                        merged.update(parent)
                merged.update(own)
                if merged:
                    resp_by_mod[(mod, path.stem)] = merged
                    if path.stem not in resp_by_name or len(merged) > len(resp_by_name[path.stem]):
                        resp_by_name[path.stem] = merged

            do_by_mod: dict[tuple[str, str], dict[str, tuple[str, str]]] = {}
            for path in self._java_files("DO.java"):
                if "/controller/app/" in path.as_posix():
                    continue
                rel = self._relative(path)
                txt = self._read(rel)
                mod = _mod_of(txt, rel)
                b = parse_resp_vo_code_bindings(txt, consts)
                if b and path.stem.endswith("DO"):
                    do_by_mod[(mod, path.stem[:-2])] = b

            save_by_mod: dict[tuple[str, str], dict[str, tuple[str, str]]] = {}
            for suffix in ("SaveReqVO.java", "UpdateReqVO.java", "CreateReqVO.java"):
                stem_len = len(suffix) - len(".java")
                for path in self._java_files(suffix):
                    if "/controller/app/" in path.as_posix():
                        continue
                    rel = self._relative(path)
                    txt = self._read(rel)
                    mod = _mod_of(txt, rel)
                    own = parse_resp_vo_code_bindings(txt, consts)
                    merged_s: dict[str, tuple[str, str]] = {}
                    m_ext = extends_re.search(txt)
                    if m_ext:
                        parent = base_by_mod.get((mod, m_ext.group(1)))
                        if parent:
                            merged_s.update(parent)
                    merged_s.update(own)
                    if merged_s:
                        key = (mod, path.stem[:-stem_len])
                        prev = save_by_mod.get(key)
                        if prev is None:
                            save_by_mod[key] = merged_s
                        else:
                            save_by_mod[key] = {**merged_s, **prev}

            endpoint_mod_vo: dict[str, tuple[str, str]] = {}
            for path in self._java_files("Controller.java"):
                rel = self._relative(path)
                if "/controller/app/" in rel:
                    continue
                txt = self._read(rel)
                base = _CLASS_MAPPING.search(txt)
                if base is None:
                    continue
                mod = _mod_of(txt, rel)
                masked_ctrl = _mask(txt)
                for _name, start, bs, _be in _methods(masked_ctrl):
                    head = txt[max(0, start - 600):bs]
                    head = head[head.rfind(";") + 1:] if ";" in head else head
                    head = head[head.rfind("}") + 1:] if "}" in head else head
                    mapping = _METHOD_MAPPING.search(head)
                    if mapping is None or mapping.group(1) != "Get":
                        continue
                    url = (base.group(1).strip("/") + "/" + mapping.group(2).strip("/")).strip("/")
                    m_rv = resp_inner_re.search(txt[start:bs])
                    vo_cls = m_rv.group(1) if m_rv else ""
                    endpoint_mod_vo[url] = (mod, vo_cls)

            tables = {
                "resp_by_name": resp_by_name,
                "resp_by_mod": resp_by_mod,
                "do_by_mod": do_by_mod,
                "save_by_mod": save_by_mod,
                "endpoint_mod_vo": endpoint_mod_vo,
            }
            self._resp_vo_bindings_cache = resp_by_name
            self._code_binding_tables_cache = tables
            return tables

    def _operations(self) -> dict[str, tuple[str, str, str, str, str, str]]:
        """URL path ("crm/contract/submit") -> (service type, service method, permission,
        operation summary, HTTP verb, controller file), from the controllers."""
        out = {}
        for path in self._java_files("Controller.java"):
            relative = self._relative(path)
            text = self._read(relative)
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
                            summary.group(1) if summary else "", mapping.group(1),
                            relative)
        return out

    def _refusals(self, messages: dict[str, str]) -> dict[tuple[str, str], list[_Refusal]]:
        """(service interface, method) -> the refusals it raises itself, through a method
        of its own class it calls, or through a sibling service method it calls.
        A helper may receive its code as a parameter
        (`validateX(id, USER_ADMIN_NOT_ALLOW_DELETE)`); the call site then names
        the code the helper throws."""
        out: dict[tuple[str, str], list[_Refusal]] = {}
        names = self._enum_names()
        per_file = []
        for path in self._java_files("ServiceImpl.java"):
            relative = self._relative(path)
            raw = self._read(relative)
            text = _mask(raw)
            interface = path.stem[:-len("Impl")]
            sigs: dict[str, list[tuple[int, int, int]]] = {}
            for name, start, body_start, body_end in _methods(text):
                sigs.setdefault(name, []).append((start, body_start, body_end))
            own = {(name, i): _throws(text, bs, be, messages, relative, names, raw=raw)
                   for name, spans in sigs.items() for i, (_s, bs, be) in enumerate(spans)}
            # Parameter-deferred throws: (method, overload) -> list of (arg index
            # naming a code-typed parameter, refusal template without a code).
            deferred = {(name, i): _throws_param(text, s, bs, be, sigs, relative, raw=raw)
                        for name, spans in sigs.items()
                        for i, (s, bs, be) in enumerate(spans)}
            fields = {name: kind for kind, name in _SERVICE_FIELD.findall(text)}
            per_file.append((relative, text, interface, sigs, own, deferred, fields, raw))
        # First pass: own body throws + same-class callee throws. Helper chains
        # nest (`validateA` calls `validateB` which throws), so the walk is
        # transitive with a visited set; the deferred-code call site is read in
        # the body of the method that makes the call. Overloaded callees are
        # picked by argument count; a `::` reference or an unmatched arity takes
        # all overloads.
        local: dict[tuple[str, str], list[_Refusal]] = {}
        scopes: dict[tuple[str, str], list[tuple[str, int, int]]] = {}
        for relative, text, interface, sigs, own, deferred, _fields, _raw in per_file:
            arities = {(name, i): len(_signature_params(text, s, bs))
                       for name, spans in sigs.items()
                       for i, (s, bs, be) in enumerate(spans)}
            ptypes = {(name, i): _signature_param_types(text, s, bs)
                      for name, spans in sigs.items()
                      for i, (s, bs, be) in enumerate(spans)}
            for name, spans in sigs.items():
                visited: set[tuple[str, int]] = set()
                edges = set()  # (caller, callee) -- the deferred code sits in the caller's body
                work = [(name, i) for i in range(len(spans))]
                while work:
                    cur = work.pop()
                    if cur in visited:
                        continue
                    visited.add(cur)
                    cbs, cbe = sigs[cur[0]][cur[1]][1:]
                    cbody = text[cbs:cbe]
                    for match in _CALL.finditer(cbody):
                        called = match.group(1)
                        paren = cbs + match.end() - 1
                        arity = _call_arity(text, paren)
                        first = _call_first_arg(text, paren)
                        for span in _resolve_overloads(sigs, arities, ptypes, called, arity, first):
                            if span != cur:
                                # The edge records WHO called the helper: a shared
                                # validator visited through one caller still owes
                                # every other caller its call-site code.
                                edges.add((cur, span))
                                if span not in visited:
                                    work.append(span)
                    for match in _METHOD_REF.finditer(cbody):
                        for span in _resolve_overloads(sigs, arities, ptypes, match.group(1), None, ""):
                            if span != cur:
                                edges.add((cur, span))
                                if span not in visited:
                                    work.append(span)
                found: list[_Refusal] = []
                for span in visited:
                    found += own[span]
                for caller, callee in edges:
                    cbs, cbe = sigs[caller[0]][caller[1]][1:]
                    cs, cbs2, _cbe2 = sigs[callee[0]][callee[1]]
                    for refusal in deferred.get(callee, ()):
                        for code in _callsite_codes(text, cbs, cbe,
                                                    callee[0], refusal[1], (cs, cbs2)):
                            found.append(_Refusal(code=code, message=messages.get(code, ""),
                                                  condition=refusal[0].condition,
                                                  path=refusal[0].path,
                                                  start=refusal[0].start, end=refusal[0].end))
                deduped = list({(r.code, r.path, r.start): r for r in found}.values())
                local[(interface, name)] = deduped
                scopes[(interface, name)] = [(relative, sigs[v[0]][v[1]][1], sigs[v[0]][v[1]][2])
                                             for v in visited]
        # Second pass: a service-field call inside the scanned bodies raises the
        # callee's own rules on this operation (one hop, other impls' throws only).
        for relative, text, interface, sigs, _own, _def, fields, _raw in per_file:
            for name in sigs:
                # Copy: pass-2 appends siblings' rules for output only. Mutating
                # the stored list would leak a second hop to later services.
                found = list(local[(interface, name)])
                for _rel, bs, be in scopes[(interface, name)]:
                    for match in _SERVICE_CALL.finditer(text[bs:be]):
                        kind = fields.get(match.group(1))
                        if not kind or kind == interface:
                            continue
                        for ref in local.get((kind, match.group(2)), ()):
                            if ref not in found:
                                found.append(ref)
                if found:
                    out[(interface, name)] = found
        return out

    def _cascades(self) -> dict[tuple[str, str], dict[str, list]]:
        """(service interface, method) -> {"calls": [...], "untraceable": [...]}."""
        out: dict[tuple[str, str], dict[str, list]] = {}
        for path in self._java_files("ServiceImpl.java"):
            relative = self._relative(path)
            raw = self._read(relative)
            text = _mask(raw)
            interface = path.stem[:-len("Impl")]
            fields = {name: kind for kind, name in _SERVICE_FIELD.findall(text)}
            mapper_fields = {name: kind for kind, name in _MAPPER_FIELD.findall(text)}
            own_mapper = interface.removesuffix("Service") + "Mapper"
            # A field type imported from outside yudao (Flowable's engine
            # services, etc.) is not a module entity; types without an import
            # sit in the same package and count as business services. Static
            # and wildcard imports count too; a wildcard cannot name its
            # types, so those stay business (conservative, same as before).
            foreign = set()
            for m in re.finditer(r"import\s+(?:static\s+)?([\w.*]+);", text):
                target = m.group(1)
                if not target.startswith("cn.iocoder.yudao.") and not target.endswith(".*"):
                    foreign.add(target.rsplit(".", 1)[-1])
            sigs: dict[str, list[tuple[int, int, int]]] = {}
            for name, start, body_start, body_end in _methods(text):
                sigs.setdefault(name, []).append((start, body_start, body_end))
            arities = {(name, i): len(_signature_params(text, s, bs))
                       for name, spans in sigs.items()
                       for i, (s, bs, be) in enumerate(spans)}
            ptypes = {(name, i): _signature_param_types(text, s, bs)
                      for name, spans in sigs.items()
                      for i, (s, bs, be) in enumerate(spans)}

            def calls_in(body: str, base: int):
                for pattern, kind_map, skip in (
                        (_SERVICE_CALL, fields, interface),
                        (_MAPPER_CALL, mapper_fields, own_mapper)):
                    for match in pattern.finditer(body):
                        kind = kind_map.get(match.group(1))
                        if (not kind or kind == skip or kind in foreign
                                or not self._CASCADE_VERBS.fullmatch(match.group(2))):
                            continue
                        at = base + match.start()
                        condition = " && ".join(_conditions(text, base, at, raw=raw)[0])
                        line = text.count("\n", 0, at) + 1
                        yield kind, match.group(2), relative, line, line, condition

            for name, spans in sigs.items():
                seen: list[tuple[str, str, str, int, int, str]] = []
                unknown: list[tuple[str, str, int]] = []
                visited: set[tuple[str, int]] = set()
                work = [(name, i) for i in range(len(spans))]
                bodies = []
                while work:
                    cur = work.pop()
                    if cur in visited:
                        continue
                    visited.add(cur)
                    cs, cbs, cbe = sigs[cur[0]][cur[1]]
                    bodies.append((cbs, cbe))
                    async_mark = re.search(r"@Async\b", text[cs:cbs])
                    if async_mark:
                        unknown.append(("async", relative,
                                        text.count("\n", 0, cs + async_mark.start()) + 1))
                    cbody = text[cbs:cbe]
                    found_callees = []
                    for match in _CALL.finditer(cbody):
                        paren = cbs + match.end() - 1
                        found_callees.append((match.group(1), _call_arity(text, paren),
                                              _call_first_arg(text, paren)))
                    for match in _METHOD_REF.finditer(cbody):
                        found_callees.append((match.group(1), None, ""))
                    for called, arity, first_arg in found_callees:
                        for span in _resolve_overloads(sigs, arities, ptypes, called, arity, first_arg):
                            if span != cur and span not in visited:
                                work.append(span)
                for bs, be in bodies:
                    for entry in calls_in(text[bs:be], bs):
                        if entry[:2] not in [e[:2] for e in seen]:
                            seen.append(entry)
                    for kind, pattern in self._UNTRACEABLE:
                        for match in pattern.finditer(text[bs:be]):
                            unknown.append((kind, relative,
                                            text.count("\n", 0, bs + match.start()) + 1))
                if seen or unknown:
                    uniq = []
                    for entry in unknown:
                        if entry not in uniq:
                            uniq.append(entry)
                    out[(interface, name)] = {"calls": seen, "untraceable": uniq}
        return out

    # -- admin UI -------------------------------------------------------------------------
    def _api_url(self, module_path: str, function: str) -> str:
        if not any(module_path == m or module_path.startswith(m + "/")
                   for m in self.modules):
            self._ui_cache_complete = False
        for candidate in (f"ui/src/api/{module_path}.ts", f"ui/src/api/{module_path}/index.ts"):
            if (self.root / candidate).is_file():
                found = re.search(_API_URL.format(name=re.escape(function)), self._read(candidate))
                return found.group(1).strip("/") if found else ""
        return ""

    def _handler_url(self, page: str, page_path: Path, click: str) -> str:
        return resolve_handler_url(self, page, page_path, click)

    def _buttons(self):
        return scan_buttons(self)

    def _form_fields(self):
        return scan_form_fields(self)

    def _server_rules(self):
        paths = set()
        suffixes = ("ErrorCodeConstants.java", "Enum.java", "Controller.java", "ServiceImpl.java")
        for module in self.modules:
            base = self._module_base(module)
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
                cascades = {k: {kind: tuple(v) for kind, v in entry.items()}
                            for k, entry in self._cascades().items()}
            finally:
                self._server_snapshot = None
            result = operations, refusals, cascades
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
        with self._instance_lock:
            operations, refusals, cascades = self._server_rules()
            buttons, form_fields = self._ui_rules()
            return self._assemble_rules(entities, operations, refusals, cascades, buttons, form_fields)

    def _assemble_rules(self, entities, operations, refusals, cascades, buttons, form_fields):
        by_dir = sorted((e for e in entities if e.component_dir), key=lambda e: -len(e.component_dir))
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
        for url, (service, method, permission, summary, _verb, _c) in sorted(operations.items()):
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
            cascade = cascades.get((service, method), {})
            for owner in owners:
                about = labels_by_url.get((owner.key, url)) or summary or url.rsplit("/", 1)[-1]
                for other_kind, other_method, path, start, end, condition in cascade.get("calls", ()):
                    entity = other_kind.removesuffix("Service").removesuffix("Mapper")
                    text = f"连带改动：{entity}.{other_method}"
                    if condition:
                        text += "（满足某条件时：" + _short(_readable(condition, owner)) + "）"
                    out[owner.key].append(IndexRule(
                        id=next_id(), kind="cascade", about=about,
                        text=text, citation=self._cite(path, start, end), subject=url))
                if cascade.get("untraceable"):
                    kinds = sorted({k for k, _p, _l in cascade["untraceable"]})
                    _k, first_path, first_line = cascade["untraceable"][0]
                    out[owner.key].append(IndexRule(
                        id=next_id(), kind="cascade", about=about,
                        text="连带改动：未知（源码含" + "/".join(kinds) +
                             "，动态调用、消息或异步任务追不到，可能未列全）",
                        citation=self._cite(first_path, first_line, first_line), subject=url))
        rules = {k: tuple(v[:_MAX_RULES_PER_ENTITY]) for k, v in out.items() if v}
        return rules, {k: tuple(v) for k, v in controls.items() if v}


__all__ = ["EntitySource", "YudaoSource"]
