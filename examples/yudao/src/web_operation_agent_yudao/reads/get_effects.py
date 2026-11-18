"""Which admin GET handlers of a release can write, found by reading its server source.

The method fence admits every plain GET while the assistant works a page, so the handlers
that are not pure reads have to be named. This scan follows each admin GET handler through
the services it calls, to a bounded depth, and lists the handler when anything it reaches
calls a mapper write or a call it cannot resolve whose name says it changes something.

It is a release-time aid for the deployment's owner, read through the same source allowlist
as every other source read. It errs towards listing a handler: an unresolved call with a
writing name is listed, and so is a handler whose calls ran past the depth bound. It cannot
see writes made through reflection, SQL in XML, events consumed elsewhere or other systems.
Refusal evidence and data checks qualify an observed guarded run; they cannot prove that
an unreviewed editor has no delayed effects after the window closes.

    python -m web_operation_agent_yudao.get_effects --source-root /opt/source \\
        --source-release v2026.09 --output native-fence.json
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..rules.repository import repository_tree

POLICY_SCHEMA = 2
POLICY_MODE = "method-fence"
_DEPTH = 8
_ADMIN = "/controller/admin/"
_CLASS = re.compile(r"\b(?:class|interface)\s+(\w+)(?:\s+extends\s+([\w.<>, ]+?))?"
                    r"(?:\s+implements\s+([\w.<>, ]+?))?\s*\{")
_FIELD = re.compile(r"^\s*(?:@\w+(?:\([^)]*\))?\s*)*(?:private|protected|public)?\s*(?:static\s+)?"
                    r"(?:final\s+)?([A-Z]\w*)(?:<[^;=()]*>)?\s+(\w+)\s*(?:;|=)", re.M)
_NESTED = r"\((?:[^()]|\((?:[^()]|\([^()]*\))*\))*\)"
_METHOD = re.compile(r"(?:^|\n)[ \t]*((?:@\w+(?:" + _NESTED + r")?\s*)*)"
                     r"(?:(?:public|protected|private|default|static|final|synchronized)\s+)*"
                     r"(?:<[^>]+>\s+)?[\w.<>\[\], ?]+?\s+(\w+)\s*" + _NESTED
                     + r"\s*(?:throws\s+[\w., ]+)?\s*\{")
_LITERAL = re.compile(r'"""[\s\S]*?"""|//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'')
#: Types whose calls reach persistence, messaging or another system.
_COLLABORATOR = re.compile(r"\w*(?:Service|Api|Mapper|DAO|Dao|Producer|Client|Template|Manager|"
                           r"Publisher|Sender|Repository|Handler)(?:Impl)?")
_CALL = re.compile(r"\b(\w+)\s*(?:\.\s*(\w+)\s*\(|::\s*(\w+))")
_LOCAL_CALL = re.compile(r"(?<![.\w])(\w+)\s*\(")
_MAPPING_PATH = re.compile(r"\"([^\"]*)\"")
_WRITES = re.compile(r"(?!create\w*Query$)(?:insert|update|delete|remove|save|create|add|incr|increment|"
                     r"decrement|ensure|init|sync|refresh|send|publish|mark|generate|allocate|put|"
                     r"clear|reset|cancel|close|lock|unlock|consume|execute|complete|start|suspend|"
                     r"activate|claim|assign|approve|reject|transfer|delegate|submit|apply|pay|"
                     r"refund|notify|trigger|record|set)(?![a-z])\w*")
#: A mapper call is a write unless its name says it only reads.
_MAPPER_READS = re.compile(r"(?:select|count|get|find|query|list|exists|page|sum|max|min|load|search|is|has)(?![a-z])\w*")
_KEYWORDS = frozenset({"if", "for", "while", "switch", "catch", "return", "new", "throw", "super",
                       "this", "synchronized", "try", "else", "assert", "lambda"})


def _blank(text: str) -> str:
    """Comments and literal contents replaced by spaces, so braces and names are code."""
    def spaces(match):
        found = match.group(0)
        if found[0] in "\"'" and not found.startswith('"""'):
            return found[0] + " " * (len(found) - 2) + found[0]
        return re.sub(r"[^\n]", " ", found)
    return _LITERAL.sub(spaces, text)


