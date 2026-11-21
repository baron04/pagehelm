"""Static compiler deriving 4-category API write specs from Yudao Java AST, OpenAPI, and Vue/TS UI."""
from __future__ import annotations

from collections import OrderedDict
from contextlib import nullcontext
from dataclasses import dataclass, replace
from pathlib import Path
import re
from threading import RLock

from web_operation_agent.business.compiled_writes import (
    CompiledAggregateUpdateSpec,
    CompiledChildCollectionSpec,
    CompiledCreateSpec,
    CompiledFieldDefault,
    CompiledRelationSpec,
    is_rejected_ui_sentinel,
)
from web_operation_agent.business.convention_writes import (
    ConventionFieldSpec,
    ConventionUpdateSpec,
    TwoStateStatusSpec,
)
from web_operation_agent.business.write_pipeline import DEFAULT_SECRET_FIELD_PATTERN

from .java_scan import _CALL, _METHOD_MAPPING, _OPERATION, _SERVICE_CALL, _SERVICE_FIELD, _mask, _methods, _signature_params

_SECRET = DEFAULT_SECRET_FIELD_PATTERN
_VOLATILE = frozenset({"updateTime", "updater", "updaterName", "modifyTime", "version", "createTime", "creator", "creatorName"})
_NAME_CANDIDATES = (
    "name", "title", "no", "employeeName", "postName", "companyName", "nickname",
    "username", "voucherNumber", "digest", "subject",
    "code", "jobNumber", "signature", "label",
    "itemName", "spuName", "userNickname", "content",
)

# High-risk or async/workflow domains excluded across ALL compiled write categories
_BLOCKED_ENTITY_PREFIXES = (
    "pay/demo-",
    "pay/wallet",
    "pay/order",
    "pay/refund",
    "pay/transfer",
    "pay/notify",
    "pay/channel",
    "trade/after-sale",
    "trade/brokerage-withdraw",
    "bpm/task",
    "bpm/process-instance",
    "bpm/oa",
    "system/auth",
    "system/sms-send",
    "system/sms-channel",
    "system/mail-account",
    "system/mail-log",
    "system/notify-message",
    "infra/codegen",
    "infra/db-doc",
    "infra/job",
    "infra/redis",
    "infra/api-access-log",
    "infra/api-error-log",
    "infra/demo",
    "mp/message-template",
)

# Read-only service verb prefixes allowed inside Cat 1-4 service methods
_READ_ONLY_SERVICE_METHOD = re.compile(
    r"^(?:validate|valid|get|select|query|check|has|is|exists|count|find|list|calculate|build|normalize|merge|sum)\w*$",
    re.I,
)
_SAFE_EXTRA_SERVICE_METHODS = frozenset({
    "createPermission",
    "publishUserProfileUpdatedIfChanged",
    "lockAccountSet",
    "updateReceivablePlanReceivableId",
    "setAuxiliaryCombinationIds",
    "createSalaryChangeRecord",
    "updateSalaryChangeRecord",
    "updatePurchaseOrderInCount",
    "updatePurchaseOrderReturnCount",
    "updateSaleOrderOutCount",
    "updateSaleOrderReturnCount",
    "updatePurchaseInPaymentPrice",
    "updatePurchaseReturnRefundPrice",
    "updateSaleInReceiptPrice",
    "updateSaleReturnRefundPrice",
    "parsePeriod",
})
_DANGEROUS_SERVICE_TYPES = re.compile(
    r"(?:Pay\w*Service|Wallet\w*Service|Sms\w*Service|Mail\w*Service|Notify\w*Service|MQ|Producer|Flowable|ProcessEngine|RuntimeService|TaskService)",
    re.I,
)

# Java VO / Controller AST regexes
_PACKAGE_RE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.M)
_IMPORT_RE = re.compile(r"^\s*import\s+(?:static\s+)?([\w.*]+)\s*;", re.M)
_CLASS_DECL_RE = re.compile(r"\bclass\s+([A-Z]\w*)\b[^{]*\{")
_VO_FIELD_DECL_RE = re.compile(
    r"^[ \t]*private\s+(?!static\b|final\b)([\w<>?.,\[\]\s@$()=-]+?)\s+(\w+)\s*(?:=[^;]*)?;",
    re.M,
)
_CTRL_CLASS_MAPPING_RE = re.compile(r'@RequestMapping\(\s*(?:value\s*=\s*)?"([^"]+)"')
_RESPONSE_INNER_RE = re.compile(r"CommonResult<\s*([\w.$<>]+)\s*>")
_REQ_BODY_TYPE_RE = re.compile(r"@RequestBody\s+([A-Za-z_][\w.$]*)\s+(\w+)")
_REQ_PARAM_RE = re.compile(r'@RequestParam\(\s*(?:value\s*=\s*|name\s*=\s*)?"([^"]+)"')
_IN_ENUM_RE = re.compile(r"@InEnum\(\s*(?:value\s*=\s*)?([A-Za-z_]\w*)\.class")
_NOT_REQ_ANN_RE = re.compile(r"@(?:NotNull|NotBlank|NotEmpty)\b")
_LIST_ELEM_RE = re.compile(r"(?:List|Set|Collection)<\s*(?:@[A-Za-z_]\w*(?:\([^)]*\))?\s*)*([A-Za-z_][\w.$]*)\s*>")
_ASSERT_TRUE_RE = re.compile(r"@AssertTrue\b")

# TS / Vue form default scanning regexes (single-function boundaries, never crossing request.get calls)
_POST_URL_ARROW_RE = re.compile(
    r"\b(\w+)\s*[:=]\s*(?:async\s*)?\([^)]*\)\s*(?::[^=>{]+)?=>\s*\{?\s*"
    r"(?:return\s+)?(?:await\s+)?request\.post(?:<[^>]+>)?\(\{\s*url:\s*[`'\"]([^`'\"?$]+)"
)
_POST_URL_FN_RE = re.compile(
    r"(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\([^)]*\)\s*(?::[^{]+)?\{\s*"
    r"(?:return\s+)?(?:await\s+)?request\.post(?:<[^>]+>)?\(\{\s*url:\s*[`'\"]([^`'\"?$]+)"
)
_TS_CONST_OBJ_RE = re.compile(
    r"export\s+const\s+([A-Za-z_]\w*)\s*=\s*\{([^}]+)\}(?:\s+as\s+const)?",
    re.S,
)
_TS_ENUM_RE = re.compile(r"export\s+enum\s+([A-Za-z_]\w*)\s*\{([^}]+)\}", re.S)
_TS_SCALAR_CONST_RE = re.compile(
    r"export\s+const\s+([A-Za-z_]\w*)\s*=\s*(['\"][^'\"]*['\"]|-?\d+(?:\.\d+)?|true|false)\b"
)
_API_IMPORT_RE = re.compile(r"import\s+(?:\*\s+as\s+(\w+)|\{([^}]*)\})\s+from\s+['\"]@/([^'\"]+)['\"]")
_OWNER_ACTOR_ASSIGN_RE = re.compile(
    r"formData(?:\.value)?\.(\w+)\s*=\s*(?:getCurrentUserId\(\s*\)|[A-Za-z_]\w*\.getUser\.id\b)"
)
_SET_NULL_RE = re.compile(r"\.set([A-Z]\w*)\(\s*null\s*\)")

_COMPILED_ALL_CACHE: OrderedDict[tuple[int, int, str], dict] = OrderedDict()
_COMPILED_ALL_LOCK = RLock()


@dataclass(frozen=True, slots=True)
class JavaFieldAST:
    name: str
    java_type: str
    required: bool
    enum_class: str = ""
    date_kind: str = ""
    list_element_class: str = ""
    raw_annotations: str = ""


@dataclass(frozen=True, slots=True)
class JavaVOAST:
    full_name: str
    package: str
    imports: dict[str, str]
    fields: tuple[JavaFieldAST, ...]
    password_create_required: bool = False
    password_optional_present: bool = False


@dataclass(frozen=True, slots=True)
class ControllerEndpointAST:
    path: str
    verb: str
    service_iface: str
    service_method: str
    request_vo: str
    response_type: str
    query_params: tuple[str, ...]


