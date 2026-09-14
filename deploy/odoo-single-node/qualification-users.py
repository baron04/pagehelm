"""Odoo-shell program for the isolated deployment qualification profile only."""
from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path

from odoo import fields


OUTPUT = Path("/run/woa-generated")
DATABASE = "woa_trial"
READ_SCOPES = [
    "customers:read", "invoices:read", "products:read",
    "sale_orders:read",
]
WRITE_SCOPES = READ_SCOPES + [
    "customers:write", "invoices:write", "sale_orders:write",
]


def secret(name):
    value = Path("/run/woa-private", name).read_text().strip()
    if len(value) < 20:
        raise RuntimeError(name + " is too short")
    return value


def group(*xmlids):
    return [env.ref(xmlid).id for xmlid in xmlids]


def user(login, name, password, groups):
    users = env["res.users"].sudo().with_context(no_reset_password=True)
    found = users.search([("login", "=", login)], limit=1)
    values = {
        "name": name,
        "login": login,
        "email": login,
        "password": password,
        "company_id": env.company.id,
        "company_ids": [(6, 0, [env.company.id])],
        "groups_id": [(6, 0, groups)],
        "active": True,
    }
    if found:
        found.write(values)
        return found
    return users.create(values)


def issue(owner, name):
    keys = env["res.users.apikeys"].sudo()
    keys.search([("user_id", "=", owner.id), ("name", "=", name)]).unlink()
    return keys.with_user(owner)._generate(
        "rpc", name, fields.Datetime.now() + timedelta(days=90))


writer = user(
    "woa-writer@example.test", "WOA Qualification Writer",
    secret("writer-login-password"),
    group("base.group_user", "sales_team.group_sale_manager",
          "account.group_account_manager"),
)
reader = user(
    "woa-reader@example.test", "WOA Qualification Reader",
    secret("reader-login-password"),
    group("base.group_user", "sales_team.group_sale_salesman_all_leads",
          "account.group_account_readonly"),
)
writer_key = issue(writer, "woa-single-node-writer")
reader_key = issue(reader, "woa-single-node-reader")
env.cr.commit()

OUTPUT.mkdir(mode=0o700, exist_ok=True)
payloads = {
    "writer.api-key.json": {
        "database": DATABASE, "login": writer.login, "api_key": writer_key,
    },
    "reader.api-key.json": {
        "database": DATABASE, "login": reader.login, "api_key": reader_key,
    },
    "delegations.json": {
        "schema_version": 1,
        "principals": {
            f"{DATABASE}:{writer.id}": {
                "api_key_file": "writer.api-key.json",
                "scopes": WRITE_SCOPES,
                "allow_mutations": True,
            },
            f"{DATABASE}:{reader.id}": {
                "api_key_file": "reader.api-key.json",
                "scopes": READ_SCOPES,
                "allow_mutations": False,
            },
        },
    },
}
for filename, payload in payloads.items():
    path = OUTPUT / filename
    path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    os.chmod(path, 0o600)
