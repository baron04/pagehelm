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
    CompanionWriteSpec,
    ConventionFieldSpec,
    ConventionUpdateSpec,
    TwoStateStatusSpec,
    ValidationRuleSpec,
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
    "system/role",
    "system/menu",
    "system/permission",
    "system/tenant",
    "system/oauth2",
    "system/social-",
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
    r"(?:^Pay\w*(?:Service|Api)|Wallet\w*(?:Service|Api)|Sms\w*(?:Service|Api)|Mail\w*(?:Service|Api)|Notify\w*(?:Service|Api)|BpmProcessInstance\w*(?:Service|Api)|MQ|Producer|Flowable|ProcessEngine|RuntimeService|(?:^|Bpm)TaskService$)",
    re.I,
)
_AUTH_CONFIG_ENTRY_SERVICE_RE = re.compile(
    r"^(?:Role|Menu|Permission|Tenant\w*|OAuth2\w*|SocialClient\w*|.*ContentPermission)(?:Service|Api)$"
)
_AUTH_CONFIG_CALL_SERVICE_RE = re.compile(
    r"^(?:Role|Menu|Permission|Tenant\w*|OAuth2Client\w*|SocialClient\w*|.*ContentPermission)(?:Service|Api)$"
)

# Java VO / Controller AST regexes
_PACKAGE_RE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.M)
_IMPORT_RE = re.compile(r"^\s*import\s+(?:static\s+)?([\w.*]+)\s*;", re.M)
_CLASS_DECL_RE = re.compile(r"\bclass\s+([A-Z]\w*)\b(?:\s+extends\s+([A-Za-z_][\w.$]*))?[^{]*\{")
_SCHEMA_DESC_RE = re.compile(r'@Schema\([^)]*description\s*=\s*"([^"]+)"')
_SVC_OR_API_FIELD_RE = re.compile(
    r"private\s+(?:final\s+)?(?:[\w]+\.)*(\w+(?:Service|Api|Producer|Client|Template|Mapper))(?:<[^;{}()]*>)?\s+(\w+)\s*[=;]"
)
_SVC_OR_API_CALL_RE = re.compile(r"\b(\w+)\s*\.\s*(\w+)\s*\(")
_TOKEN_REVOKE_METHOD_RE = re.compile(r"^(?:remove|revoke|delete|clear|invalidate)\w*(?:Token|Session)\w*|^logout\b")
_TOKEN_REVOKE_TYPE_RE = re.compile(r"(?:Token|Session|OAuth2|Auth)\w*(?:Service|Api)$", re.I)
_CTRL_CLASS_MAPPING_RE = re.compile(r'@RequestMapping\(\s*(?:value\s*=\s*)?"([^"]+)"')
_RESPONSE_INNER_RE = re.compile(r"CommonResult<\s*([\w.$<>]+)\s*>")
_REQ_BODY_TYPE_RE = re.compile(r"@RequestBody\s+([A-Za-z_][\w.$]*)\s+(\w+)")
_REQ_PARAM_RE = re.compile(r'@RequestParam\(\s*(?:value\s*=\s*|name\s*=\s*)?"([^"]+)"')
_IN_ENUM_RE = re.compile(r"@InEnum\(\s*(?:value\s*=\s*)?([A-Za-z_]\w*)\.class")
_NOT_REQ_ANN_RE = re.compile(r"@(?:NotNull|NotBlank|NotEmpty)\b")
_LIST_ELEM_RE = re.compile(r"(?:List|Set|Collection)<\s*([A-Za-z_][\w.$]*)\s*>")
_ASSERT_TRUE_RE = re.compile(r"@AssertTrue\b")
_GET_URL_ARROW_RE = re.compile(
    r"\b(\w+)\s*[:=]\s*(?:async\s*)?\([^)]*\)\s*(?::[^=>{]+)?=>\s*\{?\s*"
    r"(?:return\s+)?(?:await\s+)?request\.get(?:<[^>]+>)?\(\{\s*url:\s*[`'\"]([^`'\"?$]+)"
)
_GET_URL_FN_RE = re.compile(
    r"(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\([^)]*\)\s*(?::[^{]+)?\{\s*"
    r"(?:return\s+)?(?:await\s+)?request\.get(?:<[^>]+>)?\(\{\s*url:\s*[`'\"]([^`'\"?$]+)"
)

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
    minimum: float | None = None
    maximum: float | None = None
    min_length: int | None = None
    max_length: int | None = None
    non_empty: bool = False


@dataclass(frozen=True, slots=True)
class JavaVOAST:
    full_name: str
    package: str
    imports: dict[str, str]
    fields: tuple[JavaFieldAST, ...]
    password_create_required: bool = False
    password_optional_present: bool = False
    assert_true_props: frozenset[str] = frozenset()
    validation_rules: tuple[ValidationRuleSpec, ...] = ()


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
    class_spans_out: dict[str, tuple[str, dict[str, str], tuple[str, ...], str, str, tuple[str, ...], str]],
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
            parent_raw = (m.group(2) or "").strip()
            new_chain = (*outer_chain, cname)
            full_key = f"{pkg}.{('$'.join(new_chain))}" if pkg else "$".join(new_chain)
            inner_masked = span_text[open_idx + 1:close_idx]
            inner_raw = raw_span[open_idx + 1:close_idx]
            # Recurse into child classes first and blank out child bodies to avoid double-counting child fields
            cleaned_chunks = []
            raw_chunks = []
            cpos = 0
            for cm in _CLASS_DECL_RE.finditer(inner_masked):
                c_open = cm.end() - 1
                c_close = _balanced_brace(inner_masked, c_open)
                if c_close is not None and cm.start() >= cpos:
                    cleaned_chunks.append(inner_masked[cpos:cm.start()])
                    raw_chunks.append(inner_raw[cpos:cm.start()])
                    cpos = c_close + 1
            cleaned_chunks.append(inner_masked[cpos:])
            raw_chunks.append(inner_raw[cpos:])
            own_masked = "".join(cleaned_chunks)
            own_raw = "".join(raw_chunks)
            class_spans_out[full_key] = (pkg, imports, wildcard_pkgs, own_masked, own_raw, new_chain, parent_raw)
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


_BOUND_MIN_RE = re.compile(r"@(?:Min|DecimalMin)\s*\(\s*(?:value\s*=\s*)?(?:\"(-?\d+(?:\.\d+)?)\"|(-?\d+(?:\.\d+)?))")
_BOUND_MAX_RE = re.compile(r"@(?:Max|DecimalMax)\s*\(\s*(?:value\s*=\s*)?(?:\"(-?\d+(?:\.\d+)?)\"|(-?\d+(?:\.\d+)?))")
_BOUND_SIZE_RE = re.compile(r"@(?:Size|Length)\s*\(([^)]+)\)")


def _strip_generic_type_annotations(type_str: str) -> str:
    """Flatten annotations inside generic type args so ``List<@NotBlank(...) String>`` -> ``List<String>``."""
    out: list[str] = []
    i = 0
    n = len(type_str)
    while i < n:
        if type_str[i] == "@":
            i += 1
            while i < n and (type_str[i].isalnum() or type_str[i] in "_."):
                i += 1
            while i < n and type_str[i].isspace():
                i += 1
            if i < n and type_str[i] == "(":
                depth = 0
                while i < n:
                    if type_str[i] == "(":
                        depth += 1
                    elif type_str[i] == ")":
                        depth -= 1
                        if depth == 0:
                            i += 1
                            break
                    i += 1
            continue
        out.append(type_str[i])
        i += 1
    return " ".join("".join(out).split())


def _iter_vo_field_decls(own_body: str):
    """Yield ``(ctx_start, decl_start, decl_end, raw_type, fname)`` for private instance fields."""
    priv_re = re.compile(r"^[ \t]*private\s+(?!static\b|final\b|class\b|interface\b|enum\b)", re.M)
    pos = 0
    n = len(own_body)
    for m in priv_re.finditer(own_body):
        if m.start() < pos:
            continue
        i = m.end()
        angle = 0
        paren = 0
        brace = 0
        is_method = False
        header_end = -1
        stmt_end = -1
        while i < n:
            ch = own_body[i]
            if ch == "<":
                angle += 1
            elif ch == ">" and angle > 0:
                angle -= 1
            elif ch == "(":
                if angle == 0 and paren == 0 and brace == 0:
                    is_method = True
                    break
                paren += 1
            elif ch == ")" and paren > 0:
                paren -= 1
            elif ch == "{":
                if angle == 0 and paren == 0:
                    is_method = True
                    break
                brace += 1
            elif ch == "}" and brace > 0:
                brace -= 1
            elif ch in "=;" and angle == 0 and paren == 0 and brace == 0:
                header_end = i
                if ch == ";":
                    stmt_end = i + 1
                else:
                    semi = own_body.find(";", i + 1)
                    if semi < 0:
                        is_method = True
                        break
                    stmt_end = semi + 1
                break
            i += 1
        if is_method or header_end < 0 or stmt_end < 0:
            continue
        header = own_body[m.end():header_end].strip()
        m_name = re.search(r"^(.*?)\s+([A-Za-z_]\w*)$", header, re.S)
        if not m_name:
            continue
        raw_type = _strip_generic_type_annotations(m_name.group(1))
        fname = m_name.group(2)
        if not raw_type or raw_type in {"void"}:
            continue
        pos = stmt_end
        yield m.start(), stmt_end, raw_type, fname


def _extract_field_bounds(ctx: str) -> tuple[float | None, float | None, int | None, int | None]:
    minimum: float | None = None
    maximum: float | None = None
    min_length: int | None = None
    max_length: int | None = None
    m_min = _BOUND_MIN_RE.search(ctx)
    if m_min:
        num = m_min.group(1) or m_min.group(2)
        try:
            minimum = int(num) if "." not in num else float(num)
        except ValueError:
            pass
    m_max = _BOUND_MAX_RE.search(ctx)
    if m_max:
        num = m_max.group(1) or m_max.group(2)
        try:
            maximum = int(num) if "." not in num else float(num)
        except ValueError:
            pass
    m_sz = _BOUND_SIZE_RE.search(ctx)
    if m_sz:
        args_s = m_sz.group(1)
        m_smin = re.search(r"\bmin\s*=\s*(\d+)", args_s)
        m_smax = re.search(r"\bmax\s*=\s*(\d+)", args_s)
        if m_smin and int(m_smin.group(1)) > 0:
            min_length = int(m_smin.group(1))
        if m_smax:
            max_length = int(m_smax.group(1))
    return minimum, maximum, min_length, max_length


def _java_expr_to_field(expr: str, known_fields: set[str]) -> str | None:
    clean = expr.strip()
    m_get = re.fullmatch(r"(?:this\.)?(?:get|is)([A-Z]\w*)\s*\(\s*\)", clean)
    if m_get:
        cand = m_get.group(1)[0].lower() + m_get.group(1)[1:]
        return cand if cand in known_fields else None
    m_var = re.fullmatch(r"(?:this\.)?([A-Za-z_]\w*)", clean)
    if m_var and m_var.group(1) in known_fields:
        return m_var.group(1)
    return None


