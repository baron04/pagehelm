"""The application index a goal declaration is written against.

An adapter compiles it from the application's own metadata under the acting user. It is a
vocabulary, not a capability: naming an entity, a page or a control here grants nothing;
the adapter's resolver still reads and routes under the same actor.
"""
from __future__ import annotations

from dataclasses import dataclass, field

_MAX_ENTITIES = 80
_MAX_PAGES = 160
_MAX_FIELDS = 80
#: Fields rendered per entity in the full index; a focused index renders what it holds.
_SHOWN_FIELDS = 20
_MAX_LABEL = 80
_MAX_RULE = 640
_MAX_RULES_PER_ENTITY = 160


def _label(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return " ".join(value.split())[:_MAX_LABEL]


@dataclass(frozen=True)
class IndexField:
    key: str
    label: str
    #: text | number | money | date | choice | link | html | bool | other
    kind: str
    #: The entity a `link` points to, or the choice labels of a `choice`.
    relation: str = ""
    choices: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", _label(self.key, "field key"))
        object.__setattr__(self, "label", _label(self.label, "field label"))
        object.__setattr__(self, "choices", tuple(_label(c, "choice") for c in self.choices)[:8])


@dataclass(frozen=True)
class IndexRule:
    """A rule of the deployment's source about an entity: when a control shows, when a
    field can be changed, what an action refuses, what a field means. The model names a
    rule by its id; the citation stays on the server and is rendered verbatim."""
    id: str
    #: visibility | editability | refusal | meaning | target
    kind: str
    #: The control, field or action the rule is about, as the page labels it.
    about: str
    #: The rule in one sentence, in the application's own terms and identifiers.
    text: str
    #: `path:start-end@release sha256:<hex>` -- the exact lines, hashed.
    citation: str = ""
    #: The operation the rule concerns (the method a control calls, the action it
    #: opens, the method that refuses), so that the rules of one operation -- a control's
    #: conditions, what it opens, what it refuses -- are answered together.
    subject: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _label(self.id, "rule id"))
        object.__setattr__(self, "subject", " ".join(str(self.subject).split())[:_MAX_LABEL])
        object.__setattr__(self, "kind", _label(self.kind, "rule kind"))
        object.__setattr__(self, "about", " ".join(str(self.about).split())[:_MAX_LABEL])
        object.__setattr__(self, "text", " ".join(str(self.text).split())[:_MAX_RULE])


