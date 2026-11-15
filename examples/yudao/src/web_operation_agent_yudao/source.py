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
_SERVICE_FIELD = re.compile(r"private\s+(?:final\s+)?(\w+Service)(?:<[^;{}()]*>)?\s+(\w+)\s*[=;]")
_SERVICE_CALL = re.compile(r"\b(\w+Service)\.(\w+)\(")
_MAPPER_FIELD = re.compile(r"private\s+(?:final\s+)?(\w+Mapper)(?:<[^;{}()]*>)?\s+(\w+)\s*[=;]")
_MAPPER_CALL = re.compile(r"\b(\w+Mapper)\.(\w+)\(")
_METHOD_KEYWORDS = (r"(?:if|for|while|return|throw|switch|case|new|assert|else|"
                    r"try|catch|finally|synchronized|do|import|package|super|"
                    r"this|break|continue|record|class|interface|enum|abstract|"
                    r"default|var|final|static|yield)\b")
_METHOD_HEAD = re.compile(
    r"(?m)^[ \t]*(?:@\w+(?:\([^)]*\))?\s*)*(?:(public|private|protected)\s+)?"
    r"(?:(?:static|final|synchronized|abstract|native|strictfp)\s+)*(?:<[^>]+>\s*)?"
    r"(?!" + _METHOD_KEYWORDS + r")"
    # Horizontal whitespace only inside the bounded type segment, and the
    # type-name gap bounded too: masked regions (text blocks, long comments
    # blanked to whitespace) must not give the quantifiers a quadratic runway.
    # A return type stays on its own line in practice; a wrapped name still
    # matches through the newline-capable gap.
    r"[\w<>\[\],.? \t]{1,300}?\s{1,20}?(?!" + _METHOD_KEYWORDS + r")(\w+)\s*\(")
_THROW = re.compile(r"throw\s+exception\(")
#: A call on this class by bare or explicit name: `helper(`, `this.helper(`,
#: `super.helper(`. Receiver-qualified calls (`svc.helper(`) stay out -- they
#: belong to the field-call patterns, not to same-class resolution.
_CALL = re.compile(r"(?<![.\w])(?:this\.|super\.)?(\w+)\s*\(")
#: `this::helper` / `Type::helper` references -- a same-class callee handed to a
#: lambda (`convertList(ids, this::validateX)`) has no `(` after its name.
_METHOD_REF = re.compile(r"::\s*(\w+)")


