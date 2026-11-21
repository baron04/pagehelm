"""Application-neutral domain capability packs and routing helpers."""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class DomainToolpack:
    """Static metadata and bounded read/write guidance for one application domain."""

    domain_id: str
    label: str
    aliases: tuple[str, ...] = ()
    route_prefixes: tuple[str, ...] = ()
    write_contract_text: str = ""
    read_contract_text: str = ""
    default_priority: int = 0
    co_activate_domains: tuple[str, ...] = ()


def _route_path(page_url: str) -> str:
    raw = str(page_url or "").strip()
    if not raw:
        return ""
    parsed = urlsplit(raw)
    fragment = parsed.fragment or ""
    if fragment.startswith("/"):
        return fragment.split("?", 1)[0].rstrip("/") or "/"
    return (parsed.path or "").split("?", 1)[0].rstrip("/") or "/"


def resolve_domain_pack(
    packs: tuple[DomainToolpack, ...],
    token: str,
) -> DomainToolpack | None:
    """Resolve one domain pack by exact domain_id, label, or casefolded alias."""
    norm = str(token or "").strip().casefold()
    if not norm:
        return None
    for pack in packs:
        if pack.domain_id.casefold() == norm or pack.label.casefold() == norm:
            return pack
        if any(alias.casefold() == norm for alias in pack.aliases):
            return pack
    return None


def select_active_domains(
    packs: tuple[DomainToolpack, ...],
    *,
    page_url: str = "",
    prompt: str = "",
    pinned_domains: tuple[str, ...] = (),
    top_k: int = 2,
    max_total: int = 3,
) -> tuple[DomainToolpack, ...]:
    """Select the most relevant active domains deterministically for the current page/prompt."""
    if not packs:
        return ()
    limit = max(1, int(top_k))
    co_limit = max(limit, int(max_total), len(pinned_domains))
    by_id = {pack.domain_id: pack for pack in packs}
    selected: list[DomainToolpack] = []
    matched_packs: list[DomainToolpack] = []
    seen: set[str] = set()

    for pinned_id in pinned_domains:
        pack = by_id.get(pinned_id) or resolve_domain_pack(packs, pinned_id)
        if pack is not None and pack.domain_id not in seen:
            selected.append(pack)
            matched_packs.append(pack)
            seen.add(pack.domain_id)

    path = _route_path(page_url).casefold()
    prompt_cf = str(prompt or "").casefold()
    scored: list[tuple[tuple[int, int, int, int], bool, DomainToolpack]] = []
    for index, pack in enumerate(packs):
        if pack.domain_id in seen:
            continue
        best_prefix = 0
        for prefix in pack.route_prefixes:
            norm_prefix = str(prefix or "").strip().rstrip("/").casefold()
            if norm_prefix and (path == norm_prefix or path.startswith(norm_prefix + "/")):
                best_prefix = max(best_prefix, len(norm_prefix))
        alias_hits = 0
        for candidate in (pack.label, pack.domain_id, *pack.aliases):
            needle = str(candidate or "").strip().casefold()
            if len(needle) >= 2 and needle in prompt_cf:
                alias_hits += len(needle)
        matched = best_prefix > 0 or alias_hits > 0
        if not matched and pack.default_priority <= 0:
            continue
        rank = (best_prefix, alias_hits, pack.default_priority, -index)
        scored.append((rank, matched, pack))

    scored.sort(key=lambda item: item[0], reverse=True)
    for _rank, matched, pack in scored:
        if not matched:
            continue
        if len(selected) >= max(limit, len(pinned_domains)):
            break
        selected.append(pack)
        matched_packs.append(pack)
        seen.add(pack.domain_id)

    for base_pack in matched_packs:
        for co_id in base_pack.co_activate_domains:
            if len(selected) >= co_limit:
                break
            co_pack = by_id.get(co_id) or resolve_domain_pack(packs, co_id)
            if co_pack is not None and co_pack.domain_id not in seen:
                selected.append(co_pack)
                seen.add(co_pack.domain_id)
        if len(selected) >= co_limit:
            break

    # Fill up to top_k with default_priority packs only after matched domains and their co-activations.
    for _rank, matched, pack in scored:
        if matched or pack.domain_id in seen:
            continue
        if len(selected) >= max(limit, len(pinned_domains)):
            break
        selected.append(pack)
        seen.add(pack.domain_id)
    return tuple(selected[:co_limit])


def build_switch_domain_schema(
    packs: tuple[DomainToolpack, ...],
    *,
    name: str = "switch_domain",
) -> dict:
    """Build an OpenAI function-calling schema for switching active domain contracts."""
    domain_ids = [pack.domain_id for pack in packs]
    choices = " / ".join(f"{pack.domain_id}({pack.label})" for pack in packs)
    domain_schema: dict[str, object] = {
        "type": "string",
        "minLength": 1,
        "description": f"目标业务域标识：{choices}" if choices else "目标业务域标识。",
    }
    if domain_ids:
        domain_schema["enum"] = domain_ids
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "当前已自动激活的业务域不够用时，手动切换或追加激活目标业务域规则与写契约，不重置本轮已读数据。",
            "parameters": {
                "type": "object",
                "properties": {
                    "domain": domain_schema,
                    "reason": {
                        "type": "string",
                        "maxLength": 200,
                        "description": "可选：说明为何需要切换到该业务域。",
                    },
                },
                "required": ["domain"],
                "additionalProperties": False,
            },
        },
    }


__all__ = [
    "DomainToolpack",
    "build_switch_domain_schema",
    "resolve_domain_pack",
    "select_active_domains",
]
