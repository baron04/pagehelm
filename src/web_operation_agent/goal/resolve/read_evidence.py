"""Evidence captured at deterministic result sites, never from rendered answer text."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import math
import time

from ...answer.evidence import (ComputationEvidence, Origin, QueryEvidence,
                                RecordEvidence, SelectionEvidence)
from ...business.errors import BusinessToolContractError
from ..data import LinkValue


class ReadEvidence:
    """One resolver call's bounded, actor-bound proof projection.

    Intermediate name searches and diagnostic reads are deliberately not published.
    An aggregate's native result is captured where it was computed; display strings
    are never parsed into numbers, record identities or completeness claims.
    """

    def __init__(self, binding: str):
        self.binding = binding
        self.items = []
        self.refs = []
        self._start = 0
        self._serial = 0

    def begin(self):
        self._start = len(self.items)

    def settle(self, result):
        if result.denied or result.read_failure or (result.problem and not result.facts):
            del self.items[self._start:]
        self.refs.append(tuple(item.evidence_id for item in self.items[self._start:]))

    def origin(self, scope, *, kind="typed"):
        self._serial += 1
        return Origin(kind, f"resolve_data:{self.binding}:{self._serial}", str(time.time()),
                      scope)

    def add(self, cls, origin, **values):
        ref = f"data#{len(self.items) + 1}"
        self.items.append(cls(evidence_id=ref, origin=origin, **values))
        return ref

    def records(self, model, view, rows, fields, origin):
        for row in rows:
            record = f"{model}:{row.id}"
            self.add(RecordEvidence, origin, record_id=record, field_name="@name",
                     field_meaning="Record name", field_was_read=True,
                     value=row.display, value_type="text")
            for key in dict.fromkeys(fields):
                info = view.info(key)
                if info is None:
                    raise BusinessToolContractError("the read field is absent from the bound schema")
                read = key in row.values
                values = {"value": None, "value_type": "", "unit": ""}
                if read:
                    values = _cell(row.values[key], row, key, info.kind)
                self.add(RecordEvidence, origin, record_id=record, field_name=key,
                         field_meaning=view.label_of(key), field_was_read=read, **values)

    def facts(self, model, view, row, fields):
        self.records(model, view, (row,), fields, self.origin(f"{view.label}: {row.display}"))

    def query(self, model, subject, count, *, scanned=None, complete=False, exact=True):
        origin = self.origin(subject)
        ref = self.add(QueryEvidence, origin, collection=model, conditions=(subject,),
                       matched_count=count, records_scanned=scanned,
                       covers_whole_scope=complete, count_exact=exact)
        return ref, origin

    def listing(self, model, view, rows, fields, *, subject, before, after, limit):
        stable = before == after
        complete = stable and len(rows) == after and len(rows) <= limit
        ref, origin = self.query(model, subject, after, scanned=min(len(rows), limit),
                                 complete=complete, exact=stable)
        self.records(model, view, rows[:limit], fields, origin)
        return ref

    def aggregate(self, model, subject, count, *, sums=(), groups=(),
                  grouped=False):
        # A count says nothing about whether every row or group was returned.
        ref, origin = self.query(model, subject, count)
        self.add(ComputationEvidence, origin, operation="count()", inputs=(ref,),
                 value=str(count), value_type="number", covers_whole_scope=True)
        for key, amount, unit in sums:
            validate_number(amount)
            if not isinstance(unit, str) or unit == "unknown-currency":
                raise BusinessToolContractError("the application did not bind the aggregate unit")
            self.add(ComputationEvidence, origin, operation=f"sum {key}", inputs=(ref,),
                     value=str(amount), value_type="number", unit=unit,
                     covers_whole_scope=True)
        if grouped:
            if sum(g.count for g in groups) > count:
                raise BusinessToolContractError("group counts exceed the complete query count")
            whole = sum(g.count for g in groups) == count
            # Group keys are scoped identifiers, not business record identities.
            group_ref, group_origin = self.query(f"{model} groups", subject,
                len(groups), scanned=len(groups), complete=whole, exact=whole)
            for group in groups:
                self.add(RecordEvidence, group_origin, record_id=f"{model}:group:{group.key}",
                         field_name="group", field_meaning="Group", field_was_read=True,
                         value=group.label, value_type="text")
                self.add(RecordEvidence, group_origin, record_id=f"{model}:group:{group.key}",
                         field_name="count", field_meaning="Count", field_was_read=True,
                         value=str(group.count), value_type="number")
            # A truncated grouped read stays partial, including when top-N was requested:
            # DataPort.groups does not certify ranking or the total number of groups.
            return group_ref, whole
        return ref, True

    def extreme(self, model, view, rows, fields, *, subject, count, operation, key):
        ref, origin = self.query(model, subject, count, scanned=len(rows), complete=True)
        known = [r for r in rows if r.values[key] not in (None, "")]
        if not known:
            return
        value = (max if operation == "max" else min)(r.values[key] for r in known)
        tied = sorted((r for r in known if r.values[key] == value), key=lambda r: r.id)
        if len(tied) > 50:
            raise BusinessToolContractError("more than 50 tied records need a narrower query")
        self.records(model, view, tied, fields, origin)
        self.add(ComputationEvidence, self.origin(subject, kind="computed"),
                 operation=f"{operation} {key}", inputs=(ref,),
                 value=str(value), value_type="number", unit=tied[0].units.get(key, ""),
                 covers_whole_scope=True)
        self.add(SelectionEvidence, self.origin(subject, kind="computed"), operation=operation,
                 inputs=(ref,), condition=f"{view.label_of(key)} = {value}",
                 members=tuple(f"{model}:{r.id}" for r in tied), covers_whole_scope=True)


def validate_number(value):
    if (isinstance(value, bool) or not isinstance(value, (int, float, Decimal))
            or not math.isfinite(value)):
        raise BusinessToolContractError("the application returned an invalid numeric value")


def _cell(value, row, key, kind):
    unit = ""
    if value is None or value == "":
        return {"value": None, "value_type": "number" if kind in ("number", "money") else "text",
                "unit": ""}
    if kind in ("number", "money"):
        validate_number(value)
        unit = row.units.get(key, "")
        if not isinstance(unit, str) or unit == "unknown-currency":
            raise BusinessToolContractError("the application did not bind the field unit")
        value_type = "number"  # Empty unit means native field units, never inferred currency.
    elif isinstance(value, LinkValue):
        value, value_type = value.display, "text"
    elif isinstance(value, (date, datetime)):
        value, value_type = value.isoformat(), kind
    elif isinstance(value, bool):
        value, value_type = "true" if value else "false", "boolean"
    elif kind == "choice":
        value, value_type = row.shown.get(key, str(value)), "text"
    elif isinstance(value, (str, int, float)):
        value_type = "text"
    else:
        raise BusinessToolContractError("the application returned an unsupported field value")
    return {"value": str(value), "value_type": value_type, "unit": unit}
