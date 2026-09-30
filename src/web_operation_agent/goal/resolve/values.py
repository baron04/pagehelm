"""Candidate values around the records a request names ("values" capability).

Before the model declares details, the core lists the real values near the records the
request already names: a record's lines, a customer's recent documents on the entities
the request is about, and those documents' lines. Every read runs under the acting user;
nothing is searched across the whole application by a guessed name.
"""
from __future__ import annotations

from datetime import date, timedelta

from ..data import Compare, DataRefused
from ..ports import Candidate
from .words import norm

#: Per source, what is listed: a customer's documents, a record's lines.
_DOCUMENTS = 8
_LINES = 20
#: Records whose names hold a located record's name, listed beside it.
_NEAR = 4


class ValuesMixin:
    """Mixed into `Resolver`."""

    def candidates(self, *, entities: tuple[str, ...] = (), known: tuple = (),
                   dates: tuple[str, ...] = ()) -> tuple[Candidate, ...]:
        wanted = [e for e in entities if self.view(e) is not None]
        # The owners of line entities asked about: their documents carry the lines.
        owners = {}
        for entity in wanted:
            parent = self._parent_link(entity)
            if parent is not None:
                owners[parent[0]] = entity
        out: list[Candidate] = []
        seen: set[tuple[str, str]] = set()

        def add(candidate: Candidate) -> None:
            key = (candidate.entity, candidate.display)
            if key not in seen:
                seen.add(key)
                out.append(candidate)

        for entity, name in known:
            if self.view(entity) is None:
                continue
            found = self._find_record(entity, name)
            if not (isinstance(found, tuple) and len(found) == 3):
                continue
            model, rid, display = found
            documents = []
            if model in wanted or model in owners:
                add(Candidate(kind="record", entity=model, display=display,
                              detail=self._summary(model, rid),
                              binds=self._owner_binds(owners.get(model), display)))
                documents = [(model, rid, display)]
                # Records whose names hold this one's ("Office Chair Black" beside "Office
                # Chair"): what the request says around the name may mean one.
                for near_id, near in self._near_names(model, rid, display):
                    add(Candidate(kind="record", entity=model, display=near,
                                  detail=self._summary(model, near_id),
                                  binds=self._owner_binds(owners.get(model), near)))
            else:
                # A partner (or any linked record) named: its documents on the entities the
                # request is about, newest first. Lines appear through their records.
                targets = [t for t in dict.fromkeys(list(owners) + wanted) if self._parent_link(t) is None]
                for target in targets:
                    for doc in self._documents(target, model, rid):
                        add(Candidate(kind="record", entity=target, display=doc[2], detail=doc[3],
                                      binds=self._owner_binds(owners.get(target), doc[2])))
                        documents.append(doc[:3])
            for doc_model, doc_id, doc_name in documents[:_DOCUMENTS]:
                line_entity = owners.get(doc_model)
                if line_entity is None and doc_model in wanted:
                    line_entity = next((e.key for e in self.schema.entities
                                        if e.lines_of == doc_model and e.key in wanted), None)
                if line_entity is None:
                    continue
                for line in self._lines_of(doc_model, doc_id, line_entity):
                    add(self._line_candidate(line_entity, doc_name, line))
        # Days the request names: the documents of the entities asked about dated that
        # day, and their lines when lines are asked about.
        for day in dates:
            for target in [t for t in dict.fromkeys(list(owners) + wanted) if self._parent_link(t) is None]:
                for doc in self._dated(target, day):
                    add(Candidate(kind="record", entity=target, display=doc[2], detail=doc[3],
                                  binds=self._owner_binds(owners.get(target), doc[2])))
                    line_entity = owners.get(target)
                    if line_entity is None:
                        continue
                    for line in self._lines_of(target, doc[1], line_entity):
                        add(self._line_candidate(line_entity, doc[2], line))
        return tuple(out)

    def _near_names(self, model: str, rid: str, display: str) -> list[tuple[str, str]]:
        plain = self._own_name(display)
        if len(plain) < 3:
            return []
        try:
            refs = self._find(model, plain, _NEAR + 1)
        except DataRefused:
            return []
        wanted = norm(plain)
        return [(r.id, r.display) for r in refs if r.id != rid and wanted in norm(r.own_name)][:_NEAR]

    def _owner_binds(self, line_entity: str | None, doc_name: str) -> tuple:
        """For a document whose lines are asked about: the condition on the lines that
        names it (Order Reference = S00016)."""
        if not line_entity:
            return ()
        parent = self._parent_link(line_entity)
        view = self.view(line_entity)
        if parent is None or view is None:
            return ()
        return ((view.label_of(parent[1]), doc_name),)

    def _line_candidate(self, line_entity: str, doc_name: str, line) -> Candidate:
        """One line of one record: shown by its item, standing for the record it is on and
        its item, since the same item is on many records' lines."""
        item_name, detail, item_label = line
        parent = self._parent_link(line_entity)
        owner_label = self.view(line_entity).label_of(parent[1]) if parent else ""
        binds = (((owner_label, doc_name),) if owner_label else ()) + ((item_label, item_name),)
        return Candidate(kind="line", entity=line_entity, display=item_name,
                         detail=f"{doc_name}: {detail}", binds=binds)

    def _dated(self, target: str, day: str):
        """(entity, id, name, summary) of `target` records whose main date is `day`."""
        view = self.view(target)
        if view is None:
            return []
        key = view.schema.main_date
        if not key:
            return []
        start = date.fromisoformat(day)
        if view.info(key).kind == "date":
            domain = [Compare(key, "eq", start)]
        else:
            domain = [Compare(key, "ge", start), Compare(key, "lt", start + timedelta(days=1))]
        fields = self._distinguishing(view)
        try:
            rows = self._rows(target, domain, fields=fields, limit=_DOCUMENTS)
        except DataRefused:
            return []
        return [(target, r.id, r.display or r.id, _describe(view, r, fields)) for r in rows]

    def _summary(self, model: str, rid: str) -> str:
        view = self.view(model)
        fields = self._distinguishing(view)
        if not fields:
            return ""
        try:
            row = self._read(model, rid, fields)
        except DataRefused:
            return ""
        return _describe(view, row, fields)

    def _documents(self, target: str, linked_model: str, rid: str):
        """(entity, id, name, summary) of `target` records whose link field points at the
        record, newest first."""
        view = self.view(target)
        if view is None:
            return []
        link = next((k for k in view.listed + list(view.fields)
                     if view.info(k) is not None and view.info(k).kind == "link"
                     and view.info(k).relation == linked_model), None)
        if link is None:
            return []
        fields = self._distinguishing(view)
        op = "under" if view.info(link).inclusive else "eq"
        try:
            rows = self._rows(target, [Compare(link, op, (rid,) if op == "under" else rid)],
                              fields=fields, limit=_DOCUMENTS)
        except DataRefused:
            return []
        return [(target, r.id, r.display or r.id, _describe(view, r, fields)) for r in rows]

    def _lines_of(self, parent_model: str, parent_id: str, line_model: str):
        """(item name, "qty 2; price 355.00; subtotal 710.00") for each line of one record,
        as its page embeds them."""
        embedded = self._embedded_lines(parent_model, line_model)
        parent = self._parent_link(line_model)
        view = self.view(line_model)
        if embedded is None or parent is None or view is None:
            return []
        _field, line_scope, columns = embedded
        from .lines import _item_field
        item = _item_field(view, parent[1])
        if not item:
            return []
        try:
            rows = self._rows(line_model, [Compare(parent[1], "eq", parent_id), line_scope],
                              fields=[item] + list(columns), limit=_LINES)
        except DataRefused:
            return []
        out = []
        for row in rows:
            value = row.values.get(item)
            if value is None or not hasattr(value, "display"):
                continue
            detail = "; ".join(f"{view.label_of(c)} {row.values.get(c)}" for c in columns
                               if row.values.get(c) is not None)
            out.append((str(value.display), detail, view.label_of(item)))
        return out

    @staticmethod
    def _distinguishing(view) -> list[str]:
        """What tells one record from another in a list: its date, its amounts (a total
        before a subtotal) and its states, from the fields its list shows."""
        if view is None:
            return []
        listed = view.listed
        of = {kind: [k for k in listed if view.info(k).kind in kinds]
              for kind, kinds in (("date", ("date", "datetime")), ("money", ("money",)),
                                  ("state", ("choice",)))}
        total = view.schema.total
        money = sorted(of["money"], key=lambda k: (k != total, listed.index(k)))
        return of["date"][:1] + money[:2] + of["state"][:2]


def _describe(view, row, fields: list[str]) -> str:
    parts = []
    for key in fields:
        value = row.values.get(key)
        if value in (None, False, ""):
            continue
        info = view.info(key)
        if hasattr(value, "display"):
            value = value.display
        elif info is not None and info.kind == "choice":
            value = next((c.label for c in info.choices if c.key == value), value)
        parts.append(f"{view.label_of(key)} {value}")
    return "; ".join(parts)


__all__ = ["ValuesMixin"]
