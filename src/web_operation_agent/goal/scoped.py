"""Bind required DataPort reads to application-owned conditions without model authority."""
from __future__ import annotations

from types import MappingProxyType

from .data import EVERYTHING, ID, Compare, DataRefused, Ref, all_of


class ScopedData:
    """A per-resolution view; it never changes the shared adapter or its index.

    The application must create and validate its scope under the current actor and
    applied query. This wrapper combines that condition with every read; it does
    not infer a scope from text, controls or source, nor grant a new capability.
    """

    def __init__(self, data, scopes):
        self.data = data
        self.scopes = MappingProxyType(dict(scopes))

    def __getattr__(self, name):
        return getattr(self.data, name)

    def _where(self, entity, where):
        return all_of(self.scopes.get(entity), where)

    def count(self, entity, where):
        return self.data.count(entity, self._where(entity, where))

    def rows(self, entity, where, *, fields=(), order=(), limit=80):
        return self.data.rows(entity, self._where(entity, where),
                              fields=fields, order=order, limit=limit)

    def total(self, entity, where, field):
        return self.data.total(entity, self._where(entity, where), field)

    def groups(self, entity, where, field, *, limit=12, by_month=False):
        return self.data.groups(entity, self._where(entity, where), field,
                                limit=limit, by_month=by_month)

    def find(self, entity, text, *, limit=8):
        if entity not in self.scopes:
            return self.data.find(entity, text, limit=limit)
        exact = text.strip().casefold()
        if not exact:
            return ()
        # Read the complete bounded scope before matching the adapter's display
        # names. A disambiguated row label may contain a query-local reference
        # which is absent from the raw NAME field and cannot be pushed to the API.
        condition = EVERYTHING
        count = self.count(entity, condition)
        if count > 4000:
            raise DataRefused("scoped name lookup exceeds its row bound", read_failed=True)
        if not count:
            return ()
        rows = self.rows(entity, condition, limit=count)
        if len(rows) != count or self.count(entity, condition) != count:
            raise DataRefused("scoped name lookup is incomplete or changed", read_failed=True)
        rows = [r for r in rows if exact in r.display.casefold()]
        rows = sorted(rows, key=lambda r: (r.display.casefold()!=exact,
                                          not r.display.casefold().startswith(exact), len(r.display)))
        return tuple(Ref(entity, r.id, r.display) for r in rows[:limit])

    def read(self, entity, id, fields):
        if entity not in self.scopes:
            return self.data.read(entity, id, fields)
        rows = self.rows(entity, Compare(ID, "eq", id), fields=fields, limit=2)
        if len(rows)!=1 or rows[0].id != id:
            raise DataRefused("row is not unique/readable in the bound query scope")
        return rows[0]
