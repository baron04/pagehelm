"""Candidate values around the records a request names ("values" capability).

Before the model declares details, the core asks which real values are near the records
the request already names: an order's lines (the product it calls 白色隔音屏 is
"[FURN_6667] Acoustic Bloc Screens (White)" there), a customer's recent documents on the
entities the request is about (the Gemini order of 1582 dollars is one of them), and, when
the request is about lines, those documents' lines. All reads run under the delegated
user; nothing is searched across the whole database by a guessed name.
"""
from __future__ import annotations

from datetime import date, timedelta

from web_operation_agent.business.runtime import BusinessToolError
from web_operation_agent.goal.ports import Candidate

from .text import _ModelMeta

#: Per source, what is listed: a customer's documents, a record's lines.
_DOCUMENTS = 8
_LINES = 20
#: Field types that tell records apart in one short line.
_DISTINGUISHING = ("date", "datetime", "monetary", "selection", "many2one")


class _ValueCandidates:
    """Mixed into ``OdooGoalPort``."""

    def candidates(self, *, entities: tuple[str, ...] = (), known: tuple = (),
                   dates: tuple[str, ...] = (), context=None) -> tuple[Candidate, ...]:
        self.index(context=context)
        wanted = [e for e in entities if e in self._models]
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
            if entity not in self._models:
                continue
            found = self._find_record(entity, name)
            if not (isinstance(found, tuple) and len(found) == 3):
                continue
            model, rid, display = found
            documents = []
            if model in wanted or model in owners:
                add(Candidate(kind="record", entity=model, display=display,
                              detail=self._summary(model, rid)))
                documents = [(model, rid, display)]
            else:
                # A partner (or any linked record) named: its documents on the
                # entities the request is about, newest first.
                for target in list(dict.fromkeys(list(owners) + wanted)):
                    for doc in self._documents(target, model, rid):
                        add(Candidate(kind="record", entity=target, display=doc[2],
                                      detail=doc[3]))
                        documents.append(doc[:3])
            for doc_model, doc_id, doc_name in documents[:_DOCUMENTS]:
                line_entity = owners.get(doc_model)
                if line_entity is None and doc_model in wanted:
                    line_entity = next((m for m, meta in self._models.items()
                                        if meta.parent == doc_model and m in wanted), None)
                if line_entity is None:
                    continue
                for line in self._lines_of(doc_model, doc_id, line_entity):
                    add(Candidate(kind="line", entity=line_entity, display=line[0],
                                  detail=f"{doc_name}: {line[1]}"))
        # Days the request names: the documents of the entities asked about dated that
        # day, and their lines when lines are asked about.
        for day in dates:
            for target in list(dict.fromkeys(list(owners) + wanted)):
                for doc in self._dated(target, day):
                    add(Candidate(kind="record", entity=target, display=doc[2], detail=doc[3]))
                    line_entity = owners.get(target)
                    if line_entity is None:
                        continue
                    for line in self._lines_of(target, doc[1], line_entity):
                        add(Candidate(kind="line", entity=line_entity, display=line[0],
                                      detail=f"{doc[2]}: {line[1]}"))
        return tuple(out)

    def _dated(self, target: str, day: str):
        """(model, id, name, summary) of `target` records whose main date is `day`."""
        meta = self._models.get(target)
        if meta is None:
            return []
        field = next((f for f in list(dict.fromkeys(meta.list_fields + meta.form_fields))
                      if (meta.fields.get(f) or {}).get("type") in ("date", "datetime")), None)
        if field is None:
            return []
        if meta.fields[field].get("type") == "date":
            domain = [[field, "=", day]]
        else:
            domain = [[field, ">=", self._utc_boundary(date.fromisoformat(day))],
                      [field, "<", self._utc_boundary(date.fromisoformat(day) + timedelta(days=1))]]
        fields = self._distinguishing(meta)
        try:
            rows = self._call(target, "search_read", [domain],
                              {"fields": ["display_name"] + fields, "limit": _DOCUMENTS})
        except BusinessToolError:
            return []
        return [(target, int(row["id"]), str(row.get("display_name") or row["id"]),
                 _describe(meta, row, fields)) for row in rows]

    # -- reads ------------------------------------------------------------------------
    def _summary(self, model: str, rid: int) -> str:
        meta = self._models.get(model)
        fields = self._distinguishing(meta)
        if not fields:
            return ""
        try:
            rows = self._call(model, "read", [[rid]], {"fields": fields})
        except BusinessToolError:
            return ""
        return _describe(meta, rows[0], fields) if rows else ""

    def _documents(self, target: str, linked_model: str, rid: int):
        """(model, id, name, summary) of `target` records whose link field points at the
        record, newest first."""
        meta = self._models.get(target)
        if meta is None:
            return []
        link = next((f for f in meta.list_fields + list(meta.fields)
                     if (meta.fields.get(f) or {}).get("type") == "many2one"
                     and meta.fields[f].get("relation") == linked_model), None)
        if link is None:
            return []
        operator = "child_of" if linked_model == "res.partner" else "="
        fields = self._distinguishing(meta)
        try:
            rows = self._call(target, "search_read", [[[link, operator, rid]]],
                              {"fields": ["display_name"] + fields, "limit": _DOCUMENTS})
        except BusinessToolError:
            return []
        return [(target, int(row["id"]), str(row.get("display_name") or row["id"]),
                 _describe(meta, row, fields)) for row in rows]

    def _lines_of(self, parent_model: str, parent_id: int, line_model: str):
        """(item name, "qty 2; price 355.00; subtotal 710.00") for each line of one record,
        as its form embeds them."""
        embedded = self._embedded_lines(parent_model, line_model)
        parent = self._parent_link(line_model)
        meta = self._models.get(line_model)
        if embedded is None or parent is None or meta is None:
            return []
        _field, domain, columns = embedded
        item = next((f for f in meta.list_fields
                     if (meta.fields.get(f) or {}).get("type") == "many2one" and f != parent[1]),
                    None)
        if item is None:
            return []
        try:
            rows = self._call(line_model, "search_read",
                              [[[parent[1], "=", parent_id]] + list(domain)],
                              {"fields": [item] + list(columns), "limit": _LINES})
        except BusinessToolError:
            return []
        out = []
        for row in rows:
            value = row.get(item)
            if not (isinstance(value, (list, tuple)) and len(value) == 2):
                continue
            detail = "; ".join(f"{meta.fields[c].get('string') or c} {row.get(c)}"
                               for c in columns if row.get(c) not in (None, False))
            out.append((str(value[1]), detail))
        return out

    @staticmethod
    def _distinguishing(meta: _ModelMeta | None) -> list[str]:
        if meta is None:
            return []
        return [f for f in meta.list_fields
                if (meta.fields.get(f) or {}).get("type") in _DISTINGUISHING][:4]


def _describe(meta: _ModelMeta, row: dict, fields: list[str]) -> str:
    parts = []
    for field in fields:
        value = row.get(field)
        if value in (None, False, ""):
            continue
        info = meta.fields.get(field) or {}
        if isinstance(value, (list, tuple)) and len(value) == 2:
            value = value[1]
        elif info.get("type") == "selection":
            value = dict(info.get("selection") or ()).get(value, value)
        parts.append(f"{info.get('string') or field} {value}")
    return "; ".join(parts)


__all__ = ["_ValueCandidates"]
