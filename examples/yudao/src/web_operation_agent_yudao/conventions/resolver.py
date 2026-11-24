"""Per-turn convention conflict resolution against target page vocabulary.

Conventions remain active by default unless the turn resolves to a non-target entity or
page whose UI/schema vocabulary (columns, filters, filter options, dictionaries, tabs)
uses the convention term with a different local meaning.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable, Sequence

from web_operation_agent.goal.data import DataRefused

#: Hub/navigation shell routes that never carry business page vocabulary on their own.
_HUB_ROUTES: frozenset[str] = frozenset({"", "/", "/index", "/home", "/login", "/404", "/403"})
#: Trailing unit suffixes (`(元)`, `（天）`) stripped before exact/compound matching.
_UNIT_SUFFIX = re.compile(r"[（(][^（）()]{1,12}[）)]\s*$")
#: Generic UI suffixes stripped when deriving a candidate menu/entity stem.
_UI_SUFFIX = re.compile(r"(?:信息管理|管理|列表|台账|记录|查询|中心|设置|配置|申请|明细|详情)+$")
#: Module/group generic suffixes stripped from parent breadcrumbs.
_MODULE_SUFFIX = re.compile(r"(?:系统|管理|平台|模块|中心|协同|服务)+$")
#: Generic single words that never disambiguate a target entity on their own.
_GENERIC_STEMS: frozenset[str] = frozenset({
    "记录", "列表", "管理", "明细", "详情", "信息", "统计", "配置", "设置", "类型", "状态",
    "全部", "我的", "首页", "数据", "查询", "工作台", "看板", "汇总", "报表", "日志", "目录",
    "分类", "分组", "标签", "模板", "规则", "审核", "审批", "流程", "任务", "通知", "消息",
})
_MEANING_ENTITY = re.compile(r"^\s*([a-z][a-z0-9-]*/[a-z0-9/_-]+)\b")
_MEANING_RECORD_SLUG = re.compile(
    r"^\s*([a-z][a-z0-9-]*/[a-z0-9/_-]+)(?=\s+(?:records?|items?|rows?|entries|whose|where)\b)"
)


@dataclass(frozen=True)
class SuppressedConvention:
    """One convention suppressed for a turn because a non-target page uses its words."""

    convention: object
    matched_words: tuple[str, ...]
    target_entities: tuple[str, ...] = ()
    target_routes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConventionResolution:
    """Result of resolving deployment conventions for one turn."""

    active: tuple[object, ...]
    suppressed: tuple[SuppressedConvention, ...]
    target_entities: tuple[str, ...] = ()
    target_routes: tuple[str, ...] = ()
    source: str = "unresolved"


def _clean_label(raw: object) -> str:
    return " ".join(str(raw or "").split()).strip().rstrip(":：").strip()


def _strip_units(word: str) -> str:
    prev = None
    cur = word
    while prev != cur:
        prev = cur
        cur = _UNIT_SUFFIX.sub("", cur).strip().rstrip(":：").strip()
    return cur


def _compact(raw: object) -> str:
    return re.sub(r"\s+", "", str(raw or "")).lower()


def _normalize_route(route: object) -> str:
    return str(route or "").split("#", 1)[0].split("?", 1)[0].rstrip("/")


def _is_valid_boundary(label: str, term: str, start: int, end: int, working: str) -> bool:
    if not term:
        return False
    if term.isascii():
        if start > 0 and working[start - 1].isalnum():
            return False
        if end < len(working) and working[end].isalnum():
            return False
    # Exclude cross-morpheme Chinese false positives where adjacent characters form an
    # unrelated lexical head rather than a compound of `term`.
    if term == "客户" and end < len(working) and working[end] == "端":
        return False
    if term == "产品" and start >= 1 and working[start - 1 : end] == "资产品":
        return False
    if term.endswith("单") and end < len(working) and working[end] == "价":
        return False
    if term == "公告" and start >= 2 and working[start - 2 : end] == "通知公告":
        return False
    return True


def label_matches_term(
    label: str,
    term: str,
    *,
    sibling_longer_terms: tuple[str, ...] = (),
) -> bool:
    """Return True if `label` matches `term` exactly or as a boundary-valid compound."""
    raw_lbl = _clean_label(label)
    raw_term = _clean_label(term)
    clean_lbl = _strip_units(raw_lbl)
    clean_term = _strip_units(raw_term)
    if not clean_lbl or not clean_term:
        return False
    if clean_lbl == clean_term or raw_lbl == raw_term:
        return True

    working = clean_lbl
    longer = sorted(
        {s for s in (_strip_units(_clean_label(t)) for t in sibling_longer_terms)
         if len(s) > len(clean_term) and clean_term in s},
        key=len,
        reverse=True,
    )
    for sib in longer:
        idx = working.find(sib)
        while idx != -1:
            working = working[:idx] + ("\x00" * len(sib)) + working[idx + len(sib) :]
            idx = working.find(sib, idx + len(sib))

    pos = working.find(clean_term)
    while pos != -1:
        if _is_valid_boundary(clean_lbl, clean_term, pos, pos + len(clean_term), working):
            return True
        pos = working.find(clean_term, pos + 1)
    return False


def _sibling_longer_map(conventions: Sequence[object]) -> dict[str, tuple[str, ...]]:
    all_terms = tuple(dict.fromkeys(
        _strip_units(_clean_label(t))
        for c in conventions
        for t in getattr(c, "terms", ()) or ()
        if _clean_label(t)
    ))
    return {
        term: tuple(other for other in all_terms if len(other) > len(term) and term in other)
        for term in all_terms
    }


def _effective_target_entity(convention: object, known_entities: frozenset[str]) -> str:
    direct = str(getattr(convention, "entity", "") or "").strip()
    if direct:
        return direct
    meaning = str(getattr(convention, "meaning", "") or "")
    m = _MEANING_ENTITY.match(meaning)
    if not m:
        return ""
    slug = m.group(1)
    if known_entities:
        return slug if slug in known_entities else ""
    return slug if _MEANING_RECORD_SLUG.match(meaning) else ""


def _prompt_residual(prompt: str, conventions: Sequence[object]) -> str:
    """Blank out convention spans in `prompt` using longest-match non-overlapping scan."""
    text = str(prompt or "")
    if not text or not conventions:
        return text
    terms = sorted(
        {
            _strip_units(_clean_label(t))
            for conv in conventions
            for t in getattr(conv, "terms", ()) or ()
            if _clean_label(t)
        },
        key=len,
        reverse=True,
    )
    used = [False] * len(text)
    for term in terms:
        pos = text.find(term)
        while pos != -1:
            end = pos + len(term)
            if not any(used[pos:end]) and _is_valid_boundary(text, term, pos, end, text):
                for idx in range(pos, end):
                    used[idx] = True
            pos = text.find(term, pos + 1)
    return "".join(" " if used[i] else ch for i, ch in enumerate(text))


def _collect_catalog(goal_data, knowledge) -> list[tuple[str, str, str, tuple[str, ...]]]:
    entries: list[tuple[str, str, str, tuple[str, ...]]] = []
    if goal_data is not None and hasattr(goal_data, "page_catalog"):
        entries.extend(goal_data.page_catalog())
    menu_rows = getattr(knowledge, "_menu", None)
    if isinstance(menu_rows, list):
        known_routes = {route for _, route, _, _ in entries if route}
        for row in menu_rows:
            if not isinstance(row, dict):
                continue
            route = _normalize_route(row.get("path"))
            if not route or route in known_routes:
                continue
            ent = ""
            if goal_data is not None and hasattr(goal_data, "route_entity"):
                ent = goal_data.route_entity(route)
            leaf = str(row.get("leaf") or "").strip()
            trail = tuple(s.strip() for s in str(row.get("name") or "").split(">") if s.strip())
            entries.append((ent, route, leaf, trail))
    return entries


def _prompt_targets(
    prompt: str,
    residual_prompt: str,
    catalog: Sequence[tuple[str, str, str, tuple[str, ...]]],
    convention_terms: frozenset[str],
) -> list[tuple[str, str]]:
    if not prompt or not catalog:
        return []
    full_c = _compact(prompt)
    res_c = _compact(residual_prompt)

    stem_owners: dict[str, set[str]] = {}
    entry_candidates: list[tuple[str, str, list[str], list[str], list[str]]] = []

    for ent, route, label, extras in catalog:
        names = [s for s in dict.fromkeys((label, *(x for x in extras if "/" not in x))) if s]
        prefixes: list[str] = []
        stems: list[str] = []
        for raw_name in names:
            parts = [p for p in re.split(r"[\s/>]+", raw_name.strip()) if p]
            if len(parts) >= 2:
                head = _compact(_MODULE_SUFFIX.sub("", parts[0]) or parts[0])
                if len(head) >= 2:
                    prefixes.append(head)
                tail_stem = _compact(_UI_SUFFIX.sub("", parts[-1]) or parts[-1])
                if len(tail_stem) >= 2:
                    stems.append(tail_stem)
            base_stem = _compact(_UI_SUFFIX.sub("", raw_name) or raw_name)
            if len(base_stem) >= 2:
                stems.append(base_stem)
                if ent or route:
                    stem_owners.setdefault(base_stem, set()).add(ent or f"route:{route}")
        for raw_ext in extras:
            if "/" in raw_ext:
                segs = [p.strip() for p in raw_ext.split("/") if p.strip()]
                if len(segs) >= 2:
                    head = _compact(_MODULE_SUFFIX.sub("", segs[0]) or segs[0])
                    if len(head) >= 2:
                        prefixes.append(head)
        entry_candidates.append((ent, route, names, list(dict.fromkeys(prefixes)), list(dict.fromkeys(stems))))

    chosen: list[tuple[str, str]] = []
    seen_targets: set[tuple[str, str]] = set()
    used_full = [False] * len(full_c)
    used_res = [False] * len(res_c)

    def _add_hit(ent: str, route: str, full_span: tuple[int, int] | None = None,
                 res_span: tuple[int, int] | None = None) -> None:
        if full_span is not None:
            for idx in range(full_span[0], full_span[1]):
                used_full[idx] = True
        if res_span is not None:
            for idx in range(res_span[0], res_span[1]):
                used_res[idx] = True
        pair = (ent, route)
        if pair not in seen_targets:
            seen_targets.add(pair)
            chosen.append(pair)

    def _find_unused(haystack: str, used: list[bool], needle: str) -> tuple[int, int] | None:
        pos = haystack.find(needle)
        while pos != -1:
            end = pos + len(needle)
            if not any(used[pos:end]):
                return (pos, end)
            pos = haystack.find(needle, pos + 1)
        return None

    # Tier 4: exact route string in prompt (longest first)
    for ent, route, _names, _prefixes, _stems in sorted(entry_candidates, key=lambda x: -len(x[1])):
        if route and len(route) > 1:
            m_route = re.search(re.escape(route) + r"(?=$|[^A-Za-z0-9_/-])", prompt)
            if m_route:
                cr = _compact(route)
                _add_hit(ent, route, _find_unused(full_c, used_full, cr), _find_unused(res_c, used_res, cr))

    # Tier 3: full entity/page name in prompt with unused residual evidence (longest first)
    tier3_cands: list[tuple[int, str, str, str, str]] = []
    for ent, route, names, _prefixes, _stems in entry_candidates:
        for name in names:
            cn = _compact(name)
            if len(cn) < 2 or cn in _GENERIC_STEMS:
                continue
            tier3_cands.append((len(cn), cn, name, ent, route))
    for _ln, cn, raw_name, ent, route in sorted(tier3_cands, key=lambda x: -x[0]):
        full_span = _find_unused(full_c, used_full, cn)
        if full_span is None:
            continue
        if cn not in convention_terms:
            res_span = _find_unused(res_c, used_res, cn)
            if res_span is not None:
                _add_hit(ent, route, full_span, res_span)
                continue
        if " " in raw_name.strip():
            tokens = [_compact(tok) for tok in raw_name.split() if tok.strip()]
            for tok in tokens:
                if not tok:
                    continue
                tok_span = _find_unused(res_c, used_res, tok)
                if tok_span is not None:
                    _add_hit(ent, route, full_span, tok_span)
                    break

    # Tier 2: adjacent module prefix (unused in residual) + entity core noun in prompt
    tier2_cands: list[tuple[int, str, str, str, str]] = []
    for ent, route, _names, prefixes, stems in entry_candidates:
        for pref in prefixes:
            for stem in stems:
                if stem in _GENERIC_STEMS or stem == pref:
                    continue
                tier2_cands.append((len(pref) + len(stem), pref, stem, ent, route))
    for _sc, pref, stem, ent, route in sorted(tier2_cands, key=lambda x: -x[0]):
        pref_span = _find_unused(res_c, used_res, pref)
        if pref_span is None:
            continue
        m_adj = re.search(
            re.escape(pref) + r"(?:里面的|里的|中的|的|里|中|内|下){0,1}" + re.escape(stem),
            full_c,
        )
        if m_adj is not None and not any(used_full[m_adj.start():m_adj.end()]):
            _add_hit(ent, route, m_adj.span(), pref_span)

    # Tier 1: only when no stronger Tier >= 2 entity/page matched in the prompt (or for additional
    # unambiguous stems outside already-matched spans).
    tier1_cands: list[tuple[int, str, str, str]] = []
    for ent, route, _names, _prefixes, stems in entry_candidates:
        for stem in stems:
            if stem in _GENERIC_STEMS or stem in convention_terms:
                continue
            if len(stem_owners.get(stem, ())) == 1:
                tier1_cands.append((len(stem), stem, ent, route))
    for _ln, stem, ent, route in sorted(tier1_cands, key=lambda x: -x[0]):
        full_span = _find_unused(full_c, used_full, stem)
        res_span = _find_unused(res_c, used_res, stem)
        if full_span is not None and res_span is not None:
            _add_hit(ent, route, full_span, res_span)

    return chosen


def resolve_turn_conventions(
    conventions: Iterable[object],
    *,
    prompt: str,
    goal_data=None,
    knowledge=None,
    active_route: str = "",
) -> ConventionResolution:
    """Resolve active vs page-suppressed conventions for a single turn.

    Policy (owner-confirmed Phase 53 Step 3):
    1. On a convention's own target entity, the convention stays active.
    2. On a resolved non-target entity/page whose page vocabulary matches a convention
       term (exact after stripping punctuation/units, or compound with morpheme boundary
       checks), suppress that convention for this turn.
    3. When neither the prompt nor the active page resolves to a non-target entity/page,
       all conventions stay active as before.
    """
    conv_tuple = tuple(conventions or ())
    if not conv_tuple or goal_data is None:
        return ConventionResolution(active=conv_tuple, suppressed=(), source="unresolved")

    try:
        catalog = _collect_catalog(goal_data, knowledge)
    except DataRefused:
        return ConventionResolution(active=conv_tuple, suppressed=(), source="unresolved")

    known_entities = frozenset(ent for ent, _, _, _ in catalog if ent)
    sibling_longer = _sibling_longer_map(conv_tuple)
    convention_terms = frozenset(
        _compact(_strip_units(_clean_label(t)))
        for c in conv_tuple
        for t in getattr(c, "terms", ()) or ()
        if _clean_label(t)
    )
    residual = _prompt_residual(prompt, conv_tuple)
    targets = _prompt_targets(prompt, residual, catalog, convention_terms)
    source = "prompt" if targets else "active_page"

    if not targets:
        clean_active = _normalize_route(active_route)
        if clean_active in _HUB_ROUTES:
            return ConventionResolution(active=conv_tuple, suppressed=(), source="unresolved")
        try:
            active_ent = goal_data.route_entity(clean_active) if hasattr(goal_data, "route_entity") else ""
        except DataRefused:
            return ConventionResolution(active=conv_tuple, suppressed=(), source="unresolved")
        targets = [(active_ent, clean_active)]

    target_vocabs: list[tuple[str, str, tuple[str, ...]]] = []
    for ent, route in targets:
        try:
            words = tuple(goal_data.page_words(entity=ent, route=route)) if hasattr(goal_data, "page_words") else ()
        except DataRefused:
            return ConventionResolution(active=conv_tuple, suppressed=(), source="unresolved")
        resolved_ent = ent
        if not resolved_ent and route and hasattr(goal_data, "route_entity"):
            try:
                resolved_ent = goal_data.route_entity(route)
            except DataRefused:
                resolved_ent = ""
        target_vocabs.append((resolved_ent, route, words))

    target_entities = tuple(dict.fromkeys(ent for ent, _, _ in target_vocabs if ent))
    target_routes = tuple(dict.fromkeys(route for _, route, _ in target_vocabs if route))
    target_entity_set = frozenset(target_entities)

    active: list[object] = []
    suppressed: list[SuppressedConvention] = []
    for conv in conv_tuple:
        conv_entity = _effective_target_entity(conv, known_entities)
        if conv_entity and conv_entity in target_entity_set:
            active.append(conv)
            continue

        terms = tuple(getattr(conv, "terms", ()) or ())
        matched_words: list[str] = []
        seen_words: set[str] = set()
        for ent, _route, words in target_vocabs:
            if conv_entity and ent == conv_entity:
                continue
            for word in words:
                if word in seen_words:
                    continue
                if any(
                    label_matches_term(word, term, sibling_longer_terms=sibling_longer.get(_strip_units(_clean_label(term)), ()))
                    for term in terms
                ):
                    seen_words.add(word)
                    matched_words.append(word)

        if matched_words:
            suppressed.append(
                SuppressedConvention(
                    convention=conv,
                    matched_words=tuple(matched_words),
                    target_entities=target_entities,
                    target_routes=target_routes,
                )
            )
        else:
            active.append(conv)

    return ConventionResolution(
        active=tuple(active),
        suppressed=tuple(suppressed),
        target_entities=target_entities,
        target_routes=target_routes,
        source=source,
    )


__all__ = [
    "ConventionResolution",
    "SuppressedConvention",
    "label_matches_term",
    "resolve_turn_conventions",
]