def _mask(text: str) -> str:
    """Blank comments and literal contents (strings, chars, text blocks) with
    spaces, preserving length and newlines: every position, line number and
    citation stays valid while code-shaped text inside them stops matching."""
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        if text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            for k in range(i, j):
                out[k] = " "
            i = j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            for k in range(i, j):
                if text[k] != "\n":
                    out[k] = " "
            i = j
        elif text.startswith('"""', i):
            j = text.find('"""', i + 3)
            j = n if j < 0 else j + 3
            for k in range(i, j):
                if text[k] != "\n":
                    out[k] = " "
            i = j
        elif text[i] in "\"'":
            quote, j = text[i], i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == quote:
                    j += 1
                    break
                if text[j] == "\n" and quote == "'":
                    break
                j += 1
            for k in range(i, min(j, n)):
                if text[k] != "\n":
                    out[k] = " "
            i = j
        else:
            i += 1
    return "".join(out)


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
    def _module_base(self, module: str) -> Path:
        base = self.root / "server"
        for seg in module.split("/"):
            base = base / f"yudao-module-{seg}"
        return base / "src" / "main" / "java"

    def _java_files(self, suffix: str):
        if self._server_snapshot is not None:
            yield from (self.root / p for p in self._server_snapshot
                        if p.startswith("server/") and p.endswith(suffix))
            return
        for module in self.modules:
            base = self._module_base(module)
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
        operation summary, HTTP verb, controller file), from the controllers."""
        out = {}
        for path in self._java_files("Controller.java"):
            relative = self._relative(path)
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

            def resolve(called: str, arity: int | None, first_arg: str):
                candidates = [(called, i) for i in range(len(sigs.get(called, [])))]
                if arity is None:
                    return candidates
                matched = [span for span in candidates if arities[span] == arity]
                if not matched:
                    return candidates
                if len(matched) > 1 and arity:
                    tag = _arg_is_coll(first_arg)
                    if tag is not None:
                        narrowed = [span for span in matched
                                    if ptypes[span] and
                                    bool(_COLL_PARAM.search(ptypes[span][0])) == tag]
                        if narrowed:
                            return narrowed
                return matched

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
                        for span in resolve(called, arity, first):
                            if span != cur:
                                # The edge records WHO called the helper: a shared
                                # validator visited through one caller still owes
                                # every other caller its call-site code.
                                edges.add((cur, span))
                                if span not in visited:
                                    work.append(span)
                    for match in _METHOD_REF.finditer(cbody):
                        for span in resolve(match.group(1), None, ""):
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

    #: The verb families an entity change goes through on a service interface.
    #: `batch*` counts only before a write verb -- `batchGet`/`batchQuery` are
    #: reads, `batchUpdate`/`batchDelete` are writes.
    _CASCADE_VERBS = re.compile(
        r"(?:create|insert|save|update|delete|remove|add|audit|submit|"
        r"approve|reject|cancel|close|open|enable|disable|reduce|increase|"
        r"bind|unbind|copy|import|send|sync|init|clear|reset|stop|finish|"
        r"complete|pay|refund|ship|receive|stock|deduct|rollback|handle|"
        r"process|change|set|move|fill|modify|edit|confirm|sign|transfer|"
        r"merge|lock|unlock|release|grant|revoke|assign|dispatch|schedule|"
        r"activate|deactivate|register|generate|build|restock|mark|"
        r"batch(?:create|insert|save|update|delete|remove|add|audit|submit|"
        r"approve|reject|cancel|close|open|enable|disable|reduce|increase|"
        r"bind|unbind|copy|import|send|sync|init|clear|reset|stop|finish|"
        r"complete|pay|refund|ship|receive|stock|deduct|rollback|handle|"
        r"process|change|set|move|fill|modify|edit|confirm|sign|transfer|"
        r"merge|lock|unlock|release|grant|revoke|assign|dispatch|schedule|"
        r"activate|deactivate|register|generate|build|restock|mark))\w*", re.I)

    #: Mutation paths a static walk cannot follow: Spring events, async
    #: dispatch, message producers/templates, reflection and dynamic beans.
    #: A `*Sender` that names mail is synchronous SMTP, not a broker.
    _UNTRACEABLE = (
        ("event", re.compile(r"\bpublishEvent\s*\(")),
        ("async", re.compile(r"\b(?:CompletableFuture\.(?:runAsync|supplyAsync)|"
                            r"registerSynchronization|afterCommit)\s*\(")),
        ("mq", re.compile(r"\b(?!\w*[Mm]ail)\w*(?:[Pp]roducer|[Tt]emplate|[Ss]ender|"
                          r"[Bb]roker|mq)\w*\."
                          r"(?:send\w*|convertAndSend|publish|syncSend|asyncSend|convert)\s*\(")),
        ("reflection", re.compile(r"(?:\.\s*invoke\s*\(|\.\s*newInstance\s*\(|"
                                  r"\bClass\.forName\s*\()")),
        ("dynamic-bean", re.compile(r"\b(?:getBean|getBeanProvider)\s*\("
                                    r"(?!getClass\s*\(|this\s*\.\s*getClass\s*\()")),
        ("executor", re.compile(r"\b\w*[Ee]xecutor\w*\.(?:submit|execute|invokeAll|invokeAny|schedule)\s*\("
                                r"|\bnew\s+Thread\s*\(")),
    )

    def _cascades(self) -> dict[tuple[str, str], dict[str, list]]:
        """(service interface, method) -> {"calls": [...], "untraceable": [...]}.

        "calls" are other entities the operation writes: mutations through
        another service interface, or through a mapper whose entity differs
        from this service's own (a mutation of the record's own table belongs
        to its own storage). Each call is (type, method, file, start, end,
        condition) — a nonempty condition reads "when <condition> holds".
        Framework engines (Flowable, etc.) are out of scope. "untraceable"
        marks dynamic dispatch, messaging and async work the walk cannot
        follow, as (kind, file, line)."""
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
                for match in _SERVICE_CALL.finditer(body):
                    kind = fields.get(match.group(1))
                    if (not kind or kind == interface or kind in foreign
                            or not self._CASCADE_VERBS.fullmatch(match.group(2))):
                        continue
                    at = base + match.start()
                    condition = " && ".join(_conditions(text, base, at, raw=raw)[0])
                    line = text.count("\n", 0, at) + 1
                    yield kind, match.group(2), relative, line, line, condition
                for match in _MAPPER_CALL.finditer(body):
                    kind = mapper_fields.get(match.group(1))
                    if (not kind or kind == own_mapper or kind in foreign
                            or not self._CASCADE_VERBS.fullmatch(match.group(2))):
                        continue
                    at = base + match.start()
                    condition = " && ".join(_conditions(text, base, at, raw=raw)[0])
                    line = text.count("\n", 0, at) + 1
                    yield kind, match.group(2), relative, line, line, condition

            for name, spans in sigs.items():
                seen: list[tuple[str, str, str, int, int, str]] = []
                unknown: list[tuple[str, str, int]] = []
                # Same-class helpers mutate for their caller, as refusals do;
                # helper chains nest, so the walk is transitive, resolving
                # overloads by call-site arity.
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
                        candidates = [(called, i) for i in range(len(sigs.get(called, [])))]
                        if arity is not None:
                            matched = [span for span in candidates if arities[span] == arity]
                            if matched:
                                if len(matched) > 1 and arity:
                                    tag = _arg_is_coll(first_arg)
                                    if tag is not None:
                                        narrowed = [span for span in matched
                                                    if ptypes[span] and
                                                    bool(_COLL_PARAM.search(ptypes[span][0])) == tag]
                                        if narrowed:
                                            matched = narrowed
                                candidates = matched
                        for span in candidates:
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
        operations, refusals, cascades = self._server_rules()
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
                for other_kind, other_method, path, start, end, condition in cascade.get(
                        "calls", ()):
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


# -- Java reading -----------------------------------------------------------------------------
def _param_parens(head: str) -> tuple[int, int] | None:
    """The (open, close) of the method's parameter list inside its head text.
    Annotations before the signature (`@Transactional(...)`) and on parameters
    (`@Size(max=1)`) carry their own parens; the parameter list is the group
    whose close is the last `)` before the body."""
    close = head.rfind(")")
    if close < 0:
        return None
    depth = 0
    for i in range(close, -1, -1):
        if head[i] == ")":
            depth += 1
        elif head[i] == "(":
            depth -= 1
            if depth == 0:
                return i, close
    return None


def _signature_params(text: str, start: int, body_start: int) -> list[str]:
    """Parameter names of the method whose head spans `start..body_start`."""
    head = text[start:body_start]
    parens = _param_parens(head)
    if parens is None:
        return []
    params = head[parens[0] + 1:parens[1]]
    names = []
    for part in _split_params(params):
        words = re.sub(r"@\w+(?:\([^)]*\))?\s*", "", part).split()
        if len(words) >= 2:
            names.append(words[-1])
    return names


def _split_params(params: str) -> list[str]:
    """Comma split at top level only: generics (`Map<String, Long>`), arrays,
    annotation lists and literals never break a parameter apart."""
    parts, depth, angle, start, quote, esc = [], 0, 0, 0, "", False
    for i, ch in enumerate(params):
        if quote:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        elif ch == "<":
            angle += 1
        elif ch == ">":
            angle = max(0, angle - 1)
        elif ch == "," and depth == 0 and angle == 0:
            parts.append(params[start:i])
            start = i + 1
    parts.append(params[start:])
    return [part.strip() for part in parts]


def _throws_param(text: str, start: int, body_start: int, body_end: int,
                  methods: dict, relative: str, raw: str | None = None) -> list[tuple[_Refusal, str]]:
    """Throws `exception(x)` where `x` is one of the method's own parameters:
    the refusal is real but its code is picked by the caller."""
    params = _signature_params(text, start, body_start)
    if not params:
        return []
    out = []
    body = text[body_start:body_end]
    for match in _THROW.finditer(body):
        at = body_start + match.start()
        if _try_swallowed(text, body_start, body_end, at):
            continue
        close = _balanced(text, at + match.end() - match.start() - 1, "(", ")")
        if close is None:
            continue
        arg = _call_first_arg(text, at + match.end() - match.start() - 1).strip()
        if re.fullmatch(r"\w+", arg) and arg in params:
            conditions, first = _conditions(text, body_start, at, raw=raw)
            start_line = text.count("\n", 0, first if first is not None else at) + 1
            out.append((_Refusal(code="", message="", condition=" && ".join(conditions),
                                 path=relative, start=start_line,
                                 end=text.count("\n", 0, close) + 1), arg))
    return out


#: `<` opens an angle bracket only in generic syntax (after a Capitalized type
#: name, a closing `>`, `?`, `,` or a bracket) -- not in `a < b`.
_GENERIC_LT = re.compile(r"(?:[A-Z][\w$]*|[>\?\[\],])\s*$")


def _call_arity(text: str, open_paren: int) -> int | None:
    """Argument count of the call whose `(` sits at `open_paren`; None if the
    parens do not balance. Overloaded siblings are told apart by arity."""
    close = _balanced(text, open_paren, "(", ")")
    if close is None:
        return None
    if not text[open_paren + 1:close].strip():
        return 0
    args, depth, angle, quote, esc = 1, 0, 0, "", False
    for i in range(open_paren + 1, close):
        ch = text[i]
        if quote:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
        elif ch == "<":
            angle += bool(_GENERIC_LT.search(text, max(0, i - 24), i))
        elif ch == ">":
            angle = max(0, angle - 1)
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0 and angle == 0:
            args += 1
    return args


def _call_first_arg(text: str, open_paren: int) -> str:
    """First argument's text of the call whose `(` sits at `open_paren`."""
    close = _balanced(text, open_paren, "(", ")")
    if close is None:
        return ""
    inner = text[open_paren + 1:close]
    inner_start = open_paren + 1
    depth, angle, quote, esc = 0, 0, "", False
    for i, ch in enumerate(inner):
        if quote:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
        elif ch == "<":
            angle += bool(_GENERIC_LT.search(text, max(0, inner_start + i - 24), inner_start + i))
        elif ch == ">":
            angle = max(0, angle - 1)
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0 and angle == 0:
            return inner[:i]
    return inner


