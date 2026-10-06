"""On-demand form facts from allowlisted raw Vue source.

This compiler supplies evidence, not permission to type. A form port must still bind an
application-verified entry, actor, create/update target and fresh visible dialog before
using these facts. No source map, global form directory or framework code enters the core.
Scripts are never executed. Single-choice literals, shallow view-local radio constants,
integer dictionary bindings and the fixed user-list read pattern may describe options; other dynamic/custom controls remain
uncompiled. These facts never authorize a request extracted from source.
"""
from __future__ import annotations

import hashlib
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from html.parser import HTMLParser
from threading import RLock

from web_operation_agent.planning.sourcetree import AccessDenied, SourceTree
from web_operation_agent.schemas import EvidenceRef
from web_operation_agent.schemas.browser import MAX_FORM_LABEL_CHARS
from .form_radio import constant_imports, radio_choices

_COMPONENT = re.compile(r"ui/src/views/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.vue\Z")
_FIELD = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,79}\Z")
_VOID = frozenset({"input", "br", "hr", "img", "meta", "link", "wbr", "area",
                   "base", "col", "embed", "param", "source", "track"})
_CONDITIONAL = frozenset({"v-if", "v-else", "v-else-if", "v-for", "v-show"})


@dataclass(frozen=True, slots=True)
class ScalarSourceField:
    reference: str
    label: str
    input_type: str
    max_utf16_units: int | None
    conditional: bool
    # Declared handlers are facts only. Their absence does not prove side-effect freedom.
    handlers: tuple[str, ...]
    evidence: EvidenceRef
    minimum: str | None = None
    maximum: str | None = None
    precision: int | None = None
    choices: tuple[tuple[str, str], ...] = ()
    choice_source: str = ""
    choice_reference: str = ""
    modes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ScalarFormSource:
    fields: tuple[ScalarSourceField, ...]
    evidence: EvidenceRef
    dependencies: tuple[EvidenceRef, ...] = ()

    @property
    def binding_digest(self) -> str:
        ref = self.evidence
        return hashlib.sha256((ref.repository_id + "\0" + ref.release + "\0"
                               + ref.path + "\0" + ref.sha256 + "".join(
                                   "\0" + dep.path + "\0" + dep.sha256
                                   for dep in self.dependencies)).encode()).hexdigest()


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str | None]
    start: int
    end: int = 0
    children: list[_Node] = field(default_factory=list)
    text: str = ""


