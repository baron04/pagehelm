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

from web_operation_agent.privacy import has_unredacted_sensitive_assignment, redact_sensitive_assignments
from .api_content import redacted
from ..goal_compat.goal import _SECRET


MAX_COLUMNS = 128
MAX_ROWS = 5000
MAX_TABLES = 36
MAX_SQL_ROWS = 200
MAX_SQL_CHARS = 14000
EXPORT_MAX_COLS = 24
EXPORT_MAX_ROWS = 2000
EXPORT_CELL_CHARS = 300
EXPORT_TITLE_CHARS = 120
_FUNCTIONS = frozenset("""
abs avg coalesce count date datetime dense_rank first_value glob group_concat
ifnull iif instr json_array_length json_extract json_type julianday lag last_value
lead length like lower ltrim match max min nullif rank replace round row_number
rtrim strftime substr substring sum time total trim typeof unixepoch upper
""".split())


class TurnReadError(ValueError):
    """A bounded error suitable for returning as tool feedback."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _quoted(identifier):
    return '"' + identifier.replace('"', '""') + '"'


def _strip_comments(statement):
    """Remove `--` and `/* */` comments, honouring string literals. Only used
    for parsing: the wrapped probe query re-embeds the cleaned text, which is
    semantically identical."""
    out, i, end = [], 0, len(statement)
    while i < end:
        ch = statement[i]
        if ch in "'\"`":
            j = i + 1
            while j < end and (statement[j] != ch or
                               j + 1 < end and statement[j + 1] == ch):
                j += 2 if j + 1 < end and statement[j] == ch and statement[j + 1] == ch else 1
            out.append(statement[i:j + 1])
            i = j + 1
            continue
        if statement.startswith("--", i):
            stop = statement.find("\n", i)
            i = end if stop < 0 else stop
            continue
        if statement.startswith("/*", i):
            stop = statement.find("*/", i + 2)
            if stop < 0:
                break
            out.append(" ")
            i = stop + 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


_KEYWORDS = re.compile(r"(?i)\bORDER\s+BY\b|\bLIMIT\b|\bOFFSET\b")


def _top_level(statement):
    """(keyword, start, end) marks outside parentheses and string literals."""
    marks, depth, i, end = [], 0, 0, len(statement)
    while i < end:
        char = statement[i]
        if char in "'\"`":
            stop = i + 1
            while stop < end and (statement[stop] != char or
                                  stop + 1 < end and statement[stop + 1] == char):
                stop += 2 if statement[stop] == char else 1
            i = stop + 1
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            match = _KEYWORDS.match(statement, i)
            if match is not None:
                marks.append((match.group(0).split()[0].upper(), i, match.end()))
                i = match.end()
                continue
        i += 1
    return marks


def _split_top(text):
    parts, depth, start, i = [], 0, 0, 0
    while i < len(text):
        char = text[i]
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            parts.append(text[start:i])
            start = i + 1
        i += 1
    parts.append(text[start:])
    return parts


def _split_direction(term):
    """Split an ORDER BY term into (key part, direction/NULLS suffix). A term
    that opens with a quoted identifier keeps everything through its closing
    quote as the key -- `ORDER BY "x DESC"` names a column literally called
    `x DESC`, it does not order descending."""
    t = term.strip()
    if t[:1] in "\"'`[":
        close = {"\"": '"', "'": "'", "`": "`", "[": "]"}[t[0]]
        i = 1
        while i < len(t):
            if t[i] == "\\":
                i += 2
                continue
            if t[0] == '"' and t[i:i + 2] == '""':
                i += 2
                continue
            if t[i] == close:
                rest = t[i + 1:].strip()
                if rest and not re.fullmatch(
                        r"(?i)(?:(?:ASC|DESC)(?:\s+NULLS\s+(?:FIRST|LAST))?|NULLS\s+(?:FIRST|LAST))",
                        rest):
                    return t, ""
                return t[:i + 1], rest
            i += 1
        return t, ""
    core = re.sub(r"(?i)\s+(?:ASC|DESC)\b(?:\s+NULLS\s+(?:FIRST|LAST)\b)?\s*$", "", t)
    return core.rstrip(), t[len(core.rstrip()):].strip()


def _order_key(core):
    """The output-column name an ORDER BY key resolves to: qualified prefixes
    (`t.col`, `"t"."col"`, `[t].[col]`) peel off, then one quoting layer drops.
    A quoted name containing a dot (`"my.col"`) stays whole."""
    t = core.strip()
    while True:
        m = re.match(r'^(?:"(?:[^"]|"")*"|`[^`]*`|\[[^\]]*\]|[\w]+)\s*\.\s*', t)
        if not m:
            break
        t = t[m.end():]
    if len(t) >= 2 and t[0] == '"' and t[-1] == '"':
        return t[1:-1].replace('""', '"')
    if len(t) >= 2 and ((t[0] == "`" and t[-1] == "`") or (t[0] == "[" and t[-1] == "]")):
        return t[1:-1]
    return t


def _secret_field(key):
    return bool(_SECRET.search(key) or re.search(r"密码|口令|密钥|令牌", key))


def _cell(value):
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if type(value) is int:
        return value if -(2**63) <= value < 2**63 else str(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _export_cell(value, handoffs) -> str:
    if value is None:
        raw = ""
    elif isinstance(value, bool):
        raw = "true" if value else "false"
    elif isinstance(value, (int, float)):
        raw = str(value)
    elif isinstance(value, str):
        raw = value
    else:
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    cleaned = redact_sensitive_assignments(redacted(raw, handoffs))
    return " ".join(str(cleaned).split())[:EXPORT_CELL_CHARS]


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
        self.queries = {}

    def close(self):
        self.connection.close()
        self.receipts.clear()
        self.tables.clear()
        self.queries.clear()

    def issue_receipt(self):
        reference = f"ev_{self.prefix}_{len(self.receipts) + 1}"
        self.receipts.add(reference)
        return reference

    def add(
        self,
        *,
        path,
        params,
        rows,
        total,
        complete,
        published=(),
        column_labels=None,
        code_maps=None,
        table_name: str | None = None,
    ):
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
        raw_columns = list(columns)
        derived_maps: list[tuple[str, str, dict[str, str]]] = []
        if isinstance(code_maps, dict):
            for raw_col in raw_columns:
                cmap = code_maps.get(raw_col)
                if not isinstance(cmap, dict) or not cmap:
                    continue
                label_col = f"{raw_col}_label"
                if (len(columns) < MAX_COLUMNS and label_col.casefold() not in seen
                        and not _secret_field(label_col)):
                    columns.append(label_col)
                    seen.add(label_col.casefold())
                    derived_maps.append((
                        label_col,
                        raw_col.casefold(),
                        {str(k): str(v) for k, v in cmap.items()},
                    ))
        if table_name is not None:
            if not re.fullmatch(r"t\d+(?:_sib\d*)?", table_name) or table_name in self.tables:
                raise TurnReadError("table_limit", "本轮行表名称冲突或非法。")
            name = table_name
        else:
            primary_count = sum(1 for k in self.tables if "_sib" not in k)
            name = f"t{primary_count + 1}"
        field_defs = ", ".join(_quoted(key) for key in ("_row", *columns))
        insert = f"INSERT INTO {name} VALUES ({','.join('?' for _ in range(len(columns) + 1))})"
        folded_keys = [key.casefold() for key in raw_columns]

        def values(row):
            folded = {}
            for k, v in row.items():
                folded.setdefault(str(k).casefold(), v)
            base_vals = [_cell(folded.get(key)) for key in folded_keys]
            for _lbl_col, raw_folded, cmap in derived_maps:
                raw_val = folded.get(raw_folded)
                mapped = cmap.get(str(raw_val)) if raw_val is not None and not isinstance(raw_val, (dict, list)) else None
                base_vals.append(_cell(mapped))
            return tuple(base_vals)

        try:
            with self.connection:
                self.connection.execute(f"CREATE TABLE {name} ({field_defs})")
                self.connection.executemany(insert, (
                    (index, *values(row)) for index, row in enumerate(rows, 1)))
        except (sqlite3.Error, ValueError, OverflowError) as exc:
            self.connection.execute(f"DROP TABLE IF EXISTS {name}")
            raise TurnReadError("table_unavailable", "未能完整保存这份行表；请缩小读取范围。") from exc
        info = {"name": name, "receipt": self.issue_receipt(), "path": path,
                "params": redacted(params, self.handoffs), "columns": columns,
                "row_count": len(rows), "total": total, "complete": complete}
        if isinstance(column_labels, dict) and column_labels:
            clean_labels = {
                col: str(column_labels[col])[:60]
                for col in raw_columns
                if col in column_labels and column_labels[col]
            }
            for lbl_col, raw_folded, _ in derived_maps:
                orig_col = next((c for c in raw_columns if c.casefold() == raw_folded), "")
                base_lbl = clean_labels.get(orig_col, orig_col)
                if base_lbl:
                    clean_labels[lbl_col] = f"{base_lbl}字典标签"
            if clean_labels:
                info["column_labels"] = clean_labels
        self.tables[name] = info
        return info

    def describe(self):
        queries = [
            {k: v for k, v in q.items() if k != "rows"}
            for q in self.queries.values()
        ]
        return {"tables": list(self.tables.values()), **({"queries": queries} if queries else {})}

    def receipt_for_table_ref(self, ref: str) -> str:
        key = str(ref or "").strip()
        if key in self.queries:
            return str(self.queries[key].get("receipt") or "")
        if key in self.tables:
            return str(self.tables[key].get("receipt") or "")
        return ""

    def export_table(self, ref: str) -> dict:
        if not isinstance(ref, str) or not ref.strip():
            raise TurnReadError("table_not_found", "请提供有效的表格句柄（如 t1 或 q1）。")
        key = ref.strip()
        if key in self.queries:
            entry = self.queries[key]
            raw_cols = list(entry.get("columns") or ())[:EXPORT_MAX_COLS]
            cols = [
                " ".join(redact_sensitive_assignments(redacted(str(col), self.handoffs)).split())[:80]
                for col in raw_cols
            ]
            if not cols or any(not col for col in cols):
                raise TurnReadError("table_unavailable", "该查询结果缺少可导出的列名。")
            raw_rows = list(entry.get("rows") or ())[:EXPORT_MAX_ROWS]
            rows = [
                [_export_cell(row[idx] if idx < len(row) else "", self.handoffs) for idx in range(len(cols))]
                for row in raw_rows
            ]
            total_rows = max(len(rows), int(entry.get("total_rows") or len(rows)))
            title = " ".join(
                redact_sensitive_assignments(redacted(str(entry.get("title") or key), self.handoffs)).split()
            )[:EXPORT_TITLE_CHARS] or key
            return {"title": title, "columns": cols, "rows": rows, "total_rows": total_rows}
        if key in self.tables:
            info = self.tables[key]
            cols = [
                " ".join(redact_sensitive_assignments(redacted(str(col), self.handoffs)).split())[:80]
                for col in list(info.get("columns") or ())[:EXPORT_MAX_COLS]
            ]
            raw_cols = list(info.get("columns") or ())[:EXPORT_MAX_COLS]
            if not raw_cols or not cols or any(not col for col in cols):
                raise TurnReadError("table_unavailable", "该行表没有可导出的列。")
            select_cols = ", ".join(_quoted(col) for col in raw_cols)
            cursor = self.connection.execute(f"SELECT {select_cols} FROM {key} ORDER BY _row LIMIT ?", (EXPORT_MAX_ROWS,))
            rows = [
                [_export_cell(row[idx] if idx < len(row) else "", self.handoffs) for idx in range(len(raw_cols))]
                for row in cursor
            ]
            declared_total = info.get("total")
            total_rows = declared_total if isinstance(declared_total, int) and declared_total >= len(rows) else len(rows)
            title = " ".join(
                redact_sensitive_assignments(redacted(str(info.get("path") or key), self.handoffs)).split()
            )[:EXPORT_TITLE_CHARS] or key
            return {"title": title, "columns": cols, "rows": rows, "total_rows": total_rows}
        raise TurnReadError("table_not_found", f"本轮不存在表格或查询结果引用：{key}。")

    def query(self, statement):
        if not isinstance(statement, str) or not re.match(r"\s*(?:SELECT|WITH)\b", statement, re.IGNORECASE):
            raise TurnReadError("sql_refused", "只接受一条 SQLite SELECT 或 WITH 查询。")
        deadline = time.monotonic() + 1.5
        expired = False
        sources = set()

        def authorize(action, first, second, database, _trigger):
            if action == sqlite3.SQLITE_SELECT:
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_READ and database in {"main", None} \
                    and str(first).casefold() in {name.casefold() for name in self.tables}:
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
            export_rows = []
            total_rows = 0
            size = 0
            truncated = False
            for row in cursor:
                safe_row = [value.hex() if isinstance(value, bytes) else value
                            for value in redacted(list(row), self.handoffs)]
                total_rows += 1
                if len(export_rows) < EXPORT_MAX_ROWS:
                    export_rows.append(safe_row)
                if not truncated:
                    size += len(json.dumps(safe_row, ensure_ascii=False))
                    if len(result) >= MAX_SQL_ROWS or size > MAX_SQL_CHARS:
                        truncated = True
                    else:
                        result.append(safe_row)
                elif len(export_rows) >= EXPORT_MAX_ROWS:
                    # Keep counting bounded so total_rows reflects larger results without stalling.
                    if total_rows >= EXPORT_MAX_ROWS + 500:
                        break
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
        qname = f"q{len(self.queries) + 1}"
        receipt = self.issue_receipt()
        out = {"name": qname, "table_ref": qname, "receipt": receipt, "columns": columns, "rows": result,
               "returned_rows": len(result), "truncated": truncated,
               "sources": [self.tables[name]["receipt"] for name in sorted(sources)],
               "sources_complete": all(self.tables[name]["complete"] for name in sources)}
        tie = self._tie_boundary(conn, statement, authorize, deadline)
        if tie is not None:
            out["tie"] = tie
            rows_out = out["rows"]
            verified = tie["complete"] and out["sources_complete"] and not truncated
            if tie["tied"]:
                extra = [row for row in tie["extra_rows"] if row not in rows_out]
                extra = extra[:max(0, MAX_SQL_ROWS - len(rows_out))]
                out["rows"] = rows_out = rows_out + extra
                out["returned_rows"] = len(rows_out)
                for row in extra:
                    if len(export_rows) < EXPORT_MAX_ROWS and row not in export_rows:
                        export_rows.append(row)
                total_rows = max(total_rows, len(export_rows))
                group = "该并列组共" + str(tie["group_rows"]) + "行" if tie.get("group_rows") else "并列组行数未知"
                out["tie_note"] = (f"第 {tie['at']} 行与后续行按排序键同值并列，{group}，"
                                   f"已按上限返回 {len(extra)} 行。" if verified else
                                   f"第 {tie['at']} 行与后续行同值并列；并列组未读全或来源未读全，"
                                   "排名与并列均标为未查全。")
            elif not verified:
                reason = ("来源行表未读全或结果已截断，" if (not out["sources_complete"] or truncated)
                          else "")
                out["tie_note"] = (f"{reason}第 {tie['at']} 行之外无法核对并列；排名与并列均标为未查全。")
        else:
            extremum_note = self._scalar_extremum_note(conn, statement, sources, result, authorize, deadline)
            if extremum_note:
                out["tie_note"] = extremum_note
        source_paths = [str(self.tables[name].get("path") or name) for name in sorted(sources)]
        title = " / ".join(dict.fromkeys(source_paths)) if source_paths else qname
        self.queries[qname] = {
            "name": qname,
            "receipt": receipt,
            "title": title[:EXPORT_TITLE_CHARS],
            "columns": list(columns),
            "rows": export_rows,
            "row_count": len(export_rows),
            "total_rows": max(total_rows, len(export_rows)),
            "sources": out["sources"],
            "sources_complete": out["sources_complete"],
        }
        return out

    def _scalar_extremum_note(self, conn, statement, sources, rows_out, authorize, deadline) -> str:
        """When a scalar ``SELECT MIN(col)`` or ``MAX(col)`` on one table matches multiple rows,
        remind the caller so a follow-up answer never silently drops tied winners."""
        if len(sources) != 1 or len(rows_out) != 1 or not rows_out[0]:
            return ""
        stmt = _strip_comments(statement).rstrip().rstrip(";").rstrip()
        if re.search(r"\bGROUP\s+BY\b", stmt, re.IGNORECASE):
            return ""
        stmt = re.sub(r"(?is)\s+(?:ORDER\s+BY\b.*|LIMIT\s+\d+(?:\s*,\s*\d+|\s+OFFSET\s+\d+)?)\s*$", "", stmt)
        m = re.match(
            r"(?is)^\s*SELECT\s+(?:MIN|MAX)\s*\(\s*"
            r"((?:[A-Za-z_][A-Za-z0-9_]*\.)?(?:[A-Za-z_][A-Za-z0-9_]*|\"[^\"]+\"))\s*\)"
            r"(?:\s+(?:AS\s+)?(?:\"[^\"]+\"|[A-Za-z_][A-Za-z0-9_]*))?\s+FROM\s+([A-Za-z_][A-Za-z0-9_]*)"
            r"(?:\s+(?:AS\s+)?(?!WHERE\b|GROUP\b|ORDER\b|LIMIT\b)([A-Za-z_][A-Za-z0-9_]*))?"
            r"(?:\s+WHERE\s+(.+))?\s*$",
            stmt,
        )
        if m is None:
            return ""
        col, table, alias, where = m.group(1), m.group(2), m.group(3), m.group(4)
        value = rows_out[0][0]
        if value is None or table.casefold() not in {name.casefold() for name in self.tables}:
            return ""
        from_clause = f"{table} {alias}" if alias else table
        sql = f"SELECT COUNT(*) FROM {from_clause} WHERE ({col}) = ?" + (f" AND ({where})" if where else "")
        conn.set_authorizer(authorize)
        conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 500)
        try:
            count = conn.execute(sql, (value,)).fetchone()[0]
        except sqlite3.Error:
            return ""
        finally:
            conn.set_progress_handler(None, 0)
            conn.set_authorizer(None)
        return (f"该极值对应的记录在本轮行表中共有 {count} 行同值并列；若需列出对应记录，须完整给出全部 {count} 行并列项。"
                if isinstance(count, int) and count >= 2 else "")

    def _tie_boundary(self, conn, statement, authorize, deadline):
        """Top-level ``ORDER BY <terms> LIMIT <k>``: fetch row k+1 and, when it ties
        with row k on every order key, the whole tied group. Order terms that are not
        plain output columns or positions leave the boundary unchecked and said so."""
        stmt = _strip_comments(statement).rstrip().rstrip(";").rstrip()
        marks = _top_level(stmt)
        order = next((m for m in reversed(marks) if m[0] == "ORDER"), None)
        limit = next((m for m in reversed(marks) if m[0] == "LIMIT"), None)
        if order is None or limit is None or order[1] > limit[1]:
            return None

        def mark(complete, **fields):
            return {"at": start + k, "tied": False, "complete": complete,
                    "extra_rows": [], "group_rows": None, **fields}

        offset = next((m for m in reversed(marks) if m[0] == "OFFSET" and m[1] > limit[1]), None)
        limit_end = offset[1] if offset is not None else len(stmt)
        m = re.match(r"\s*([0-9]+)\s*$", stmt[limit[2]:limit_end])
        comma = re.match(r"\s*([0-9]+)\s*,\s*([0-9]+)\s*$", stmt[limit[2]:limit_end])
        start = k = 0
        if m is not None:
            k = int(m.group(1))
        elif comma is not None:
            start, k = int(comma.group(1)), int(comma.group(2))
        else:
            return None
        if offset is not None:
            if not re.fullmatch(r"\s*[0-9]+\s*", stmt[offset[2]:]):
                return mark(False, unchecked=True)
            start += int(stmt[offset[2]:].strip())
        if k < 1 or k >= MAX_SQL_ROWS:
            return None
        unchecked = mark(False, unchecked=True)
        if start + k >= MAX_SQL_ROWS:
            return mark(False, unreachable=True)
        order_text = stmt[order[2]:limit[1]]
        terms_full = [t.strip() for t in _split_top(order_text) if t.strip()]
        parsed = [_split_direction(t) for t in terms_full]
        terms = [core for core, _suffix in parsed]
        if not terms or not all(terms):
            return unchecked
        inner = stmt[:order[1]].strip()
        if not re.match(r"(?is)^(?:SELECT|WITH)\b", inner):
            return unchecked
        # The probe re-emits ORDER BY against the subquery's output columns:
        # keys are quoted identifiers (any content is safe once `"` doubles),
        # directions/NULLS keep their spelling, and only an unquoted integer
        # literal stays positional -- `ORDER BY "1"` names a column, not a spot.
        wrap_terms = []
        for core, suffix in parsed:
            direction = f" {suffix}" if suffix else ""
            if re.fullmatch(r"[0-9]+", core.strip()):
                wrap_terms.append(core.strip() + direction)
            else:
                wrap_terms.append('"' + _order_key(core).replace('"', '""') + '"' + direction)

        def probe(n):
            conn.set_authorizer(authorize)
            conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 500)
            try:
                cursor = conn.execute(
                    f"SELECT * FROM ({inner}) ORDER BY {', '.join(wrap_terms)} LIMIT {n}")
                cols = [item[0] for item in cursor.description]
                return cols, cursor.fetchall()
            except sqlite3.Error:
                return None, None
            finally:
                conn.set_progress_handler(None, 0)
                conn.set_authorizer(None)

        probe_cols, rows = probe(start + k + 1)
        if rows is None:
            return unchecked
        if len(rows) <= start + k:
            return mark(True)
        lowered = {}
        for i, col in enumerate(probe_cols):
            lowered.setdefault(col.casefold(), i)
        tie_terms = list(terms)
        while len(tie_terms) >= 2:
            tail = tie_terms[-1].strip()
            if not re.fullmatch(r"[0-9]+", tail) and _order_key(tail).casefold() in {"id", "_row"}:
                tie_terms.pop()
            else:
                break
        idx = []
        for term in tie_terms:
            if re.fullmatch(r"[0-9]+", term.strip()):
                idx.append(int(term.strip()) - 1)
            else:
                key = _order_key(term).casefold()
                if key in lowered:
                    idx.append(lowered[key])
                else:
                    return unchecked
        if any(i < 0 or i >= len(probe_cols) for i in idx):
            return unchecked
        key = lambda row: tuple(row[i] for i in idx)
        boundary = key(rows[start + k - 1])
        if key(rows[start + k]) != boundary:
            return mark(True)
        leading = start > 0 and key(rows[start - 1]) == boundary
        _, widened = probe(MAX_SQL_ROWS + 1)
        if widened is None:
            return mark(False, tied=True)
        same = [row for row in widened if key(row) == boundary]
        ended = len(widened) <= MAX_SQL_ROWS or key(widened[-1]) != boundary
        extra = [redacted(list(row), self.handoffs)
                 for row in widened[start + k:] if key(row) == boundary]
        return {"at": start + k, "tied": True, "complete": ended and not leading,
                "extra_rows": extra, "group_rows": len(same), "leading_group": leading}

    def validate_delivery(self, answer, items):
        if any(has_unredacted_sensitive_assignment(value) or self.handoffs.has_unredacted(value)
               for value in (answer, *(item["item"] for item in items))):
            raise TurnReadError("delivery_sensitive", "交付内容含凭据；删去后重新交付。")
        for item in items:
            if item.get("kind") == "page":
                continue          # a page is checked as the user's page stands, not by a quoted receipt
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
