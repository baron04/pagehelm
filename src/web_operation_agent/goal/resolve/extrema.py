"""Numeric extremes over a complete, actor-scoped read; never a top-N sample."""
from decimal import Decimal
import math

from ..data import Row
from ..ports import Aggregate, Resolved

EXTREME_READ_LIMIT = 4000


def resolve_extreme(*, operation: str, label: str, key: str, subject: str,
                    count: int, after: int, rows: tuple[Row, ...]) -> Resolved:
    def refused(reason):
        return Resolved(kind="aggregate", problem=reason)

    if count != after or len(rows) != count or len({r.id for r in rows}) != count \
            or any(not r.id or r.id == "None" for r in rows):
        return refused("Cannot establish an extreme from an incomplete or changing scope.")
    known = []
    for row in rows:
        if key not in row.values:
            return refused(f"Cannot compare unread field {label!r}.")
        value = row.values[key]
        if value is None or value == "":
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return refused(f"Cannot compare invalid numeric values of {label!r}.")
        known.append((Decimal(str(value)), row))
    units = {row.units.get(key, "") for _, row in known}
    if "unknown-currency" in units or len(units) > 1:
        return refused(f"Cannot compare {label!r} across different or missing units/currencies.")
    if not known:
        return Resolved(kind="aggregate", aggregate=Aggregate(
            subject=subject, count=count, extreme=(operation, label, None)))
    value = (max if operation == "max" else min)(v for v, _ in known)
    tied = sorted((row for v, row in known if v == value), key=lambda r: r.id)
    display = tied[0].shown.get(key) or format(value, ",.2f")
    return Resolved(kind="aggregate", aggregate=Aggregate(
        subject=subject, count=count, extreme=(operation, label, display),
        records=tuple(row.display for row in tied)))