_COLL_PARAM = re.compile(r"\b(?:Collection|List|Set|Map|Iterable)\b|\.\.\.|\[\]")
_COLL_ARG = re.compile(
    r"\b(?:ids|list|set|collection|map)\b|getIds\(|getList\(|IdList|toSet\(|"
    r"toList\(|convertList|convertSet|emptyIfNull|List\.of|Set\.of|Collections\.|"
    r"Arrays\.asList|new\s+\w*(?:List|Set|Map)\b", re.I)
_SCALAR_ARG = re.compile(
    r"getId\(|getNo\(|getStatus\(|getType\(|getCount\(|\.toString\(\)|"
    r"Long\.|Integer\.|String\.|^[\d'\"]|^true$|^false$|^null$")


def _arg_is_coll(arg: str) -> bool | None:
    """The first argument looks collection-typed (True), scalar (False), or
    undecidable (None -> keep every same-arity overload)."""
    a = arg.strip()
    if _COLL_ARG.search(a):
        return True
    if _SCALAR_ARG.search(a):
        return False
    return None


def _signature_param_types(text: str, start: int, body_start: int) -> list[str]:
    """Parameter type expressions of the method whose head spans
    `start..body_start`, parallel to `_signature_params`."""
    head = text[start:body_start]
    parens = _param_parens(head)
    if parens is None:
        return []
    params = head[parens[0] + 1:parens[1]]
    types = []
    for part in _split_params(params):
        words = re.sub(r"@\w+(?:\([^)]*\))?\s*", "", part).split()
        if len(words) >= 2:
            types.append(" ".join(words[:-1]))
    return types


