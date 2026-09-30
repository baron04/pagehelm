"""Derive MySQL DDL for yudao business tables from the MIT-licensed entity classes.

Upstream's MIT schema (sql/mysql/ruoyi-vue-pro.sql) only creates the system and
infra tables; the business-module schemas are distributed under separate terms.
This script reconstructs every table that the MIT code maps but the MIT schema
does not create, using only the checkout's own `@TableName` data objects:

- column names follow MyBatis-Plus defaults (camelCase -> snake_case) unless
  `@TableField("name")` overrides them; `exist = false`, static and transient
  fields are skipped;
- `tenant_id` is added unless the class carries `@TenantIgnore`, matching the
  framework's rule that every table is tenant-scoped by default;
- a Long `@TableId` becomes an auto-increment primary key (the framework's
  `id-type: NONE` resolves to AUTO on MySQL); an INPUT id is stored as given;
- every non-key column is nullable, so inserts that omit a field succeed;
- every non-key String is `text`: entities declare no lengths;
- Integer, Short, Byte, BigDecimal and Boolean columns default to zero, which
  services rely on for initial states, unless the code anywhere tests that
  property for null; a column whose NULL is tested only indirectly may be wrong;
- the first sentence of each Javadoc becomes the column or table COMMENT.

Lengths, indexes and defaults are approximations, not upstream's choices.

Usage: generate_ddl.py <server-checkout> > business-schema.sql
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

CLASS_RE = re.compile(r"\bclass\s+(\w+)(?:<[^>{]*>)?(?:\s+extends\s+(\w+))?")
TABLE_NAME_RE = re.compile(r'@TableName\(\s*(?:value\s*=\s*)?"([^"]+)"')
FIELD_RE = re.compile(
    r"((?:/\*\*(?:(?!\*/).)*\*/\s*)?)((?:@\w+(?:\([^;{}]*?\))?\s*)*)"
    r"private\s+((?:static\s+|final\s+|transient\s+)*)([\w.<>, ?\[\]]+?)\s+(\w+)\s*(?:=[^;]*)?;",
    re.S,
)
# Services rely on column defaults (a new CRM contract's audit status is the
# column's 0, "draft"), so small numbers, amounts and flags default to zero.
# Long columns are mostly references and stay NULL, and so does any property the
# code tests for null (a CRM opportunity is open while `endStatus` is null).
NULL_TEST_RES = [
    re.compile(r"\bget(\w+)\(\)\s*[!=]=\s*null"),
    re.compile(r"\bnull\s*[!=]=\s*\w+\.get(\w+)\(\)"),
    re.compile(r"\b(?:isNull|nonNull|isNotNull|notNull|isEmpty|isNotEmpty|anyNull|allNotNull)\(\s*\w+\.get(\w+)\(\)\s*\)"),
]
ZERO_DEFAULT = {"int", "smallint", "tinyint", "decimal(24,6)"}
CREATE_RE = re.compile(r"CREATE TABLE\s+`?(\w+)`?", re.I)


@dataclass
class Column:
    name: str
    java_type: str
    comment: str
    annotations: str
    is_id: bool = False


@dataclass
class Entity:
    cls: str
    parent: str | None
    table: str | None
    comment: str
    tenant_ignore: bool
    columns: list[Column] = field(default_factory=list)


def snake(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name).lower()


def first_sentence(javadoc: str) -> str:
    text = re.sub(r"^\s*/\*\*|\*/\s*$", "", javadoc, flags=re.S)
    lines = [re.sub(r"^\s*\*\s?", "", l).strip() for l in text.splitlines()]
    for l in lines:
        if l and not l.startswith("@"):
            l = re.sub(r"\{@link\s+([^}]+)\}", r"\1", l)
            return l.replace("'", "").replace("\\", "")[:250]
    return ""


def strip_nested_classes(body: str) -> str:
    """Drop the bodies of nested classes/enums so their fields are not mapped."""
    out, depth, i = [], 0, 0
    nested = re.compile(r"\b(?:static\s+)?(?:class|enum|interface|record)\s+\w+[^{;]*\{")
    while i < len(body):
        m = nested.search(body, i) if depth == 0 else None
        if m is None:
            out.append(body[i:])
            break
        out.append(body[i:m.start()])
        j, level = m.end(), 1
        while j < len(body) and level:
            level += {"{": 1, "}": -1}.get(body[j], 0)
            j += 1
        i = j
    return "".join(out)


def parse(path: Path) -> Entity | None:
    src = path.read_text(encoding="utf-8")
    cm = CLASS_RE.search(src)
    if not cm:
        return None
    header = src[: cm.start()]
    tm = TABLE_NAME_RE.search(header)
    docs = re.findall(r"/\*\*.*?\*/", header, re.S)
    brace = src.index("{", cm.end() - 1) if "{" in src[cm.end() - 1:] else len(src)
    body = strip_nested_classes(src[brace + 1: src.rfind("}")])
    ent = Entity(cm.group(1), cm.group(2), tm.group(1) if tm else None,
                 first_sentence(docs[-1]) if docs else "", "@TenantIgnore" in header)
    for m in FIELD_RE.finditer(body):
        doc, ann, mods, jtype, name = m.groups()
        if "static" in mods or "transient" in mods or re.search(r"exist\s*=\s*false", ann):
            continue
        col = snake(name)
        named = re.search(r'@TableField\(\s*(?:value\s*=\s*)?"([^"]+)"', ann)
        if named:
            col = named.group(1).strip("`")
        ent.columns.append(Column(col, jtype.strip(), first_sentence(doc) if doc else "", ann, "@TableId" in ann))
    return ent


def sql_type(c: Column) -> str:
    t, a = c.java_type, c.annotations
    if "typeHandler" in a:
        return "varchar(1024)" if "ListTypeHandler" in a or "SetTypeHandler" in a else "longtext"
    base = t.split("<")[0].split(".")[-1]
    simple = {
        "Long": "bigint", "long": "bigint", "Integer": "int", "int": "int",
        "Short": "smallint", "short": "smallint", "Byte": "tinyint", "byte": "tinyint",
        "Boolean": "bit(1)", "boolean": "bit(1)", "BigDecimal": "decimal(24,6)",
        "Double": "double", "double": "double", "Float": "float", "float": "float",
        "LocalDateTime": "datetime", "Date": "datetime", "LocalDate": "date", "LocalTime": "time",
        "byte[]": "longblob",
    }
    if t == "byte[]":
        return "longblob"
    if base in simple:
        return simple[base]
    if base == "String":
        # Entity classes carry no length, and names do not predict it (a
        # workflow's `simple_model` holds a JSON tree), so every non-key string
        # is text; key columns are narrowed in ddl().
        return "text"
    return "longtext"  # enums of objects, nested JSON types


def resolve(ent: Entity, by_name: dict[str, Entity]) -> list[Column]:
    chain, cur, seen = [], ent, set()
    while cur and cur.cls not in seen:
        seen.add(cur.cls)
        chain.append(cur)
        cur = by_name.get(cur.parent or "")
    cols: dict[str, Column] = {}
    for e in reversed(chain):
        for c in e.columns:
            cols[c.name] = c
    return list(cols.values())


def ddl(ent: Entity, cols: list[Column], tenant: bool, null_tested: set[str]) -> str:
    lines, pk = [], None
    for c in cols:
        typ = sql_type(c)
        comment = f" COMMENT '{c.comment}'" if c.comment else ""
        if c.is_id or (pk is None and c.name == "id" and not any(x.is_id for x in cols)):
            if typ == "bigint" and "INPUT" not in c.annotations:
                lines.append(f"  `{c.name}` bigint NOT NULL AUTO_INCREMENT{comment}")
            else:
                typ = "varchar(64)" if typ in ("text", "longtext", "varchar(255)") else typ
                lines.append(f"  `{c.name}` {typ} NOT NULL{comment}")
            pk = c.name
        elif c.name == "deleted":
            lines.append(f"  `deleted` bit(1) NOT NULL DEFAULT b'0'{comment}")
        elif c.name == "create_time":
            lines.append(f"  `create_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP{comment}")
        elif c.name == "update_time":
            lines.append(f"  `update_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP{comment}")
        elif c.name in ("creator", "updater"):
            lines.append(f"  `{c.name}` varchar(64) NULL DEFAULT ''{comment}")
        elif typ == "bit(1)":
            lines.append(f"  `{c.name}` bit(1) NULL DEFAULT b'0'{comment}")
        elif typ in ZERO_DEFAULT and c.name not in null_tested:
            lines.append(f"  `{c.name}` {typ} NULL DEFAULT 0{comment}")
        else:
            lines.append(f"  `{c.name}` {typ} NULL{comment}")
    if tenant and not any(c.name == "tenant_id" for c in cols):
        lines.append("  `tenant_id` bigint NOT NULL DEFAULT 0 COMMENT '租户编号'")
    if pk:
        lines.append(f"  PRIMARY KEY (`{pk}`)")
    comment = ent.comment.replace("'", "")
    return (f"DROP TABLE IF EXISTS `{ent.table}`;\nCREATE TABLE `{ent.table}` (\n" + ",\n".join(lines)
            + f"\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='{comment}';\n")


def main() -> None:
    root = Path(sys.argv[1])
    existing = {m.lower() for f in (root / "sql" / "mysql").glob("*.sql")
                for m in CREATE_RE.findall(f.read_text(encoding="utf-8"))}
    entities = [e for p in root.rglob("*.java")
                if "/src/main/" in p.as_posix() and (e := parse(p))]
    by_name = {e.cls: e for e in entities}
    null_tested = {snake(m[0].lower() + m[1:])
                   for p in root.rglob("*.java") if "/src/main/" in p.as_posix()
                   for rx in NULL_TEST_RES for m in rx.findall(p.read_text(encoding="utf-8"))}
    out, emitted = ["SET NAMES utf8mb4;", "SET FOREIGN_KEY_CHECKS = 0;", ""], set()
    for e in sorted((e for e in entities if e.table), key=lambda e: e.table):
        if e.table.lower() in existing or e.table in emitted:
            continue
        emitted.add(e.table)
        chain_ignore = e.tenant_ignore
        out.append(ddl(e, resolve(e, by_name), tenant=not chain_ignore, null_tested=null_tested))
    out.append("SET FOREIGN_KEY_CHECKS = 1;")
    sys.stdout.write("\n".join(out) + "\n")
    print(f"generated {len(emitted)} tables; {len(existing)} already in the MIT schema", file=sys.stderr)


if __name__ == "__main__":
    main()