def _parse_assert_true_rules(
    own_body: str,
    own_raw: str,
    known_fields: set[str],
) -> tuple[frozenset[str], tuple[ValidationRuleSpec, ...]]:
    """Extract synthetic Swagger `@AssertTrue` property names and declarative validation rules."""
    assert_props: set[str] = set()
    rules: list[ValidationRuleSpec] = []
    seen_sigs: set[tuple[str, tuple[str, ...], str]] = set()
    raw_msgs: dict[str, str] = {}
    for m_raw in re.finditer(
        r"@AssertTrue\s*(?:\([^)]*?\bmessage\s*=\s*\"([^\"]+)\"[^)]*\))?[\s\S]{0,160}?\bpublic\s+boolean\s+(is[A-Z]\w*|assert[A-Z]\w*)\s*\(\s*\)",
        own_raw,
    ):
        if m_raw.group(1):
            raw_msgs[m_raw.group(2)] = m_raw.group(1).strip()

    for m_at in _ASSERT_TRUE_RE.finditer(own_body):
        tail = own_body[m_at.end():m_at.end() + 700]
        m_sig = re.match(r"(?:\s*\([^)]*\))?\s*public\s+boolean\s+(is[A-Z]\w*|assert[A-Z]\w*)\s*\(\s*\)\s*\{", tail)
        if not m_sig:
            continue
        mname = m_sig.group(1)
        if mname.startswith("is") and len(mname) > 2:
            prop_name = mname[2].lower() + mname[3:]
            if prop_name not in known_fields:
                assert_props.add(prop_name)
        b_open = m_at.end() + m_sig.end() - 1
        b_close = _balanced_brace(own_body, b_open)
        if b_close is None:
            continue
        body = " ".join(own_body[b_open + 1:b_close].split())
        msg = raw_msgs.get(mname) or f"字段校验未通过（{mname}）"

        def _add_rule(rule: ValidationRuleSpec) -> None:
            sig = (rule.kind, rule.fields, rule.condition_field)
            if sig not in seen_sigs:
                seen_sigs.add(sig)
                rules.append(rule)

        # 1. Date order: !start.isAfter(end) | !end.isBefore(start) | end.isAfter(start) | start.isBefore(end) | isAfterOrEqual(end, start) | isBeforeOrEqual(start, end)
        m_d_not_after = re.search(r"!\s*([A-Za-z_]\w*(?:\s*\(\s*\))?)\s*\.isAfter\s*\(\s*([A-Za-z_]\w*(?:\s*\(\s*\))?)\s*\)", body)
        m_d1 = re.search(r"!\s*([A-Za-z_]\w*(?:\s*\(\s*\))?)\s*\.isBefore\s*\(\s*([A-Za-z_]\w*(?:\s*\(\s*\))?)\s*\)", body)
        m_d2 = None if m_d_not_after else re.search(r"(?<![!\w])([A-Za-z_]\w*(?:\s*\(\s*\))?)\s*\.isAfter\s*\(\s*([A-Za-z_]\w*(?:\s*\(\s*\))?)\s*\)", body)
        m_d_before = None if m_d1 else re.search(r"(?<![!\w])([A-Za-z_]\w*(?:\s*\(\s*\))?)\s*\.isBefore\s*\(\s*([A-Za-z_]\w*(?:\s*\(\s*\))?)\s*\)", body)
        m_d3 = re.search(r"\bisAfterOrEqual\s*\(\s*([^,]+),\s*([^)]+)\)", body)
        m_d4 = re.search(r"\bisBeforeOrEqual\s*\(\s*([^,]+),\s*([^)]+)\)", body)
        if m_d_not_after:
            start_f = _java_expr_to_field(m_d_not_after.group(1), known_fields)
            end_f = _java_expr_to_field(m_d_not_after.group(2), known_fields)
            if start_f and end_f and start_f != end_f:
                _add_rule(ValidationRuleSpec("date_order", (start_f, end_f), msg))
                continue
        if m_d1 or m_d2 or m_d3:
            m_use = m_d1 or m_d2 or m_d3
            end_f = _java_expr_to_field(m_use.group(1), known_fields)
            start_f = _java_expr_to_field(m_use.group(2), known_fields)
            if start_f and end_f and start_f != end_f:
                _add_rule(ValidationRuleSpec("date_order", (start_f, end_f), msg))
                continue
        if m_d_before or m_d4:
            m_use = m_d_before or m_d4
            start_f = _java_expr_to_field(m_use.group(1), known_fields)
            end_f = _java_expr_to_field(m_use.group(2), known_fields)
            if start_f and end_f and start_f != end_f:
                _add_rule(ValidationRuleSpec("date_order", (start_f, end_f), msg))
                continue

        # 2. Not equal: ObjectUtil.notEqual(a, b) / !Objects.equals(a, b)
        m_ne = re.search(r"(?:ObjectUtil|ObjUtil)\.notEqual\s*\(\s*([^,]+),\s*([^)]+)\)|!\s*Objects\.equals\s*\(\s*([^,]+),\s*([^)]+)\)", body)
        if m_ne:
            e1 = m_ne.group(1) or m_ne.group(3)
            e2 = m_ne.group(2) or m_ne.group(4)
            f1 = _java_expr_to_field(e1, known_fields)
            f2 = _java_expr_to_field(e2, known_fields)
            if f1 and f2 and f1 != f2:
                _add_rule(ValidationRuleSpec("not_equal", (f1, f2), msg))
                continue

        # 3. Conditional required on boolean flag
        m_c1 = re.search(
            r"if\s*\(\s*!\s*(?:BooleanUtil\.isTrue|Boolean\.TRUE\.equals)\s*\(\s*([^)]+)\s*\)\s*\)\s*\{\s*return\s+true\s*;\s*\}\s*return\s+(?:Objects\.nonNull|StrUtil\.isNotBlank|CollUtil\.isNotEmpty)\s*\(\s*([^)]+)\s*\)",
            body,
        )
        m_c2 = re.search(
            r"return\s+!\s*(?:Boolean\.TRUE\.equals|BooleanUtil\.isTrue)\s*\(\s*([^)]+)\s*\)\s*\|\|\s*(?:Objects\.nonNull|StrUtil\.isNotBlank|CollUtil\.isNotEmpty)\s*\(\s*([^)]+)\s*\)",
            body,
        )
        m_cu = m_c1 or m_c2
        if m_cu:
            cond_f = _java_expr_to_field(m_cu.group(1), known_fields)
            req_f = _java_expr_to_field(m_cu.group(2), known_fields)
            if cond_f and req_f and cond_f != req_f:
                _add_rule(ValidationRuleSpec("conditional_required", (req_f,), msg, condition_field=cond_f, condition_values=(True,)))
                continue

        # 4. At least one non-empty (require all OR-terms to resolve so collection-aware checks aren't truncated)
        m_or_ret = re.fullmatch(r"return\s+(.+?)\s*;", body)
        if m_or_ret and "||" in m_or_ret.group(1) and "&&" not in m_or_ret.group(1) and "?" not in m_or_ret.group(1):
            or_fields: list[str] = []
            all_resolved = True
            for raw_term in m_or_ret.group(1).split("||"):
                t_clean = raw_term.strip()
                while t_clean.startswith("(") and t_clean.endswith(")"):
                    t_clean = t_clean[1:-1].strip()
                m_wrap = re.fullmatch(
                    r"(?:CollUtil\.isNotEmpty|StrUtil\.isNotBlank|Objects\.nonNull|ObjectUtil\.isNotNull|ObjUtil\.isNotNull|BooleanUtil\.isTrue|Boolean\.TRUE\.equals)\s*\(\s*(.+?)\s*\)",
                    t_clean,
                )
                inner_expr = m_wrap.group(1) if m_wrap else t_clean
                f_hit = _java_expr_to_field(inner_expr, known_fields)
                if not f_hit:
                    all_resolved = False
                    break
                or_fields.append(f_hit)
            uniq_or = tuple(dict.fromkeys(or_fields))
            if all_resolved and len(uniq_or) >= 2:
                _add_rule(ValidationRuleSpec("at_least_one", uniq_or, msg))
                continue

    return frozenset(assert_props), tuple(rules)