class _Template(HTMLParser):
    """Strict bounded nesting, rather than a regex ending at the first slot template."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.roots: list[_Node] = []
        self.stack: list[_Node] = []
        self.valid = True
        self.count = 0

    def handle_starttag(self, tag, attrs):
        self.count += 1
        if self.count > 4096 or len(self.stack) >= 64:
            raise ValueError("source template exceeds the compiler bound")
        if len({key for key, _ in attrs}) != len(attrs):
            self.valid = False
        node = _Node(tag, dict(attrs), self.getpos()[0])
        (self.stack[-1].children if self.stack else self.roots).append(node)
        if tag in _VOID:
            node.end = self.getpos()[0]
        else:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1].tag != tag:
            self.valid = False
            return
        self.stack.pop().end = self.getpos()[0]

    def handle_data(self, data):
        if self.stack:
            self.stack[-1].text += data


def _walk(node: _Node, ancestors: tuple[_Node, ...] = ()):
    yield node, ancestors
    for child in node.children:
        yield from _walk(child, ancestors + (node,))


def _dynamic(attrs: dict, *names: str) -> bool:
    if "v-bind" in attrs:
        return True
    return any(key == prefix or key.startswith(prefix + ".")
               for name in names for prefix in (":" + name, "v-bind:" + name)
               for key in attrs)


def _evidence(tree: SourceTree, path: str, lines: list[str], start: int, end: int):
    # Hash the bytes actually parsed, avoiding a second read racing the first one.
    body = "".join(lines[start - 1:end])
    return EvidenceRef(repository_id=tree.repository_id, release=tree.release, path=path,
                       start_line=start, end_line=end,
                       sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(), excerpt="")


def _number_attribute(attrs: dict, name: str) -> str | None:
    """Read a decimal literal only; never evaluate a Vue expression or spread."""
    keys = [key for key in attrs if key in {name, ":" + name, "v-bind:" + name}]
    if not keys:
        return None
    value = attrs[keys[0]]
    if (len(keys) != 1 or not isinstance(value, str) or len(value) > 32
            or not re.fullmatch(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", value)):
        raise ValueError("numeric form constraint is not a decimal literal")
    return value


def _select_choices(control: _Node, text: str, dictionary: str):
    attrs = control.attrs
    if (_dynamic(attrs, "multiple", "remote", "allow-create", "remote-method") or any(key in attrs for key in
            ("multiple", ":multiple", "v-bind:multiple", "remote", ":remote", "allow-create",
             ":allow-create", "remote-method", ":remote-method"))):
        return None
    options = [node for node, _ in _walk(control) if node.tag == "el-option"]
    if not options or len(options) > 50:
        return None
    if len(options) == 1 and "v-for" in options[0].attrs:
        option = options[0].attrs
        dict_loop = re.fullmatch(r"([A-Za-z][A-Za-z0-9_]*) in getIntDictOptions\(DICT_TYPE\.([A-Z][A-Z0-9_]*)\)",
                                 option["v-for"] or "")
        if dict_loop:
            variable, constant = dict_loop.groups()
            if (option.get(":label") != variable + ".label" or option.get(":value") != variable + ".value"
                    or any(k.startswith(("@", "v-on", ":disabled", "v-bind")) or k == "disabled" for k in option)
                    or not re.search(r"import\s*\{[^}]*\bgetIntDictOptions\b[^}]*\}\s*from\s*['\"]@/utils/dict['\"]", text)):
                return None
            enums = re.findall(r"export enum DICT_TYPE\s*\{([^}]+)\}", dictionary)
            keys = re.findall(r"^\s*" + re.escape(constant) + r"\s*=\s*['\"]([a-z][a-z0-9_]{0,119})['\"]\s*,?\s*(?://[^\n]*)?$",
                              enums[0], re.MULTILINE) if len(enums) == 1 else []
            return ((), "dictionary", keys[0]) if len(keys) == 1 else None
        user_loop = re.fullmatch(r"([A-Za-z][A-Za-z0-9_]*) in ([A-Za-z][A-Za-z0-9_]*)", option["v-for"] or "")
        if user_loop:
            variable, collection = user_loop.groups()
            imports = re.findall(r"import \* as ([A-Za-z][A-Za-z0-9_]*) from ['\"]@/api/system/user['\"]", text)
            assignments = re.findall(re.escape(collection) + r"\.value\s*=([^\n;]+)", text)
            if (len(imports) == 1 and assignments == [" await " + imports[0] + ".getSimpleUserList()"]
                    and option.get(":label") == variable + ".nickname" and option.get(":value") == variable + ".id"
                    and not any(k.startswith(("@", "v-on", ":disabled", "v-bind")) or k == "disabled" for k in option)):
                return (), "users", ""
        return None
    choices = []
    for node in options:
        attrs = node.attrs
        if any(key.startswith((":label", "v-", "@", ":disabled")) or key == "disabled" for key in attrs):
            return None
        label, value = attrs.get("label"), attrs.get("value")
        if value is None and re.fullmatch(r"-?(?:0|[1-9][0-9]*)", attrs.get(":value") or ""):
            value = attrs[":value"]
        if not isinstance(label, str) or not label.strip() or not isinstance(value, str):
            return None
        choices.append((value, " ".join(label.split())))
    if len({v for v, _ in choices}) != len(choices) or len({l for _, l in choices}) != len(choices):
        return None
    return tuple(choices), "literal", ""


def _compile(tree: SourceTree, path: str, text: str, dictionary: str = "", *,
             constants=None, dependencies=()) -> ScalarFormSource | None:
    parser = _Template()
    try:
        parser.feed(text)
        parser.close()
    except (ValueError, RecursionError):
        return None
    templates = [node for node in parser.roots if node.tag == "template"]
    if not parser.valid or parser.stack or len(templates) != 1:
        return None
    forms = [(node, parents) for node, parents in _walk(templates[0]) if node.tag == "el-form"]
    if len(forms) != 1 or forms[0][0].attrs.get(":model") != "formData":
        return None
    lines = text.splitlines(keepends=True)
    fields = []
    for item, ancestors in _walk(*forms[0]):
        if item.tag != "el-form-item":
            continue
        label, reference = item.attrs.get("label"), item.attrs.get("prop")
        if (not isinstance(label, str) or not label.strip() or len(label) > MAX_FORM_LABEL_CHARS
                or "{{" in label or not isinstance(reference, str)
                or not _FIELD.fullmatch(reference)
                or _dynamic(item.attrs, "label", "prop")):
            continue
        controls = [(node, parents) for node, parents in _walk(item)
                    if node.tag in {"el-input", "el-input-number", "el-date-picker", "el-select", "el-radio-group"}]
        if len(controls) != 1:
            continue
        control, parents = controls[0]
        attrs = control.attrs
        if (attrs.get("v-model") != f"formData.{reference}"
                or any(key.startswith("v-model.") for key in attrs)
                or _dynamic(attrs, "type", "maxlength")):
            continue
        minimum = maximum = precision = None
        choices, choice_source, choice_reference = (), "", ""
        if control.tag == "el-radio-group":
            if _dynamic(attrs, "disabled") or 'disabled' in attrs or 'v-bind' in attrs:
                continue
            compiled = radio_choices(control, constants or {})
            if compiled is None:
                continue
            choices, choice_source, input_type = compiled, "literal", "radio"
        elif control.tag == "el-select":
            compiled = _select_choices(control, text, dictionary)
            if compiled is None:
                continue
            choices, choice_source, choice_reference = compiled
            input_type = "choice"
        elif control.tag == "el-input-number":
            # Step-strict typing or a custom formatter can alter the entered value.
            if ("v-bind" in attrs or any(key in attrs for key in
                    ("step-strictly", ":step-strictly", "v-bind:step-strictly", "formatter", ":formatter"))
                    or any(key.startswith((":min.", ":max.", ":precision.", "v-bind:min.",
                                           "v-bind:max.", "v-bind:precision.")) for key in attrs)):
                continue
            try:
                minimum, maximum = (_number_attribute(attrs, name) for name in ("min", "max"))
                raw_precision = _number_attribute(attrs, "precision")
                if raw_precision is not None:
                    if not raw_precision.isdecimal() or not 0 <= int(raw_precision) <= 15:
                        continue
                    precision = int(raw_precision)
            except ValueError:
                continue
            input_type = "integer" if precision == 0 else "number"
        elif control.tag == "el-date-picker":
            # Only fixed, editable single-value displays can be typed and checked
            # exactly. The application translates datetime wall time, never epochs.
            input_type = attrs.get("type", "date")
            display = {"date": "YYYY-MM-DD", "datetime": "YYYY-MM-DD HH:mm:ss"}.get(input_type)
            if (display is None or attrs.get("format", display) != display
                    or attrs.get("value-format", display) not in {display, "x"}
                    or _dynamic(attrs, "format", "value-format", "editable", "readonly", "disabled")
                    or attrs.get("editable") == "false" or "readonly" in attrs or "disabled" in attrs
                    or "v-bind" in attrs):
                continue
        else:
            input_type = attrs.get("type", "text")
            if input_type not in {"text", "textarea", "email", "tel", "url", "date"}:
                continue
        limit = attrs.get("maxlength")
        if "maxlength" in attrs and (not isinstance(limit, str)
                                  or not re.fullmatch(r"[0-9]{1,6}", limit)
                                  or not 1 <= int(limit) <= 4000):
            continue
        chain = ancestors + parents + (control,)
        conditions = [(key, node.attrs[key]) for node in chain for key in _CONDITIONAL.intersection(node.attrs)]
        modes = {'create', 'update'}
        for key, value in conditions:
            match = re.fullmatch(r"formType\s*===\s*(['\"])(create|update)\1", value or '') if key == 'v-if' else None
            modes.intersection_update({match[2]} if match else set())
        fields.append(ScalarSourceField(
            reference, " ".join(label.split()), input_type,
            int(limit) if limit is not None else None,
            any(_CONDITIONAL.intersection(node.attrs) for node in chain),
            tuple(sorted({key for node in chain for key in node.attrs
                          if key.startswith(("@", "v-on"))})),
            _evidence(tree, path, lines, item.start, item.end),
            minimum, maximum, precision,
            choices, choice_source, choice_reference,
            tuple(sorted(modes)) if conditions else (),
        ))
    # Repeated fields/labels cannot establish a unique semantic binding, even if their
    # source branches appear mutually exclusive. The compiler never evaluates Vue code.
    if (not fields or len(fields) > 200
            or len({item.reference for item in fields}) != len(fields)
            or len({item.label for item in fields}) != len(fields)):
        return None
    refs = (_evidence(tree, "ui/src/utils/dict.ts", dictionary.splitlines(keepends=True),
                      1, len(dictionary.splitlines())),) if dictionary else ()
    refs += tuple(_evidence(tree, dep_path, body.splitlines(keepends=True), 1, len(body.splitlines()))
                  for dep_path, body in dependencies)
    return ScalarFormSource(tuple(fields), _evidence(tree, path, lines, 1, len(lines)), refs)


class SourceFormReader:
    """Read one already located component; no repository enumeration or eager catalog.

    Cached parsing is bounded and bound to the exact current bytes, repository and release.
    Every access rechecks the allowlist and reads that component only. A cached successful
    parse therefore cannot survive source deletion, replacement, or a secret-path denial.
    """

    def __init__(self, tree: SourceTree):
        self._tree = tree
        self._cache: OrderedDict[str, tuple[str, ScalarFormSource | None]] = OrderedDict()
        self._lock = RLock()

    def read(self, path: str) -> ScalarFormSource | None:
        if (not isinstance(path, str) or not _COMPONENT.fullmatch(path)
                or any(part in {".", ".."} for part in path.split("/"))):
            return None
        try:
            text = self._tree.read_text(path)
        except (AccessDenied, OSError, UnicodeError):
            return None
        dictionary = ""
        if "getIntDictOptions(DICT_TYPE." in text:
            try:
                dictionary = self._tree.read_text("ui/src/utils/dict.ts")
            except (AccessDenied, OSError, UnicodeError):
                pass
        constants, dependencies = constant_imports(self._tree, text) if '<el-radio-group' in text else ({}, ())
        digest = hashlib.sha256((text + "\0" + dictionary + ''.join(
            '\0' + p + '\0' + body for p, body in dependencies)).encode("utf-8")).hexdigest()
        with self._lock:
            cached = self._cache.get(path)
            if cached is not None and cached[0] == digest:
                self._cache.move_to_end(path)
                return cached[1]
            result = _compile(self._tree, path, text, dictionary, constants=constants, dependencies=dependencies)
            self._cache[path] = (digest, result)
            self._cache.move_to_end(path)
            while len(self._cache) > 128:
                self._cache.popitem(last=False)
            return result


__all__ = ["ScalarFormSource", "ScalarSourceField", "SourceFormReader"]