def _callsite_codes(text: str, body_start: int, body_end: int, called: str,
                    param: str, callee_sig: tuple[int, int]) -> list[str]:
    """The literal code constant a caller passes for the callee's parameter
    `param`, at each `called(...)` site in its body."""
    params = _signature_params(text, callee_sig[0], callee_sig[1])
    if param not in params:
        return []
    index = params.index(param)
    out = []
    for match in re.finditer(r"(?<![.\w])(?:(?:this|super)\.)?" + re.escape(called)
                             + r"\s*\(", text[body_start:body_end]):
        open_paren = body_start + match.end() - 1
        close = _balanced(text, open_paren, "(", ")")
        if close is None:
            continue
        args, depth, angle, start, quote, esc = [], 0, 0, open_paren + 1, "", False
        for i in range(open_paren + 1, close):
            ch = text[i]
            if quote:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == quote:
                    quote = ""
                continue
            if ch in "\"'":
                quote = ch
            elif ch == "<":
                angle += bool(_GENERIC_LT.search(text, max(0, i - 24), i))
            elif ch == ">":
                angle = max(0, angle - 1)
            elif ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth = max(0, depth - 1)
            elif ch == "," and depth == 0 and angle == 0:
                args.append(text[start:i].strip())
                start = i + 1
        args.append(text[start:close].strip())
        if index < len(args):
            const = re.fullmatch(r"(?:\w+\.)*([A-Z][A-Z0-9_]{3,})", args[index] or "")
            if const is not None:
                out.append(const.group(1))
    return out


