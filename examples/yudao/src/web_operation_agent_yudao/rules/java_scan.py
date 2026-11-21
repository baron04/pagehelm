"""Java source scanning primitives for yudao controllers and service implementations."""
from __future__ import annotations

from dataclasses import dataclass
import re

_ENUM_CONSTANT = re.compile(r'\b([A-Z][A-Z0-9_]+)\(\s*-?\d+\s*,(?:[^"()]*\([^()]*\)[^"()]*,|[^"();]+,)?\s*"([^"]+)"')
_ENUM_CODE_VALUE = re.compile(r'\b[A-Z][A-Z0-9_]+\(\s*(-?\d+)\s*,(?:[^"()]*\([^()]*\)[^"()]*,|[^"();]+,)?\s*"([^"]+)"')
_ENUM_STR_CODE_VALUE = re.compile(r'\b[A-Z][A-Z0-9_]+\(\s*"([^"]+)"\s*,\s*"([^"]+)"')
_ENUM_NAME = re.compile(r"\b(\w+Enum)\.([A-Z][A-Z0-9_]+)\.getName\(\)")
_DICT_CONST_DECL = re.compile(r'\bString\s+([A-Z][A-Z0-9_]+)\s*=\s*"([^"]+)"')
_RESP_VO_FIELD = re.compile(
    r"^[ \t]*private\s+(?!static\b|final\b)([\w<>?.,\[\]\s]+?)\s+(\w+)\s*(?:=[^;]*)?;",
    re.M,
)
_DICT_FORMAT_ANN = re.compile(r'@DictFormat\(\s*(?:[\w.]+\.)?(?:([A-Z][A-Z0-9_]+)|"([^"]+)")\s*\)')
_ENUM_LINK_HINT = re.compile(r"(?:\{@link\s+|参见\s*)(?:[\w.]+\.)?([A-Za-z0-9_]+Enum)\b(?!#)")
_SCALAR_VO_TYPES = frozenset({"Integer", "Long", "Short", "Byte", "String", "Boolean", "int", "long", "boolean"})


def parse_resp_vo_code_bindings(text: str, dict_consts: dict[str, str]) -> dict[str, tuple[str, str]]:
    """Extract field -> ('dict', dict_type) | ('enum', enum_class) from a *RespVO.java file."""
    bindings: dict[str, tuple[str, str]] = {}
    for m in _RESP_VO_FIELD.finditer(text):
        field_type = m.group(1).strip()
        field = m.group(2)
        if field_type not in _SCALAR_VO_TYPES:
            continue
        start = m.start()
        sep_semi = text.rfind(";", 0, start)
        sep_brace = text.rfind("{", 0, start)
        sep = max(sep_semi, sep_brace)
        nl = text.find("\n", sep) if sep >= 0 else -1
        cut = (nl + 1) if 0 <= nl < start else max(sep + 1, 0)
        line_end = text.find("\n", m.end())
        same_line = text[m.end():line_end if line_end >= 0 else len(text)]
        ctx = text[max(cut, start - 600):start] + "\n" + same_line
        d = _DICT_FORMAT_ANN.search(ctx)
        if d:
            const_name, literal_type = d.group(1), d.group(2)
            dtype = literal_type or dict_consts.get(const_name or "", "")
            if dtype:
                bindings[field] = ("dict", dtype)
                continue
        e = _ENUM_LINK_HINT.search(ctx)
        if e:
            bindings[field] = ("enum", e.group(1))
    return bindings
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

#: `<` opens an angle bracket only in generic syntax (after a Capitalized type
#: name, a closing `>`, `?`, `,` or a bracket) -- not in `a < b`.
_GENERIC_LT = re.compile(r"(?:[A-Z][\w$]*|[>\?\[\],])\s*$")
_COLL_PARAM = re.compile(r"\b(?:Collection|List|Set|Map|Iterable)\b|\.\.\.|\[\]")
_COLL_ARG = re.compile(
    r"\b(?:ids|list|set|collection|map)\b|getIds\(|getList\(|IdList|toSet\(|"
    r"toList\(|convertList|convertSet|emptyIfNull|List\.of|Set\.of|Collections\.|"
    r"Arrays\.asList|new\s+\w*(?:List|Set|Map)\b", re.I)
_SCALAR_ARG = re.compile(
    r"getId\(|getNo\(|getStatus\(|getType\(|getCount\(|\.toString\(\)|"
    r"Long\.|Integer\.|String\.|^[\d'\"]|^true$|^false$|^null$")
_HEAD_BAD_WORD = re.compile(
    r"\b(?:new|return|throw|if|else|for|while|switch|case|do|assert|break|"
    r"continue|try|catch|finally|import|package|super|this|enum|class|"
    r"interface|record|yield|instanceof)\b")
_TYPE_DECL = re.compile(r"\b(?:class|interface|enum|record|@interface)\s+\w+")
_ANON_NEW = re.compile(r"\bnew\s+[\w.$<>\[\]]+(?:\s*\([^()]*\))?\s*\{")
#: Catch types that cover the `ServiceException` family `exception(...)` throws.
#: Anything narrower is a sibling type and does not catch it; anything unknown
#: is treated as not covering, so the refusal is kept (conservative).
_SWALLOW_TYPES = {"Exception", "RuntimeException", "Throwable", "ServiceException"}

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


@dataclass
class _Refusal:
    code: str
    message: str
    condition: str
    path: str
    start: int
    end: int


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


def _resolve_overloads(sigs: dict, arities: dict, ptypes: dict,
                       called: str, arity: int | None, first_arg: str) -> list[tuple[str, int]]:
    """Resolve same-class callee overloads by argument count and collection vs scalar hint."""
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
                        if ptypes[span] and bool(_COLL_PARAM.search(ptypes[span][0])) == tag]
            if narrowed:
                return narrowed
    return matched


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