@dataclass(frozen=True)
class IndexEntity:
    key: str
    label: str
    aliases: tuple[str, ...] = ()
    fields: tuple[IndexField, ...] = ()
    #: Sections of a record's page (tabs), by their visible names.
    sections: tuple[str, ...] = ()
    #: Business controls on a record's page (header buttons), by their visible names.
    actions: tuple[str, ...] = ()
    #: For a line entity: the label of the entity whose records own these lines. The
    #: model then knows "the lines of an invoice" are this entity, not the invoice's.
    lines_of: str = ""
    #: Rules compiled from the deployment's source about this entity's controls, fields
    #: and actions. Rendered only in a focused index (a neighbourhood), never in full.
    rules: tuple[IndexRule, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", _label(self.key, "entity key"))
        object.__setattr__(self, "label", _label(self.label, "entity label"))
        object.__setattr__(self, "aliases", tuple(_label(a, "alias") for a in self.aliases)[:12])
        object.__setattr__(self, "fields", tuple(self.fields)[:_MAX_FIELDS])
        object.__setattr__(self, "sections", tuple(_label(s, "section") for s in self.sections)[:16])
        object.__setattr__(self, "actions", tuple(_label(a, "action") for a in self.actions)[:16])

    def field_by_label(self, label: str) -> IndexField | None:
        wanted = _norm(label)
        for item in self.fields:
            if _norm(item.label) == wanted or _norm(item.key) == wanted:
                return item
        for item in self.fields:
            if wanted and (wanted in _norm(item.label) or _norm(item.label) in wanted):
                return item
        return None


@dataclass(frozen=True)
class IndexPage:
    key: str
    label: str
    #: Where the application's own navigation shows it, e.g. "Sales / Orders / Quotations".
    breadcrumb: str
    entity: str
    views: tuple[str, ...] = ()
    filters: tuple[str, ...] = ()
    groups: tuple[str, ...] = ()
    #: Actor-visible operations the adapter can guide to, not permission to execute them.
    operations: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    #: Key of the page whose filters and groups this page shares, when it lists none.
    filters_as: str = ""
    #: Tabs on the page itself (a workspace with tabs), by their visible names; a
    #: record's tabs are on its entity instead.
    sections: tuple[str, ...] = ()
    #: Filters the page applies on its own when opened, by name: the page shows a
    #: narrower set than its entity (customers among contacts), and a declaration that
    #: means every record of the entity names the entity without this page.
    scope: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", _label(self.key, "page key"))
        object.__setattr__(self, "label", _label(self.label, "page label"))
        object.__setattr__(self, "breadcrumb", _label(self.breadcrumb, "breadcrumb"))
        object.__setattr__(self, "entity", _label(self.entity, "page entity"))
        object.__setattr__(self, "views", tuple(_label(v, "view") for v in self.views)[:8])
        object.__setattr__(self, "filters", tuple(_label(f, "filter") for f in self.filters)[:24])
        object.__setattr__(self, "groups", tuple(_label(g, "group") for g in self.groups)[:24])
        object.__setattr__(self, "operations", tuple(_label(o, "operation") for o in self.operations)[:8])
        object.__setattr__(self, "aliases", tuple(_label(a, "alias") for a in self.aliases)[:12])


@dataclass(frozen=True)
class ApplicationIndex:
    entities: tuple[IndexEntity, ...] = ()
    pages: tuple[IndexPage, ...] = ()
    #: A digest of the metadata this index was compiled from, for the audit record.
    source_digest: str = ""
    complete: bool = True
    #: A focused index (the neighbourhood of a few entities) renders its rules too; the
    #: full index never does, since it is bounded by what a small model can read.
    focused: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "entities", tuple(self.entities)[:_MAX_ENTITIES])
        object.__setattr__(self, "pages", tuple(self.pages)[:_MAX_PAGES])
        keys = [e.key for e in self.entities]
        if len(set(keys)) != len(keys):
            raise ValueError("entity keys must be unique")
        page_keys = [p.key for p in self.pages]
        if len(set(page_keys)) != len(page_keys):
            raise ValueError("page keys must be unique")

    def rule(self, rule_id: str) -> tuple[IndexEntity, IndexRule] | None:
        for entity in self.entities:
            for rule in entity.rules:
                if rule.id == rule_id:
                    return entity, rule
        return None

    def entity(self, key: str) -> IndexEntity | None:
        wanted = _norm(key)
        for item in self.entities:
            if _norm(item.key) == wanted:
                return item
        for item in self.entities:
            if _norm(item.label) == wanted or wanted in (_norm(a) for a in item.aliases):
                return item
        return None

    def page(self, key: str) -> IndexPage | None:
        wanted = _norm(key)
        for item in self.pages:
            if _norm(item.key) == wanted:
                return item
        for item in self.pages:
            if (_norm(item.label) == wanted or _norm(item.breadcrumb) == wanted
                    or wanted in (_norm(a) for a in item.aliases)):
                return item
        return None

    def render(self) -> str:
        """The compact text the model reads. One line per entity and per page."""
        lines = ["ENTITIES (key: label [aliases] | fields | sections | actions)"]
        for entity in self.entities:
            shown = entity.fields if self.focused else entity.fields[:_SHOWN_FIELDS]
            fields = ", ".join(_render_field(f) for f in shown)
            parts = [f"{entity.key}: {entity.label}"
                     + (f" [{', '.join(entity.aliases)}]" if entity.aliases else "")
                     + (f" (lines of {entity.lines_of})" if entity.lines_of else "")]
            if fields:
                parts.append(f"fields: {fields}")
            if entity.sections:
                parts.append("sections: " + ", ".join(entity.sections))
            if entity.actions:
                parts.append("actions: " + ", ".join(entity.actions))
            lines.append(" | ".join(parts))
        lines.append("")
        lines.append("PAGES (key: breadcrumb [aliases] | entity | views | filters | groups | shows only | operations | sections)")
        for page in self.pages:
            parts = [f"{page.key}: {page.breadcrumb}"
                     + (f" [{', '.join(page.aliases)}]" if page.aliases else ""),
                     f"entity {page.entity}"]
            if page.views:
                parts.append("views: " + ", ".join(page.views))
            if page.filters:
                parts.append("filters: " + ", ".join(page.filters))
            if page.groups:
                parts.append("groups: " + ", ".join(page.groups))
            if page.filters_as and not (page.filters or page.groups):
                parts.append(f"filters/groups: as {page.filters_as}")
            if page.scope:
                parts.append("shows only: " + ", ".join(page.scope))
            if page.sections:
                parts.append("sections: " + ", ".join(page.sections))
            if page.operations:
                parts.append("operations: " + ", ".join(page.operations))
            lines.append(" | ".join(parts))
        if self.focused and any(e.rules for e in self.entities):
            # A catalogue, not the rules themselves: id, kind and what each is about,
            # grouped by entity and kind. The model names ids; the server answers with
            # the rules' text and citations. Kinds: visibility (when a control shows),
            # editability (when a field can be changed), refusal (what an action
            # refuses), meaning (what a field is for), target (what a control opens).
            lines.append("")
            lines.append("RULES of the deployment's source, by entity and kind (id about; ...)")
            for entity in self.entities:
                by_kind: dict[str, list[str]] = {}
                for rule in entity.rules[:_MAX_RULES_PER_ENTITY]:
                    by_kind.setdefault(rule.kind, []).append(f"{rule.id} {rule.about}")
                for kind, items in by_kind.items():
                    lines.append(f"{entity.label} · {kind}: " + "; ".join(items))
        return "\n".join(lines)


def _render_field(item: IndexField) -> str:
    if item.kind == "link" and item.relation:
        return f"{item.label}->{item.relation}"
    if item.kind == "choice" and item.choices:
        return f"{item.label}({'/'.join(item.choices)})"
    if item.kind in ("money", "number", "date", "html"):
        return f"{item.label}({item.kind})"
    if item.kind == "bool":
        # A flag reads as a yes/no question ("Sales" is whether it can be sold).
        return f"{item.label}(yes/no)"
    return item.label


def _norm(text: str) -> str:
    return "".join(ch for ch in (text or "").casefold() if not ch.isspace())


__all__ = ["ApplicationIndex", "IndexEntity", "IndexField", "IndexPage", "IndexRule"]