_HEAD_BAD_WORD = re.compile(
    r"\b(?:new|return|throw|if|else|for|while|switch|case|do|assert|break|"
    r"continue|try|catch|finally|import|package|super|this|enum|class|"
    r"interface|record|yield|instanceof)\b")


_TYPE_DECL = re.compile(r"\b(?:class|interface|enum|record|@interface)\s+\w+")
_ANON_NEW = re.compile(r"\bnew\s+[\w.$<>\[\]]+(?:\s*\([^()]*\))?\s*\{")


def _type_spans(text: str) -> list[tuple[int, int]]:
    """Brace spans of every type body: named types and anonymous `new X(){}`."""
    spans = []
    for match in _TYPE_DECL.finditer(text):
        brace = text.find("{", match.end())
        if brace < 0:
            continue
        end = _balanced(text, brace, "{", "}")
        if end is not None:
            spans.append((brace, end))
    for match in _ANON_NEW.finditer(text):
        end = _balanced(text, match.end() - 1, "{", "}")
        if end is not None:
            spans.append((match.end() - 1, end))
    return spans


def _methods(text: str):
    """(name, header start, body start, body end) of each method with a body.
    Only members of the outermost type count: a same-named method inside a
    nested or anonymous class is not an overload of the service's own."""
    spans = _type_spans(text)

    def depth(pos: int) -> int:
        return sum(1 for start, end in spans if start < pos < end)

    for match in _METHOD_HEAD.finditer(text):
        if depth(match.start()) > 1:
            continue
        # `[\w\s]+?` can backtrack a leading statement keyword (`new Foo().bar(`)
        # into the "return type" span; reject any that slipped past the lookaheads.
        # Annotations live on their own lines and may legitimately carry `.class`
        # or keyword words, so only the signature's own line is checked.
        line_start = text.rfind("\n", 0, match.start(2)) + 1
        if _HEAD_BAD_WORD.search(text, line_start, match.start(2)):
            continue
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


#: Catch types that cover the `ServiceException` family `exception(...)` throws.
#: Anything narrower is a sibling type and does not catch it; anything unknown
#: is treated as not covering, so the refusal is kept (conservative).
_SWALLOW_TYPES = {"Exception", "RuntimeException", "Throwable", "ServiceException"}


