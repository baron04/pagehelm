#!/usr/bin/env python3
"""The SQL that moves a yudao database from one pinned release to the next, keeping its data.

A database built by `reset.sh` holds the upstream's own schema and reference rows
(`sql/mysql/ruoyi-vue-pro.sql`: menus, dictionaries, settings, jobs) and the business
tables `generate_ddl.py` writes. Seeding then adds records, roles and grants through the
application. Rebuilding would lose those, so an upgrade is additive:

- tables the new release has and the old did not are created;
- columns a table gained are added (a column removed upstream is left in place);
- reference rows are brought to the new release: rows the new release adds are inserted,
  rows it changes are replaced, menus it removes are marked deleted;
- the new release's grants of menus to its built-in roles are added by role and menu, not
  by id, since seeded grants may already hold those ids.

Run `harden.sql` afterwards: replacing a changed menu brings back what hardening removed.

    python3 upgrade.py <old-checkout> <new-checkout> <old-business-schema.sql> \\
        <new-business-schema.sql> > upgrade.sql
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

#: Upstream reference tables brought to the new release by id.
_REFERENCE = ("system_menu", "system_dict_type", "system_dict_data", "infra_config",
              "infra_job", "system_sms_template")
_CREATE = re.compile(r"CREATE TABLE `(\w+)` \((.*?)\n\)([^;]*);", re.S)
_COLUMN = re.compile(r"^\s*`(\w+)`\s+(.*?),?\s*$", re.M)


def tables(sql: str) -> dict[str, tuple[str, dict[str, str]]]:
    """name -> (the whole CREATE statement, column -> its definition)."""
    out = {}
    for m in _CREATE.finditer(sql):
        columns = {c: d.rstrip(",") for c, d in _COLUMN.findall(m.group(2))}
        out[m.group(1)] = (m.group(0), columns)
    return out


def rows(sql: str, table: str) -> dict[int, str]:
    """id -> the INSERT statement of one row (the upstream writes one row per statement)."""
    return {int(m.group(1)): m.group(0).rstrip()
            for m in re.finditer(rf"^INSERT INTO `{table}` \([^)]*\) VALUES \((\d+),.*$", sql, re.M)}


def main(old_root: str, new_root: str, old_business: str, new_business: str) -> int:
    old_mit = Path(old_root, "sql/mysql/ruoyi-vue-pro.sql").read_text(encoding="utf-8")
    new_mit = Path(new_root, "sql/mysql/ruoyi-vue-pro.sql").read_text(encoding="utf-8")
    before = {**tables(old_mit), **tables(Path(old_business).read_text(encoding="utf-8"))}
    after = {**tables(new_mit), **tables(Path(new_business).read_text(encoding="utf-8"))}
    out = ["SET NAMES utf8mb4;", "SET FOREIGN_KEY_CHECKS = 0;", ""]
    created = altered = 0
    for name, (statement, columns) in after.items():
        if name not in before:
            out.append(statement.replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS", 1))
            created += 1
            continue
        for column, definition in columns.items():
            if column not in before[name][1] and not column.startswith(("PRIMARY", "KEY", "UNIQUE")):
                out.append(f"ALTER TABLE `{name}` ADD COLUMN `{column}` {definition};")
                altered += 1
    counts = {}
    for table in _REFERENCE:
        old, new = rows(old_mit, table), rows(new_mit, table)
        added = [new[i] for i in sorted(set(new) - set(old))]
        changed = [new[i] for i in sorted(set(new) & set(old)) if new[i] != old[i]]
        out += [s.replace("INSERT INTO", "INSERT IGNORE INTO", 1) for s in added]
        out += [s.replace("INSERT INTO", "REPLACE INTO", 1) for s in changed]
        removed = sorted(set(old) - set(new))
        if removed and table == "system_menu":
            out.append(f"UPDATE `system_menu` SET deleted = b'1' WHERE id IN ({','.join(map(str, removed))});")
        counts[table] = (len(added), len(changed), len(removed) if table == "system_menu" else 0)
    def granted(sql: str) -> set[tuple[str, str, str]]:
        out = set()
        for statement in rows(sql, "system_role_menu").values():
            values = re.search(r"VALUES \((\d+), (\d+), (\d+), .*?, (\d+)\);$", statement)
            if values is not None:
                out.add(values.groups()[1:])
        return out

    # Only the grants the new release adds: one seeding or an owner took away stays away.
    grants = 0
    for role, menu, tenant in sorted(granted(new_mit) - granted(old_mit)):
        out.append(
            "INSERT INTO `system_role_menu` (role_id, menu_id, creator, updater, tenant_id) "
            f"SELECT {role}, {menu}, '1', '1', {tenant} FROM DUAL WHERE NOT EXISTS "
            f"(SELECT 1 FROM `system_role_menu` WHERE role_id = {role} AND menu_id = {menu} "
            f"AND tenant_id = {tenant} AND deleted = b'0');")
        grants += 1
    out.append("SET FOREIGN_KEY_CHECKS = 1;")
    print("\n".join(out))
    print(f"-- tables created {created}, columns added {altered}, reference rows "
          + ", ".join(f"{t} +{a}/~{c}/-{r}" for t, (a, c, r) in counts.items())
          + f", role grants checked {grants}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 5:
        raise SystemExit(__doc__)
    raise SystemExit(main(*sys.argv[1:]))