def _balanced_brace(text: str, open_idx: int) -> int | None:
    depth = 0
    for idx in range(open_idx, len(text)):
        ch = text[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return idx
    return None


def _resolve_java_type(
    raw_name: str,
    pkg: str,
    imports: dict[str, str],
    wildcard_pkgs: tuple[str, ...],
    enclosing: tuple[str, ...],
    known: set[str],
    unique_top: dict[str, str],
) -> str:
    clean = raw_name.strip().split("<", 1)[0].strip()
    if "." in clean:
        dotted = clean.replace(".", "$")
        for candidate in (clean, dotted):
            if candidate in known:
                return candidate
        parts = clean.split(".")
        outer = _resolve_java_type(parts[0], pkg, imports, wildcard_pkgs, enclosing, known, unique_top)
        nested = f"{outer}${'$'.join(parts[1:])}"
        if nested in known:
            return nested
        last = parts[-1]
        if last in imports:
            imported_outer = imports[last]
            if f"{imported_outer}${last}" in known:
                return f"{imported_outer}${last}"
            if imported_outer in known:
                return imported_outer
        return dotted
    # Check enclosing Outer$Inner classes from innermost to outermost
    for k in range(len(enclosing), 0, -1):
        prefix = "$".join(enclosing[:k])
        for cand in (f"{pkg}.{prefix}${clean}" if pkg else "", f"{prefix}${clean}"):
            if cand and cand in known:
                return cand
    if clean in imports and imports[clean] in known:
        return imports[clean]
    if pkg and f"{pkg}.{clean}" in known:
        return f"{pkg}.{clean}"
    for wp in wildcard_pkgs:
        cand = f"{wp}.{clean}"
        if cand in known:
            return cand
    if clean in unique_top and unique_top[clean] in known:
        return unique_top[clean]
    return clean


def _parse_vo_classes_in_text(
    masked: str,
    raw: str,
    pkg: str,
    imports: dict[str, str],
    wildcard_pkgs: tuple[str, ...],
    class_spans_out: dict[str, tuple[str, dict[str, str], tuple[str, ...], str, tuple[str, ...]]],
) -> None:
    """Walk top-level and nested static classes in one VO file, storing exact own-class snippets."""
    def walk(span_text: str, raw_span: str, outer_chain: tuple[str, ...]) -> None:
        pos = 0
        while pos < len(span_text):
            m = _CLASS_DECL_RE.search(span_text, pos)
            if m is None:
                break
            open_idx = m.end() - 1
            close_idx = _balanced_brace(span_text, open_idx)
            if close_idx is None:
                pos = m.end()
                continue
            cname = m.group(1)
            new_chain = (*outer_chain, cname)
            full_key = f"{pkg}.{('$'.join(new_chain))}" if pkg else "$".join(new_chain)
            inner_masked = span_text[open_idx + 1:close_idx]
            inner_raw = raw_span[open_idx + 1:close_idx]
            # Recurse into child classes first and blank out child bodies to avoid double-counting child fields
            cleaned_chunks = []
            cpos = 0
            for cm in _CLASS_DECL_RE.finditer(inner_masked):
                c_open = cm.end() - 1
                c_close = _balanced_brace(inner_masked, c_open)
                if c_close is not None and cm.start() >= cpos:
                    cleaned_chunks.append(inner_masked[cpos:cm.start()])
                    cpos = c_close + 1
            cleaned_chunks.append(inner_masked[cpos:])
            own_masked = "".join(cleaned_chunks)
            class_spans_out[full_key] = (pkg, imports, wildcard_pkgs, own_masked, new_chain)
            walk(inner_masked, inner_raw, new_chain)
            pos = close_idx + 1

    walk(masked, raw, ())


def _index_java_tree(
    source,
    *,
    vo_do_texts: list[tuple[str, str]] | None = None,
    ctrl_texts: list[tuple[str, str]] | None = None,
) -> tuple[dict[str, JavaVOAST], dict[str, ControllerEndpointAST]]:
    if vo_do_texts is None or ctrl_texts is None:
        if source is None or not getattr(source, "root", None):
            return {}, {}
        lock = getattr(source, "_instance_lock", None) or nullcontext()
        with lock:
            if vo_do_texts is None:
                vo_do_texts = [
                    (rel, source._read(rel))
                    for path in (*source._java_files("VO.java"), *source._java_files("DO.java"))
                    for rel in (source._relative(path),)
                ]
            if ctrl_texts is None:
                ctrl_texts = [
                    (rel, source._read(rel))
                    for path in source._java_files("Controller.java")
                    for rel in (source._relative(path),)
                    if "/controller/app/" not in rel.replace("\\", "/")
                ]
    return _index_java_tree_locked(vo_do_texts, ctrl_texts)


def _index_java_tree_locked(
    vo_do_texts: list[tuple[str, str]],
    ctrl_texts: list[tuple[str, str]],
) -> tuple[dict[str, JavaVOAST], dict[str, ControllerEndpointAST]]:
    raw_class_spans: dict[str, tuple[str, dict[str, str], tuple[str, ...], str, tuple[str, ...]]] = {}
    for _rel, raw in vo_do_texts:
        masked = _mask(raw)
        m_pkg = _PACKAGE_RE.search(masked)
        pkg = m_pkg.group(1) if m_pkg else ""
        all_imports = _IMPORT_RE.findall(masked)
        imports = {imp.rsplit(".", 1)[-1]: imp for imp in all_imports if not imp.endswith(".*")}
        wildcard_pkgs = tuple(imp[:-2] for imp in all_imports if imp.endswith(".*"))
        _parse_vo_classes_in_text(masked, raw, pkg, imports, wildcard_pkgs, raw_class_spans)

    known_vos = set(raw_class_spans)
    top_counts: dict[str, list[str]] = {}
    for full_key in known_vos:
        if "$" in full_key:
            continue
        simple = full_key.rsplit(".", 1)[-1]
        top_counts.setdefault(simple, []).append(full_key)
    unique_top = {simple: keys[0] for simple, keys in top_counts.items() if len(keys) == 1}

    vo_map: dict[str, JavaVOAST] = {}
    for full_key, (pkg, imports, wildcard_pkgs, own_body, chain) in raw_class_spans.items():
        fields: list[JavaFieldAST] = []
        for m in _VO_FIELD_DECL_RE.finditer(own_body):
            raw_type = " ".join(m.group(1).split())
            fname = m.group(2)
            start = m.start()
            sep = max(own_body.rfind(";", 0, start), own_body.rfind("{", 0, start), own_body.rfind("}", 0, start))
            ctx = own_body[max(sep + 1, start - 500):start]
            required = bool(_NOT_REQ_ANN_RE.search(ctx))
            m_enum = _IN_ENUM_RE.search(ctx)
            enum_cls = m_enum.group(1) if m_enum else ""
            base_type = raw_type.split("<", 1)[0].strip()
            date_kind = "epoch_ms" if base_type == "LocalDateTime" else ("date" if base_type == "LocalDate" else "")
            list_elem = ""
            m_list = _LIST_ELEM_RE.search(raw_type)
            if m_list:
                list_elem = _resolve_java_type(
                    m_list.group(1), pkg, imports, wildcard_pkgs, chain, known_vos, unique_top
                )
            fields.append(JavaFieldAST(
                name=fname,
                java_type=raw_type,
                required=required,
                enum_class=enum_cls,
                date_kind=date_kind,
                list_element_class=list_elem,
                raw_annotations=ctx,
            ))
        has_pwd_assert = bool(
            _ASSERT_TRUE_RE.search(own_body) and re.search(r"\bpassword\b|isPasswordValid", own_body, re.I)
        )
        pwd_required_field = any(f.name == "password" and f.required for f in fields)
        pwd_present = any(f.name == "password" for f in fields)
        vo_map[full_key] = JavaVOAST(
            full_name=full_key,
            package=pkg,
            imports=imports,
            fields=tuple(fields),
            password_create_required=bool(pwd_present and (has_pwd_assert or pwd_required_field)),
            password_optional_present=bool(pwd_present and not pwd_required_field),
        )

    endpoints: dict[str, ControllerEndpointAST] = {}
    for rel, raw_text in ctrl_texts:
        if "/controller/app/" in rel.replace("\\", "/"):
            continue
        m_pkg = _PACKAGE_RE.search(raw_text)
        pkg = m_pkg.group(1) if m_pkg else ""
        all_imports = _IMPORT_RE.findall(raw_text)
        imports = {imp.rsplit(".", 1)[-1]: imp for imp in all_imports if not imp.endswith(".*")}
        wildcard_pkgs = tuple(imp[:-2] for imp in all_imports if imp.endswith(".*"))
        m_base = _CTRL_CLASS_MAPPING_RE.search(raw_text)
        if m_base is None:
            continue
        base_url = m_base.group(1).strip("/")
        svc_fields = {name: kind for kind, name in _SERVICE_FIELD.findall(raw_text)}
        for _mname, start, bs, be in _methods(_mask(raw_text)):
            head = raw_text[max(0, start - 700):bs]
            head = head[head.rfind(";") + 1:] if ";" in head else head
            head = head[head.rfind("}") + 1:] if "}" in head else head
            m_map = _METHOD_MAPPING.search(head)
            if m_map is None:
                continue
            verb, sub = m_map.group(1), m_map.group(2).strip("/")
            url = (base_url + "/" + sub).strip("/")
            sig = raw_text[start:bs]
            m_req = _REQ_BODY_TYPE_RE.search(sig)
            req_vo = ""
            if m_req:
                req_vo = _resolve_java_type(
                    m_req.group(1), pkg, imports, wildcard_pkgs, (), known_vos, unique_top
                )
            m_resp = _RESPONSE_INNER_RE.search(sig)
            resp_type = ""
            if m_resp:
                raw_resp = m_resp.group(1).strip()
                resolved_resp = _resolve_java_type(
                    raw_resp, pkg, imports, wildcard_pkgs, (), known_vos, unique_top
                )
                resp_type = resolved_resp
            qparams = tuple(_REQ_PARAM_RE.findall(sig))
            m_call = _SERVICE_CALL.search(raw_text[bs:be])
            svc_iface = svc_fields.get(m_call.group(1), "") if m_call else ""
            svc_method = m_call.group(2) if m_call else ""
            endpoints[f"{verb}:{url}"] = ControllerEndpointAST(
                path=url,
                verb=verb,
                service_iface=svc_iface,
                service_method=svc_method,
                request_vo=req_vo,
                response_type=resp_type,
                query_params=qparams,
            )

    return vo_map, endpoints


def _load_ui_constants(ui_src: Path) -> dict[str, object]:
    """Load exported scalar & object constants from release UI ``.ts`` files."""
    consts: dict[str, object] = {}
    for folder in ("views", "utils", "components"):
        base = ui_src / folder
        if not base.is_dir():
            continue
        for ts_path in sorted(base.rglob("*.ts")):
            raw = ts_path.read_text(encoding="utf-8", errors="ignore")
            for obj_name, block in _TS_CONST_OBJ_RE.findall(raw):
                mapping = {}
                for k, val in re.findall(r"^\s*([A-Za-z_]\w*)\s*:\s*(['\"][^'\"]*['\"]|-?\d+(?:\.\d+)?|true|false)", block, re.M):
                    mapping[k] = _eval_ts_literal(val, consts)
                if mapping:
                    consts[obj_name] = mapping
            for enum_name, block in _TS_ENUM_RE.findall(raw):
                mapping = {}
                for k, val in re.findall(r"^\s*([A-Za-z_]\w*)\s*=\s*(['\"][^'\"]*['\"]|-?\d+(?:\.\d+)?|true|false)", block, re.M):
                    mapping[k] = _eval_ts_literal(val, consts)
                if mapping:
                    consts[enum_name] = mapping
            for cname, val in _TS_SCALAR_CONST_RE.findall(raw):
                consts.setdefault(cname, _eval_ts_literal(val, consts))
    return consts


def _eval_ts_literal(expr: str, consts: dict[str, object]) -> object:
    tok = expr.strip().rstrip(",")
    if not tok or tok in {"undefined", "null", "NaN"}:
        return None
    if tok == "true":
        return True
    if tok == "false":
        return False
    if (tok.startswith("'") and tok.endswith("'")) or (tok.startswith('"') and tok.endswith('"')):
        return tok[1:-1]
    if re.fullmatch(r"-?\d+", tok):
        return int(tok)
    if re.fullmatch(r"-?\d+\.\d+", tok):
        return float(tok)
    if "." in tok:
        left, right = tok.split(".", 1)
        obj = consts.get(left.strip())
        if isinstance(obj, dict) and right.strip() in obj:
            return obj[right.strip()]
    if tok in consts and not isinstance(consts[tok], dict):
        return consts[tok]
    return None


def _extract_brace_object_literal(text: str, marker_pos: int) -> str | None:
    idx = text.find("{", marker_pos)
    if idx < 0:
        return None
    end = _balanced_brace(text, idx)
    if end is None:
        return None
    return text[idx + 1:end]


def _extract_vue_child_defaults(vue_path: Path, text: str, ts_consts: dict) -> tuple[CompiledFieldDefault, ...]:
    """Scan local child sub-form Vue components imported by a form for handleAdd/addRow defaults."""
    blocks_text = [text]
    for _alias, _named, mod in _API_IMPORT_RE.findall(text):
        pass
    for imp in re.findall(r"""import\s+\w+\s+from\s+['"](\.[^'"]+\.vue)['"]""", text):
        cand = (vue_path.parent / imp).resolve()
        if cand.is_file():
            blocks_text.append(cand.read_text(encoding="utf-8", errors="ignore"))
    out: dict[str, CompiledFieldDefault] = {}
    row_triggers = (
        re.compile(r"function\s+(?:handleAdd\w*|addRow\w*)\b[^{]*\{[\s\S]{0,200}?\bconst\s+row\s*=\s*(?=\{)"),
        re.compile(r"const\s+(?:handleAdd\w*|addRow\w*)\s*=\s*\([^)]*\)\s*=>\s*\{[\s\S]{0,200}?\bconst\s+row\s*=\s*(?=\{)"),
        re.compile(r"\bpush\s*\(\s*(?=\{)"),
    )
    for src_txt in blocks_text:
        for pat in row_triggers:
            for m in pat.finditer(src_txt):
                block = _extract_brace_object_literal(src_txt, m.end())
                if not block:
                    continue
                for k, expr in re.findall(r"^\s*([A-Za-z_]\w*)\s*:\s*([^,\n/]+)", block, re.M):
                    if k == "id" or _SECRET.search(k):
                        continue
                    val = _eval_ts_literal(expr, ts_consts)
                    if val is not None and val != "":
                        out.setdefault(k, CompiledFieldDefault(field=k, kind="literal", value=val, display=str(val)))
    return tuple(out.values())


def _compile_vue_create_defaults(source, ui=None) -> tuple[dict[str, tuple[CompiledFieldDefault, ...]], dict[str, tuple[CompiledFieldDefault, ...]]]:
    """Extract D1 (current actor) and D2 (literal/constant) parent & child create defaults from ``*.vue`` forms."""
    ui_src: Path | None = None
    if source is not None and getattr(source, "root", None):
        cand = Path(source.root) / "ui" / "src"
        if (cand / "views").is_dir():
            ui_src = cand
    if ui_src is None and ui is not None and getattr(ui, "root", None):
        cand = Path(ui.root) / "src"
        if (cand / "views").is_dir():
            ui_src = cand
    if ui_src is None:
        return {}, {}

    ui_api = ui_src / "api"
    api_creates: dict[str, dict[str, str]] = {}
    if ui_api.is_dir():
        for ts_path in sorted(ui_api.rglob("*.ts")):
            rel = ts_path.relative_to(ui_api).as_posix()
            mod = "api/" + (rel[:-len("/index.ts")] if rel.endswith("/index.ts") else rel[:-len(".ts")])
            txt = ts_path.read_text(encoding="utf-8", errors="ignore")
            fn_urls: dict[str, str] = {}
            for pat in (_POST_URL_ARROW_RE, _POST_URL_FN_RE):
                for fn_name, url in pat.findall(txt):
                    cleaned = url.strip().removeprefix("/admin-api").strip("/")
                    if cleaned.endswith("/create"):
                        fn_urls[fn_name] = cleaned[:-len("/create")]
            if fn_urls:
                api_creates[mod] = fn_urls

    ts_consts = _load_ui_constants(ui_src)
    defaults_out: dict[str, tuple[CompiledFieldDefault, ...]] = {}
    child_defaults_out: dict[str, tuple[CompiledFieldDefault, ...]] = {}
    for vue_path in sorted((ui_src / "views").rglob("*.vue")):
        text = vue_path.read_text(encoding="utf-8", errors="ignore")
        if "create" not in text and "Create" not in text:
            continue
        aliases: dict[str, str] = {}
        directs: dict[str, str] = {}
        for alias, named, mod in _API_IMPORT_RE.findall(text):
            if alias and mod in api_creates:
                aliases[alias] = mod
            elif named and mod in api_creates:
                for tok in named.split(","):
                    parts = tok.strip().split(" as ")
                    orig = parts[0].strip()
                    local = parts[-1].strip()
                    if orig in api_creates[mod] and local:
                        directs[local] = api_creates[mod][orig]
        matched_entities: set[str] = set()
        for obj, fn in re.findall(r"\b(\w+)\.(\w+)\(", text):
            mod = aliases.get(obj)
            if mod and fn in api_creates[mod]:
                matched_entities.add(api_creates[mod][fn])
        for local, ent in directs.items():
            if re.search(r"\b" + re.escape(local) + r"\s*\(", text):
                matched_entities.add(ent)
        if len(matched_entities) != 1:
            continue
        entity = next(iter(matched_entities))

        field_defaults: dict[str, CompiledFieldDefault] = {}
        # Check both createDefaultFormData() return literals and ref<{...}> / resetForm() literals
        default_pat_triggers = (
            re.compile(r"function\s+createDefaultFormData\b[^{]*\{[\s\S]{0,120}?\breturn\s*(?=\{)"),
            re.compile(r"\bresetForm\b[^{]*\{[\s\S]{0,120}?formData(?:\.value)?\s*=\s*(?=\{)"),
            re.compile(r"const\s+formData\s*=\s*ref<[^>]+>\s*\(\s*(?=\{)"),
        )
        for pat in default_pat_triggers:
            m = pat.search(text)
            if m:
                block = _extract_brace_object_literal(text, m.end())
                if block:
                    for k, expr in re.findall(r"^\s*([A-Za-z_]\w*)\s*:\s*([^,\n/]+)", block, re.M):
                        if k == "id" or _SECRET.search(k):
                            continue
                        val = _eval_ts_literal(expr, ts_consts)
                        if val is not None and val != "":
                            field_defaults[k] = CompiledFieldDefault(field=k, kind="literal", value=val, display=str(val))
        # Scan D1 actor defaults (ownerUserId = getCurrentUserId() / store user id)
        for m_act in _OWNER_ACTOR_ASSIGN_RE.finditer(text):
            f_name = m_act.group(1)
            field_defaults[f_name] = CompiledFieldDefault(field=f_name, kind="actor", display="当前登录账号")

        if field_defaults:
            defaults_out[entity] = tuple(field_defaults.values())
        cdefs = _extract_vue_child_defaults(vue_path, text, ts_consts)
        if cdefs:
            child_defaults_out[entity] = cdefs
    return defaults_out, child_defaults_out


_DO_FIELD_RE = re.compile(r"(?:(?<=^)|(?<=[;{}\s]))private\s+(?!static\b|final\b)[\w<>\[\], ?]+\s+(\w+)\s*(?:=|;)", re.M)
_DO_CLASS_RE = re.compile(r"(?:public\s+)?(?:abstract\s+)?class\s+(\w+)(?:\s+extends\s+(\w+))?")
_BASE_DO_FIELDS = frozenset({"id", "createTime", "updateTime", "creator", "updater", "deleted", "tenantId"})


def _collect_do_fields(
    source,
    *,
    do_texts: list[tuple[str, str]] | None = None,
) -> dict[str, set[str]]:
    """Index declared entity DO fields (including superclass DO chains) for BeanUtils drop detection."""
    if do_texts is None:
        if source is None or not getattr(source, "root", None):
            return {}
        lock = getattr(source, "_instance_lock", None) or nullcontext()
        with lock:
            do_texts = [
                (rel, source._read(rel))
                for path in source._java_files("DO.java")
                for rel in (source._relative(path),)
            ]
    raw_fields: dict[str, set[str]] = {}
    parents: dict[str, str] = {}
    for _rel, raw in do_texts:
        text = _mask(raw)
        m_cls = _DO_CLASS_RE.search(text)
        if not m_cls:
            continue
        cname = m_cls.group(1)
        parents[cname] = m_cls.group(2) or ""
        raw_fields[cname] = {m.group(1) for m in _DO_FIELD_RE.finditer(text)} | set(_BASE_DO_FIELDS)
    resolved: dict[str, set[str]] = {}
    for cname, fields in raw_fields.items():
        merged = set(fields)
        cur = parents.get(cname, "")
        seen: set[str] = {cname}
        while cur and cur in raw_fields and cur not in seen:
            seen.add(cur)
            merged |= raw_fields[cur]
            cur = parents.get(cur, "")
        resolved[cname] = merged
    return resolved


def _analyze_service_method(
    source,
    ep: ControllerEndpointAST | None,
    *,
    allow_child_mappers: bool = False,
    do_fields: dict[str, set[str]] | None = None,
    vo_field_names: tuple[str, ...] = (),
    svc_by_iface: dict[str, str] | None = None,
    parsed_svc_cache: dict[str, tuple[str, dict[str, str], dict[str, list[tuple[int, int, int]]]]] | None = None,
) -> tuple[bool, set[str], set[str]]:
    """Return ``(safe, unconditional_null_fields, dropped_vo_fields)`` for one service method."""
    if ep is None or not ep.service_iface or not ep.service_method:
        return False, set(), set()
    if svc_by_iface is None and (source is None or not getattr(source, "root", None)):
        return False, set(), set()
    if _DANGEROUS_SERVICE_TYPES.search(ep.service_iface):
        return False, set(), set()

    cached_svc = parsed_svc_cache.get(ep.service_iface) if parsed_svc_cache is not None else None
    if cached_svc is not None:
        masked, svc_fields, sigs = cached_svc
    else:
        raw_svc = svc_by_iface.get(ep.service_iface) if svc_by_iface is not None else None
        if raw_svc is None:
            if svc_by_iface is not None:
                return False, set(), set()
            target_file = None
            for path in source._java_files("ServiceImpl.java"):
                if path.stem[:-len("Impl")] == ep.service_iface:
                    target_file = path
                    break
            if target_file is None:
                return False, set(), set()
            raw_svc = source._read(source._relative(target_file))
        masked = _mask(raw_svc)
        svc_fields = {name: kind for kind, name in _SERVICE_FIELD.findall(masked)}
        sigs = {}
        for name, start, bs, be in _methods(masked):
            sigs.setdefault(name, []).append((start, bs, be))
        if parsed_svc_cache is not None:
            parsed_svc_cache[ep.service_iface] = (masked, svc_fields, sigs)
    spans = sigs.get(ep.service_method)
    if not spans:
        return False, set(), set()

    visited: set[tuple[str, int]] = set()
    work = [(ep.service_method, i) for i in range(len(spans))]
    bodies: list[tuple[str, int, int, int]] = []
    while work:
        mname, idx = work.pop()
        if (mname, idx) in visited:
            continue
        visited.add((mname, idx))
        s, bs, be = sigs[mname][idx]
        bodies.append((mname, s, bs, be))
        for cm in _CALL.finditer(masked[bs:be]):
            callee = cm.group(1)
            for ci in range(len(sigs.get(callee, ()))):
                if (callee, ci) not in visited:
                    work.append((callee, ci))

    allow_employee_station_notice = (
        ep.service_iface == "HrmEmployeeService" and ep.service_method == "createEmployee"
    )
    if ep.verb.casefold() == "put" and bodies:
        # Version-forking pseudo-updates disable old record and insert a new ID so GET(old_id) never updates.
        entry_chunk = masked[bodies[0][2]:bodies[0][3]]
        if ".setId(null)" in entry_chunk and re.search(r"\b\w*Mapper\.insert\s*\(", entry_chunk):
            return False, set(), set()

    for mname, s, bs, be in bodies:
        if re.search(r"@Async\b", masked[max(0, s - 200):bs]):
            return False, set(), set()
        if allow_employee_station_notice and mname in {
            "executeAfterCommit",
            "sendEmployeeOpenedMessage",
            "sendEmployeeMessage",
        }:
            continue
        chunk = masked[bs:be]
        # Untraceable async / broker / reflection / thread calls
        # (Allow pure UI/IM profile-sync helper in AdminUserServiceImpl#updateUser while blocking all real MQ/async brokers.)
        if mname != "publishUserProfileUpdatedIfChanged":
            for _kind, pat in getattr(source, "_UNTRACEABLE", ()):
                if pat.search(chunk):
                    return False, set(), set()
        # Own-mapper updates must use standard updateById or updateForEdit
        for m_name, m_verb in re.findall(r"\b(\w+Mapper)\.(update\w+)\s*\(", chunk):
            if m_verb not in {"updateById", "updateForEdit"}:
                if not allow_child_mappers:
                    return False, set(), set()
        # Check sibling Service field calls
        for sc_var, sc_method in _SERVICE_CALL.findall(chunk):
            sc_type = svc_fields.get(sc_var, sc_var)
            if _DANGEROUS_SERVICE_TYPES.search(sc_type):
                return False, set(), set()
            if _READ_ONLY_SERVICE_METHOD.fullmatch(sc_method) or sc_method in _SAFE_EXTRA_SERVICE_METHODS:
                continue
            if allow_child_mappers and (
                "ItemService" in sc_type
                or "DetailService" in sc_type
                or "EntryService" in sc_type
                or "SkuService" in sc_type
            ):
                continue
            return False, set(), set()

    # Check unconditional vs conditional setXxx(null) on entry method (PUT updates only; CREATE DO builders normalize derived nulls before insert)
    uncond_nulls: set[str] = set()
    if ep.verb.casefold() == "put":
        s0, bs0, be0 = spans[0]
        params = _signature_params(masked, s0, bs0)
        if params:
            req_var = params[0]
            req_pat = re.compile(r"\b" + re.escape(req_var) + r"\s*\.set[A-Z]\w*\(")
            brace_depth = 0
            pending = False
            for raw_ln in masked[bs0:be0].splitlines():
                ln = raw_ln.strip()
                if not ln:
                    continue
                is_branch = bool(re.match(r"^(?:if|else)\b", ln))
                ends = ln.endswith((";", "{", "}"))
                if req_pat.search(ln):
                    nprops = [p[0].lower() + p[1:] for p in _SET_NULL_RE.findall(ln)]
                    if "?" in ln and "null" in ln:
                        return False, set(), set()
                    if nprops:
                        if brace_depth == 0 and not pending and not is_branch and ln.startswith(req_var + "."):
                            uncond_nulls.update(p for p in nprops if p != "id")
                        else:
                            return False, set(), set()
                brace_depth += ln.count("{") - ln.count("}")
                if is_branch and not ln.endswith("{") and ";" not in ln:
                    pending = True
                elif ends:
                    pending = False
        for mname, _s, bs, be in bodies[1:]:
            if mname == ep.service_method:
                continue
            if any(p != "Id" for p in _SET_NULL_RE.findall(masked[bs:be])):
                return False, set(), set()

    dropped: set[str] = set()
    if do_fields and vo_field_names and bodies:
        all_body_text = "\n".join(masked[bs:be] for _m, _s, bs, be in bodies)
        entry_body = masked[bodies[0][2]:bodies[0][3]]
        do_hits = re.findall(r"BeanUtils\.toBean\s*\(\s*\w+\s*,\s*(\w+DO)\.class\s*\)", entry_body)
        if len(set(do_hits)) == 1 and do_hits[0] in do_fields:
            target_do = do_fields[do_hits[0]]
            for fname in vo_field_names:
                if fname == "id" or fname in target_do:
                    continue
                cap = fname[0].upper() + fname[1:] if fname else ""
                if not re.search(rf"\.(?:get|is){re.escape(cap)}\s*\(", all_body_text):
                    dropped.add(fname)
    return True, uncond_nulls, dropped


def _infer_extended_reference(entity: str, prop: str, kind: str, desc: str, available_paths: set[str]) -> str:
    if kind == "integer":
        if prop in {"deptId", "keeperDeptId", "receiveDeptId"} or (entity.startswith("system/dept") and prop == "parentId"):
            return "system/dept"
        if prop in {
            "leaderUserId", "ownerUserId", "managerUserId", "moderatorUserId",
            "keeperUserId", "handoverUserId", "handlerUserId", "userId", "bindUserId",
            "witnessUserId", "financeUserId", "saleUserId", "purchaseUserId",
        }:
            return "system/user"
        if prop in {"leaderEmployeeId", "employeeId", "interviewEmployeeId"}:
            return "hrm/employee"
        if prop == "areaId" and ("地区" in desc or "省" in desc or "区域" in desc):
            return "system/area"
        if prop == "customerId":
            if entity.startswith("erp/") and "/admin-api/erp/customer/simple-list" in available_paths:
                return "erp/customer"
            if "/admin-api/crm/customer/page" in available_paths:
                return "crm/customer"
        if prop == "contractId" and "/admin-api/crm/contract/page" in available_paths:
            return "crm/contract"
        if prop == "businessId" and "/admin-api/crm/business/page" in available_paths:
            return "crm/business"
        if prop == "planId" and entity.startswith("crm/") and "/admin-api/crm/receivable-plan/page" in available_paths:
            return "crm/receivable-plan"
        if prop == "supplierId" and "/admin-api/erp/supplier/simple-list" in available_paths:
            return "erp/supplier"
        if prop == "accountId" and entity.startswith("erp/") and "/admin-api/erp/account/simple-list" in available_paths:
            return "erp/account"
        if prop in {"warehouseId", "sourceWarehouseId", "targetWarehouseId"} and entity.startswith("wms/") and "/admin-api/wms/warehouse/simple-list" in available_paths:
            return "wms/warehouse"
        if prop in {"warehouseId", "fromWarehouseId", "toWarehouseId"} and "/admin-api/erp/warehouse/simple-list" in available_paths:
            return "erp/warehouse"
        if entity.startswith("wms/"):
            if prop == "skuId" and "/admin-api/wms/item-sku/page" in available_paths:
                return "wms/item-sku"
            if prop == "categoryId" and "/admin-api/wms/item-category/simple-list" in available_paths:
                return "wms/item-category"
            if prop == "brandId" and "/admin-api/wms/item-brand/simple-list" in available_paths:
                return "wms/item-brand"
            if prop == "merchantId" and "/admin-api/wms/merchant/simple-list" in available_paths:
                return "wms/merchant"
        if prop == "orderId":
            if entity in {"erp/purchase-in", "erp/purchase-return"} and "/admin-api/erp/purchase-order/page" in available_paths:
                return "erp/purchase-order"
            if entity in {"erp/sale-out", "erp/sale-return"} and "/admin-api/erp/sale-order/page" in available_paths:
                return "erp/sale-order"
        if prop == "itemId" and entity.startswith("oa/supply") and "/admin-api/oa/supply-item/page" in available_paths:
            return "oa/supply-item"
        if prop == "spuId" and entity.startswith(("product/", "promotion/")) and "/admin-api/product/spu/page" in available_paths:
            return "product/spu"
        if prop == "brandId" and entity.startswith("product/") and "/admin-api/product/brand/list" in available_paths:
            return "product/brand"
        if prop == "deliveryTemplateId" and entity.startswith("product/") and "/admin-api/trade/delivery/express-template/list-all-simple" in available_paths:
            return "trade/delivery/express-template"
        if prop == "productId":
            if entity.startswith("erp/") and "/admin-api/erp/product/simple-list" in available_paths:
                return "erp/product"
            if entity.startswith("crm/") and ("/admin-api/crm/product/simple-list" in available_paths or "/admin-api/crm/product/page" in available_paths):
                return "crm/product"
        if prop == "productUnitId" and "/admin-api/erp/product-unit/simple-list" in available_paths:
            return "erp/product-unit"
        if prop == "accountSetId" and (
            "/admin-api/fms/config/account-set/simple-list" in available_paths
            or "/admin-api/fms/config/account-set/list" in available_paths
        ):
            return "fms/config/account-set"
        if prop == "voucherWordId" and "/admin-api/fms/config/voucher-word/simple-list" in available_paths:
            return "fms/config/voucher-word"
        if prop == "subjectId" and entity.startswith("fms/") and "/admin-api/fms/config/subject/simple-list" in available_paths:
            return "fms/config/subject"
        if prop == "channelId" and entity.startswith("hrm/") and "/admin-api/hrm/recruit/channel/simple-list" in available_paths:
            return "hrm/recruit/channel"
        if prop == "postId":
            if entity.startswith("hrm/recruit") and "/admin-api/hrm/recruit/post/page" in available_paths:
                return "hrm/recruit/post"
            if "/admin-api/system/post/simple-list" in available_paths:
                return "system/post"
        if prop == "categoryId":
            mod_prefix = entity.split("/", 1)[0]
            cand = f"{mod_prefix}/category"
            if f"/admin-api/{cand}/simple-list" in available_paths or f"/admin-api/{cand}/list" in available_paths:
                return cand
            cand_p = f"{mod_prefix}/product-category"
            if f"/admin-api/{cand_p}/list" in available_paths:
                return cand_p
    if kind == "id_array":
        if prop.endswith("UserIds"):
            return "system/user"
        if prop.endswith("DeptIds"):
            return "system/dept"
        if prop.endswith("EmployeeIds"):
            return "hrm/employee"
        if prop == "postIds":
            return "system/post"
        if prop == "roleIds":
            return "system/role"
        if prop == "menuIds":
            return "system/menu"
    return ""


def _deref(schema, schemas: dict) -> dict:
    if not isinstance(schema, dict):
        return {}
    ref = schema.get("$ref")
    return schemas.get(ref.rsplit("/", 1)[-1], {}) if isinstance(ref, str) else schema


def _resp_schema_props(op: dict, schemas: dict) -> dict:
    try:
        outer_raw = next(iter(op["responses"]["200"]["content"].values()))["schema"]
    except (KeyError, StopIteration, TypeError, AttributeError):
        return {}
    outer = _deref(outer_raw, schemas)
    data_raw = (outer.get("properties") or {}).get("data") or {}
    data_schema = _deref(data_raw, schemas)
    if data_schema.get("type") == "array":
        data_schema = _deref(data_schema.get("items") or {}, schemas)
    return data_schema.get("properties") or {}


def _clean_desc_label(desc: str) -> str:
    if not desc:
        return ""
    head = re.split(r"[，,；;（(。!！\n]", desc.strip(), maxsplit=1)[0].strip()
    return head[:40] if 0 < len(head) <= 40 else ""


def _build_field_spec(
    entity: str,
    fname: str,
    raw_prop: dict,
    *,
    schemas: dict,
    available_paths: set[str],
    java_field: JavaFieldAST | None,
    req_required: set[str],
    ent_labels: dict[str, str],
    ent_dicts: dict[str, str],
    ent_scales: dict[str, tuple[int, str]],
    has_java_vo: bool,
    status_in_status_specs: bool,
    known_java_enums: frozenset[str] = frozenset(),
) -> ConventionFieldSpec | None:
    prop = _deref(raw_prop, schemas)
    desc = str(prop.get("description") or raw_prop.get("description") or "")
    lbl = ent_labels.get(fname) or _clean_desc_label(desc)
    if _SECRET.search(fname) or _SECRET.search(desc) or _SECRET.search(lbl):
        return None
    ptype = prop.get("type")
    pformat = prop.get("format") or ""
    if "$ref" in raw_prop or ptype == "object":
        return None

    date_kind = java_field.date_kind if java_field else ""
    if ptype == "array":
        items_raw = prop.get("items") or {}
        items = _deref(items_raw, schemas)
        if "$ref" in items_raw or items.get("type") not in {"integer", "string", "number"}:
            return None
        kind = "id_array"
    elif date_kind == "epoch_ms" or (ptype == "string" and pformat == "date-time"):
        kind = "integer"
        date_kind = "epoch_ms"
    elif date_kind == "date" or (ptype == "string" and pformat == "date"):
        kind = "string"
        date_kind = "date"
    elif ptype in {"string", "integer", "number", "boolean"}:
        kind = ptype
    else:
        return None

    is_req = java_field.required if (has_java_vo and java_field is not None) else (fname in req_required)
    unit_scale, display_unit = ent_scales.get(fname, (1, ""))
    if unit_scale == 1 and kind == "integer" and re.search(r"单位\s*[：:（(]?\s*分\b", desc):
        unit_scale, display_unit = 100, "元"

    enum_type = ent_dicts.get(fname, "")
    if not enum_type and java_field is not None and java_field.enum_class and java_field.enum_class in known_java_enums:
        enum_type = f"__java_enum__:{java_field.enum_class}"
    if (not enum_type and fname == "status" and kind == "integer" and not status_in_status_specs
            and ("CommonStatusEnum" in desc or "开启状态" in desc or "启用状态" in desc)):
        enum_type = "common_status"

    ref_entity = _infer_extended_reference(entity, fname, kind, desc, available_paths)
    max_len = prop.get("maxLength") if type(prop.get("maxLength")) is int else None
    min_val = prop.get("minimum") if type(prop.get("minimum")) in (int, float) else None
    max_val = prop.get("maximum") if type(prop.get("maximum")) in (int, float) else None
    min_len = prop.get("minLength") if type(prop.get("minLength")) is int and prop.get("minLength") > 0 else None
    try:
        return ConventionFieldSpec(
            name=fname,
            kind=kind,
            label=lbl,
            required=is_req,
            max_length=max_len,
            minimum=min_val,
            maximum=max_val,
            min_length=min_len,
            enum_type=enum_type,
            reference_entity=ref_entity,
            unit_scale=unit_scale,
            display_unit=display_unit,
            date_kind=date_kind,
        )
    except ValueError:
        return None


def _compile_child_collection(
    entity: str,
    col_name: str,
    raw_arr_prop: dict,
    *,
    schemas: dict,
    available_paths: set[str],
    vo_map: dict[str, JavaVOAST],
    java_list_elem_vo: str,
    resp_list_elem_vo: str = "",
    resp_arr_prop: dict,
    ent_labels: dict[str, str],
    ent_dicts: dict[str, str],
    ent_scales: dict[str, tuple[int, str]],
    depth: int = 1,
    known_java_enums: frozenset[str] = frozenset(),
) -> CompiledChildCollectionSpec | None:
    if depth > 2:
        return None
    arr_schema = _deref(raw_arr_prop, schemas)
    item_schema = _deref(arr_schema.get("items") or {}, schemas)
    item_props = item_schema.get("properties") or {}
    if not item_props:
        return None
    java_vo = vo_map.get(java_list_elem_vo)
    java_fields = {f.name: f for f in java_vo.fields} if java_vo else {}
    # If OpenAPI's shared schema name ("Item") collided with another module, reconstruct from Java fields!
    if java_vo and any(k not in item_props for k in java_fields if k != "id"):
        item_props = _synthesize_props_from_java_vo(java_vo, schemas, item_props)

    resp_item_schema = _deref((_deref(resp_arr_prop, schemas).get("items") or {}), schemas)
    resp_item_props = resp_item_schema.get("properties") or {}
    resp_java_vo = vo_map.get(resp_list_elem_vo) if resp_list_elem_vo else None
    resp_java_fields = {f.name: f for f in resp_java_vo.fields} if resp_java_vo else {}
    if resp_java_vo and any(k not in resp_item_props for k in resp_java_fields if k != "id"):
        resp_item_props = _synthesize_props_from_java_vo(resp_java_vo, schemas, resp_item_props)
    req_required = set(item_schema.get("required") or ())

    fields: list[ConventionFieldSpec] = []
    subcollections: list[CompiledChildCollectionSpec] = []
    computed: set[str] = set()
    req_add: list[str] = []

    for fname, f_raw in item_props.items():
        if fname == "id":
            continue
        f_schema = _deref(f_raw, schemas)
        jf = java_fields.get(fname)
        rjf = resp_java_fields.get(fname)
        if f_schema.get("type") == "array":
            it_raw = f_schema.get("items") or {}
            it_deref = _deref(it_raw, schemas)
            if "$ref" in it_raw or it_deref.get("type") == "object" or (jf and jf.list_element_class in vo_map):
                if depth == 2:
                    return None
                sub_col = _compile_child_collection(
                    entity,
                    fname,
                    f_raw,
                    schemas=schemas,
                    available_paths=available_paths,
                    vo_map=vo_map,
                    java_list_elem_vo=(jf.list_element_class if jf else ""),
                    resp_list_elem_vo=(rjf.list_element_class if rjf else ""),
                    resp_arr_prop=resp_item_props.get(fname) or {},
                    ent_labels=ent_labels,
                    ent_dicts=ent_dicts,
                    ent_scales=ent_scales,
                    depth=depth + 1,
                    known_java_enums=known_java_enums,
                )
                if sub_col is None:
                    return None
                subcollections.append(sub_col)
                continue
        if resp_item_props and fname not in resp_item_props:
            # Skip unverified write-only fields unless explicit known match
            continue
        f_spec = _build_field_spec(
            entity,
            fname,
            f_raw,
            schemas=schemas,
            available_paths=available_paths,
            java_field=jf,
            req_required=req_required,
            ent_labels=ent_labels,
            ent_dicts=ent_dicts,
            ent_scales=ent_scales,
            has_java_vo=bool(java_vo),
            status_in_status_specs=False,
            known_java_enums=known_java_enums,
        )
        if f_spec is None:
            if jf and jf.required:
                return None
            continue
        fields.append(f_spec)
        if f_spec.required:
            req_add.append(f_spec.name)

    if not fields:
        return None
    for known_comp in ("taxPrice", "totalPrice", "totalProductPrice", "productUnitId"):
        if entity.startswith("erp/") and known_comp in item_props and known_comp not in req_add:
            computed.add(known_comp)
    if entity == "hrm/salary/change-template" and col_name == "options" and "name" in item_props:
        computed.add("name")
    if entity == "product/spu" and col_name == "skus" and "name" in item_props:
        computed.add("name")
    if entity == "promotion/point-activity" and col_name == "products":
        for known_comp in ("activityId", "activityStatus"):
            if known_comp in item_props:
                computed.add(known_comp)
        req_add = [r for r in req_add if r not in computed]
    if entity == "crm/business-status" and col_name == "statuses" and "sort" in item_props:
        computed.add("sort")
        req_add = [r for r in req_add if r != "sort"]
    if entity == "wms/item" and col_name == "skus" and "itemId" in item_props:
        computed.add("itemId")
        req_add = [r for r in req_add if r != "itemId"]
    lbl = _clean_desc_label(str(arr_schema.get("description") or raw_arr_prop.get("description") or "")) or col_name
    min_items = arr_schema.get("minItems") if type(arr_schema.get("minItems")) is int else (2 if col_name == "entries" and entity == "fms/voucher" else 0)
    return CompiledChildCollectionSpec(
        name=col_name,
        label=lbl,
        fields=tuple(fields),
        name_fields=tuple(k for k in ("name", "code", "digest", "productName", "subjectName", "label") if k in item_props or k in resp_item_props) or ("name",),
        min_items=min_items,
        required_on_add=tuple(req_add),
        server_computed_fields=frozenset(computed),
        subcollections=tuple(subcollections),
        volatile_fields=_VOLATILE,
    )


def _synthesize_props_from_java_vo(java_vo: JavaVOAST, schemas: dict, fallback_props: dict) -> dict:
    """Reconstruct properties dict from exact Java AST inner VO when OpenAPI schema name collided (e.g. 'Item')."""
    out: dict[str, dict] = {}
    for jf in java_vo.fields:
        if jf.name in fallback_props and not fallback_props.get(jf.name, {}).get("$ref"):
            out[jf.name] = dict(fallback_props[jf.name])
            continue
        jt = jf.java_type.split("<", 1)[0].strip()
        if jt in {"Long", "Integer", "Short", "Byte", "long", "int"}:
            out[jf.name] = {"type": "integer", "description": jf.name}
        elif jt in {"BigDecimal", "Double", "Float", "double", "float"}:
            out[jf.name] = {"type": "number", "description": jf.name}
        elif jt in {"Boolean", "boolean"}:
            out[jf.name] = {"type": "boolean", "description": jf.name}
        elif jt == "LocalDateTime":
            out[jf.name] = {"type": "string", "format": "date-time", "description": jf.name}
        elif jt == "LocalDate":
            out[jf.name] = {"type": "string", "format": "date", "description": jf.name}
        elif jt == "String":
            out[jf.name] = {"type": "string", "description": jf.name}
        elif jt in {"List", "Set", "Collection"}:
            out[jf.name] = {"type": "array", "items": {"type": "integer"}, "description": jf.name}
    return out


def _classify_nested_columns(req_props: dict, schemas: dict) -> tuple[bool, list[tuple[str, dict]]]:
    has_opaque_object = False
    nested_cols: list[tuple[str, dict]] = []
    for fn, raw_fp in req_props.items():
        fp = _deref(raw_fp, schemas)
        if "$ref" in raw_fp or fp.get("type") == "object":
            has_opaque_object = True
            break
        if fp.get("type") == "array":
            it_raw = fp.get("items") or {}
            it_d = _deref(it_raw, schemas)
            if "$ref" in it_raw or it_d.get("type") == "object":
                nested_cols.append((fn, raw_fp))
    return has_opaque_object, nested_cols


def _service_required_child_cols(
    ep_ast: ControllerEndpointAST | None,
    col_names: set[str],
    parsed_svc_cache: dict[str, tuple[str, dict[str, str], dict[str, list[tuple[int, int, int]]]]],
) -> set[str]:
    """Detect child collections rejected when empty in Java create service or helper calls."""
    if ep_ast is None or not col_names or not ep_ast.service_iface or ep_ast.service_iface not in parsed_svc_cache:
        return set()
    masked, _svc_fields, sigs = parsed_svc_cache[ep_ast.service_iface]
    entry_spans = sigs.get(ep_ast.service_method) or ()
    if not entry_spans:
        return set()
    chunks = [masked[bs:be] for _s, bs, be in entry_spans]
    for mname, spans in sigs.items():
        if mname != ep_ast.service_method and any(mname in c for c in chunks):
            chunks.extend(masked[bs:be] for _s, bs, be in spans)
    body = "\n".join(chunks)
    required_cols: set[str] = set()
    for cname in col_names:
        cap = cname[0].upper() + cname[1:]
        if re.search(rf"(?:CollUtil|CollectionUtils|ObjectUtil)\.isEmpty\([^)]*\.get{re.escape(cap)}\s*\(", body):
            required_cols.add(cname)
        elif cname == "skus" and re.search(r"\bvalidateSkuList\s*\(", body):
            required_cols.add(cname)
    return required_cols


def _agg_server_computed_fields(entity: str, *, is_salary_special: bool = False) -> set[str]:
    computed: set[str] = set()
    if entity.startswith("erp/"):
        computed |= {"totalCount", "totalPrice", "totalProductPrice", "totalTaxPrice"}
        if entity not in {"erp/finance-receipt", "erp/finance-payment"}:
            computed.add("discountPrice")
    elif entity.startswith("wms/"):
        computed |= {"totalQuantity", "totalPrice", "totalAmount"}
    elif entity in {"oa/travel-reimbursement", "oa/reimbursement"}:
        computed |= {"days", "totalPrice", "invoiceCount"}
    elif entity == "crm/contract":
        computed |= {"totalPrice", "totalProductPrice"}
    elif entity == "fms/voucher":
        computed |= {"debitAmount", "creditAmount", "total"}
    elif entity == "product/spu":
        computed |= {"price", "marketPrice", "costPrice", "stock"}
    elif is_salary_special:
        computed |= {"regularSalary", "probationSalary", "changeType", "recordType"}
    return computed


def compile_yudao_write_catalog(
    *,
    docs: dict,
    source,
    ui,
    dedicated_creates: frozenset[str],
    dedicated_updates: frozenset[str],
    dedicated_statuses: frozenset[str],
    ui_metadata_fn,
    enable_cat5_master_detail_creates: bool = True,
) -> dict:
    """Compile all 4–5 write categories from Yudao OpenAPI + Java AST + Vue/TS form sources."""
    src_id = id(source) if source is not None else 0
    docs_id = id(docs)
    rel_key = str(getattr(source, "release", "")) if source is not None else ""
    cat5_flag = bool(enable_cat5_master_detail_creates)
    cache_key = (docs_id, src_id, rel_key, cat5_flag)
    with _COMPILED_ALL_LOCK:
        cached = _COMPILED_ALL_CACHE.get(cache_key)
        if cached is not None:
            _COMPILED_ALL_CACHE.move_to_end(cache_key)
            return cached
        return _compile_yudao_write_catalog_locked(
            docs=docs,
            source=source,
            ui=ui,
            dedicated_creates=dedicated_creates,
            dedicated_updates=dedicated_updates,
            dedicated_statuses=dedicated_statuses,
            ui_metadata_fn=ui_metadata_fn,
            cache_key=cache_key,
            enable_cat5_master_detail_creates=cat5_flag,
        )


def _compile_yudao_write_catalog_locked(
    *,
    docs: dict,
    source,
    ui,
    dedicated_creates: frozenset[str],
    dedicated_updates: frozenset[str],
    dedicated_statuses: frozenset[str],
    ui_metadata_fn,
    cache_key: tuple[int, int, str, bool],
    enable_cat5_master_detail_creates: bool = True,
) -> dict:
    paths = docs.get("paths") or {}
    schemas = (docs.get("components") or {}).get("schemas") or {}
    available_paths = set(paths.keys())

    vo_texts: list[tuple[str, str]] = []
    do_texts: list[tuple[str, str]] = []
    ctrl_texts: list[tuple[str, str]] = []
    svc_entries: list[tuple[str, str]] = []
    enum_labels: dict[str, dict[str, str]] = {}
    refusals: dict = {}
    if source is not None and getattr(source, "root", None):
        src_lock = getattr(source, "_instance_lock", None) or nullcontext()
        with src_lock:
            vo_texts = [
                (rel, source._read(rel))
                for path in source._java_files("VO.java")
                for rel in (source._relative(path),)
            ]
            do_texts = [
                (rel, source._read(rel))
                for path in source._java_files("DO.java")
                for rel in (source._relative(path),)
            ]
            ctrl_texts = [
                (rel, source._read(rel))
                for path in source._java_files("Controller.java")
                for rel in (source._relative(path),)
                if "/controller/app/" not in rel.replace("\\", "/")
            ]
            svc_entries = [
                (path.stem, source._read(source._relative(path)))
                for path in source._java_files("ServiceImpl.java")
            ]
            if hasattr(source, "enum_value_labels"):
                enum_labels = source.enum_value_labels()
            if hasattr(source, "_server_rules"):
                refusals = source._server_rules()[1]
            elif hasattr(source, "_refusals") and hasattr(source, "_messages"):
                refusals = source._refusals(source._messages())
    elif source is not None:
        if hasattr(source, "enum_value_labels"):
            enum_labels = source.enum_value_labels()
        if hasattr(source, "_server_rules"):
            refusals = source._server_rules()[1]
        elif hasattr(source, "_refusals") and hasattr(source, "_messages"):
            refusals = source._refusals(source._messages())

    ui_labels, ui_dicts, ui_scales = ui_metadata_fn(source, ui)
    vo_map, ctrl_endpoints = _index_java_tree(source, vo_do_texts=[*vo_texts, *do_texts], ctrl_texts=ctrl_texts)
    do_fields = _collect_do_fields(source, do_texts=do_texts)
    svc_by_iface = {
        stem[:-len("Impl")]: txt for stem, txt in svc_entries if stem.endswith("Impl")
    }
    parsed_svc_cache: dict[str, tuple[str, dict[str, str], dict[str, list[tuple[int, int, int]]]]] = {}
    known_java_enums = frozenset(k for k, v in enum_labels.items() if v)
    form_defaults, form_child_defaults = _compile_vue_create_defaults(source, ui)

    creates: dict[str, CompiledCreateSpec] = {}
    excluded_create_reasons: dict[str, str] = {}
    updates: dict[str, ConventionUpdateSpec] = {}
    aggregates: dict[str, CompiledAggregateUpdateSpec] = {}
    statuses: dict[str, TwoStateStatusSpec] = {}
    relations: dict[str, tuple[CompiledRelationSpec, ...]] = {}
    immutable_fields: dict[str, frozenset[str]] = {}
    nonnull_refusals: dict[str, tuple[tuple[str, str], ...]] = {}

    def _blocked(entity: str) -> bool:
        return any(entity == p or entity.startswith(p) for p in _BLOCKED_ENTITY_PREFIXES)

    # ------------------------------------------------------------------
    # Category 4a: Reversible two-state status toggles (dedicated + query/body/split/list-read)
    # ------------------------------------------------------------------
    common_synonyms = (
        ("启用", "开启"),
        ("正常", "开启"),
        ("停用", "关闭"),
        ("禁用", "关闭"),
        ("有效", "开启"),
        ("无效", "关闭"),
    )
    bool_synonyms = (
        ("开启", "开启"), ("启用", "开启"), ("打开", "开启"), ("设为默认", "开启"), ("显示", "开启"), ("置顶", "开启"), ("锁定", "开启"), ("冻结", "开启"), ("是", "开启"), ("true", "开启"),
        ("关闭", "关闭"), ("停用", "关闭"), ("取消", "关闭"), ("取消默认", "关闭"), ("隐藏", "关闭"), ("取消置顶", "关闭"), ("解锁", "关闭"), ("解冻", "关闭"), ("否", "关闭"), ("false", "关闭"),
    )
    for p, item in sorted(paths.items()):
        if not p.startswith("/admin-api/") or not isinstance(item, dict):
            continue
        ent_part = p[len("/admin-api/"):]
        if "/" not in ent_part:
            continue
        entity, tail = ent_part.rsplit("/", 1)
        if _blocked(entity) or entity in dedicated_statuses:
            continue
        put = item.get("put")
        if not isinstance(put, dict):
            continue
        try:
            resp_ref = next(iter(put["responses"]["200"]["content"].values()))["schema"].get("$ref", "")
        except (KeyError, StopIteration, TypeError, AttributeError):
            resp_ref = ""
        if not resp_ref.endswith("/CommonResultBoolean"):
            continue
        has_get = f"/admin-api/{entity}/get" in paths or f"/admin-api/{entity}/get-detail" in paths
        has_list = f"/admin-api/{entity}/list" in paths
        if not (has_get or has_list):
            continue
        read_path = "product/spu/get-detail" if entity == "product/spu" and f"/admin-api/{entity}/get-detail" in paths else (f"{entity}/get" if f"/admin-api/{entity}/get" in paths else "")
        read_list_path = f"{entity}/list" if not read_path and has_list else ""
        tag = ((put.get("tags") or [""])[0]).replace("管理后台 - ", "").strip() or entity
        resp_op = (paths.get(f"/admin-api/{read_path or read_list_path}") or {}).get("get") or {}
        resp_props = _resp_schema_props(resp_op, schemas)
        name_fields = tuple(k for k in _NAME_CANDIDATES if k in resp_props) or ("name",)
        qparams = [q.get("name") for q in (put.get("parameters") or ()) if isinstance(q, dict) and isinstance(q.get("name"), str)]
        bprops: dict = {}
        if "requestBody" in put:
            try:
                bschema = _deref(next(iter(put["requestBody"]["content"].values()))["schema"], schemas)
                bprops = bschema.get("properties") or {}
            except (KeyError, StopIteration, TypeError, AttributeError):
                bprops = {}

        if tail == "update-status":
            if "ids" in bprops or "statusId" in bprops or "processStatus" in qparams or "stopReason" in bprops:
                continue
            if "accountSetId" in bprops and entity != "fms/config/auxiliary-item":
                continue
            param_mode = "query" if ("status" in qparams and not bprops) else "body"
            if entity == "product/spu":
                statuses[entity] = TwoStateStatusSpec(
                    entity=entity,
                    label=tag,
                    states=(("上架", 1), ("下架", 0)),
                    name_fields=name_fields,
                    volatile_fields=_VOLATILE,
                    synonyms=(("开启", "上架"), ("启用", "上架"), ("关闭", "下架"), ("停用", "下架")),
                    action_path=ent_part,
                    param_mode="body",
                    read_path=read_path,
                )
            elif ("id" in bprops and "status" in bprops) or ("id" in qparams and "status" in qparams):
                # Require verified two-state CommonStatusEnum (exclude ErpAuditStatus & multi-stage workflow enums)
                ep_st = ctrl_endpoints.get(f"Put:{ent_part}")
                req_vo_st = vo_map.get(ep_st.request_vo) if ep_st and ep_st.request_vo else None
                status_enums = [f.enum_class for f in req_vo_st.fields if f.name == "status" and f.enum_class] if req_vo_st else []
                verified_common = "CommonStatusEnum" in status_enums
                if not verified_common and source is not None and not status_enums:
                    svc_cand_stem = None
                    if ep_st and ep_st.service_iface:
                        svc_cand_stem = ep_st.service_iface + "Impl"
                    ent_slug = re.sub(r"[^a-z0-9]", "", entity.lower())
                    for sf_stem, stxt in svc_entries:
                        stem_l = re.sub(r"[^a-z0-9]", "", sf_stem.removesuffix("ServiceImpl").lower())
                        if (svc_cand_stem and sf_stem == svc_cand_stem) or (ent_slug and stem_l.endswith(ent_slug)):
                            if "CommonStatusEnum" in stxt and not re.search(
                                r"(?:AuditStatus|CandidateStatus|PostStatus|BusinessEndStatus)\w*Enum", stxt
                            ):
                                verified_common = True
                                break
                if not verified_common and source is not None:
                    continue
                statuses[entity] = TwoStateStatusSpec(
                    entity=entity,
                    label=tag,
                    states=(("开启", 0), ("关闭", 1)),
                    name_fields=name_fields,
                    volatile_fields=_VOLATILE,
                    synonyms=common_synonyms,
                    action_path=ent_part,
                    param_mode=param_mode,
                    read_path=read_path,
                    read_list_path=read_list_path,
                )
        elif tail == "update-default-status" and "id" in qparams and "defaultStatus" in qparams:
            statuses[entity] = TwoStateStatusSpec(
                entity=entity,
                label=tag,
                states=(("开启", True), ("关闭", False)),
                status_field="defaultStatus",
                name_fields=name_fields,
                volatile_fields=_VOLATILE,
                synonyms=bool_synonyms,
                action_path=ent_part,
                param_mode="query",
                status_param="defaultStatus",
                read_path=read_path,
                read_list_path=read_list_path,
            )
        elif tail in {"update-enabled", "update-visible", "update-frozen", "update-favorite", "update-conversation-pinned", "update-brokerage-enable", "mute-all"}:
            if entity in statuses:
                continue
            bool_key = next(
                (k for k, v in bprops.items() if k != "id" and _deref(v, schemas).get("type") == "boolean"),
                None,
            )
            if "id" in bprops and len(bprops) == 2 and bool_key:
                status_field = "brokerageEnabled" if tail == "update-brokerage-enable" and "brokerageEnabled" in resp_props else bool_key
                extra_vol = frozenset({"brokerageTime"}) if tail == "update-brokerage-enable" else frozenset()
                statuses[entity] = TwoStateStatusSpec(
                    entity=entity,
                    label=tag,
                    states=(("开启", True), ("关闭", False)),
                    status_field=status_field,
                    name_fields=name_fields,
                    volatile_fields=_VOLATILE | extra_vol,
                    synonyms=bool_synonyms,
                    action_path=ent_part,
                    param_mode="body",
                    status_param=bool_key,
                    read_path=read_path,
                    read_list_path=read_list_path,
                )
        elif tail == "update-state" and "id" in bprops and "state" in bprops and len(bprops) == 2:
            statuses[entity] = TwoStateStatusSpec(
                entity=entity,
                label=tag,
                states=(("开启", 1), ("关闭", 2)),
                status_field="deploymentState" if "deploymentState" in resp_props else "state",
                name_fields=name_fields,
                volatile_fields=_VOLATILE,
                synonyms=common_synonyms,
                action_path=ent_part,
                param_mode="body",
                status_param="state",
                read_path=read_path,
                read_list_path=read_list_path,
            )
        elif tail == "enable" and f"/admin-api/{entity}/disable" in paths and "id" in qparams:
            statuses[entity] = TwoStateStatusSpec(
                entity=entity,
                label=tag,
                states=(("开启", 1), ("关闭", 0)),
                status_field="status",
                name_fields=name_fields,
                volatile_fields=_VOLATILE,
                synonyms=common_synonyms,
                action_path=ent_part,
                second_action_path=f"{entity}/disable",
                param_mode="query",
                read_path=read_path,
                read_list_path=read_list_path,
            )

    # ------------------------------------------------------------------
    # Category 4b: Idempotent relation set / single-FK assignment specs
    # ------------------------------------------------------------------
    rel_registry = [
        CompiledRelationSpec(
            entity="system/user", label="用户", relation="roleIds", relation_label="分配角色",
            target_entity="system/role", action_path="system/permission/assign-user-role",
            read_path="system/permission/list-user-roles", target_param="userId", items_param="roleIds",
            param_mode="body", http_method="POST", aliases=("role", "roles", "角色"),
        ),
        CompiledRelationSpec(
            entity="system/role", label="角色", relation="menuIds", relation_label="分配菜单权限",
            target_entity="system/menu", action_path="system/permission/assign-role-menu",
            read_path="system/permission/list-role-menus", target_param="roleId", items_param="menuIds",
            param_mode="body", http_method="POST", aliases=("menu", "menus", "菜单", "权限"),
        ),
        CompiledRelationSpec(
            entity="trade/delivery/pick-up-store", label="自提门店", relation="verifyUserIds", relation_label="核销员",
            target_entity="system/user", action_path="trade/delivery/pick-up-store/bind",
            read_path="trade/delivery/pick-up-store/get", target_param="id", items_param="verifyUserIds",
            param_mode="body", http_method="POST", read_field="verifyUserIds", target_id_param="id",
            aliases=("verifyUsers", "核销员", "绑定核销员"),
        ),
        CompiledRelationSpec(
            entity="oa/note", label="记事本笔记", relation="receiverUserIds", relation_label="共享成员",
            target_entity="system/user", action_path="oa/note/update-share",
            read_path="oa/note/get", target_param="id", items_param="receiverUserIds",
            param_mode="body", http_method="PUT", read_field="receiverUserIds", target_id_param="id",
            name_fields=("title", "name"), aliases=("share", "共享", "接收人"),
        ),
        CompiledRelationSpec(
            entity="hrm/recruit/candidate", label="招聘候选人", relation="postId", relation_label="应聘职位",
            target_entity="hrm/recruit/post", action_path="hrm/recruit/candidate/update-post",
            read_path="hrm/recruit/candidate/get", target_param="id", items_param="postId",
            param_mode="body", http_method="PUT", single_value=True, read_field="postId", target_id_param="id",
            aliases=("post", "职位", "应聘职位"),
        ),
        CompiledRelationSpec(
            entity="hrm/recruit/candidate", label="招聘候选人", relation="channelId", relation_label="招聘渠道",
            target_entity="hrm/recruit/channel", action_path="hrm/recruit/candidate/update-channel",
            read_path="hrm/recruit/candidate/get", target_param="id", items_param="channelId",
            param_mode="body", http_method="PUT", single_value=True, read_field="channelId", target_id_param="id",
            aliases=("channel", "渠道", "招聘渠道"),
        ),
        CompiledRelationSpec(
            entity="hrm/insurance/employee-info", label="员工社保信息", relation="schemeId", relation_label="参保方案",
            target_entity="hrm/insurance/scheme", action_path="hrm/insurance/employee-info/update-scheme",
            read_path="hrm/insurance/employee-info/get", target_param="employeeId", items_param="schemeId",
            param_mode="body", http_method="PUT", single_value=True, id_field="employeeId",
            read_field="schemeId", target_id_param="employeeId", name_fields=("employeeName", "name"),
            aliases=("scheme", "方案", "参保方案"),
        ),
        CompiledRelationSpec(
            entity="member/user", label="会员用户", relation="levelId", relation_label="会员等级",
            target_entity="member/level", action_path="member/user/update-level",
            read_path="member/user/get", target_param="id", items_param="levelId",
            param_mode="body", http_method="PUT", single_value=True, read_field="levelId", target_id_param="id",
            name_fields=("nickname", "name"), aliases=("level", "等级", "会员等级"),
        ),
        CompiledRelationSpec(
            entity="oa/file-node", label="云盘节点", relation="parentId", relation_label="父级目录",
            target_entity="oa/file-node", action_path="oa/file-node/update-parent",
            read_path="oa/file-node/get", target_param="id", items_param="parentId",
            param_mode="body", http_method="PUT", single_value=True, read_field="parentId", target_id_param="id",
            aliases=("parent", "目录", "文件夹"),
        ),
        CompiledRelationSpec(
            entity="pms/pm/work-item", label="项目工作项", relation="iterationId", relation_label="所属迭代",
            target_entity="pms/pm/iteration", action_path="pms/pm/work-item/update-iteration",
            read_path="pms/pm/work-item/get", target_param="id", items_param="iterationId",
            param_mode="body", http_method="PUT", single_value=True, read_field="iterationId", target_id_param="id",
            aliases=("iteration", "迭代"),
        ),
        CompiledRelationSpec(
            entity="pms/kb/document-share", label="知识库文档", relation="shareUserIds", relation_label="共享成员",
            target_entity="system/user", action_path="pms/kb/document-share/update-member-list",
            read_path="pms/kb/document-share/get", target_param="documentId", items_param="shareUserIds",
            param_mode="body", http_method="PUT", id_field="documentId", read_field="shareUserIds",
            target_id_param="documentId", name_fields=("title", "name"), aliases=("members", "共享成员"),
        ),
        CompiledRelationSpec(
            entity="trade/brokerage-user", label="分销用户", relation="bindUserId", relation_label="推广人",
            target_entity="member/user", action_path="trade/brokerage-user/update-bind-user",
            read_path="trade/brokerage-user/get", target_param="id", items_param="bindUserId",
            param_mode="body", http_method="PUT", single_value=True, read_field="bindUserId", target_id_param="id",
            name_fields=("nickname", "name"), target_name_fields=("nickname", "name"),
            aliases=("bindUser", "推广人", "上级推广人"),
        ),
        CompiledRelationSpec(
            entity="pms/pm/work-item", label="项目工作项", relation="statusId", relation_label="工作项状态",
            target_entity="pms/pm/work-item-status", action_path="pms/pm/work-item/update-status",
            read_path="pms/pm/work-item/get", target_param="id", items_param="statusId",
            param_mode="body", http_method="PUT", single_value=True, read_field="statusId", target_id_param="id",
            aliases=("workItemStatus", "工作项状态", "状态"),
        ),
        CompiledRelationSpec(
            entity="im/group", label="IM 群组", relation="newOwnerUserId", relation_label="转让群主",
            target_entity="system/user", action_path="im/group/transfer-owner",
            read_path="im/group/get", target_param="id", items_param="newOwnerUserId",
            param_mode="body", http_method="PUT", single_value=True, read_field="ownerUserId", target_id_param="id",
            target_name_fields=("nickname", "username"), aliases=("owner", "群主", "转让群主"),
        ),
    ]
    for rspec in rel_registry:
        ep_url = f"/admin-api/{rspec.action_path}"
        op_item = paths.get(ep_url)
        if not isinstance(op_item, dict):
            continue
        op_def = op_item.get(rspec.http_method.lower())
        if not isinstance(op_def, dict):
            continue
        extra_req = set()
        if "requestBody" in op_def:
            try:
                rb_raw = next(iter(op_def["requestBody"]["content"].values()))["schema"]
                rb = _deref(rb_raw, schemas)
                extra_req = {k for k in (rb.get("required") or ()) if k not in {rspec.target_param, rspec.items_param}}
            except (KeyError, StopIteration, TypeError, AttributeError):
                pass
        if extra_req:
            continue
        relations[rspec.entity] = (*relations.get(rspec.entity, ()), rspec)

    # ------------------------------------------------------------------
    # Categories 2 & 3: Relaxed Flat Updates + 1-2 Level Aggregate Updates
    # ------------------------------------------------------------------
    nonnull_pat = re.compile(r"^(?:Objects\.nonNull\(\s*\w+\.get([A-Z]\w*)\(\)\s*\)|\w+\.get([A-Z]\w*)\(\)\s*!=\s*null)$")

    for p, item in sorted(paths.items()):
        if not p.startswith("/admin-api/") or not p.endswith("/update") or not isinstance(item, dict):
            continue
        put = item.get("put")
        if not isinstance(put, dict) or "requestBody" not in put:
            continue
        entity = p[len("/admin-api/"):-len("/update")]
        if _blocked(entity) or entity in dedicated_updates:
            continue
        get_path = "product/spu/get-detail" if entity == "product/spu" and f"/admin-api/{entity}/get-detail" in paths else f"{entity}/get"
        get_op = (paths.get(f"/admin-api/{get_path}") or {}).get("get")
        if not isinstance(get_op, dict):
            continue
        try:
            put_resp_ref = next(iter(put["responses"]["200"]["content"].values()))["schema"].get("$ref", "")
            req_raw = next(iter(put["requestBody"]["content"].values()))["schema"]
        except (KeyError, StopIteration, TypeError, AttributeError):
            continue

        is_salary_special = entity == "hrm/salary/employee-info"
        if not is_salary_special and not put_resp_ref.endswith("/CommonResultBoolean"):
            continue
        if is_salary_special and not put_resp_ref.endswith("/CommonResultLong"):
            continue

        ep_ast = ctrl_endpoints.get(f"Put:{p[len('/admin-api/'):]}")
        java_vo = vo_map.get(ep_ast.request_vo) if ep_ast and ep_ast.request_vo else None
        req_schema = _deref(req_raw, schemas)
        req_props = req_schema.get("properties") or {}
        resp_props = _resp_schema_props(get_op, schemas)
        req_required = set(req_schema.get("required") or ())

        # Detect nested object-array child collections vs opaque config objects
        has_opaque_object, nested_cols = _classify_nested_columns(req_props, schemas)
        if has_opaque_object:
            continue

        is_aggregate = bool(nested_cols)
        if not is_aggregate and not put_resp_ref.endswith("/CommonResultBoolean"):
            continue
        if not is_aggregate and ("id" not in req_props or "id" not in resp_props):
            continue

        dropped_fields: set[str] = set()
        if ep_ast is not None:
            safe_svc, uncond_nulls, dropped_fields = _analyze_service_method(
                source,
                ep_ast,
                allow_child_mappers=is_aggregate,
                do_fields=do_fields,
                vo_field_names=tuple(req_props.keys()),
                svc_by_iface=svc_by_iface,
                parsed_svc_cache=parsed_svc_cache,
            )
            if not safe_svc:
                continue
            if uncond_nulls or dropped_fields:
                immutable_fields[entity] = frozenset(uncond_nulls | dropped_fields)
            guards = []
            for ref in refusals.get((ep_ast.service_iface, ep_ast.service_method), ()):
                cond = (getattr(ref, "condition", "") or "").strip()
                m_nn = nonnull_pat.fullmatch(cond)
                if m_nn:
                    g_prop = m_nn.group(1) or m_nn.group(2)
                    guards.append((g_prop[0].lower() + g_prop[1:], getattr(ref, "message", "") or cond))
            if guards:
                nonnull_refusals[entity] = tuple(guards)

        ent_labels = ui_labels.get(entity, {})
        ent_dicts = ui_dicts.get(entity, {})
        ent_scales = ui_scales.get(entity, {})
        java_fields = {f.name: f for f in java_vo.fields} if java_vo else {}
        tag = ((put.get("tags") or [""])[0]).replace("管理后台 - ", "").strip() or entity
        name_fields = tuple(k for k in _NAME_CANDIDATES if k in resp_props) or ("name",)

        if not is_aggregate:
            field_specs: list[ConventionFieldSpec] = []
            disqualified = False
            for fname, raw_fp in req_props.items():
                if fname == "id" or fname in dropped_fields:
                    continue
                jf = java_fields.get(fname)
                if _SECRET.search(fname):
                    if (jf and jf.required) or fname in req_required:
                        if not (java_vo and java_vo.password_optional_present):
                            disqualified = True
                            break
                    continue
                if fname not in resp_props:
                    disqualified = True
                    break
                f_spec = _build_field_spec(
                    entity, fname, raw_fp, schemas=schemas, available_paths=available_paths,
                    java_field=jf, req_required=req_required, ent_labels=ent_labels,
                    ent_dicts=ent_dicts, ent_scales=ent_scales, has_java_vo=bool(java_vo),
                    status_in_status_specs=(entity in statuses),
                    known_java_enums=known_java_enums,
                )
                if f_spec is None:
                    disqualified = True
                    break
                field_specs.append(f_spec)
            if not disqualified and field_specs:
                extra_vol = frozenset({"minStock", "maxStock"}) if entity == "mes/md/item" else frozenset()
                updates[entity] = ConventionUpdateSpec(
                    entity=entity, label=tag, fields=tuple(field_specs),
                    id_field="id", name_fields=name_fields, volatile_fields=_VOLATILE | extra_vol,
                )
        else:
            # Category 3: 1-2 level Aggregate Root update spec
            get_ep = ctrl_endpoints.get(f"Get:{get_path}")
            resp_java_vo = vo_map.get(get_ep.response_type) if get_ep and get_ep.response_type else None
            resp_java_fields = {f.name: f for f in resp_java_vo.fields} if resp_java_vo else {}
            child_specs: list[CompiledChildCollectionSpec] = []
            parent_specs: list[ConventionFieldSpec] = []
            disqualified = False
            col_names = {c for c, _ in nested_cols}
            for fname, raw_fp in req_props.items():
                if fname == "id":
                    continue
                if fname in col_names:
                    jf = java_fields.get(fname)
                    rjf = resp_java_fields.get(fname)
                    col_spec = _compile_child_collection(
                        entity, fname, raw_fp, schemas=schemas, available_paths=available_paths,
                        vo_map=vo_map, java_list_elem_vo=(jf.list_element_class if jf else ""),
                        resp_list_elem_vo=(rjf.list_element_class if rjf else ""),
                        resp_arr_prop=resp_props.get(fname) or {},
                        ent_labels=ent_labels, ent_dicts=ent_dicts, ent_scales=ent_scales, depth=1,
                        known_java_enums=known_java_enums,
                    )
                    if col_spec is None:
                        disqualified = True
                        break
                    child_specs.append(col_spec)
                    continue
                if fname in dropped_fields:
                    continue
                if fname not in resp_props and not (is_salary_special and fname == "recordType"):
                    disqualified = True
                    break
                f_spec = _build_field_spec(
                    entity, fname, raw_fp, schemas=schemas, available_paths=available_paths,
                    java_field=java_fields.get(fname), req_required=req_required, ent_labels=ent_labels,
                    ent_dicts=ent_dicts, ent_scales=ent_scales, has_java_vo=bool(java_vo),
                    status_in_status_specs=False,
                    known_java_enums=known_java_enums,
                )
                if f_spec is None:
                    disqualified = True
                    break
                parent_specs.append(f_spec)

            if not disqualified and child_specs:
                get_qparams = tuple(
                    q.get("name") for q in (get_op.get("parameters") or ())
                    if isinstance(q, dict) and q.get("required") is True and q.get("name")
                )
                comp_ids = tuple(k for k in get_qparams if k != "id") + (("id",) if "id" in get_qparams else ())
                if not comp_ids:
                    comp_ids = ("id",)
                id_field = "employeeId" if is_salary_special else "id"
                computed_parent = _agg_server_computed_fields(entity, is_salary_special=is_salary_special)
                aggregates[entity] = CompiledAggregateUpdateSpec(
                    entity=entity,
                    label=tag,
                    fields=tuple(parent_specs),
                    collections=tuple(child_specs),
                    id_field=id_field,
                    name_fields=name_fields,
                    volatile_fields=_VOLATILE | {"status", "no"} | ({"recordType", "changeReason", "effectTime"} if is_salary_special else frozenset()),
                    server_computed_fields=frozenset(computed_parent),
                    omit_root_id_in_payload=is_salary_special,
                    composite_id_fields=comp_ids if len(comp_ids) > 1 else (),
                    readback_strategy="change_record_id" if is_salary_special else "same_record",
                )

    # ------------------------------------------------------------------
    # Categories 1 & 5: Flat POST .../create + 1–2 Level Master-Detail POST .../create
    # ------------------------------------------------------------------
    for p, item in sorted(paths.items()):
        if not p.startswith("/admin-api/") or not p.endswith("/create") or not isinstance(item, dict):
            continue
        post = item.get("post")
        if not isinstance(post, dict) or "requestBody" not in post:
            continue
        entity = p[len("/admin-api/"):-len("/create")]
        if _blocked(entity) or entity in dedicated_creates:
            continue
        get_path = (
            "product/spu/get-detail"
            if entity == "product/spu" and f"/admin-api/{entity}/get-detail" in paths
            else (
                f"{entity}/get"
                if f"/admin-api/{entity}/get" in paths
                else (f"{entity}/get-detail" if f"/admin-api/{entity}/get-detail" in paths else "")
            )
        )
        get_op = (paths.get(f"/admin-api/{get_path}") or {}).get("get") if get_path else None
        if not isinstance(get_op, dict):
            continue
        try:
            resp_ref = next(iter(post["responses"]["200"]["content"].values()))["schema"].get("$ref", "")
            req_raw = next(iter(post["requestBody"]["content"].values()))["schema"]
        except (KeyError, StopIteration, TypeError, AttributeError):
            continue
        if not resp_ref.endswith("/CommonResultLong"):
            continue
        ep_ast = ctrl_endpoints.get(f"Post:{p[len('/admin-api/'):]}")
        java_vo = vo_map.get(ep_ast.request_vo) if ep_ast and ep_ast.request_vo else None
        if java_vo and java_vo.password_create_required:
            excluded_create_reasons[entity] = "新建接口包含必填密码校验字段（@AssertTrue），禁止由助手代填密码"
            continue
        req_schema = _deref(req_raw, schemas)
        req_props = req_schema.get("properties") or {}
        has_opaque_object, nested_cols = _classify_nested_columns(req_props, schemas)
        if has_opaque_object:
            continue
        if nested_cols and not enable_cat5_master_detail_creates:
            continue
        create_dropped: set[str] = set()
        if ep_ast is not None:
            safe_svc, _, create_dropped = _analyze_service_method(
                source,
                ep_ast,
                allow_child_mappers=bool(nested_cols and enable_cat5_master_detail_creates),
                do_fields=do_fields,
                vo_field_names=tuple(req_props.keys()),
                svc_by_iface=svc_by_iface,
                parsed_svc_cache=parsed_svc_cache,
            )
            if not safe_svc:
                continue

        resp_props = _resp_schema_props(get_op, schemas)
        req_required = set(req_schema.get("required") or ())
        java_fields = {f.name: f for f in java_vo.fields} if java_vo else {}
        get_ep = ctrl_endpoints.get(f"Get:{get_path}") if nested_cols else None
        resp_java_vo = vo_map.get(get_ep.response_type) if get_ep and get_ep.response_type else None
        resp_java_fields = {f.name: f for f in resp_java_vo.fields} if resp_java_vo else {}
        ent_labels = ui_labels.get(entity, {})
        ent_dicts = ui_dicts.get(entity, {})
        ent_scales = ui_scales.get(entity, {})
        col_names = {c for c, _ in nested_cols}
        svc_req_cols = _service_required_child_cols(ep_ast, col_names, parsed_svc_cache)
        raw_child_defaults = form_child_defaults.get(entity, ())

        field_specs: list[ConventionFieldSpec] = []
        child_specs: list[CompiledChildCollectionSpec] = []
        disqualified = False
        req_pwd = False
        for fname, raw_fp in req_props.items():
            if fname == "id":
                continue
            jf = java_fields.get(fname)
            if fname in col_names:
                rjf = resp_java_fields.get(fname)
                col_spec = _compile_child_collection(
                    entity,
                    fname,
                    raw_fp,
                    schemas=schemas,
                    available_paths=available_paths,
                    vo_map=vo_map,
                    java_list_elem_vo=(jf.list_element_class if jf else ""),
                    resp_list_elem_vo=(rjf.list_element_class if rjf else ""),
                    resp_arr_prop=resp_props.get(fname) or {},
                    ent_labels=ent_labels,
                    ent_dicts=ent_dicts,
                    ent_scales=ent_scales,
                    depth=1,
                    known_java_enums=known_java_enums,
                )
                if col_spec is None:
                    disqualified = True
                    break
                col_field_map = {cf.name: cf for cf in col_spec.fields}
                filtered_cdefs = tuple(
                    cd
                    for cd in raw_child_defaults
                    if cd.field in col_field_map
                    and (
                        cd.kind == "actor"
                        or not is_rejected_ui_sentinel(
                            cd.value,
                            is_fk_or_enum=bool(
                                col_field_map[cd.field].reference_entity or col_field_map[cd.field].enum_type
                            ),
                            is_hierarchy_parent=(cd.field == "parentId"),
                        )
                    )
                )
                req_min = max(col_spec.min_items, 1 if (fname in req_required or (jf and jf.required) or fname in svc_req_cols) else 0)
                if filtered_cdefs or req_min != col_spec.min_items:
                    col_spec = replace(col_spec, defaults=filtered_cdefs, min_items=req_min)
                child_specs.append(col_spec)
                continue
            if fname in create_dropped:
                continue
            if _SECRET.search(fname):
                if (jf and jf.required) or fname in req_required:
                    req_pwd = True
                    break
                continue
            f_spec = _build_field_spec(
                entity, fname, raw_fp, schemas=schemas, available_paths=available_paths,
                java_field=jf, req_required=req_required, ent_labels=ent_labels,
                ent_dicts=ent_dicts, ent_scales=ent_scales, has_java_vo=bool(java_vo),
                status_in_status_specs=False,
                known_java_enums=known_java_enums,
            )
            if f_spec is None:
                disqualified = True
                break
            field_specs.append(f_spec)
        if req_pwd:
            excluded_create_reasons[entity] = "新建接口包含必填密码或密钥字段，禁止通过通用 API 代填"
            continue
        if disqualified or (not field_specs and not child_specs):
            continue
        if nested_cols and not child_specs:
            continue

        raw_defaults = form_defaults.get(entity, ())
        spec_by_name = {f.name: f for f in field_specs}
        filtered_defaults = tuple(
            d for d in raw_defaults
            if d.field in spec_by_name and (
                d.kind == "actor" or not is_rejected_ui_sentinel(
                    d.value,
                    is_fk_or_enum=bool(spec_by_name[d.field].reference_entity or spec_by_name[d.field].enum_type),
                    is_hierarchy_parent=(d.field == "parentId"),
                )
            )
        )
        computed = frozenset(k for k in req_props if k != "id" and k not in col_names and k not in resp_props)
        if child_specs:
            computed = computed | _agg_server_computed_fields(entity)
        if entity == "crm/receivable":
            computed = computed | frozenset({"customerId"})
        elif entity == "hrm/employee":
            computed = computed | frozenset({"age", "regularTime", "status", "companyAge", "companyAgeStartTime"})
        elif entity == "mes/dv/check-plan":
            computed = computed | frozenset({"status"})
        elif entity == "mes/wm/stocktaking-plan":
            computed = computed | frozenset({"status", "startTime", "endTime"})
        elif entity == "ai/knowledge":
            computed = computed | frozenset({"embeddingModel"})
        elif entity == "hrm/insurance/scheme":
            computed = computed | frozenset({"areaId"})
        elif entity == "product/spu":
            computed = computed | frozenset({"status", "salesCount", "browseCount"})
        tag = ((post.get("tags") or [""])[0]).replace("管理后台 - ", "").strip() or entity
        name_fields = tuple(k for k in _NAME_CANDIDATES if k in resp_props) or ("name",)
        cond_req = _hrm_employee_conditional_required if entity == "hrm/employee" else None
        creates[entity] = CompiledCreateSpec(
            entity=entity,
            label=tag,
            fields=tuple(field_specs),
            defaults=filtered_defaults,
            collections=tuple(child_specs),
            id_field="id",
            name_fields=name_fields,
            server_computed_fields=computed,
            conditional_required_fn=cond_req,
        )

    out = {
        "creates": creates,
        "excluded_create_reasons": excluded_create_reasons,
        "updates": updates,
        "aggregates": aggregates,
        "statuses": statuses,
        "relations": relations,
        "immutable_fields": immutable_fields,
        "nonnull_refusals": nonnull_refusals,
    }
    with _COMPILED_ALL_LOCK:
        _COMPILED_ALL_CACHE[cache_key] = out
        while len(_COMPILED_ALL_CACHE) > 8:
            _COMPILED_ALL_CACHE.popitem(last=False)
    return out


def _hrm_employee_conditional_required(payload: dict) -> tuple[tuple[str, str], ...]:
    # Mirrors EmployeeForm.vue: when entryStatus == 1 (在职), jobNumber is required.
    if payload.get("entryStatus") == 1 and not payload.get("jobNumber"):
        return (("jobNumber", "在职员工工号（jobNumber）"),)
    return ()


__all__ = ["compile_yudao_write_catalog"]