def _try_swallowed(text: str, body_start: int, body_end: int, at: int) -> bool:
    """Position `at` sits in a `try` whose covering `catch` swallows the throw.
    Enclosing tries are walked innermost-first: a catch covers the throw only
    for a known supertype (`Exception`/`RuntimeException`/`Throwable`/
    `ServiceException`, multi-catch included); `throw e` propagates to the next
    enclosing try, and an uncovered try propagates the same way."""
    body = text[body_start:body_end]
    trys = []
    for match in re.finditer(r"\btry\b", body):
        cursor = body_start + match.end()
        if text[cursor:body_end].lstrip().startswith("("):
            open_paren = body_end - len(text[cursor:body_end].lstrip())
            resources = _balanced(text, open_paren, "(", ")")
            if resources is None:
                continue
            cursor = resources + 1
        brace = text.find("{", cursor)
        if brace < 0 or brace >= body_end or text[cursor:brace].strip():
            continue
        try_end = _balanced(text, brace, "{", "}")
        if try_end is not None and brace < at < try_end:
            trys.append((brace, try_end))
    for _brace, try_end in sorted(trys, reverse=True):
        cursor = try_end + 1
        rethrown = False
        while True:
            catch = re.match(r"\s*catch\s*\(([^)]*)\)\s*\{", text[cursor:body_end])
            if catch is None:
                break  # finally or nothing -- this try does not catch
            types_part = catch.group(1).strip()
            parts = types_part.rsplit(None, 1)
            var = parts[-1] if len(parts) == 2 else ""
            types_text = re.sub(r"\b(?:final|var)\s+", "", parts[0] if len(parts) == 2 else "")
            types = {seg.strip().split(".")[-1]
                     for seg in re.split(r"[|,]", types_text) if seg.strip()}
            catch_brace = cursor + catch.end() - 1
            catch_end = _balanced(text, catch_brace, "{", "}")
            catch_body = text[catch_brace:catch_end or body_end]
            cursor = (catch_end if catch_end is not None else body_end) + 1
            if not types & _SWALLOW_TYPES:
                continue  # this catch cannot see the throw; try the next clause
            if var and re.search(rf"\bthrow\s+{re.escape(var)}\b", catch_body):
                rethrown = True  # `throw e` propagates to the next enclosing try
                break
            return True  # logged, wrapped or ignored -- the code does not escape
        if rethrown:
            continue
    return False


def _throws(text: str, body_start: int, body_end: int, messages: dict, relative: str,
            names: dict | None = None, raw: str | None = None) -> list[_Refusal]:
    """Each `throw exception(CODE)` in a method body with the `if` conditions it is under.
    A message's `{}` is filled with what the throw passes it, when that is a word the source
    states: a string, or an enum constant's name."""
    out = []
    body = text[body_start:body_end]
    for match in _THROW.finditer(body):
        at = body_start + match.start()
        if _try_swallowed(text, body_start, body_end, at):
            continue
        close = _balanced(text, at + match.end() - match.start() - 1, "(", ")")
        codes = re.findall(r"\b([A-Z][A-Z0-9_]{3,})\b", text[at:close or at])
        codes = [c for c in codes if c in messages]
        if not codes:
            continue
        conditions, first = _conditions(text, body_start, at, raw=raw)
        start_line = text.count("\n", 0, first if first is not None else at) + 1
        end_line = text.count("\n", 0, close or at) + 1
        message = " / ".join(dict.fromkeys(messages[c] for c in codes))
        if "{}" in message:
            passed = (raw or text)[at:close or at].split(codes[0], 1)[-1]
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


def _conditions(text: str, body_start: int, at: int,
                raw: str | None = None) -> tuple[list[str], int | None]:
    """The `if` conditions enclosing position `at`, outermost first, and where the
    innermost `if` starts. `text` is the masked scan text; condition wording
    comes from `raw` at the same positions so literals stay readable."""
    found: list[tuple[int, str]] = []
    for match in re.finditer(r"\b(else\s+)?if\s*\(", text[body_start:at]):
        start = body_start + match.start()
        paren = body_start + match.end() - 1
        close = _balanced(text, paren, "(", ")")
        if close is None or close > at:
            continue
        after = text[close + 1:at].lstrip()
        condition = " ".join((raw or text)[paren + 1:close].split())
        if after.startswith("{"):
            block_end = _balanced(text, close + 1 + (len(text[close + 1:at]) - len(after)), "{", "}")
            if block_end is not None and block_end < at:
                continue
        elif ";" in text[close + 1:at]:
            # An unbraced `if` body is one statement; once its `;` has passed,
            # `at` sits outside the condition (a brace-less `if (c) throw ...;`
            # still carries `c` for positions inside that very throw).
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
