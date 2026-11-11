"""Bounded read-only SQL over rows already observed by this turn's actor."""
from __future__ import annotations

from datetime import date, datetime, timedelta
import json
import math
import re
import secrets
import sqlite3
import time
from zoneinfo import ZoneInfo

from web_operation_agent.privacy import has_unredacted_sensitive_assignment
from .api_content import redacted
from .goal import _SECRET


MAX_COLUMNS = 128
MAX_ROWS = 5000
MAX_TABLES = 36
MAX_SQL_ROWS = 200
MAX_SQL_CHARS = 14000
_FUNCTIONS = frozenset("""
abs avg coalesce count date datetime dense_rank first_value group_concat ifnull iif
instr json_array_length json_extract json_type julianday lag last_value lead length
lower ltrim max min nullif rank replace round row_number rtrim strftime substr
substring sum time total trim typeof unixepoch upper
""".split())


class TurnReadError(ValueError):
    """A bounded error suitable for returning as tool feedback."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _quoted(identifier):
    return '"' + identifier.replace('"', '""') + '"'


def _secret_field(key):
    return bool(_SECRET.search(key) or re.search(r"密码|口令|密钥|令牌", key))


def _cell(value):
    if value is None or isinstance(value, (bool, str)):
        return value
    if type(value) is int:
        return value if -(2**63) <= value < 2**63 else str(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class TurnTables:
    """One database and receipt namespace, discarded at turn end."""

    def __init__(self, handoffs):
        self.handoffs = handoffs
        self.connection = sqlite3.connect(":memory:", cached_statements=0)
        self.connection.execute("PRAGMA temp_store=MEMORY")
        self.connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 2_000_000)
        self.connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 9000)
        self.connection.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
        self.connection.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, MAX_COLUMNS + 1)
        self.prefix = secrets.token_hex(6)
        self.receipts = set()
        self.tables = {}

    def close(self):
        self.connection.close()
        self.receipts.clear()
        self.tables.clear()

    def issue_receipt(self):
        reference = f"ev_{self.prefix}_{len(self.receipts) + 1}"
        self.receipts.add(reference)
        return reference

    def add(self, *, path, params, rows, total, complete, published=()):
        if len(self.tables) >= MAX_TABLES or len(rows) > MAX_ROWS:
            raise TurnReadError("table_limit", "本轮行表已达容量上限。")
        rows = [row if isinstance(row, dict) else {"value": row}
                for row in redacted(rows, self.handoffs)]
        # SQLite resolves column names case insensitively. Keep the first spelling.
        columns, seen = [], {"_row"}
        for row in (*rows, dict.fromkeys(published)):
            for key in row:
                if (isinstance(key, str) and 0 < len(key) <= 100 and "\x00" not in key
                        and not _secret_field(key) and key.casefold() not in seen):
                    columns.append(key)
                    seen.add(key.casefold())
                    if len(columns) >= MAX_COLUMNS:
                        break
            if len(columns) >= MAX_COLUMNS:
                break
        name = f"t{len(self.tables) + 1}"
        field_defs = ", ".join(_quoted(key) for key in ("_row", *columns))
        insert = f"INSERT INTO {name} VALUES ({','.join('?' for _ in range(len(columns) + 1))})"
        try:
            with self.connection:
                self.connection.execute(f"CREATE TABLE {name} ({field_defs})")
                self.connection.executemany(insert, (
                    (index, *(_cell(row.get(key)) for key in columns))
                    for index, row in enumerate(rows, 1)))
        except (sqlite3.Error, ValueError, OverflowError) as exc:
            self.connection.execute(f"DROP TABLE IF EXISTS {name}")
            raise TurnReadError("table_unavailable", "未能完整保存这份行表；请缩小读取范围。") from exc
        info = {"name": name, "receipt": self.issue_receipt(), "path": path,
                "params": redacted(params, self.handoffs), "columns": columns,
                "row_count": len(rows), "total": total, "complete": complete}
        self.tables[name] = info
        return info

    def describe(self):
        return {"tables": list(self.tables.values())}

    def query(self, statement):
        if not isinstance(statement, str) or not re.match(r"\s*(?:SELECT|WITH)\b", statement, re.IGNORECASE):
            raise TurnReadError("sql_refused", "只接受一条 SQLite SELECT 或 WITH 查询。")
        deadline = time.monotonic() + 1.5
        expired = False
        sources = set()

        def authorize(action, first, second, database, _trigger):
            if action == sqlite3.SQLITE_SELECT:
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_READ and database in {"main", None} and first in self.tables:
                sources.add(first)
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_FUNCTION and second in _FUNCTIONS:
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY

        def progress():
            nonlocal expired
            expired = time.monotonic() >= deadline
            return int(expired)

        conn = self.connection
        conn.set_authorizer(authorize)
        conn.set_progress_handler(progress, 500)
        try:
            cursor = conn.execute(statement)
            columns = [item[0] for item in cursor.description]
            result = []
            size = 0
            truncated = False
            for row in cursor:
                safe_row = redacted(list(row), self.handoffs)
                size += len(json.dumps(safe_row, ensure_ascii=False))
                if len(result) >= MAX_SQL_ROWS or size > MAX_SQL_CHARS:
                    truncated = True
                    break
                result.append(safe_row)
        except sqlite3.Error as exc:
            code = getattr(exc, "sqlite_errorname", "SQLITE_ERROR")
            raise TurnReadError("sql_timeout" if expired else "sql_refused",
                                "查询超过本轮计算时限。" if expired else
                                f"查询未执行或未能完成（{code}）；核对语句、表和列。") from exc
        finally:
            conn.set_progress_handler(None, 0)
            conn.set_authorizer(None)
        if not sources or any(_secret_field(col) for col in columns):
            raise TurnReadError("sql_refused", "查询必须读取本轮行表，且不能输出凭据字段。")
        return {"receipt": self.issue_receipt(), "columns": columns, "rows": result,
                "returned_rows": len(result), "truncated": truncated,
                "sources": [self.tables[name]["receipt"] for name in sorted(sources)]}

    def validate_delivery(self, answer, items):
        if any(has_unredacted_sensitive_assignment(value) or self.handoffs.has_unredacted(value)
               for value in (answer, *(item["item"] for item in items))):
            raise TurnReadError("delivery_sensitive", "交付内容含凭据；删去后重新交付。")
        for item in items:
            if item["status"] == "delivered" and not item["receipts"]:
                raise TurnReadError("delivery_missing_receipt", "已交付项必须引用至少一份本轮回执。")
            if any(ref not in self.receipts for ref in item["receipts"]):
                raise TurnReadError("delivery_foreign_receipt", "有回执不存在或不属于本轮；请用本轮工具返回的编号。")


def date_range(*, today, zone, unit, offset=0, elapsed=None, year=None, month=None,
               weekday=None, scope="occurred", days=None, start_date=None, end_date=None):
    """Resolve explicit Gregorian boundaries in the application actor's time zone."""
    if type(offset) is not int or not -366 <= offset <= 366:
        raise TurnReadError("date_range", "日期偏移超出范围。")
    try:
        anchor = date.fromisoformat(today)
        tz = ZoneInfo(zone)
        if scope not in {"occurred", "planned"} or elapsed is not None and type(elapsed) is not bool:
            raise ValueError()
        explicit = start_date is not None or end_date is not None
        if explicit != (unit == "range") or (days is not None) != (unit == "rolling_days"):
            raise ValueError()
        if unit in {"range", "rolling_days"} and (offset or year is not None or
                                                  month is not None or weekday is not None):
            raise ValueError()
        if weekday is not None and (unit != "week" or type(weekday) is not int or not 1 <= weekday <= 7):
            raise ValueError()
        if unit == "range":
            start = date.fromisoformat(start_date)
            stop = date.fromisoformat(end_date) + timedelta(days=1)
            if start >= stop:
                raise ValueError()
        elif unit == "rolling_days":
            if type(days) is not int or not 1 <= days <= 36600:
                raise ValueError()
            start = anchor - timedelta(days=days - 1)
            stop = anchor + timedelta(days=1)
        elif year is not None or month is not None:
            if offset or weekday is not None:
                raise ValueError()
            if unit == "year" and month is None:
                start = date(year, 1, 1)
            elif unit == "month" and month is not None:
                start = date(year if year is not None else anchor.year, month, 1)
            else:
                raise ValueError()
        elif unit == "day":
            start = anchor + timedelta(days=offset)
        elif unit == "week":
            start = anchor - timedelta(days=anchor.weekday()) + timedelta(weeks=offset)
        elif unit in {"month", "quarter", "year"}:
            stride = {"month": 1, "quarter": 3, "year": 12}[unit]
            serial = anchor.year * 12 + (anchor.month - 1) // stride * stride + offset * stride
            start = date(serial // 12, serial % 12 + 1, 1)
        else:
            raise ValueError()
        if unit in {"range", "rolling_days"}:
            pass
        elif weekday is not None:
            start += timedelta(days=weekday - 1)
            stop = start + timedelta(days=1)
        elif unit in {"day", "week"}:
            stop = start + timedelta(days=1 if unit == "day" else 7)
        else:
            serial = start.year * 12 + start.month - 1 + {"month": 1, "quarter": 3, "year": 12}[unit]
            stop = date(serial // 12, serial % 12 + 1, 1)
        to_today = scope == "occurred" if elapsed is None else elapsed
        if to_today and not explicit and weekday is None and start <= anchor < stop:
            stop = anchor + timedelta(days=1)
        begin_dt = datetime(start.year, start.month, start.day, tzinfo=tz)
        stop_dt = datetime(stop.year, stop.month, stop.day, tzinfo=tz)
    except (ValueError, OverflowError, TypeError) as exc:
        raise TurnReadError("date_range", "日期或口径无效；请核对年份、月份和周期。") from exc
    return {"start": start.isoformat(), "end_inclusive": (stop - timedelta(days=1)).isoformat(),
            "end_exclusive": stop.isoformat(), "start_ms": int(begin_dt.timestamp() * 1000),
            "end_exclusive_ms": int(stop_dt.timestamp() * 1000), "time_zone": zone}
