"""Rules of the deployment's source, as index entries on Odoo entities.

The compiler (`sourcemap.OdooCompiler`) records, with a citation into the exact source
lines, the buttons a form declares and the conditions on them, the fields a form shows and
the conditions on those, the refusals a model method raises and the condition each is
under, and the fields a model declares with their help text. This module turns those into
`IndexRule`s in the page's own words: which control, when it shows, what refuses what.
Nothing here reads source at turn time; the citation carries the lines' hash, computed
when the map was compiled against the bound release.
"""
from __future__ import annotations

import re

from web_operation_agent.goal.index import IndexRule
from web_operation_agent.schemas.planning import SourceMap, SourceRelation

from .text import _ModelMeta, _norm

_MAX_CONDITION = 200
_MAX_MESSAGE = 160


def _citation(relation: SourceRelation, release: str) -> str:
    ref = relation.evidence[0]
    return f"{ref.path}:{ref.start_line}-{ref.end_line}@{release} sha256:{ref.sha256}"


def _short(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _field_label(meta: _ModelMeta | None, name: str) -> str:
    if meta is None:
        return name
    info = meta.fields.get(name) or {}
    return str(info.get("string") or name)


def rules_for(source_map: SourceMap, *, release: str, metas: dict[str, _ModelMeta],
              models: tuple[str, ...]) -> dict[str, tuple[IndexRule, ...]]:
    """The rules about each model, in the order they help a person: what a control
    needs to be shown, what a field needs to be changed, what an action refuses, what a
    field means. Rule ids are stable within one compiled map (r1, r2, ...)."""
    wanted = set(models)
    out: dict[str, list[IndexRule]] = {m: [] for m in wanted}
    counter = 0

    def add(model: str, kind: str, about: str, text: str, relation: SourceRelation) -> None:
        nonlocal counter
        if model not in wanted or not text:
            return
        counter += 1
        out[model].append(IndexRule(id=f"r{counter}", kind=kind, about=about, text=text,
                                    citation=_citation(relation, release)))

    seen: set[tuple] = set()
    for relation in source_map.relations:
        attrs = relation.attrs or {}
        model = str(attrs.get("model") or "")
        if relation.kind == "form_target" and attrs.get("label"):
            label = str(attrs["label"])
            conditions = []
            if attrs.get("invisible"):
                conditions.append(f"hidden when {_short(attrs['invisible'], _MAX_CONDITION)}")
            if attrs.get("readonly"):
                conditions.append(f"disabled when {_short(attrs['readonly'], _MAX_CONDITION)}")
            target = relation.object
            what = (f"opens {target}" if attrs.get("target_type") == "action"
                    else f"calls {target}")
            style = str(attrs.get("class") or "")
            look = (" (btn-primary, highlighted)" if "btn-primary" in style
                    else " (btn-secondary)" if "btn-secondary" in style else "")
            key = (model, "button", label, tuple(conditions), what, look)
            if key in seen:
                continue
            seen.add(key)
            text = f'Button "{label}"{look} {what}' + ("; " + "; ".join(conditions) if conditions else "")
            add(model, "visibility" if conditions else "target", label, text, relation)
        elif relation.kind == "form_field" and any(attrs.get(k) for k in ("readonly", "required", "invisible")):
            name = relation.object
            label = _field_label(metas.get(model), name)
            parts = []
            for key, word in (("readonly", "read-only when"), ("required", "required when"),
                              ("invisible", "hidden when")):
                if attrs.get(key):
                    parts.append(f"{word} {_short(attrs[key], _MAX_CONDITION)}")
            sig = (model, "field", name, tuple(parts))
            if sig in seen:
                continue
            seen.add(sig)
            add(model, "editability", label, f'Field "{label}" ({name}): ' + "; ".join(parts), relation)
        elif relation.kind == "service_guard":
            model_name, _, method = relation.subject.rpartition(".")
            message = _short(relation.object, _MAX_MESSAGE)
            condition = _short(attrs.get("condition") or "", _MAX_CONDITION)
            sig = (model_name, "guard", method, message)
            if sig in seen:
                continue
            seen.add(sig)
            add(model_name, "refusal", method,
                f'{method} refuses with "{message}"' + (f" when {condition}" if condition else ""),
                relation)
        elif relation.kind == "service_entity" and attrs.get("help"):
            name = relation.object
            label = str(attrs.get("string") or _field_label(metas.get(relation.subject), name))
            sig = (relation.subject, "help", name)
            if sig in seen:
                continue
            seen.add(sig)
            add(relation.subject, "meaning", label,
                f'Field "{label}" ({name}): {_short(attrs["help"], _MAX_MESSAGE)}', relation)
    return {m: tuple(rules) for m, rules in out.items()}


__all__ = ["rules_for"]