def _body(code: str, opening: int) -> str:
    depth = 0
    for i in range(opening, len(code)):
        if code[i] == "{":
            depth += 1
        elif code[i] == "}":
            depth -= 1
            if depth == 0:
                return code[opening + 1:i]
    return code[opening + 1:]


@dataclass
class _Type:
    name: str
    path: str
    parents: tuple[str, ...]
    fields: dict[str, str] = field(default_factory=dict)
    methods: dict[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class GetHandler:
    path: str
    controller: str
    method: str
    #: Empty for a handler the scan followed to its end without meeting a write.
    reason: str


def _parse(path: str, text: str) -> tuple[_Type | None, str, str]:
    code = _blank(text)
    declared = _CLASS.search(code)
    if declared is None:
        return None, code, text
    parents = tuple(part.split("<")[0].strip().rsplit(".", 1)[-1]
                    for group in declared.groups()[1:] if group for part in group.split(","))
    kind = _Type(declared.group(1), path, parents)
    members, last = [], 0
    for match in _METHOD.finditer(code):
        name = match.group(2)
        if name in _KEYWORDS or match.start() < last:
            continue
        body = _body(code, match.end() - 1)
        kind.methods.setdefault(name, []).append(body)
        # Fields are what is declared outside every method body; a local variable of a
        # collection type is not a collaborator this class holds.
        members.append(code[last:match.end()])
        last = match.end() + len(body)
    members.append(code[last:])
    kind.fields = {name: owner for owner, name in _FIELD.findall("\n".join(members))}
    return kind, code, text


class _Scan:
    """Types are parsed when a handler first reaches them, by the file that bears their name."""

    def __init__(self, read, paths):
        self._read = read
        self._paths: dict[str, str | None] = {}
        for path in paths:
            name = path.rsplit("/", 1)[-1][:-len(".java")]
            # The same simple name in two modules is resolved for neither.
            self._paths[name] = path if name not in self._paths else None
        self._types: dict[str, _Type | None] = {}
        self.controllers = sorted(path for path in paths if _ADMIN in path)

    def _type(self, name: str) -> _Type | None:
        if name not in self._types:
            path = self._paths.get(name)
            parsed = None
            if path:
                try:
                    parsed = _parse(path, self._read(path))[0]
                except (OSError, UnicodeError, ValueError):
                    parsed = None
            self._types[name] = parsed if parsed is not None and parsed.name == name else None
        return self._types[name]

    def _targets(self, owner: str) -> list[_Type]:
        return [kind for kind in (self._type(owner), self._type(owner + "Impl")) if kind is not None]

    def _ancestors(self, kind: _Type, depth: int, seen=None) -> list[_Type]:
        seen = set() if seen is None else seen
        if kind.name in seen:
            raise ValueError("cyclic inheritance")
        if depth < 0:
            raise ValueError("inheritance beyond scan depth")
        branch = seen | {kind.name}
        parents = []
        for name in kind.parents:
            if name == "Object":
                continue
            parent = self._type(name)
            if parent is None:
                raise ValueError("unresolved inheritance: " + name)
            parents.extend(self._ancestors(parent, depth - 1, branch))
            parents.append(parent)
        return parents

    @staticmethod
    def _definitions(kind: _Type, parents: list[_Type], method: str) -> list[_Type]:
        if method in kind.methods:
            return [kind]
        # Include every available inherited definition rather than guess Java overload
        # or interface-default dispatch from regex parsing.
        return [parent for parent in parents if method in parent.methods]

    def writes(self, kind: _Type, method: str, depth: int, seen: set) -> str:
        """Why this method may write, or an empty string."""
        if (kind.name, method) in seen:
            return ""
        seen.add((kind.name, method))
        try:
            parents = self._ancestors(kind, _DEPTH)
        except ValueError as exc:
            return f"{kind.name}.{method} ({exc})"
        fields = {name: owner for parent in (*parents, kind) for name, owner in parent.fields.items()}
        if method not in kind.methods:
            definitions = self._definitions(kind, parents, method)
            if not definitions and _WRITES.fullmatch(method):
                return f"{kind.name}.{method} (unresolved)"
            for parent in definitions:
                reason = self.writes(parent, method, depth - 1, seen)
                if reason:
                    return reason
        for body in kind.methods.get(method, ()):
            for receiver, called, referenced in _CALL.findall(body):
                owner, call = fields.get(receiver, ""), called or referenced
                if receiver in {"this", "super"}:
                    definitions = self._definitions(kind, parents, call) if receiver == "this" else [
                        parent for parent in parents if call in parent.methods]
                    for target in definitions:
                        reason = self.writes(target, call, depth - 1, seen)
                        if reason:
                            return reason
                    continue
                if not owner:
                    continue
                if owner.endswith("Mapper"):
                    if not _MAPPER_READS.fullmatch(call):
                        return f"{kind.name}.{method} -> {owner}.{call}"
                    continue
                targets = self._targets(owner)
                if not targets:
                    # Another system, a template or an API this release's source does not
                    # hold: only its name can be read.
                    if _COLLABORATOR.fullmatch(owner) and _WRITES.fullmatch(call):
                        return f"{kind.name}.{method} -> {owner}.{call} (unresolved)"
                    continue
                if depth <= 0:
                    return f"{kind.name}.{method} -> {owner}.{call} (beyond scan depth)"
                for target in targets:
                    reason = self.writes(target, call, depth - 1, seen)
                    if reason:
                        return f"{kind.name}.{method} -> {reason}"
            for call in _LOCAL_CALL.findall(body):
                if call != method and call not in _KEYWORDS:
                    for target in self._definitions(kind, parents, call):
                        if depth <= 0:
                            return f"{kind.name}.{method} -> {target.name}.{call} (beyond scan depth)"
                        reason = self.writes(target, call, depth - 1, seen)
                        if reason:
                            return reason
        return ""

    def handlers(self) -> list[GetHandler]:
        out = []
        for path in self.controllers:
            try:
                kind, code, raw = _parse(path, self._read(path))
            except (OSError, UnicodeError, ValueError):
                continue
            if kind is None or "@RestController" not in raw:
                continue
            head = raw[:raw.find("class " + kind.name)]
            bases = re.findall(r"@RequestMapping\(([^)]*)\)", head)
            base = (_MAPPING_PATH.findall(bases[-1]) or [""])[0] if bases else ""
            for match in _METHOD.finditer(code):
                annotations, name = raw[match.start(1):match.end(1)], match.group(2)
                mapping = re.search(r"@GetMapping(?:\(((?:[^()]|\([^()]*\))*)\))?", annotations)
                generic = re.search(r"@RequestMapping\(((?:[^()]|\([^()]*\))*)\)", annotations)
                if mapping is None and not (generic and "RequestMethod.GET" in generic.group(1)):
                    continue
                paths = _MAPPING_PATH.findall((mapping or generic).group(1) or "") or [""]
                reason = self.writes(kind, name, _DEPTH, set())
                for sub in paths:
                    full = "/admin-api/" + "/".join(
                        part for part in (base.strip("/") + "/" + sub.strip("/")).split("/") if part)
                    out.append(GetHandler(re.sub(r"\{(\w+)(?::[^}]*)?\}", r"{\1}", full),
                                          kind.name, name, reason))
        return sorted(out, key=lambda h: (h.path, h.controller, h.method))


def scan(tree) -> list[GetHandler]:
    """Every admin GET handler of the allowlisted server source, with why it may write."""
    paths = [path for path in tree.files() if path.endswith(".java") and path.startswith("server/")]
    return _Scan(tree.read_text, paths).handlers()


def fence_policy(tree, release: str) -> dict:
    handlers = scan(tree)
    writing = sorted({h.path for h in handlers if h.reason})
    return {"schema_version": POLICY_SCHEMA, "release": release, "mode": POLICY_MODE,
            "writing_gets": writing,
            "scan": {"get_handlers": len(handlers), "listed": len(writing), "depth": _DEPTH,
                     "source_digest": tree.surface_digest(),
                     "reasons": {h.path: h.reason for h in handlers if h.reason}}}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-release", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    policy = fence_policy(repository_tree(args.source_root, args.source_release), args.source_release)
    args.output.write_text(json.dumps(policy, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{policy['scan']['get_handlers']} admin GET handlers, {policy['scan']['listed']} listed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