def _index_java_tree_locked(
    vo_do_texts: list[tuple[str, str]],
    ctrl_texts: list[tuple[str, str]],
) -> tuple[dict[str, JavaVOAST], dict[str, ControllerEndpointAST]]:
    raw_class_spans: dict[str, tuple[str, dict[str, str], tuple[str, ...], str, str, tuple[str, ...], str]] = {}
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
    vo_parents: dict[str, str] = {}
    for full_key, (pkg, imports, wildcard_pkgs, own_body, own_raw, chain, parent_raw) in raw_class_spans.items():
        fields: list[JavaFieldAST] = []
        for start, decl_end, raw_type, fname in _iter_vo_field_decls(own_body):
            sep = max(own_body.rfind(";", 0, start), own_body.rfind("{", 0, start), own_body.rfind("}", 0, start))
            ctx_start = max(sep + 1, start - 500)
            ctx = own_body[ctx_start:start]
            # Strictly bound @Schema description to this field declaration (never crossing ;, {, }, or comments)
            raw_ctx = own_raw[ctx_start:min(len(own_raw), decl_end)]
            m_schema = re.search(r"@Schema\s*\(", own_body[ctx_start:decl_end])
            ann_ctx = raw_ctx[m_schema.start():] if m_schema else ctx
            required = bool(_NOT_REQ_ANN_RE.search(ctx))
            m_enum = _IN_ENUM_RE.search(ctx)
            enum_cls = m_enum.group(1) if m_enum else ""
            f_min, f_max, f_min_len, f_max_len = _extract_field_bounds(ctx)
            base_type = raw_type.split("<", 1)[0].strip()
            date_kind = "epoch_ms" if base_type == "LocalDateTime" else ("date" if base_type == "LocalDate" else "")
            list_elem = ""
            m_list = _LIST_ELEM_RE.search(raw_type)
            if m_list:
                list_elem = _resolve_java_type(
                    m_list.group(1), pkg, imports, wildcard_pkgs, chain, known_vos, unique_top
                )
            non_empty = bool(re.search(r"@NotEmpty\b", ctx)) or (f_min_len is not None and f_min_len >= 1)
            fields.append(JavaFieldAST(
                name=fname,
                java_type=raw_type,
                required=required,
                enum_class=enum_cls,
                date_kind=date_kind,
                list_element_class=list_elem,
                raw_annotations=ann_ctx,
                minimum=f_min,
                maximum=f_max,
                min_length=f_min_len,
                max_length=f_max_len,
                non_empty=non_empty,
            ))
        if parent_raw and not parent_raw.endswith("BaseDO"):
            resolved_parent = _resolve_java_type(
                parent_raw, pkg, imports, wildcard_pkgs, chain[:-1], known_vos, unique_top
            )
            if resolved_parent in known_vos and resolved_parent != full_key:
                vo_parents[full_key] = resolved_parent
        has_pwd_assert = bool(
            _ASSERT_TRUE_RE.search(own_body) and re.search(r"\bpassword\b|isPasswordValid", own_body, re.I)
        )
        pwd_required_field = any(f.name == "password" and f.required for f in fields)
        pwd_present = any(f.name == "password" for f in fields)
        own_field_names = {f.name for f in fields}
        assert_props, val_rules = _parse_assert_true_rules(own_body, own_raw, own_field_names)
        vo_map[full_key] = JavaVOAST(
            full_name=full_key,
            package=pkg,
            imports=imports,
            fields=tuple(fields),
            password_create_required=bool(pwd_present and (has_pwd_assert or pwd_required_field)),
            password_optional_present=bool(pwd_present and not pwd_required_field),
            assert_true_props=assert_props,
            validation_rules=val_rules,
        )

    for full_key, p_key in list(vo_parents.items()):
        chain_fields: list[JavaFieldAST] = []
        cur = p_key
        visited_p = {full_key}
        depth = 0
        ancestors: list[JavaVOAST] = []
        while cur and cur in vo_map and cur not in visited_p and depth < 4:
            visited_p.add(cur)
            ancestors.append(vo_map[cur])
            cur = vo_parents.get(cur, "")
            depth += 1
        if ancestors:
            own_vo = vo_map[full_key]
            seen_names = {f.name for f in own_vo.fields}
            merged_assert = set(own_vo.assert_true_props)
            merged_rules: list[ValidationRuleSpec] = []
            for anc in reversed(ancestors):
                merged_assert |= anc.assert_true_props
                merged_rules.extend(anc.validation_rules)
                for pf in anc.fields:
                    if pf.name not in seen_names:
                        chain_fields.append(pf)
                        seen_names.add(pf.name)
            merged_rules.extend(own_vo.validation_rules)
            merged_fields = (*chain_fields, *own_vo.fields)
            pwd_req = own_vo.password_create_required or any(a.password_create_required for a in ancestors)
            pwd_opt = own_vo.password_optional_present or any(a.password_optional_present for a in ancestors)
            vo_map[full_key] = replace(
                own_vo,
                fields=merged_fields,
                password_create_required=pwd_req,
                password_optional_present=bool(pwd_opt and not pwd_req),
                assert_true_props=frozenset(merged_assert),
                validation_rules=tuple(merged_rules),
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
    if re.fullmatch(r"\[\s*\](?:\s+as\s+[A-Za-z_][\w.<>\[\]\s]*)?", tok):
        return []
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
                        disp = "[]（空列表）" if val == [] else str(val)
                        out.setdefault(k, CompiledFieldDefault(field=k, kind="literal", value=val, display=disp))
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
                            disp = "[]（空列表）" if val == [] else str(val)
                            field_defaults[k] = CompiledFieldDefault(field=k, kind="literal", value=val, display=disp)
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


_AUTH_PERMISSION_MAPPER_RE = re.compile(
    r"^(?:RoleMenu|UserRole|(?:Sys|System|Admin)?Role|(?:Sys|System|Admin)?Menu|Tenant\w*|OAuth2\w*|Social\w*|\w*(?:File|Content)Permission\w*)Mapper$"
)
_CAMEL_TO_KEBAB_RE = re.compile(r"(?<!^)(?=[A-Z])")
_POLYMORPHIC_REF_STEMS = frozenset({"biz", "object", "target", "source", "sourceDoc", "sourceLine", "ref", "orderItem", "parent"})


def _collect_table_mappings(source, *, do_texts: list[tuple[str, str]] | None = None) -> tuple[dict[str, str], dict[str, str]]:
    """Return ``(do_to_table, mapper_to_table)`` from ``DO.java`` ``@TableName`` and ``Mapper.java``."""
    do_to_table: dict[str, str] = {}
    mapper_to_table: dict[str, str] = {}
    if do_texts is None and source is not None and getattr(source, "root", None):
        lock = getattr(source, "_instance_lock", None) or nullcontext()
        with lock:
            do_texts = [
                (source._relative(path), source._read(source._relative(path)))
                for path in source._java_files("DO.java")
            ]
    for _rel, raw in do_texts or ():
        m_cls = re.search(r"\bclass\s+(\w+DO)\b", raw)
        m_tbl = re.search(r'@TableName\s*\(\s*(?:value\s*=\s*)?"([^"]+)"', raw)
        if m_cls and m_tbl:
            do_cls = m_cls.group(1)
            tbl = m_tbl.group(1).strip()
            do_to_table[do_cls] = tbl
            mapper_to_table[do_cls[:-2] + "Mapper"] = tbl
    if source is not None and getattr(source, "root", None):
        lock = getattr(source, "_instance_lock", None) or nullcontext()
        with lock:
            for mp in source._java_files("Mapper.java"):
                mraw = source._read(source._relative(mp))
                m_base = re.search(r"BaseMapperX<\s*(\w+DO)\s*>", mraw)
                if m_base and m_base.group(1) in do_to_table:
                    mapper_to_table.setdefault(mp.stem, do_to_table[m_base.group(1)])
    return do_to_table, mapper_to_table


def _balanced_paren(text: str, open_idx: int) -> int | None:
    if open_idx < 0 or open_idx >= len(text) or text[open_idx] != "(":
        return None
    depth = 0
    for idx in range(open_idx, len(text)):
        ch = text[idx]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return idx
    return None


def _split_top_args(arg_text: str) -> list[str]:
    args: list[str] = []
    depth = 0
    start = 0
    for idx, ch in enumerate(arg_text):
        if ch in "([<":
            depth += 1
        elif ch in ")]>":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            args.append(arg_text[start:idx].strip())
            start = idx + 1
    tail = arg_text[start:].strip()
    if tail:
        args.append(tail)
    return args


def _extract_local_var_defs(chunk: str, limit_pos: int) -> dict[str, str]:
    """Collect bounded local variable initializer expressions declared before ``limit_pos``."""
    defs: dict[str, str] = {}
    prefix = chunk[:limit_pos]
    for stmt in prefix.split(";"):
        st = stmt.strip()
        if not st or len(st) > 400 or "{" in st or st.startswith(("if", "for", "while", "return", "throw")):
            continue
        m_as = re.match(
            r"(?:final\s+)?(?:[A-Za-z_]\w*(?:<[^=;{}()]*>)?\s+)?([a-z_]\w*)\s*=\s*([\s\S]+)$",
            st,
        )
        if m_as:
            var_name, rhs = m_as.group(1), m_as.group(2).strip()
            if (
                var_name not in {"if", "else", "return", "id"}
                and len(rhs) <= 280
                and not re.search(rf"\b{re.escape(var_name)}\b", rhs)
            ):
                defs[var_name] = rhs
    return defs


def _expand_expr_vars(expr: str, var_defs: dict[str, str], depth: int = 3) -> str:
    cur = expr
    seen: set[str] = set()
    for _ in range(depth):
        if len(cur) > 1200:
            break
        changed = False
        newly_seen: set[str] = set()

        def _rep(m: re.Match[str]) -> str:
            nonlocal changed
            name = m.group(0)
            if name in seen:
                return name
            start, end = m.start(), m.end()
            if start > 0 and cur[start - 1] == ".":
                return name
            if end < len(cur) and cur[end:].lstrip().startswith("("):
                return name
            if name in var_defs:
                changed = True
                newly_seen.add(name)
                return f"({var_defs[name]})"
            return name

        nxt = re.sub(r"\b[a-z_]\w*\b", _rep, cur)
        cur = nxt
        seen.update(newly_seen)
        if not changed:
            break
    return cur


def _extract_getter_fields(expr: str) -> tuple[str, ...]:
    fields: list[str] = []
    for g in re.findall(r"\.get([A-Z]\w*)\s*\(\s*\)", expr):
        fname = g[0].lower() + g[1:]
        if fname != "id" and fname not in fields:
            fields.append(fname)
    return tuple(fields)


def _extract_diff_remove_fields(expanded_expr: str) -> tuple[str, ...]:
    if not re.search(r"\bdiff\w*\s*\.\s*get\s*\(\s*2\s*\)|\.get\s*\(\s*2\s*\)", expanded_expr):
        return ()
    for m_diff in re.finditer(r"\bdiffList\s*\(", expanded_expr):
        close_idx = _balanced_paren(expanded_expr, m_diff.end() - 1)
        if close_idx is None:
            continue
        args = _split_top_args(expanded_expr[m_diff.end():close_idx])
        if len(args) >= 2:
            flds = _extract_getter_fields(args[1])
            if flds:
                return flds
    return ()


def _analyze_guard_at(
    chunk: str,
    call_pos: int,
    *,
    var_defs: dict[str, str] | None = None,
    param_bools: dict[str, bool] | None = None,
) -> tuple[bool, str, tuple[str, ...], str]:
    """Return ``(dead_branch, trigger_mode, trigger_fields, cond_text)`` at ``call_pos``."""
    vdefs = var_defs if var_defs is not None else _extract_local_var_defs(chunk, call_pos)
    enclosing_exprs: list[str] = []
    for m_if in re.finditer(r"\bif\s*\(", chunk[:call_pos]):
        open_p = m_if.end() - 1
        close_p = _balanced_paren(chunk, open_p)
        if close_p is None:
            continue
        after = chunk[close_p + 1:].lstrip()
        if not after.startswith("{"):
            continue
        brace_open = close_p + 1 + (len(chunk[close_p + 1:]) - len(after))
        brace_close = _balanced_brace(chunk, brace_open)
        if brace_close is not None and brace_open <= call_pos <= brace_close:
            enclosing_exprs.append(" ".join(chunk[open_p + 1:close_p].split()))

    active_exprs: list[str] = []
    for expr in enclosing_exprs:
        stripped = expr.strip()
        if param_bools:
            m_pos = re.fullmatch(r"([A-Za-z_]\w*)", stripped)
            m_neg = re.fullmatch(r"!\s*([A-Za-z_]\w*)", stripped)
            if m_pos and m_pos.group(1) in param_bools:
                if not param_bools[m_pos.group(1)]:
                    return True, "always", (), ""
                continue
            if m_neg and m_neg.group(1) in param_bools:
                if param_bools[m_neg.group(1)]:
                    return True, "always", (), ""
                continue
        active_exprs.append(expr)

    # Walk from innermost to outermost enclosing condition to determine trigger mode/fields
    for expr in reversed(active_exprs):
        expanded = _expand_expr_vars(expr, vdefs)
        diff_fields = _extract_diff_remove_fields(expanded)
        if not diff_fields:
            diff_fields = _extract_diff_remove_fields(
                _expand_expr_vars(expr.replace(".get(2)", "").strip(), vdefs)
            )
        if diff_fields:
            return (
                False,
                "on_remove",
                diff_fields,
                f"条件成立（涉及 {'/'.join(diff_fields[:3])}）",
            )
        if re.search(r"\bObj(?:ect)?Util\.notEqual\b|!\s*Obj(?:ect)?s?\.equals\b|\.compareTo\s*\(", expanded):
            flds = _extract_getter_fields(expanded)
            if flds:
                return (
                    False,
                    "on_field_change",
                    flds,
                    f"条件成立（涉及 {'/'.join(flds[:3])}）",
                )
        flds = _extract_getter_fields(expanded)
        if flds:
            return (
                False,
                "on_field_present",
                flds,
                f"条件成立（涉及 {'/'.join(flds[:3])}）",
            )

    if active_exprs or re.search(r"\breturn\s*;", chunk[:call_pos]):
        return False, "always", (), "按条件触发"
    return False, "always", (), ""


def _extract_conditional_trigger_fields(chunk: str, call_pos: int) -> set[str]:
    """Identify VO/DO getter fields referenced in the enclosing ``if (...)`` guard around ``call_pos``."""
    _dead, _mode, fields, _text = _analyze_guard_at(chunk, call_pos)
    return set(fields)


def _extract_conditional_guard_text(chunk: str, call_pos: int) -> str:
    _dead, _mode, _fields, text = _analyze_guard_at(chunk, call_pos)
    return text


def _load_parsed_service(
    iface: str,
    source,
    svc_by_iface: dict[str, str] | None,
    parsed_svc_cache: dict[str, tuple[str, dict[str, str], dict[str, list[tuple[int, int, int]]]]] | None,
) -> tuple[str, dict[str, str], dict[str, list[tuple[int, int, int]]]] | None:
    if parsed_svc_cache is not None and iface in parsed_svc_cache:
        return parsed_svc_cache[iface]
    raw_svc = svc_by_iface.get(iface) if svc_by_iface is not None else None
    if raw_svc is None:
        if svc_by_iface is not None or source is None or not getattr(source, "root", None):
            return None
        target_file = None
        for suffix in ("ServiceImpl.java", "ApiImpl.java"):
            for path in source._java_files(suffix):
                if path.stem.endswith("Impl") and path.stem[:-len("Impl")] == iface:
                    target_file = path
                    break
            if target_file is not None:
                break
        if target_file is None:
            return None
        raw_svc = source._read(source._relative(target_file))
    masked = _mask(raw_svc)
    svc_fields = {name: kind for kind, name in _SVC_OR_API_FIELD_RE.findall(masked)}
    sigs: dict[str, list[tuple[int, int, int]]] = {}
    for name, start, bs, be in _methods(masked):
        sigs.setdefault(name, []).append((start, bs, be))
    entry = (masked, svc_fields, sigs)
    if parsed_svc_cache is not None:
        parsed_svc_cache[iface] = entry
    return entry


def _collect_reachable_bodies(
    masked: str,
    sigs: dict[str, list[tuple[int, int, int]]],
    method: str,
    entry_indices: list[int] | None = None,
) -> list[tuple[str, int, int, int]]:
    spans = sigs.get(method) or []
    indices = entry_indices if entry_indices is not None else list(range(len(spans)))
    visited: set[tuple[str, int]] = set()
    work = [(method, i) for i in reversed(indices)]
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
    return bodies


def _mapper_target_table(mapper_type: str, mapper_to_table: dict[str, str] | None) -> str:
    if mapper_to_table and mapper_type in mapper_to_table:
        return mapper_to_table[mapper_type]
    stem = mapper_type[:-len("Mapper")] if mapper_type.endswith("Mapper") else mapper_type
    return _CAMEL_TO_KEBAB_RE.sub("_", stem).lower()


def _mapper_action_label(mapper_verb: str) -> str:
    if mapper_verb.startswith("insert"):
        return f"新增伴生记录（{mapper_verb}）"
    if mapper_verb.startswith("delete"):
        return f"清理伴生记录（{mapper_verb}）"
    return f"更新伴生记录（{mapper_verb}）"


def _analyze_service_method(
    source,
    ep: ControllerEndpointAST | None,
    *,
    allow_child_mappers: bool = False,
    allow_1n_child_sync: bool = False,
    do_fields: dict[str, set[str]] | None = None,
    vo_field_names: tuple[str, ...] = (),
    svc_by_iface: dict[str, str] | None = None,
    parsed_svc_cache: dict[str, tuple[str, dict[str, str], dict[str, list[tuple[int, int, int]]]]] | None = None,
    mode: str = "create_or_update",
    mapper_to_table: dict[str, str] | None = None,
    child_mapper_names: frozenset[str] = frozenset(),
    companion_readback_fn=None,
    analysis_ctx: dict | None = None,
) -> tuple[bool, set[str], set[str], set[str], tuple[object, ...]]:
    """Return ``(safe, unconditional_null_fields, dropped_fields, cond_side_effect_fields, side_effects)``."""
    if ep is None or not ep.service_iface or not ep.service_method:
        return False, set(), set(), set(), ()
    if svc_by_iface is None and (source is None or not getattr(source, "root", None)):
        return False, set(), set(), set(), ()
    if _DANGEROUS_SERVICE_TYPES.search(ep.service_iface) or _AUTH_CONFIG_ENTRY_SERVICE_RE.fullmatch(ep.service_iface):
        return False, set(), set(), set(), ()

    loaded = _load_parsed_service(ep.service_iface, source, svc_by_iface, parsed_svc_cache)
    if loaded is None:
        return False, set(), set(), set(), ()
    masked, svc_fields, sigs = loaded
    spans = sigs.get(ep.service_method)
    if not spans:
        return False, set(), set(), set(), ()

    entry_indices = list(range(len(spans)))
    if len(spans) > 1 and ep.request_vo:
        req_simple = ep.request_vo.rsplit(".", 1)[-1].rsplit("$", 1)[-1]
        matched_idx = [i for i, (s_i, bs_i, _be_i) in enumerate(spans) if re.search(rf"\b{re.escape(req_simple)}\b", masked[s_i:bs_i])]
        if matched_idx:
            entry_indices = matched_idx

    bodies = _collect_reachable_bodies(masked, sigs, ep.service_method, entry_indices)
    allow_employee_station_notice = (
        ep.service_iface == "HrmEmployeeService" and ep.service_method == "createEmployee"
    )
    if ep.verb.casefold() == "put" and bodies and mode != "status":
        # Version-forking pseudo-updates disable old record and insert a new ID so GET(old_id) never updates.
        entry_chunk = masked[bodies[0][2]:bodies[0][3]]
        if ".setId(null)" in entry_chunk and re.search(r"\b\w*Mapper\.insert\s*\(", entry_chunk):
            return False, set(), set(), set(), ()

    entry_body_top = masked[bodies[0][2]:bodies[0][3]] if bodies else ""
    do_hits_top = re.findall(r"BeanUtils\.toBean\s*\(\s*\w+\s*,\s*(\w+DO)\.class\s*\)", entry_body_top)
    if not do_hits_top:
        do_hits_top = re.findall(r"\b(\w+DO)\s+\w+\s*=\s*\w+Convert\.INSTANCE\.convert\s*\(", entry_body_top)
    primary_mapper = (
        f"{do_hits_top[0][:-2]}Mapper"
        if len(set(do_hits_top)) == 1 and do_hits_top[0].endswith("DO")
        else (f"{ep.service_iface[:-7]}Mapper" if ep.service_iface.endswith("Service") else "")
    )
    injected_mappers = {t for t in svc_fields.values() if t.endswith("Mapper")}
    if len(injected_mappers) == 1:
        primary_mapper = next(iter(injected_mappers))

    cond_side_effect_fields: set[str] = set()
    status_side_effects: list[str] = []
    companion_specs: list[CompanionWriteSpec] = []
    seen_companions: set[tuple[str, str, str]] = set()
    is_put_update = (mode == "create_or_update" and ep.verb.casefold() == "put")
    is_write_mode = mode != "status"

    def _record_companion(
        mapper_type: str,
        mapper_verb: str,
        caller_iface: str,
        caller_method: str,
        cond_text: str = "",
        *,
        destructive_delete: bool = False,
        trigger_mode: str = "always",
        trigger_fields: tuple[str, ...] = (),
        payload_fk_field: str = "",
        expect_field: str = "",
    ) -> None:
        if mode == "status":
            return
        tbl = _mapper_target_table(mapper_type, mapper_to_table)
        act = _mapper_action_label(mapper_verb)
        src_meth = f"{caller_iface}.{caller_method} -> {mapper_type}.{mapper_verb}"
        sig = (tbl, act, src_meth)
        if sig in seen_companions:
            return
        seen_companions.add(sig)
        rb_entity, rb_qfield, rb_expect = ("", "", "")
        if callable(companion_readback_fn):
            hint = companion_readback_fn(
                mapper_type,
                tbl,
                ep.path,
                payload_fk_field=payload_fk_field,
                expect_field=expect_field,
            )
            if isinstance(hint, tuple) and len(hint) >= 3:
                rb_entity, rb_qfield, rb_expect = hint[0], hint[1], hint[2]
            elif isinstance(hint, tuple) and len(hint) >= 2:
                rb_entity, rb_qfield = hint[0], hint[1]
        companion_specs.append(
            CompanionWriteSpec(
                target=tbl,
                action=act,
                source_method=src_meth,
                condition=cond_text,
                readback_entity=rb_entity,
                readback_query_field=rb_qfield,
                destructive_delete=bool(destructive_delete),
                trigger_mode=trigger_mode if trigger_mode in {"always", "on_field_change", "on_field_present", "on_remove"} else "always",
                trigger_fields=tuple(trigger_fields),
                readback_expect_field=rb_expect,
            )
        )

    def _call_literal_bool_args(call_chunk: str, call_pos: int, c_masked: str, c_spans: list[tuple[int, int, int]]) -> dict[str, bool]:
        """Bind formal boolean parameter names to literal ``true``/``false`` call arguments when present."""
        open_p = call_chunk.find("(", call_pos)
        close_p = _balanced_paren(call_chunk, open_p) if open_p >= 0 else None
        if close_p is None or not c_spans:
            return {}
        call_args = _split_top_args(call_chunk[open_p + 1:close_p])
        for s_i, bs_i, _be_i in c_spans:
            params = _signature_params(c_masked, s_i, bs_i)
            if len(params) != len(call_args):
                continue
            out: dict[str, bool] = {}
            for pname, aval in zip(params, call_args):
                if aval == "true":
                    out[pname] = True
                elif aval == "false":
                    out[pname] = False
            if out:
                return out
        return {}

    def _merge_guard(
        outer: tuple[str, tuple[str, ...], str],
        inner: tuple[str, tuple[str, ...], str],
    ) -> tuple[str, tuple[str, ...], str]:
        o_mode, o_fields, o_cond = outer
        i_mode, i_fields, i_cond = inner
        if o_fields and o_mode != "always":
            return o_mode, o_fields, o_cond
        if i_fields and i_mode != "always":
            return i_mode, i_fields, i_cond
        return o_mode, (o_fields or i_fields), (o_cond or i_cond)

    def _trace_callee_2hop(
        callee_iface: str,
        callee_method: str,
        *,
        depth: int,
        visiting: frozenset[tuple[str, str]],
        enclosing_cond: str,
        enclosing_trigger_mode: str = "always",
        enclosing_trigger_fields: tuple[str, ...] = (),
        param_bools: dict[str, bool] | None = None,
    ) -> bool:
        if depth > 2 or _DANGEROUS_SERVICE_TYPES.search(callee_iface):
            return False
        if _AUTH_CONFIG_CALL_SERVICE_RE.fullmatch(callee_iface) and not _READ_ONLY_SERVICE_METHOD.fullmatch(callee_method):
            return False
        if (callee_iface, callee_method) in visiting:
            return True
        callee_loaded = _load_parsed_service(callee_iface, source, svc_by_iface, parsed_svc_cache)
        if callee_loaded is None:
            if callee_method in _SAFE_EXTRA_SERVICE_METHODS and callee_method != "parsePeriod":
                tbl = _CAMEL_TO_KEBAB_RE.sub("_", callee_iface.removesuffix("Service").removesuffix("Api")).lower()
                sig = (tbl, f"伴生调用（{callee_method}）", f"{callee_iface}.{callee_method}")
                if sig not in seen_companions:
                    seen_companions.add(sig)
                    companion_specs.append(
                        CompanionWriteSpec(
                            target=tbl,
                            action=f"伴生调用（{callee_method}）",
                            source_method=f"{callee_iface}.{callee_method}",
                            condition=enclosing_cond,
                            trigger_mode=enclosing_trigger_mode,
                            trigger_fields=enclosing_trigger_fields,
                        )
                    )
                return True
            return False
        c_masked, c_fields, c_sigs = callee_loaded
        if callee_method not in c_sigs:
            return False
        next_vis = visiting | {(callee_iface, callee_method)}
        c_bodies = _collect_reachable_bodies(c_masked, c_sigs, callee_method)
        for _cm, cs, cbs, cbe in c_bodies:
            if re.search(r"@Async\b", c_masked[max(0, cs - 200):cbs]):
                return False
            c_chunk = c_masked[cbs:cbe]
            for _kind, pat in getattr(source, "_UNTRACEABLE", ()):
                if pat.search(c_chunk):
                    return False
            c_vdefs = _extract_local_var_defs(c_chunk, len(c_chunk))
            for m_call in re.finditer(r"\b(\w+)\s*\.\s*(\w+)\s*\(", c_chunk):
                d_var, d_meth = m_call.group(1), m_call.group(2)
                if d_var not in c_fields:
                    continue
                d_type = c_fields[d_var]
                dead, in_mode, in_fields, in_cond = _analyze_guard_at(
                    c_chunk, m_call.start(), var_defs=c_vdefs, param_bools=param_bools if depth == 1 else None
                )
                if dead:
                    continue
                eff_mode, eff_fields, eff_cond = _merge_guard(
                    (enclosing_trigger_mode, enclosing_trigger_fields, enclosing_cond),
                    (in_mode, in_fields, in_cond),
                )
                if d_type.endswith("Mapper"):
                    is_r = bool(_READ_ONLY_SERVICE_METHOD.fullmatch(d_meth)) or d_meth.startswith(("select", "count", "exists"))
                    if not is_r:
                        if _AUTH_PERMISSION_MAPPER_RE.search(d_type):
                            return False
                        expect_f = ""
                        payload_fk = ""
                        if d_meth == "updateById":
                            stmt_end = c_chunk.find(";", m_call.start())
                            stmt_txt = c_chunk[m_call.start():stmt_end] if stmt_end > m_call.start() else ""
                            setters = [
                                sname[0].lower() + sname[1:]
                                for sname in re.findall(r"\.set([A-Z]\w*)\s*\(", stmt_txt)
                                if sname != "Id"
                            ]
                            if len(setters) == 1:
                                expect_f = setters[0]
                            if eff_mode == "on_field_present" and len(eff_fields) == 1:
                                payload_fk = eff_fields[0]
                        is_destr_del = bool(
                            is_put_update
                            and not allow_child_mappers
                            and d_meth.startswith("delete")
                            and d_type not in child_mapper_names
                        )
                        _record_companion(
                            d_type,
                            d_meth,
                            callee_iface,
                            callee_method,
                            eff_cond,
                            destructive_delete=is_destr_del,
                            trigger_mode=eff_mode,
                            trigger_fields=eff_fields,
                            payload_fk_field=payload_fk,
                            expect_field=expect_f,
                        )
                    continue
                if _DANGEROUS_SERVICE_TYPES.search(d_type):
                    return False
                if _AUTH_CONFIG_CALL_SERVICE_RE.fullmatch(d_type) and not _READ_ONLY_SERVICE_METHOD.fullmatch(d_meth):
                    return False
                if _READ_ONLY_SERVICE_METHOD.fullmatch(d_meth) or d_meth == "parsePeriod":
                    continue
                if depth >= 2:
                    return False
                if not _trace_callee_2hop(
                    d_type,
                    d_meth,
                    depth=depth + 1,
                    visiting=next_vis,
                    enclosing_cond=eff_cond,
                    enclosing_trigger_mode=eff_mode,
                    enclosing_trigger_fields=eff_fields,
                ):
                    return False
        return True

    for mname, s, bs, be in bodies:
        if re.search(r"@Async\b", masked[max(0, s - 200):bs]):
            return False, set(), set(), set(), ()
        if allow_employee_station_notice and mname in {
            "executeAfterCommit",
            "sendEmployeeOpenedMessage",
            "sendEmployeeMessage",
        }:
            continue
        chunk = masked[bs:be]
        chunk_vdefs = _extract_local_var_defs(chunk, len(chunk))
        for _kind, pat in getattr(source, "_UNTRACEABLE", ()):
            if pat.search(chunk):
                return False, set(), set(), set(), ()
        # Own-mapper updates, inserts, deletes
        mapper_calls = list(re.finditer(r"\b(\w+)\.(insert\w*|update\w*|delete\w*)\s*\(", chunk))
        for mc in mapper_calls:
            m_var, m_verb = mc.group(1), mc.group(2)
            m_name = svc_fields.get(m_var, "")
            if not m_name.endswith("Mapper"):
                if m_var.endswith("Mapper"):
                    m_name = m_var[0].upper() + m_var[1:]
                else:
                    continue
            if _AUTH_PERMISSION_MAPPER_RE.search(m_name):
                return False, set(), set(), set(), ()
            _dead_mc, g_mode, g_fields, g_cond = _analyze_guard_at(chunk, mc.start(), var_defs=chunk_vdefs)
            if m_verb.startswith("update"):
                if m_verb in {"updateById", "updateForEdit", "updateForSave"} and (not primary_mapper or m_name == primary_mapper):
                    continue
                if mode == "status":
                    if "DefaultStatus" in m_verb or (m_verb == "update" and "defaultStatus" in chunk):
                        note = "设为默认时同步取消同范围原有默认记录"
                        if note not in status_side_effects:
                            status_side_effects.append(note)
                        continue
                    if m_verb in {"update", "updateStatus"}:
                        continue
                if allow_child_mappers or allow_1n_child_sync or (is_write_mode and not is_put_update):
                    if m_name != primary_mapper and m_name not in child_mapper_names:
                        _record_companion(
                            m_name, m_verb, ep.service_iface, mname, g_cond,
                            trigger_mode=g_mode, trigger_fields=g_fields,
                        )
                    elif is_put_update and m_name != primary_mapper and allow_1n_child_sync:
                        _record_companion(
                            m_name, m_verb, ep.service_iface, mname, g_cond,
                            trigger_mode=g_mode, trigger_fields=g_fields,
                        )
                    continue
                return False, set(), set(), set(), ()
            elif is_put_update and m_name != primary_mapper:
                if allow_child_mappers or (allow_1n_child_sync and not is_child_delete_unsafe(m_name, m_verb)):
                    if m_name not in child_mapper_names:
                        is_destr_del = bool(not allow_child_mappers and m_verb.startswith("delete"))
                        _record_companion(
                            m_name, m_verb, ep.service_iface, mname, g_cond,
                            destructive_delete=is_destr_del,
                            trigger_mode=g_mode, trigger_fields=g_fields,
                        )
                    continue
                return False, set(), set(), set(), ()
            elif not is_put_update and m_name != primary_mapper and m_name not in child_mapper_names:
                _record_companion(
                    m_name, m_verb, ep.service_iface, mname, g_cond,
                    trigger_mode=g_mode, trigger_fields=g_fields,
                )

        # Check sibling Service / Api / Producer / Client / Template field calls
        for call_m in _SVC_OR_API_CALL_RE.finditer(chunk):
            sc_var, sc_method = call_m.group(1), call_m.group(2)
            if sc_var not in svc_fields:
                continue
            sc_type = svc_fields[sc_var]
            if sc_type.endswith("Mapper"):
                continue
            if _DANGEROUS_SERVICE_TYPES.search(sc_type):
                return False, set(), set(), set(), ()
            if _AUTH_CONFIG_CALL_SERVICE_RE.fullmatch(sc_type) and not _READ_ONLY_SERVICE_METHOD.fullmatch(sc_method):
                return False, set(), set(), set(), ()
            if _READ_ONLY_SERVICE_METHOD.fullmatch(sc_method) or sc_method == "parsePeriod":
                continue
            if _TOKEN_REVOKE_METHOD_RE.search(sc_method) and _TOKEN_REVOKE_TYPE_RE.search(sc_type):
                if mode == "status":
                    note = "停用/禁用时同步吊销该账号已签发的登录令牌"
                    if note not in status_side_effects:
                        status_side_effects.append(note)
                    continue
                if is_put_update:
                    trig = _extract_conditional_trigger_fields(chunk, call_m.start())
                    if len(trig) == 1:
                        cond_side_effect_fields |= trig
                        continue
            if allow_child_mappers and (
                "ItemService" in sc_type
                or "DetailService" in sc_type
                or "EntryService" in sc_type
                or "SkuService" in sc_type
            ):
                continue
            if not is_write_mode and sc_method not in _SAFE_EXTRA_SERVICE_METHODS:
                return False, set(), set(), set(), ()
            _dead_sc, sc_mode, sc_fields, guard_txt = _analyze_guard_at(chunk, call_m.start(), var_defs=chunk_vdefs)
            callee_pre = _load_parsed_service(sc_type, source, svc_by_iface, parsed_svc_cache)
            p_bools = (
                _call_literal_bool_args(chunk, call_m.start(), callee_pre[0], callee_pre[2].get(sc_method, []))
                if callee_pre is not None
                else {}
            )
            if not _trace_callee_2hop(
                sc_type,
                sc_method,
                depth=1,
                visiting=frozenset({(ep.service_iface, ep.service_method)}),
                enclosing_cond=guard_txt,
                enclosing_trigger_mode=sc_mode,
                enclosing_trigger_fields=sc_fields,
                param_bools=p_bools,
            ):
                return False, set(), set(), set(), ()

    uncond_nulls: set[str] = set()
    if is_put_update:
        s0, bs0, be0 = spans[entry_indices[0]]
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
                        return False, set(), set(), set(), ()
                    if nprops:
                        if brace_depth == 0 and not pending and not is_branch and ln.startswith(req_var + "."):
                            uncond_nulls.update(p for p in nprops if p != "id")
                        else:
                            return False, set(), set(), set(), ()
                brace_depth += ln.count("{") - ln.count("}")
                if is_branch and not ln.endswith("{") and ";" not in ln:
                    pending = True
                elif ends:
                    pending = False
        for mname, _s, bs, be in bodies[1:]:
            if mname == ep.service_method:
                continue
            if any(p != "Id" for p in _SET_NULL_RE.findall(masked[bs:be])):
                return False, set(), set(), set(), ()

    dropped: set[str] = set()
    now_volatiles: set[str] = set()
    transformed_fields: set[str] = set()
    if vo_field_names and bodies and mode != "status" and not is_put_update:
        entry_body_post = masked[bodies[0][2]:bodies[0][3]]
        for tm in re.finditer(r"\.set([A-Z]\w*)\s*\(\s*[a-z]\w*\s*\(", entry_body_post):
            t_name = tm.group(1)[0].lower() + tm.group(1)[1:]
            if t_name != "id" and t_name in vo_field_names:
                transformed_fields.add(t_name)
    if do_fields and vo_field_names and bodies and mode != "status":
        all_body_text = "\n".join(masked[bs:be] for _m, _s, bs, be in bodies)
        entry_body = masked[bodies[0][2]:bodies[0][3]]
        do_hits = re.findall(r"BeanUtils\.toBean\s*\(\s*\w+\s*,\s*(\w+DO)\.class\s*\)", entry_body)
        if not do_hits:
            do_hits = re.findall(r"\b(\w+DO)\s+\w+\s*=\s*\w+Convert\.INSTANCE\.convert\s*\(", entry_body)
        if is_put_update:
            for nm in re.finditer(r"\.set([A-Z]\w*)\s*\(\s*(?:LocalDateTime\.now|new\s+Date)\b", all_body_text):
                p_name = nm.group(1)[0].lower() + nm.group(1)[1:]
                if p_name != "id":
                    now_volatiles.add(p_name)
        if len(set(do_hits)) == 1 and do_hits[0] in do_fields:
            target_do = do_fields[do_hits[0]]
            if (
                is_put_update
                and "name" not in vo_field_names
                and "name" in target_do
                and re.search(
                    r"(?:BeanUtils\.toBean\s*\([^;]*\)\s*(?:\.\s*set[A-Z]\w*\s*\([^;]*\)\s*)*|[uU]pdate\w*\s*)\.\s*setName\s*\(",
                    entry_body,
                )
            ):
                return False, set(), set(), set(), ()
            for fname in vo_field_names:
                if fname == "id" or fname in target_do:
                    continue
                cap = fname[0].upper() + fname[1:] if fname else ""
                if not re.search(rf"\.(?:get|is){re.escape(cap)}\s*\(", all_body_text):
                    dropped.add(fname)
    if analysis_ctx is not None:
        analysis_ctx["volatiles"] = frozenset(now_volatiles)
        analysis_ctx["transformed"] = frozenset(transformed_fields)

    if mode == "create_or_update" and ep.verb.casefold() == "post" and bodies:
        all_body_text_create = "\n".join(masked[bs:be] for _m, _s, bs, be in bodies)
        if re.search(r"\bvalidate\w*NotExists\s*\(\s*\)", all_body_text_create):
            return False, set(), set(), set(), ()
        _m0, s0, bs0, _be0 = bodies[0]
        has_tx = bool(re.search(r"@Transactional\b", masked[max(0, s0 - 250):bs0]))
        if (
            not has_tx
            and companion_specs
            and re.search(r"\bvalidate\w*Confirmed\s*\(", all_body_text_create)
            and re.search(
                r"Obj(?:ect)?Util\.notEqual\s*\([^;\n]*\.get[A-Z]\w*Id\s*\(\s*\)\s*,\s*[^;\n]*\.get[A-Z]\w*Id\s*\(\s*\)",
                all_body_text_create,
            )
        ):
            return False, set(), set(), set(), ()

    if is_put_update and not allow_child_mappers:
        for se in companion_specs:
            if not se.destructive_delete:
                continue
            has_live_readback = bool(
                se.readback_entity
                and se.readback_query_field
                and not se.readback_query_field.startswith("payload:")
            )
            has_on_remove_readback = bool(
                se.trigger_mode == "on_remove"
                and se.trigger_fields
                and all(tf in vo_field_names for tf in se.trigger_fields)
            )
            if not (has_live_readback or has_on_remove_readback):
                return False, set(), set(), set(), ()

    out_effects: tuple[object, ...] = (
        tuple(status_side_effects) if mode == "status" else tuple(companion_specs)
    )
    return True, uncond_nulls, dropped, cond_side_effect_fields, out_effects


def is_child_delete_unsafe(mapper_name: str, verb: str) -> bool:
    return bool(_AUTH_PERMISSION_MAPPER_RE.search(mapper_name))


def _compile_f2_ref_indexes(
    paths: dict,
    ctrl_endpoints: dict[str, ControllerEndpointAST],
    source,
    svc_by_iface: dict[str, str],
    parsed_svc_cache: dict,
    ui=None,
):
    """Build dual-source F2 reference resolver indexes (backend AST + Vue selector + module schema)."""
    listable_entities: set[str] = set()
    entity_query_params: dict[str, set[str]] = {}
    for p, item in sorted(paths.items()):
        if not p.startswith("/admin-api/") or not isinstance(item, dict) or "get" not in item:
            continue
        rel = p[len("/admin-api/"):]
        if "/" not in rel:
            continue
        ent, tail = rel.rsplit("/", 1)
        if tail in {"page", "list", "simple-list", "list-all-simple", "tree"}:
            listable_entities.add(ent)
            if tail in {"list", "page"}:
                op = item.get("get") or {}
                qnames = {
                    q.get("name") for q in (op.get("parameters") or ())
                    if isinstance(q, dict) and q.get("in") == "query" and isinstance(q.get("name"), str)
                }
                entity_query_params.setdefault(ent, set()).update(qnames)

    svc_sets: dict[str, set[str]] = {}
    for _key, ep in ctrl_endpoints.items():
        if ep.service_iface and "/" in ep.path:
            e = ep.path.rsplit("/", 1)[0]
            if e in listable_entities:
                svc_sets.setdefault(ep.service_iface, set()).add(e)
                if ep.service_iface.endswith("Service"):
                    svc_sets.setdefault(ep.service_iface[:-7] + "Mapper", set()).add(e)
    unique_svc_entity = {k: next(iter(v)) for k, v in svc_sets.items() if len(v) == 1}

    # Vue selector index: component / option-var -> listable entity
    ui_field_refs: dict[tuple[str, str], str] = {}
    ui_src: Path | None = None
    if source is not None and getattr(source, "root", None):
        cand = Path(source.root) / "ui" / "src"
        if (cand / "views").is_dir():
            ui_src = cand
    if ui_src is None and ui is not None and getattr(ui, "root", None):
        cand = Path(ui.root) / "src"
        if (cand / "views").is_dir():
            ui_src = cand
    if ui_src is not None:
        ui_api = ui_src / "api"
        api_gets: dict[str, dict[str, str]] = {}
        api_writes: dict[str, dict[str, str]] = {}
        if ui_api.is_dir():
            for ts_path in sorted(ui_api.rglob("*.ts")):
                rel = ts_path.relative_to(ui_api).as_posix()
                mod = "api/" + (rel[:-len("/index.ts")] if rel.endswith("/index.ts") else rel[:-len(".ts")])
                txt = ts_path.read_text(encoding="utf-8", errors="ignore")
                g_map: dict[str, str] = {}
                for pat in (_GET_URL_ARROW_RE, _GET_URL_FN_RE):
                    for fn_name, url in pat.findall(txt):
                        cleaned = url.strip().removeprefix("/admin-api").strip("/")
                        if "/" in cleaned:
                            ent_g, tail_g = cleaned.rsplit("/", 1)
                            if tail_g in {"page", "list", "simple-list", "list-all-simple", "tree"} and ent_g in listable_entities:
                                g_map[fn_name] = ent_g
                if g_map:
                    api_gets[mod] = g_map
                w_map: dict[str, str] = {}
                for pat in (_POST_URL_ARROW_RE, _POST_URL_FN_RE):
                    for fn_name, url in pat.findall(txt):
                        cleaned = url.strip().removeprefix("/admin-api").strip("/")
                        if cleaned.endswith("/create"):
                            w_map[fn_name] = cleaned[:-len("/create")]
                if w_map:
                    api_writes[mod] = w_map

        comp_to_entity: dict[str, str] = {}
        form_item_re = re.compile(r"<el-form-item\b([^>]*)>([\s\S]*?)</el-form-item>")
        comp_import_re = re.compile(r"import\s+(\w+)\s+from\s+['\"][^'\"]+/(\w+)\.vue['\"]")
        for search_dir in (ui_src / "views", ui_src / "components"):
            if not search_dir.is_dir():
                continue
            for vp in sorted(search_dir.rglob("*.vue")):
                txt = vp.read_text(encoding="utf-8", errors="ignore")
                if "Select" not in vp.stem and "select" not in vp.stem:
                    continue
                g_hits: set[str] = set()
                aliases: dict[str, str] = {}
                directs: dict[str, str] = {}
                for alias, named, mod in _API_IMPORT_RE.findall(txt):
                    if alias and mod in api_gets:
                        aliases[alias] = mod
                    elif named and mod in api_gets:
                        for tok in named.split(","):
                            parts = tok.strip().split(" as ")
                            orig = parts[0].strip()
                            local = parts[-1].strip()
                            if orig in api_gets[mod] and local:
                                directs[local] = api_gets[mod][orig]
                for obj, fn in re.findall(r"\b(\w+)\.(\w+)\(", txt):
                    mod = aliases.get(obj)
                    if mod and fn in api_gets.get(mod, {}):
                        g_hits.add(api_gets[mod][fn])
                for local, ent_g in directs.items():
                    if re.search(r"\b" + re.escape(local) + r"\s*\(", txt):
                        g_hits.add(ent_g)
                if len(g_hits) == 1:
                    comp_to_entity[vp.stem] = next(iter(g_hits))

        for vp in sorted((ui_src / "views").rglob("*.vue")):
            txt = vp.read_text(encoding="utf-8", errors="ignore")
            if "create" not in txt and "Create" not in txt and "update" not in txt and "Update" not in txt:
                continue
            w_aliases: dict[str, str] = {}
            w_directs: dict[str, str] = {}
            g_aliases: dict[str, str] = {}
            g_directs: dict[str, str] = {}
            for alias, named, mod in _API_IMPORT_RE.findall(txt):
                if alias:
                    if mod in api_writes:
                        w_aliases[alias] = mod
                    if mod in api_gets:
                        g_aliases[alias] = mod
                elif named:
                    for tok in named.split(","):
                        parts = tok.strip().split(" as ")
                        orig = parts[0].strip()
                        local = parts[-1].strip()
                        if mod in api_writes and orig in api_writes[mod] and local:
                            w_directs[local] = api_writes[mod][orig]
                        if mod in api_gets and orig in api_gets[mod] and local:
                            g_directs[local] = api_gets[mod][orig]
            matched_w: set[str] = set()
            for obj, fn in re.findall(r"\b(\w+)\.(\w+)\(", txt):
                mod = w_aliases.get(obj)
                if mod and fn in api_writes[mod]:
                    matched_w.add(api_writes[mod][fn])
            for local, ent_w in w_directs.items():
                if re.search(r"\b" + re.escape(local) + r"\s*\(", txt):
                    matched_w.add(ent_w)
            if len(matched_w) != 1:
                continue
            form_ent = next(iter(matched_w))
            local_comps = {local: stem for local, stem in comp_import_re.findall(txt) if stem in comp_to_entity}
            # Map reactive option variables (`fooList.value = await Api.getSimple...()`)
            var_to_ent: dict[str, str] = {}
            for vm in re.finditer(r"(\w+)(?:\.value)?\s*=\s*(?:await\s+)?(\w+)\.(\w+)\(", txt):
                vname, obj, fn = vm.group(1), vm.group(2), vm.group(3)
                mod = g_aliases.get(obj)
                if mod and fn in api_gets[mod]:
                    var_to_ent[vname] = api_gets[mod][fn]
            for vm2 in re.finditer(r"(\w+)(?:\.value)?\s*=\s*(?:await\s+)?(\w+)\(", txt):
                vname, local = vm2.group(1), vm2.group(2)
                if local in g_directs:
                    var_to_ent[vname] = g_directs[local]
            for head, body in form_item_re.findall(txt):
                m_prop = re.search(r'\bprop="([A-Za-z][A-Za-z0-9_]*)"', head)
                if not m_prop:
                    continue
                fprop = m_prop.group(1)
                cands: set[str] = set()
                for local, stem in local_comps.items():
                    if re.search(rf"<{re.escape(local)}\b|<{re.escape(_CAMEL_TO_KEBAB_RE.sub('-', local))}\b", body):
                        cands.add(comp_to_entity[stem])
                for vn, target_e in var_to_ent.items():
                    if re.search(rf"\bin\s+{re.escape(vn)}\b|:data=\"{re.escape(vn)}\"", body):
                        cands.add(target_e)
                if len(cands) == 1:
                    ui_field_refs[(form_ent, fprop)] = next(iter(cands))

    return listable_entities, entity_query_params, unique_svc_entity, ui_field_refs


def _split_top_level_call_args(text: str, open_paren_pos: int) -> list[str] | None:
    """Extract top-level call arguments starting at '(' using balanced parens."""
    depth = 0
    in_arg_start = open_paren_pos + 1
    args: list[str] = []
    i = open_paren_pos
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                tail = text[in_arg_start:i].strip()
                if tail:
                    args.append(tail)
                return args
        elif ch == "," and depth == 1:
            args.append(text[in_arg_start:i].strip())
            in_arg_start = i + 1
        elif ch in "{};" and depth == 1:
            return None
        i += 1
    return None


def _infer_f2_reference(
    entity: str,
    prop: str,
    kind: str,
    ep_ast: ControllerEndpointAST | None,
    *,
    source,
    svc_by_iface: dict[str, str],
    parsed_svc_cache: dict,
    listable_entities: set[str],
    unique_svc_entity: dict[str, str],
    ui_field_refs: dict[tuple[str, str], str],
) -> str:
    """Infer reference_entity using dual-source agreement (backend AST + UI selector + module OpenAPI)."""
    if kind not in {"integer", "id_array"} or prop == "id":
        return ""
    m_stem = re.fullmatch(r"(.+?)(?:Ids|Id)", prop)
    if not m_stem:
        return ""
    stem = m_stem.group(1)
    if not stem or stem in _POLYMORPHIC_REF_STEMS:
        return ""

    # Source A: Backend service AST validation / single-arg PK lookup calls (+ 1-hop helper forwarding)
    backend_hits: set[str] = set()
    if ep_ast and ep_ast.service_iface:
        loaded = _load_parsed_service(ep_ast.service_iface, source, svc_by_iface, parsed_svc_cache)
        if loaded is not None:
            masked, svc_fields, sigs = loaded
            bodies = _collect_reachable_bodies(masked, sigs, ep_ast.service_method)
            all_txt = "\n".join(masked[bs:be] for _mn, _s, bs, be in bodies)
            cap = prop[0].upper() + prop[1:]
            getter_re = re.compile(rf"(?:\b\w+\s*\.\s*)?get{re.escape(cap)}\s*\(\s*\)\s*$")
            dep_call_re = re.compile(r"\b(\w+)\s*\.\s*(validate\w*|get(?!\w*By(?!Id\b))\w*|selectById)\s*\(")

            all_vdefs = _extract_local_var_defs(all_txt, len(all_txt))
            alias_vars = {
                vname
                for vname, vrhs in all_vdefs.items()
                if re.search(rf"\.get{re.escape(cap)}\s*\(\s*\)", _expand_expr_vars(vrhs, all_vdefs, depth=2))
            }

            def _scan_dep_calls(scan_txt: str, var_names: set[str]) -> None:
                for m in dep_call_re.finditer(scan_txt):
                    dep_type = svc_fields.get(m.group(1), "")
                    te = unique_svc_entity.get(dep_type, "")
                    if not te or te == entity:
                        continue
                    c_args = _split_top_level_call_args(scan_txt, m.end() - 1)
                    if not c_args:
                        continue
                    m_name = m.group(2)
                    if m_name.startswith("validate") and not m_name.endswith("Unique"):
                        if not (1 <= len(c_args) <= 2):
                            continue
                    elif len(c_args) != 1:
                        continue
                    arg0 = c_args[0].strip()
                    if getter_re.search(arg0) or arg0 in var_names:
                        backend_hits.add(te)

            _scan_dep_calls(all_txt, alias_vars)

            # 1-hop private helper parameter forwarding: helper(reqVO.getProp()) -> dep.validate...(param_i)
            for h_name, sig_pos, bs, be in bodies[1:]:
                sig_hdr = masked[sig_pos:bs]
                m_params = re.search(r"\(([^)]*)\)", sig_hdr)
                if not m_params or not m_params.group(1).strip():
                    continue
                p_names = [
                    tok.strip().split()[-1].strip("[] ")
                    for tok in m_params.group(1).split(",")
                    if tok.strip()
                ]
                if not p_names:
                    continue
                forwarded_params: set[str] = set()
                for cm in re.finditer(rf"\b{re.escape(h_name)}\s*\(", all_txt):
                    c_args = _split_top_level_call_args(all_txt, cm.end() - 1)
                    if not c_args:
                        continue
                    for p_idx, a_expr in enumerate(c_args):
                        a_s = a_expr.strip()
                        if p_idx < len(p_names) and p_names[p_idx] and (getter_re.search(a_s) or a_s in alias_vars):
                            forwarded_params.add(p_names[p_idx])
                if forwarded_params:
                    _scan_dep_calls(masked[bs:be], forwarded_params)

    # Source B: Vue selector + tiered structural OpenAPI entity match
    ui_hit = ui_field_refs.get((entity, prop), "")
    ui_hits: set[str] = {ui_hit} if (ui_hit and ui_hit != entity) else set()

    mod = entity.split("/", 1)[0]
    sub_prefix = entity.rsplit("/", 1)[0] if entity.count("/") >= 2 else mod
    kebab = _CAMEL_TO_KEBAB_RE.sub("-", stem).lower()
    mod_prefix = mod + "/"
    schema_hits: set[str] = set()

    # Tier 1: exact sub_prefix/mod/entity sibling match
    for cand in (f"{sub_prefix}/{kebab}", f"{mod}/{kebab}", f"{entity}-{kebab}"):
        if cand in listable_entities and cand != entity:
            schema_hits.add(cand)

    # Tier 2: child-to-parent prefix & detail/activity sibling match
    if not schema_hits:
        for cand in listable_entities:
            if cand != entity and entity.startswith(cand + "-") and (cand.endswith("/" + kebab) or cand.endswith("-" + kebab)):
                schema_hits.add(cand)
        if not schema_hits and entity.endswith("-detail") and prop == "lineId":
            sib = entity[:-len("-detail")] + "-line"
            if sib in listable_entities:
                schema_hits.add(sib)
        if not schema_hits and entity.endswith("-activity") and kebab == "config":
            sib = entity[:-len("-activity")] + "-config"
            if sib in listable_entities:
                schema_hits.add(sib)

    def _tier_suffix(prefix: str, kb: str) -> set[str]:
        return {
            cand
            for cand in listable_entities
            if cand != entity
            and cand.startswith(prefix)
            and cand.count("/") <= 2
            and (cand.endswith("/" + kb) or cand.endswith("-" + kb))
        }

    def _tier3_mod_suffix(kb: str) -> set[str]:
        if sub_prefix != mod:
            sub_hits = _tier_suffix(sub_prefix + "/", kb)
            if sub_hits:
                return sub_hits
        return _tier_suffix(mod_prefix, kb)

    # Tier 2.5 / 3: submodule-scoped then module-scoped unique suffix match
    if not schema_hits:
        schema_hits = _tier3_mod_suffix(kebab)

    # Tier 4: directional-prefix normalized match
    if not schema_hits:
        for dir_pfx in ("from-", "to-", "source-", "target-", "bom-"):
            if kebab.startswith(dir_pfx) and len(kebab) > len(dir_pfx):
                k_norm = kebab[len(dir_pfx):]
                cand_t4 = _tier3_mod_suffix(k_norm)
                if cand_t4:
                    schema_hits = cand_t4
                    break

    # Tier 5: cross-module conventional fallbacks
    if not schema_hits and re.search(r"(?:^|[a-z])(?:[Uu]ser)(?:Id|Ids)$", prop):
        for cand in listable_entities:
            if cand.endswith("/user") and cand.startswith("system/"):
                schema_hits.add(cand)
    elif not schema_hits and re.search(r"(?:^|[a-z])(?:[Dd]ept)(?:Id|Ids)$", prop):
        for cand in listable_entities:
            if cand.endswith("/dept") and cand.startswith("system/"):
                schema_hits.add(cand)
    elif not schema_hits and re.search(r"(?:^|[a-z])(?:[Ee]mployee)(?:Id|Ids)$", prop):
        for cand in listable_entities:
            if cand.endswith("/employee") and cand.startswith("hrm/"):
                schema_hits.add(cand)
    elif not schema_hits and re.search(r"Warehouse(?:Id|Ids)$", prop):
        for suffix in (f"{sub_prefix}/warehouse", f"{mod}/wm/warehouse", f"{mod}/warehouse"):
            if suffix in listable_entities and suffix != entity:
                schema_hits.add(suffix)
                break

    if backend_hits and ui_hits and not (backend_hits & ui_hits):
        return ""
    if len(backend_hits) == 1:
        te = next(iter(backend_hits))
        if not ui_hits or te in ui_hits:
            return te
        return ""
    if len(ui_hits) == 1:
        te = next(iter(ui_hits))
        if not schema_hits or te in schema_hits:
            return te
        return ""
    if not backend_hits and not ui_hits and len(schema_hits) == 1:
        return next(iter(schema_hits))
    return ""


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
        if (prop == "areaId" or prop.endswith("AreaId")) and ("地区" in desc or "省" in desc or "区域" in desc):
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
        if prop in {"warehouseId", "sourceWarehouseId", "targetWarehouseId", "fromWarehouseId", "toWarehouseId"}:
            if entity.startswith("wms/") and "/admin-api/wms/warehouse/simple-list" in available_paths:
                return "wms/warehouse"
            if entity.startswith("mes/"):
                mes_wh = f"{entity.split('/', 1)[0]}/wm/warehouse"
                if f"/admin-api/{mes_wh}/simple-list" in available_paths:
                    return mes_wh
                return ""
            if "/admin-api/erp/warehouse/simple-list" in available_paths:
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


def _resolved_resp_props(
    get_path: str,
    get_op: dict,
    schemas: dict,
    ctrl_endpoints: dict[str, ControllerEndpointAST],
    vo_map: dict[str, JavaVOAST],
) -> tuple[dict, JavaVOAST | None]:
    """Return top-level detail response props, synthesizing from package-scoped Java VO when OpenAPI collided."""
    resp_props = _resp_schema_props(get_op, schemas)
    get_ep = ctrl_endpoints.get(f"Get:{get_path}") if get_path else None
    resp_java_vo = vo_map.get(get_ep.response_type) if get_ep and get_ep.response_type else None
    if resp_java_vo and any(f.name not in resp_props for f in resp_java_vo.fields if f.name != "id"):
        resp_props = _synthesize_props_from_java_vo(resp_java_vo, schemas, resp_props)
    return resp_props, resp_java_vo


def _clean_desc_label(desc: str) -> str:
    if not desc:
        return ""
    head = re.split(r"[，,；;（(。!！\n]", desc.strip(), maxsplit=1)[0].strip()
    return head[:40] if 0 < len(head) <= 40 else ""


def _infer_f2_or_extended_ref(
    entity: str,
    fname: str,
    kind: str,
    desc: str,
    available_paths: set[str],
    f2_ctx: dict | None = None,
) -> str:
    ref = _infer_extended_reference(entity, fname, kind, desc, available_paths)
    if ref or not f2_ctx:
        return ref
    return _infer_f2_reference(
        entity,
        fname,
        kind,
        f2_ctx.get("ep_ast"),
        source=f2_ctx.get("source"),
        svc_by_iface=f2_ctx.get("svc_by_iface") or {},
        parsed_svc_cache=f2_ctx.get("parsed_svc_cache") or {},
        listable_entities=f2_ctx.get("listable_entities") or set(),
        unique_svc_entity=f2_ctx.get("unique_svc_entity") or {},
        ui_field_refs=f2_ctx.get("ui_field_refs") or {},
    )


def _detect_1n_readback_projection(
    fname: str,
    raw_fp: dict,
    resp_props: dict,
    schemas: dict,
) -> tuple[str, str]:
    """Detect ``(resp_collection_key, item_id_key)`` for an ``*Ids`` field projected from child rows."""
    if not fname.endswith("Ids") or len(fname) <= 3:
        return ()
    fp = _deref(raw_fp, schemas)
    if fp.get("type") != "array":
        return ()
    it_d = _deref(fp.get("items") or {}, schemas)
    if it_d.get("type") not in {"integer", "number", "string"}:
        return ()
    stem = fname[:-3]
    cands: list[tuple[str, str]] = []
    for col_name, raw_col in resp_props.items():
        col_d = _deref(raw_col, schemas)
        if col_d.get("type") != "array":
            continue
        col_item = _deref(col_d.get("items") or {}, schemas)
        col_props = col_item.get("properties") or {}
        if not col_props:
            continue
        col_sing = col_name[:-1] if col_name.endswith("s") else col_name
        if not col_sing or not stem.lower().startswith(col_sing.lower()):
            continue
        rem = stem[len(col_sing):]
        key_cands = []
        if rem:
            key_cands.append(rem[0].lower() + rem[1:] + "Id")
        key_cands.extend((f"{stem}Id", f"{col_sing}Id", "id"))
        for kc in key_cands:
            if kc in col_props and kc != "id":
                cands.append((col_name, kc))
                break
    return cands[0] if len(cands) == 1 else ()


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
    readback_projection: tuple[str, str] = (),
    derived_display_fields: tuple[str, ...] = (),
    f2_ctx: dict | None = None,
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
        java_elem = java_field.list_element_class if java_field else ""
        if items.get("type") == "string" and java_elem not in {"Long", "Integer", "Short", "Byte"}:
            kind = "string_array"
        else:
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

    ref_entity = _infer_f2_or_extended_ref(entity, fname, kind, desc, available_paths, f2_ctx)
    max_len = prop.get("maxLength") if type(prop.get("maxLength")) is int else None
    min_val = prop.get("minimum") if type(prop.get("minimum")) in (int, float) else None
    max_val = prop.get("maximum") if type(prop.get("maximum")) in (int, float) else None
    min_len = prop.get("minLength") if type(prop.get("minLength")) is int and prop.get("minLength") > 0 else None
    if java_field is not None:
        if min_val is None and java_field.minimum is not None:
            min_val = java_field.minimum
        if max_val is None and java_field.maximum is not None:
            max_val = java_field.maximum
        if min_len is None and java_field.min_length is not None:
            min_len = java_field.min_length
        if max_len is None and java_field.max_length is not None:
            max_len = java_field.max_length
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
            is_hierarchy_parent=(fname == "parentId"),
            readback_projection=readback_projection,
            derived_display_fields=derived_display_fields,
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
    f2_ctx: dict | None = None,
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
                    f2_ctx=f2_ctx,
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
            f2_ctx=f2_ctx,
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
    """Reconstruct properties dict from exact Java AST VO when OpenAPI schema name collided."""
    out: dict[str, dict] = {}
    for jf in java_vo.fields:
        m_desc = _SCHEMA_DESC_RE.search(jf.raw_annotations or "")
        own_desc = m_desc.group(1).strip() if m_desc else ""
        if jf.name in fallback_props and not fallback_props.get(jf.name, {}).get("$ref"):
            prop = dict(fallback_props[jf.name])
            if own_desc:
                prop["description"] = own_desc
            out[jf.name] = prop
            continue
        desc = own_desc or jf.name
        jt = jf.java_type.split("<", 1)[0].strip()
        if jt in {"Long", "Integer", "Short", "Byte", "long", "int"}:
            out[jf.name] = {"type": "integer", "description": desc}
        elif jt in {"BigDecimal", "Double", "Float", "double", "float"}:
            out[jf.name] = {"type": "number", "description": desc}
        elif jt in {"Boolean", "boolean"}:
            out[jf.name] = {"type": "boolean", "description": desc}
        elif jt == "LocalDateTime":
            out[jf.name] = {"type": "string", "format": "date-time", "description": desc}
        elif jt == "LocalDate":
            out[jf.name] = {"type": "string", "format": "date", "description": desc}
        elif jt == "String":
            out[jf.name] = {"type": "string", "description": desc}
        elif jt in {"List", "Set", "Collection"}:
            out[jf.name] = {"type": "array", "items": {"type": "integer"}, "description": desc}
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


def _has_unresolvable_required_page_param(entity: str, paths: dict) -> bool:
    """Return True when /page requires a parent task key and no unparametrized list endpoint exists."""
    page_op = (paths.get(f"/admin-api/{entity}/page") or {}).get("get")
    if not isinstance(page_op, dict):
        return False
    req_q = {
        p.get("name") for p in (page_op.get("parameters") or ())
        if isinstance(p, dict) and p.get("in") == "query" and p.get("required") is True
        and p.get("name") not in {"pageNo", "pageSize"}
    }
    if "taskId" not in req_q:
        return False
    for alt in ("list", "simple-list", "list-all-simple", "my-page"):
        alt_op = (paths.get(f"/admin-api/{entity}/{alt}") or {}).get("get")
        if not isinstance(alt_op, dict):
            continue
        alt_req = [
            p.get("name") for p in (alt_op.get("parameters") or ())
            if isinstance(p, dict) and p.get("in") == "query" and p.get("required") is True
            and p.get("name") not in {"pageNo", "pageSize"}
        ]
        if not alt_req:
            return False
    return True


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
                for suffix in ("ServiceImpl.java", "ApiImpl.java")
                for path in source._java_files(suffix)
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
    _do_to_table, mapper_to_table = _collect_table_mappings(source, do_texts=do_texts)
    svc_by_iface = {
        stem[:-len("Impl")]: txt for stem, txt in svc_entries if stem.endswith("Impl")
    }
    parsed_svc_cache: dict[str, tuple[str, dict[str, str], dict[str, list[tuple[int, int, int]]]]] = {}
    listable_entities, entity_query_params, unique_svc_entity, ui_field_refs = _compile_f2_ref_indexes(
        paths, ctrl_endpoints, source, svc_by_iface, parsed_svc_cache, ui=ui,
    )

    def _companion_readback_hint(
        mapper_type: str,
        _table: str,
        main_ep_path: str,
        *,
        payload_fk_field: str = "",
        expect_field: str = "",
    ) -> tuple[str, str, str]:
        comp_ent = unique_svc_entity.get(mapper_type, "")
        main_ent = main_ep_path.rsplit("/", 1)[0] if "/" in main_ep_path else main_ep_path
        if not comp_ent or comp_ent == main_ent:
            return "", "", ""
        last_seg = main_ent.rsplit("/", 1)[-1]
        parts = [seg for seg in last_seg.split("-") if seg]
        if parts:
            camel_last = parts[0] + "".join(p.capitalize() for p in parts[1:])
            cq_cands = [f"{camel_last}Id"]
            if len(parts) > 1 and f"{parts[-1]}Id" not in cq_cands:
                cq_cands.append(f"{parts[-1]}Id")
            for cand_q in cq_cands:
                q_kebab = _CAMEL_TO_KEBAB_RE.sub("-", cand_q).lower()
                for tail in ("list", "simple-list", "list-all-simple", f"list-by-{q_kebab}", "page"):
                    op_dict = (paths.get(f"/admin-api/{comp_ent}/{tail}") or {}).get("get")
                    if not isinstance(op_dict, dict):
                        continue
                    params_list = [q for q in (op_dict.get("parameters") or ()) if isinstance(q, dict) and q.get("in") == "query"]
                    q_names = {q.get("name") for q in params_list if q.get("name")}
                    req_others = [
                        q.get("name") for q in params_list
                        if q.get("required") is True and q.get("name") not in {"pageNo", "pageSize", cand_q}
                    ]
                    if not req_others and cand_q in q_names:
                        return comp_ent, cand_q, ""
        if payload_fk_field and expect_field:
            comp_get = (paths.get(f"/admin-api/{comp_ent}/get") or {}).get("get")
            if isinstance(comp_get, dict):
                params_list = [q for q in (comp_get.get("parameters") or ()) if isinstance(q, dict) and q.get("in") == "query"]
                q_names = {q.get("name") for q in params_list if q.get("name")}
                req_others = [q.get("name") for q in params_list if q.get("required") is True and q.get("name") != "id"]
                if not req_others and "id" in q_names:
                    return comp_ent, f"payload:{payload_fk_field}", expect_field
        prop_op = (paths.get(f"/admin-api/{main_ent}/get-property") or {}).get("get")
        if isinstance(prop_op, dict):
            params_list = [q for q in (prop_op.get("parameters") or ()) if isinstance(q, dict) and q.get("in") == "query"]
            q_names = {q.get("name") for q in params_list if q.get("name")}
            req_others = [q.get("name") for q in params_list if q.get("required") is True and q.get("name") != "id"]
            if not req_others and "id" in q_names:
                p_props = _resp_schema_props(prop_op, schemas)
                comp_parts = [p for p in comp_ent.rsplit("/", 1)[-1].split("-") if p]
                comp_compact = "".join(comp_parts).lower()
                for pk, raw_p in p_props.items():
                    p_schema = _deref(raw_p, schemas)
                    if p_schema.get("type") != "array":
                        continue
                    pk_low = pk.lower().rstrip("s")
                    item_ref = str((p_schema.get("items") or {}).get("$ref") or "").rsplit("/", 1)[-1].lower()
                    if (comp_compact and comp_compact in item_ref) or (pk_low and pk_low in comp_parts):
                        return f"get-property:{main_ent}", "id", ""
        return "", "", ""

    def _validation_rules_for_fields(java_vo: JavaVOAST | None, active_names: set[str]) -> tuple[ValidationRuleSpec, ...]:
        if java_vo is None or not java_vo.validation_rules:
            return ()
        kept: list[ValidationRuleSpec] = []
        for rule in java_vo.validation_rules:
            if not set(rule.fields) <= active_names:
                continue
            if rule.condition_field and rule.condition_field not in active_names:
                continue
            kept.append(rule)
        return tuple(kept)
    known_java_enums = frozenset(k for k, v in enum_labels.items() if v)
    enum_by_snake = {
        re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", cls_name.removesuffix("Enum")).lower(): vals
        for cls_name, vals in enum_labels.items()
        if vals
    }

    def _resolve_enum_zero_label(f_spec: ConventionFieldSpec) -> str | None:
        et = f_spec.enum_type
        if not et:
            return None
        if et == "common_status":
            return "开启"
        if et.startswith("__java_enum__:"):
            cls_name = et.split(":", 1)[1]
            return (enum_labels.get(cls_name) or {}).get("0")
        vals = enum_by_snake.get(et)
        if vals and "0" in vals:
            return vals["0"]
        for k_snake, v_map in enum_by_snake.items():
            if (et.endswith("_" + k_snake) or k_snake.endswith("_" + et)) and "0" in v_map:
                return v_map["0"]
        return None

    def _filter_create_defaults(
        raw_defs: tuple[CompiledFieldDefault, ...],
        spec_map: dict[str, ConventionFieldSpec],
        java_fields: dict[str, JavaFieldAST] | None = None,
    ) -> tuple[CompiledFieldDefault, ...]:
        kept: list[CompiledFieldDefault] = []
        for d in raw_defs:
            f_sp = spec_map.get(d.field)
            if f_sp is None:
                continue
            jf_def = java_fields.get(d.field) if java_fields else None
            if d.value == [] and (
                (jf_def is not None and jf_def.non_empty)
                or (f_sp.min_length is not None and f_sp.min_length >= 1)
            ):
                continue
            if d.kind == "actor":
                kept.append(d)
                continue
            zero_lbl = _resolve_enum_zero_label(f_sp) if (f_sp.enum_type and not f_sp.reference_entity and str(d.value).strip() == "0") else None
            if is_rejected_ui_sentinel(
                d.value,
                is_fk=bool(f_sp.reference_entity),
                is_enum=bool(f_sp.enum_type),
                enum_zero_label=zero_lbl,
                is_hierarchy_parent=f_sp.is_hierarchy_parent,
            ):
                continue
            if zero_lbl and str(d.display).strip() in {"", "0"}:
                d = replace(d, display=zero_lbl)
            kept.append(d)
        return tuple(kept)

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
        return (
            any(entity == p or entity.startswith(p) for p in _BLOCKED_ENTITY_PREFIXES)
            or entity.endswith(("/permission", "-permission"))
        )

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
        resp_props, _ = _resolved_resp_props(read_path or read_list_path, resp_op, schemas, ctrl_endpoints, vo_map)
        name_fields = tuple(k for k in _NAME_CANDIDATES if k in resp_props) or ("name",)
        qparams = [q.get("name") for q in (put.get("parameters") or ()) if isinstance(q, dict) and isinstance(q.get("name"), str)]
        bprops: dict = {}
        if "requestBody" in put:
            try:
                bschema = _deref(next(iter(put["requestBody"]["content"].values()))["schema"], schemas)
                bprops = bschema.get("properties") or {}
            except (KeyError, StopIteration, TypeError, AttributeError):
                bprops = {}

        ep_st_any = ctrl_endpoints.get(f"Put:{ent_part}")
        st_side_effects: tuple[str, ...] = ()
        if ep_st_any is not None and source is not None and getattr(source, "root", None):
            st_safe, _, _, _, st_side_effects = _analyze_service_method(
                source,
                ep_st_any,
                svc_by_iface=svc_by_iface,
                parsed_svc_cache=parsed_svc_cache,
                mode="status",
            )
            if not st_safe:
                continue
            if tail == "enable" and f"Put:{entity}/disable" in ctrl_endpoints:
                dis_safe, _, _, _, dis_effects = _analyze_service_method(
                    source,
                    ctrl_endpoints[f"Put:{entity}/disable"],
                    svc_by_iface=svc_by_iface,
                    parsed_svc_cache=parsed_svc_cache,
                    mode="status",
                )
                if not dis_safe:
                    continue
                st_side_effects = tuple(dict.fromkeys((*st_side_effects, *dis_effects)))

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
                    side_effects=st_side_effects,
                )
            elif ("id" in bprops and "status" in bprops) or ("id" in qparams and "status" in qparams):
                # Require verified two-state CommonStatusEnum (exclude ErpAuditStatus & multi-stage workflow enums)
                ep_st = ep_st_any
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
                    side_effects=st_side_effects,
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
                side_effects=st_side_effects,
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
                    side_effects=st_side_effects,
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
                side_effects=st_side_effects,
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
                side_effects=st_side_effects,
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
        raw_req_props = req_schema.get("properties") or {}
        assert_props = java_vo.assert_true_props if java_vo else frozenset()
        req_props = {k: v for k, v in raw_req_props.items() if k not in assert_props}
        resp_props, resp_java_vo_top = _resolved_resp_props(get_path, get_op, schemas, ctrl_endpoints, vo_map)
        req_required = set(req_schema.get("required") or ()) - assert_props

        # Detect nested object-array child collections vs opaque config objects
        has_opaque_object, nested_cols = _classify_nested_columns(req_props, schemas)
        if has_opaque_object:
            continue

        is_aggregate = bool(nested_cols)
        if not is_aggregate and not put_resp_ref.endswith("/CommonResultBoolean"):
            continue
        if not is_aggregate and ("id" not in req_props or "id" not in resp_props):
            continue
        if not is_aggregate and _has_unresolvable_required_page_param(entity, paths):
            continue

        projected_map: dict[str, tuple[str, str]] = {}
        if not is_aggregate:
            for fn, rfp in req_props.items():
                if fn != "id" and fn not in resp_props:
                    proj = _detect_1n_readback_projection(fn, rfp, resp_props, schemas)
                    if proj:
                        projected_map[fn] = proj

        dropped_fields: set[str] = set()
        update_companions: tuple[CompanionWriteSpec, ...] = ()
        dynamic_volatiles: set[str] = set()
        if ep_ast is not None:
            col_mapper_hints: set[str] = set()
            if is_aggregate and ep_ast.service_iface:
                ep_stem = ep_ast.service_iface.removesuffix("Service")
                for c_name, _ in nested_cols:
                    c_sing = c_name[:-1] if c_name.endswith("s") else c_name
                    c_cap = c_sing[0].upper() + c_sing[1:] if c_sing else ""
                    if c_cap:
                        col_mapper_hints.add(f"{ep_stem}{c_cap}Mapper")
                        col_mapper_hints.add(f"{ep_stem[:-1] if ep_stem.endswith('s') else ep_stem}{c_cap}Mapper")
            has_direct_id_list_sync = (
                not is_aggregate
                and any(
                    fname in resp_props
                    and isinstance(raw_fp, dict)
                    and raw_fp.get("type") == "array"
                    and isinstance(raw_fp.get("items"), dict)
                    and raw_fp["items"].get("type") == "integer"
                    for fname, raw_fp in req_props.items()
                )
            )
            a_ctx: dict = {}
            safe_svc, uncond_nulls, dropped_fields, cond_side_effect_fields, raw_side_effects = _analyze_service_method(
                source,
                ep_ast,
                allow_child_mappers=is_aggregate,
                allow_1n_child_sync=bool(projected_map) or has_direct_id_list_sync,
                do_fields=do_fields,
                vo_field_names=tuple(req_props.keys()),
                svc_by_iface=svc_by_iface,
                parsed_svc_cache=parsed_svc_cache,
                mapper_to_table=mapper_to_table,
                child_mapper_names=frozenset(col_mapper_hints),
                companion_readback_fn=_companion_readback_hint,
                analysis_ctx=a_ctx,
            )
            if not safe_svc:
                continue
            update_companions = tuple(x for x in raw_side_effects if isinstance(x, CompanionWriteSpec))
            dynamic_volatiles = set(a_ctx.get("volatiles") or ())
            if uncond_nulls or dropped_fields or cond_side_effect_fields:
                immutable_fields[entity] = frozenset(uncond_nulls | dropped_fields | cond_side_effect_fields)
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
        f2_ctx = {
            "ep_ast": ep_ast,
            "source": source,
            "svc_by_iface": svc_by_iface,
            "parsed_svc_cache": parsed_svc_cache,
            "listable_entities": listable_entities,
            "unique_svc_entity": unique_svc_entity,
            "ui_field_refs": ui_field_refs,
        }

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
                proj = projected_map.get(fname, ())
                if fname not in resp_props and not proj:
                    disqualified = True
                    break
                derived_disp = ()
                if fname.endswith("Id") and len(fname) > 2:
                    fk_stem = fname[:-2]
                    fk_stems = [fk_stem]
                    if fk_stem.endswith("User") and len(fk_stem) > 4:
                        fk_stems.append(fk_stem[:-4])
                    disp_cands: list[str] = []
                    for st in fk_stems:
                        for sfx in ("Name", "Nickname", "Title", "Label"):
                            cand = f"{st}{sfx}"
                            if (
                                cand in resp_props
                                and cand not in req_props
                                and cand not in _VOLATILE
                                and cand not in disp_cands
                                and _deref(resp_props[cand], schemas).get("type") == "string"
                            ):
                                disp_cands.append(cand)
                    derived_disp = tuple(disp_cands)
                elif fname.endswith("Ids") and len(fname) > 3:
                    fk_stem = fname[:-3]
                    fk_stems = [fk_stem]
                    if fk_stem.endswith("User") and len(fk_stem) > 4:
                        fk_stems.append(fk_stem[:-4])
                    disp_cands = []
                    for st in fk_stems:
                        for sfx in ("Name", "Names", "Nickname", "Nicknames", "Title", "Titles", "Label", "Labels"):
                            cand = f"{st}{sfx}"
                            if (
                                cand in resp_props
                                and cand not in req_props
                                and cand not in _VOLATILE
                                and cand not in disp_cands
                                and _deref(resp_props[cand], schemas).get("type") in {"string", "array"}
                            ):
                                disp_cands.append(cand)
                        obj_cand = f"{st}s"
                        if (
                            obj_cand in resp_props
                            and obj_cand not in req_props
                            and obj_cand not in _VOLATILE
                            and obj_cand not in disp_cands
                        ):
                            obj_schema = _deref(resp_props[obj_cand], schemas)
                            if obj_schema.get("type") == "array":
                                obj_item = _deref(obj_schema.get("items") or {}, schemas)
                                if obj_item.get("type") == "object" or bool(obj_item.get("properties")):
                                    disp_cands.append(obj_cand)
                    derived_disp = tuple(disp_cands)
                f_spec = _build_field_spec(
                    entity, fname, raw_fp, schemas=schemas, available_paths=available_paths,
                    java_field=jf, req_required=req_required, ent_labels=ent_labels,
                    ent_dicts=ent_dicts, ent_scales=ent_scales, has_java_vo=bool(java_vo),
                    status_in_status_specs=(entity in statuses),
                    known_java_enums=known_java_enums,
                    readback_projection=proj,
                    derived_display_fields=derived_disp,
                    f2_ctx=f2_ctx,
                )
                if f_spec is None:
                    disqualified = True
                    break
                field_specs.append(f_spec)
            if not disqualified and field_specs:
                extra_vol = frozenset({"minStock", "maxStock"}) if entity == "mes/md/item" else frozenset()
                if projected_map:
                    req_field_names = {f.name for f in field_specs}
                    child_vols: set[str] = set()
                    for coll_name, _item_key in projected_map.values():
                        stem = coll_name[:-1].lower() if coll_name.endswith("s") and len(coll_name) > 2 else coll_name.lower()
                        for rk in resp_props:
                            if rk != coll_name and rk != "id" and rk not in req_field_names and rk.lower().startswith(stem):
                                child_vols.add(rk)
                    if child_vols:
                        extra_vol = extra_vol | frozenset(child_vols)
                val_rules = _validation_rules_for_fields(java_vo, {f.name for f in field_specs})
                updates[entity] = ConventionUpdateSpec(
                    entity=entity, label=tag, fields=tuple(field_specs),
                    id_field="id", name_fields=name_fields, volatile_fields=_VOLATILE | extra_vol | frozenset(dynamic_volatiles),
                    side_effects=update_companions,
                    validation_rules=val_rules,
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
                    if fname not in resp_props:
                        disqualified = True
                        break
                    jf = java_fields.get(fname)
                    rjf = resp_java_fields.get(fname)
                    col_spec = _compile_child_collection(
                        entity, fname, raw_fp, schemas=schemas, available_paths=available_paths,
                        vo_map=vo_map, java_list_elem_vo=(jf.list_element_class if jf else ""),
                        resp_list_elem_vo=(rjf.list_element_class if rjf else ""),
                        resp_arr_prop=resp_props.get(fname) or {},
                        ent_labels=ent_labels, ent_dicts=ent_dicts, ent_scales=ent_scales, depth=1,
                        known_java_enums=known_java_enums,
                        f2_ctx=f2_ctx,
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
                    f2_ctx=f2_ctx,
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
                val_rules = _validation_rules_for_fields(java_vo, {f.name for f in parent_specs})
                aggregates[entity] = CompiledAggregateUpdateSpec(
                    entity=entity,
                    label=tag,
                    fields=tuple(parent_specs),
                    collections=tuple(child_specs),
                    id_field=id_field,
                    name_fields=name_fields,
                    volatile_fields=_VOLATILE | {"status", "no"} | frozenset(dynamic_volatiles) | ({"id", "recordType", "changeReason", "effectTime"} if is_salary_special else frozenset()),
                    server_computed_fields=frozenset(computed_parent),
                    omit_root_id_in_payload=is_salary_special,
                    composite_id_fields=comp_ids if len(comp_ids) > 1 else (),
                    readback_strategy="change_record_id" if is_salary_special else "same_record",
                    side_effects=update_companions,
                    validation_rules=val_rules,
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
        raw_req_props = req_schema.get("properties") or {}
        assert_props = java_vo.assert_true_props if java_vo else frozenset()
        req_props = {k: v for k, v in raw_req_props.items() if k not in assert_props}
        has_opaque_object, nested_cols = _classify_nested_columns(req_props, schemas)
        if has_opaque_object:
            continue
        if nested_cols and not enable_cat5_master_detail_creates:
            continue
        resp_props, resp_java_vo_create = _resolved_resp_props(get_path, get_op, schemas, ctrl_endpoints, vo_map)
        req_required = set(req_schema.get("required") or ()) - assert_props
        java_fields = {f.name: f for f in java_vo.fields} if java_vo else {}
        col_names = {c for c, _ in nested_cols}
        svc_req_cols = _service_required_child_cols(ep_ast, col_names, parsed_svc_cache) if ep_ast is not None else set()
        # Filter out optional nested collections that GET response does not return for roundtrip readback
        dropped_optional_cols: set[str] = set()
        req_col_missing = False
        kept_nested_cols: list[tuple[str, dict]] = []
        for cname, cprop in nested_cols:
            if cname not in resp_props:
                jf_c = java_fields.get(cname)
                c_is_req = cname in req_required or (jf_c and jf_c.required) or cname in svc_req_cols
                if c_is_req:
                    req_col_missing = True
                    break
                dropped_optional_cols.add(cname)
            else:
                kept_nested_cols.append((cname, cprop))
        if req_col_missing:
            continue
        nested_cols = kept_nested_cols
        col_names = {c for c, _ in nested_cols}
        create_dropped: set[str] = set(dropped_optional_cols)
        create_companions: tuple[CompanionWriteSpec, ...] = ()
        create_analysis_ctx: dict[str, object] = {}
        if ep_ast is not None:
            col_mapper_hints_c: set[str] = set()
            if nested_cols and ep_ast.service_iface:
                ep_stem = ep_ast.service_iface.removesuffix("Service")
                for c_name, _ in nested_cols:
                    c_sing = c_name[:-1] if c_name.endswith("s") else c_name
                    c_cap = c_sing[0].upper() + c_sing[1:] if c_sing else ""
                    if c_cap:
                        col_mapper_hints_c.add(f"{ep_stem}{c_cap}Mapper")
                        col_mapper_hints_c.add(f"{ep_stem[:-1] if ep_stem.endswith('s') else ep_stem}{c_cap}Mapper")
            safe_svc, _, svc_dropped, _, raw_create_effects = _analyze_service_method(
                source,
                ep_ast,
                allow_child_mappers=bool(nested_cols and enable_cat5_master_detail_creates),
                do_fields=do_fields,
                vo_field_names=tuple(req_props.keys()),
                svc_by_iface=svc_by_iface,
                parsed_svc_cache=parsed_svc_cache,
                mapper_to_table=mapper_to_table,
                child_mapper_names=frozenset(col_mapper_hints_c),
                companion_readback_fn=_companion_readback_hint,
                analysis_ctx=create_analysis_ctx,
            )
            if not safe_svc:
                continue
            create_dropped |= svc_dropped
            create_companions = tuple(x for x in raw_create_effects if isinstance(x, CompanionWriteSpec))
            if col_names and ep_ast.service_iface:
                # Re-run svc_req_cols with parsed_svc_cache populated
                svc_req_cols = _service_required_child_cols(ep_ast, col_names, parsed_svc_cache)

        resp_java_vo = resp_java_vo_create if nested_cols else None
        resp_java_fields = {f.name: f for f in resp_java_vo.fields} if resp_java_vo else {}
        ent_labels = ui_labels.get(entity, {})
        ent_dicts = ui_dicts.get(entity, {})
        ent_scales = ui_scales.get(entity, {})
        raw_child_defaults = form_child_defaults.get(entity, ())
        f2_ctx = {
            "ep_ast": ep_ast,
            "source": source,
            "svc_by_iface": svc_by_iface,
            "parsed_svc_cache": parsed_svc_cache,
            "listable_entities": listable_entities,
            "unique_svc_entity": unique_svc_entity,
            "ui_field_refs": ui_field_refs,
        }

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
                    f2_ctx=f2_ctx,
                )
                if col_spec is None:
                    disqualified = True
                    break
                col_field_map = {cf.name: cf for cf in col_spec.fields}
                filtered_cdefs = _filter_create_defaults(raw_child_defaults, col_field_map)
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
                f2_ctx=f2_ctx,
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
        filtered_defaults = _filter_create_defaults(raw_defaults, spec_by_name, java_fields=java_fields)
        computed = frozenset(k for k in req_props if k != "id" and k not in col_names and k not in dropped_optional_cols and k not in resp_props)
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
        val_rules_c = _validation_rules_for_fields(java_vo, {f.name for f in field_specs})
        creates[entity] = CompiledCreateSpec(
            entity=entity,
            label=tag,
            fields=tuple(field_specs),
            defaults=filtered_defaults,
            collections=tuple(child_specs),
            id_field="id",
            name_fields=name_fields,
            server_computed_fields=computed,
            write_only_fields=frozenset(create_analysis_ctx.get("transformed") or ()),
            conditional_required_fn=cond_req,
            side_effects=create_companions,
            validation_rules=val_rules_c,
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
