"""What each admin GET query parameter's condition means, read from the release's source.

The slim loop's interface lines name the query parameters an endpoint publishes; whether a
parameter matches a substring, an exact value or a range is what the mapper's query wrapper
decides. This scan follows each admin GET handler to its request VO, and each request VO to
the mapper methods that take it, reading the `xxxIfPresent(Do::getField, reqVO.getParam())`
conditions. A parameter is annotated only when every querying method of the VO binds it the
same way; parameters whose condition it cannot resolve that way stay unknown.

It is a release-time aid like the method fence. Labels annotate the endpoints and reads
the actor's own account already shows. Separately proven direct equalities may reapply an
identity predicate in summaries; annotation alone never grants that executable binding.

    python -m web_operation_agent_yudao.filter_semantics --source-root /opt/source \
        --source-release v2026.09 --output native-policy/filters.json
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from .get_effects import _ADMIN, _MAPPING_PATH, _METHOD, _body, _parse

SCHEMA = 1
#: The wrapper conditions this scan resolves, and how each reads to the model. A code
#: outside this map never enters the policy.
LABELS = {"like": "包含匹配", "likeRight": "前缀匹配", "eq": "等于", "ne": "不等于",
          "in": "属于", "between": "区间", "ge": "大于等于", "gt": "大于",
          "le": "小于等于", "lt": "小于"}
_REQVO_PARAM = re.compile(r"\b(\w+(?:PageReqVO|ListReqVO))\s+\w+\s*[,)]")
_REQVO_NAME = re.compile(r"\b(\w+(?:PageReqVO|ListReqVO))\b")
#: A VO's own fields; array types (`LocalDateTime[] createTime`) are the usual range form.
_VO_FIELD = re.compile(r"^[ \t]*(?:private|protected|public)[ \t]+(?!static\b)"
                       r"(?:final[ \t]+)?[\w.]+(?:<[^;=()]*>)?(?:\[\])?[ \t]+(\w+)[ \t]*(?:;|=)",
                       re.M)
_CONDITION = re.compile(r"\b(likeRight|like|eq|ne|in|between|ge|le|gt|lt)(?:IfPresent)?[ \t]*\(")
_GETTER = re.compile(r"\.[ \t]*(?:get|is)(\w+)[ \t]*\([ \t]*\)")
_EXPORT = ("/export", "/import", "/download")


class FilterSemantics:
    """A deployed filters.json: labels for the query parameters the scan resolved.

    Missing entries mean the scan resolved no condition. Besides annotation, native
    summaries reapply only a direct same-field/value equality from `equalities`,
    when the response column is published. An equality annotation alone is not a
    binding; neither columns nor value transformations are guessed.
    """

    def __init__(self, apis: dict[str, dict[str, str]], *, equalities=None):
        self._apis = apis
        self._equalities = equalities or {}

    @classmethod
    def load(cls, document, *, release: str, source_digest: str | None = None) -> "FilterSemantics":
        if (not isinstance(document, dict)
                or set(document) != {"schema_version", "release", "source_digest", "labels", "apis"}
                or document["schema_version"] != SCHEMA
                or document["release"] != release
                or document["labels"] != LABELS
                or (source_digest is not None and document["source_digest"] != source_digest)
                or not isinstance(document["apis"], dict)):
            raise ValueError("the filter semantics policy must match the deployment release and source")
        apis: dict[str, dict[str, str]] = {}
        equalities = {}
        for path, entry in document["apis"].items():
            params = entry.get("params") if isinstance(entry, dict) else None
            if not isinstance(path, str) or not isinstance(params, dict):
                raise ValueError("the filter semantics policy must name endpoints and their parameters")
            resolved = {}
            for param, code in params.items():
                if not isinstance(param, str) or code not in LABELS:
                    raise ValueError("the filter semantics policy carries an unknown condition code")
                resolved[param] = LABELS[code]
            apis[path] = resolved
            bindings = entry.get("equalities", {})
            if (not isinstance(bindings, dict) or any(
                    not isinstance(key, str) or key != column or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", key)
                    or params.get(key) != "eq" for key, column in bindings.items())):
                raise ValueError("native equality bindings require a proven same-column equality")
            if bindings:
                equalities[path] = dict(bindings)
        return cls(apis, equalities=equalities)

    def params_for(self, path: str) -> dict[str, str]:
        return self._apis.get(path, {})

    def equalities_for(self, path: str) -> dict[str, str]:
        """Direct same-field/value bindings; old annotation-only policies prove none."""
        return self._equalities.get(path, {})

    def condition_line(self, path: str, submitted) -> str:
        """`name 包含匹配；categoryId 等于` for the submitted conditions this policy resolves."""
        return "；".join(f"{key} {self._apis[path][key]}" for key in submitted
                        if key in self._apis.get(path, {}))


def _arguments(code: str, opening: int) -> list[str]:
    """Top-level comma split of the argument list whose `(` is at `opening`."""
    depth, start = 0, opening
    for i in range(opening, len(code)):
        if code[i] == "(":
            depth += 1
        elif code[i] == ")":
            depth -= 1
            if depth == 0:
                inner, args, last, nested = code[start + 1:i], [], 0, 0
                for j, char in enumerate(inner):
                    if char in "([{":
                        nested += 1
                    elif char in ")]}":
                        nested -= 1
                    elif char == "," and nested == 0:
                        args.append(inner[last:j])
                        last = j + 1
                args.append(inner[last:])
                return args
    return []


def _java_index(paths) -> dict[str, str | None]:
    index: dict[str, str | None] = {}
    for path in paths:
        name = path.rsplit("/", 1)[-1][:-len(".java")]
        # The same simple name in two modules resolves to neither.
        index[name] = path if name not in index else None
    return index


def _vo_fields(index, read, wanted: set[str]) -> dict[str, frozenset[str]]:
    out = {}
    for vo in sorted(wanted):
        path = index.get(vo)
        if not path:
            continue
        try:
            kind, code, _raw = _parse(path, read(path))
        except (OSError, UnicodeError, ValueError):
            continue
        if kind is None or kind.name != vo:
            continue
        out[vo] = frozenset(_VO_FIELD.findall(code))
    return out


def _get_handlers(paths, read) -> list[tuple[str, str]]:
    """(endpoint, request VO) for each admin GET handler that takes a page or list VO."""
    out = []
    for path in paths:
        if _ADMIN not in path:
            continue
        try:
            kind, code, raw = _parse(path, read(path))
        except (OSError, UnicodeError, ValueError):
            continue
        if kind is None or "@RestController" not in raw:
            continue
        head = raw[:raw.find("class " + kind.name)]
        bases = re.findall(r"@RequestMapping\(([^)]*)\)", head)
        base = (_MAPPING_PATH.findall(bases[-1]) or [""])[0] if bases else ""
        for match in _METHOD.finditer(code):
            annotations = raw[match.start(1):match.end(1)]
            mapping = re.search(r"@GetMapping(?:\(((?:[^()]|\([^()]*\))*)\))?", annotations)
            generic = re.search(r"@RequestMapping\(((?:[^()]|\([^()]*\))*)\)", annotations)
            if mapping is None and not (generic and "RequestMethod.GET" in generic.group(1)):
                continue
            found = _REQVO_PARAM.findall(match.group(0))
            if not found:
                continue
            for sub in _MAPPING_PATH.findall((mapping or generic).group(1) or "") or [""]:
                full = "/admin-api/" + "/".join(
                    part for part in (base.strip("/") + "/" + sub.strip("/")).split("/") if part)
                full = re.sub(r"\{(\w+)(?::[^}]*)?\}", r"{\1}", full)
                if any(mark in full for mark in _EXPORT):
                    continue
                out.append((full, found[0]))
    return sorted(set(out))


def _vo_conditions(paths, read, fields_by_vo: dict[str, frozenset[str]]):
    """VO -> parameter -> the one condition every querying mapper method binds it to.

    An endpoint serves one of the VO's mapper methods per request, and which one can
    depend on the request itself. A method that binds no VO field (a delegating helper,
    an XML statement) makes no claim and is left out of the intersection. A parameter
    some querying methods bind and others do not, or two of them bind differently, is
    not a fact about every read the VO can serve -- the CRM contact page's
    `customerId` condition lives in another endpoint's `selectPageByCustomerId` -- so
    the parameter stays unknown rather than annotated with a branch's condition.
    """
    per_vo: dict[str, dict[tuple[str, str], dict[str, set[str]]]] = {}
    bindings_by_vo = {}
    for path in paths:
        if not path.rsplit("/", 1)[-1].endswith("Mapper.java"):
            continue
        try:
            text = read(path)
        except (OSError, UnicodeError):
            continue
        present = set(_REQVO_NAME.findall(text)) & set(fields_by_vo)
        if not present:
            continue
        try:
            kind, code, _raw = _parse(path, text)
        except (OSError, UnicodeError, ValueError):
            continue
        if kind is None:
            continue
        for match in _METHOD.finditer(code):
            vos = set(_REQVO_PARAM.findall(match.group(0))) & present
            if not vos:
                continue
            body = _body(code, match.end() - 1)
            method_key = (kind.name, match.group(2))
            for call in _CONDITION.finditer(body):
                args = _arguments(body, call.end() - 1)
                if len(args) < 2:
                    continue
                for getter in _GETTER.findall(" ".join(args[1:])):
                    param = getter[:1].lower() + getter[1:]
                    for vo in vos:
                        if param in fields_by_vo[vo]:
                            (per_vo.setdefault(vo, {}).setdefault(method_key, {})
                             .setdefault(param, set()).add(call.group(1)))
                            # Annotation says which comparison is used, not which
                            # row column/value it compares. Do not turn it into an
                            # executable filter unless both operands are direct.
                            variables = re.findall(r"\b" + re.escape(vo) + r"\s+(\w+)\b", match.group(0))
                            direct = (call.group(1) == "eq" and len(args) == 2
                                and re.fullmatch(r"\w+::(?:get|is)" + re.escape(getter), args[0].strip())
                                and any(re.fullmatch(re.escape(variable) + r"\.(?:get|is)" + re.escape(getter)
                                        + r"\s*\(\s*\)", args[1].strip()) for variable in variables))
                            (bindings_by_vo.setdefault(vo, {}).setdefault(method_key, {})
                             .setdefault(param, set()).add(param if direct else None))
    out, equalities = {}, {}
    for vo, methods in per_vo.items():
        common = None
        for params in methods.values():
            common = set(params) if common is None else common & set(params)
        resolved = {}
        for param in sorted(common or ()):
            codes = {code for params in methods.values() for code in params[param]}
            if len(codes) == 1 and next(iter(codes)) in LABELS:
                resolved[param] = next(iter(codes))
        out[vo] = resolved
        equalities[vo] = {param: param for param, code in resolved.items() if code == "eq"
            and all(bindings_by_vo[vo][method].get(param) == {param} for method in methods)}
    return out, equalities


def filter_policy(tree, release: str) -> dict:
    """Every admin GET endpoint's request VO, with the conditions the source resolved."""
    paths = [path for path in tree.files() if path.endswith(".java") and path.startswith("server/")]
    read = tree.read_text
    handlers = _get_handlers(paths, read)
    fields_by_vo = _vo_fields(_java_index(paths), read, {vo for _endpoint, vo in handlers})
    conditions, equalities = _vo_conditions(paths, read, fields_by_vo)
    apis: dict[str, dict | None] = {}
    for endpoint, vo in handlers:
        fields = fields_by_vo.get(vo)
        if not fields:
            continue
        params = conditions.get(vo, {})
        entry = {"req_vo": vo, "params": dict(params), "unknown": sorted(fields - set(params))}
        if equalities.get(vo):
            entry["equalities"] = equalities[vo]
        if endpoint in apis and apis[endpoint] != entry:
            apis[endpoint] = None
        else:
            apis.setdefault(endpoint, entry)
    return {"schema_version": SCHEMA, "release": release,
            "source_digest": tree.surface_digest(), "labels": LABELS,
            "apis": {endpoint: entry for endpoint, entry in sorted(apis.items()) if entry}}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-release", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    from ..rules.repository import repository_tree
    policy = filter_policy(repository_tree(args.source_root, args.source_release), args.source_release)
    args.output.write_text(json.dumps(policy, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    resolved = sum(len(entry["params"]) for entry in policy["apis"].values())
    unknown = sum(len(entry["unknown"]) for entry in policy["apis"].values())
    print(f"{len(policy['apis'])} admin GET endpoints, {resolved} parameters resolved, {unknown} unknown")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
