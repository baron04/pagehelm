"""On-demand scalar form facts from allowlisted raw Vue source.

This compiler supplies evidence, not permission to type. A form port must still bind an
application-verified entry, actor, create/update target and fresh visible dialog before
using these facts. No source map, global form directory or framework code enters the core.
Scripts are never executed; dynamic bindings and custom controls remain uncompiled.
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


@dataclass(frozen=True, slots=True)
class ScalarFormSource:
    fields: tuple[ScalarSourceField, ...]
    evidence: EvidenceRef

    @property
    def binding_digest(self) -> str:
        ref = self.evidence
        return hashlib.sha256((ref.repository_id + "\0" + ref.release + "\0"
                               + ref.path + "\0" + ref.sha256).encode()).hexdigest()


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str | None]
    start: int
    end: int = 0
    children: list[_Node] = field(default_factory=list)


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


def _compile(tree: SourceTree, path: str, text: str) -> ScalarFormSource | None:
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
                    if node.tag == "el-input"]
        if len(controls) != 1:
            continue
        control, parents = controls[0]
        attrs = control.attrs
        if (attrs.get("v-model") != f"formData.{reference}"
                or any(key.startswith("v-model.") for key in attrs)
                or _dynamic(attrs, "type", "maxlength")):
            continue
        input_type = attrs.get("type", "text")
        if input_type not in {"text", "textarea", "email", "tel", "url"}:
            continue
        limit = attrs.get("maxlength")
        if "maxlength" in attrs and (not isinstance(limit, str)
                                  or not re.fullmatch(r"[0-9]{1,6}", limit)
                                  or not 1 <= int(limit) <= 4000):
            continue
        chain = ancestors + parents + (control,)
        fields.append(ScalarSourceField(
            reference, " ".join(label.split()), input_type,
            int(limit) if limit is not None else None,
            any(_CONDITIONAL.intersection(node.attrs) for node in chain),
            tuple(sorted({key for node in chain for key in node.attrs
                          if key.startswith(("@", "v-on"))})),
            _evidence(tree, path, lines, item.start, item.end),
        ))
    # Repeated fields/labels cannot establish a unique semantic binding, even if their
    # source branches appear mutually exclusive. The compiler never evaluates Vue code.
    if (not fields or len(fields) > 200
            or len({item.reference for item in fields}) != len(fields)
            or len({item.label for item in fields}) != len(fields)):
        return None
    return ScalarFormSource(tuple(fields), _evidence(tree, path, lines, 1, len(lines)))


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
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        with self._lock:
            cached = self._cache.get(path)
            if cached is not None and cached[0] == digest:
                self._cache.move_to_end(path)
                return cached[1]
            result = _compile(self._tree, path, text)
            self._cache[path] = (digest, result)
            self._cache.move_to_end(path)
            while len(self._cache) > 128:
                self._cache.popitem(last=False)
            return result


__all__ = ["ScalarFormSource", "ScalarSourceField", "SourceFormReader"]
