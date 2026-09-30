"""An entity's schema as a declaration names it: fields by label, links, filters."""
from __future__ import annotations

import re

from ..data import EntitySchema, FieldSchema, FilterSchema
from .words import norm, tokens

_CJK = re.compile(r"[\u3400-\u9fff]")


class EntityView:
    """Label lookups over one entity's schema. A label may be the field's own, its key,
    or its label in the person's language (an alias the application translates)."""

    def __init__(self, schema: EntitySchema, labels_for=None):
        self.schema = schema
        #: The application's own translations, word -> labels, most specific first.
        self._labels_for = labels_for
        self.key, self.label = schema.key, schema.label
        self.fields = {f.key: f for f in schema.fields}

    @property
    def listed(self) -> list[str]:
        return [f.key for f in self.schema.fields if f.listed]

    @property
    def shown(self) -> list[str]:
        return [f.key for f in self.schema.shown]

    def info(self, key: str | None) -> FieldSchema | None:
        return self.fields.get(key or "")

    def label_of(self, key: str) -> str:
        field = self.fields.get(key)
        return field.label if field is not None else key

    def field_by_label(self, label: str) -> str | None:
        found = self._field_by_label(label)
        if found is None:
            # A label in the person's language: the field the application translates so.
            wanted = " ".join(str(label or "").split())
            translated = tuple(self._labels_for(wanted)) if self._labels_for and wanted else ()
            candidates = translated or tuple(dict.fromkeys(
                f.label for f in self.schema.fields if wanted and wanted in f.aliases))
            # A translation that is some field's exact label is meant before one that only
            # shares a word with a label (数量: Quantity, not "Method to update delivered qty"
            # for the first translation, Qty).
            found = next((key for english in candidates for key, info in self.fields.items()
                          if norm(info.label) == norm(english)), None)
            for english in candidates if found is None else ():
                found = self._field_by_label(english)
                if found is not None:
                    break
        return found

    def _field_by_label(self, label: str) -> str | None:
        wanted = norm(label)
        if not wanted:
            return None
        return self._field_by_label_among(label, self.fields)

    @staticmethod
    def _field_by_label_among(label: str, fields: dict) -> str | None:
        wanted = norm(label)
        if not wanted:
            return None
        for key, info in fields.items():
            if norm(info.label) == wanted or key == label:
                return key
        asked = tokens(label)
        if not asked:
            return None
        # Whole words, never substrings: "ID" is not inside "Validity". A field whose
        # own key says exactly the words asked for is meant even when its label differs.
        named = [k for k in fields if tokens(k.replace("_", " ")) == asked]
        if len(named) == 1:
            return named[0]
        # A label whose words include every word asked for ("Invoice/Bill Date" for
        # "Invoice Date"); the fewest extra words, then the shortest, wins.
        covering = sorted(((len(tokens(i.label) - asked), len(norm(i.label)), k)
                           for k, i in fields.items() if asked <= tokens(i.label)))
        if covering:
            return covering[0][2]
        # Or the words asked for begin with a label and qualify it ("Customer name");
        # a trailing word alone ("Date" in "Validity Date") names nothing.
        first = next(iter(re.findall(r"[0-9a-z㐀-鿿]+", label.casefold())), "")
        contained = sorted(((-len(norm(i.label)), k) for k, i in fields.items()
                            if first in tokens(i.label) and tokens(i.label) <= asked))
        if contained:
            return contained[0][1]
        if _CJK.search(label):
            # Words in a script written without spaces have no word boundaries to match
            # on: a label that holds the words ("销售状态" for "状态") is meant, one that
            # ends with them before one that only contains them, the shortest first.
            holding = sorted(((not norm(i.label).endswith(wanted), len(norm(i.label)), k)
                              for k, i in fields.items() if wanted in norm(i.label)))
            if holding:
                return holding[0][2]
        return None

    def link_by_label(self, label: str) -> str | None:
        """The link field a label names, or the one whose label its words contain
        ("Invoice Partner Display Name" is a text field; the link meant is "Partner")."""
        field = self.field_by_label(label)
        if field is not None and self.fields[field].kind == "link":
            return field
        wanted = norm(label)
        links = sorted(((-len(norm(i.label)), k) for k, i in self.fields.items()
                        if i.kind == "link" and norm(i.label)
                        and (norm(i.label) in wanted or wanted in norm(i.label))))
        return links[0][1] if links else None

    def filter_by_name(self, label: str) -> FilterSchema | None:
        wanted = norm(label)
        for item in self.schema.filters:
            if norm(item.label) == wanted or item.key == label:
                return item
        return None
