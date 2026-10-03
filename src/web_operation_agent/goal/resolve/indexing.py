"""The model-facing index derived from a data port's schema.

An adapter whose metadata the schema already carries in full need not compile an index
of its own: the entities, their fields in the order a list shows them, and the pages a
person reaches are rendered from the schema as they are.
"""
from __future__ import annotations

from ..data import Schema
from ..index import ApplicationIndex, IndexEntity, IndexField, IndexPage

#: Schema kinds as the index names them; kinds a declaration cannot use are left out.
_KINDS = {"text": "text", "number": "number", "money": "money", "date": "date",
          "datetime": "date", "choice": "choice", "link": "link", "html": "html", "bool": "bool"}


def index_from_schema(schema: Schema, *, breadcrumbs: dict[str, str] | None = None,
                      aliases: dict[str, tuple[str, ...]] | None = None,
                      conventions: tuple = (), source_digest: str = "",
                      rules: dict[str, tuple] | None = None, actor: str = "") -> ApplicationIndex:
    """`breadcrumbs` maps a page key to where the application's navigation shows it;
    `aliases` maps an entity or page key to other names people use for it; `rules` maps
    an entity key to the `IndexRule`s compiled from the deployment's source about it."""
    breadcrumbs, aliases, rules = breadcrumbs or {}, aliases or {}, rules or {}
    entities = []
    for entity in schema.entities:
        shown = [f for f in entity.fields if f.listed or f.on_form] or list(entity.fields)
        fields = tuple(IndexField(key=f.key, label=f.label, kind=_KINDS[f.kind],
                                  relation=f.relation if f.kind == "link" else "",
                                  choices=tuple(c.label for c in f.choices), aliases=f.aliases)
                       for f in shown if f.kind in _KINDS)
        owner = schema.entity(entity.lines_of) if entity.lines_of else None
        entities.append(IndexEntity(key=entity.key, label=entity.label,
                                    aliases=aliases.get(entity.key, ()), fields=fields,
                                    sections=entity.sections, actions=entity.actions,
                                    lines_of=owner.label if owner is not None else "",
                                    rules=tuple(rules.get(entity.key, ()))))
    pages = tuple(IndexPage(key=p.key, label=p.label, breadcrumb=breadcrumbs.get(p.key, p.label),
                            entity=p.entity, views=("list",) if p.entity else (),
                            filters=tuple(f.label for f in p.filters if f.usable),
                            operations=p.operations, aliases=aliases.get(p.key, ()))
                  for p in schema.pages)
    return ApplicationIndex(entities=tuple(entities), pages=pages, source_digest=source_digest,
                            actor=actor,
                            conventions=tuple(conventions))


__all__ = ["index_from_schema"]
