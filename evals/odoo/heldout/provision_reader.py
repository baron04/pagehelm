"""Give the reference fixture a non-administrator user, an API key and a browser session.

The held-out run has to answer "can this ship", and every Odoo result so far was produced
as the demo database's administrator, who can see and do everything. This creates an
ordinary salesperson instead: Odoo's own internal-user group plus sales and invoicing, and
neither Settings nor Administration. The user gets the fixture's own well-known demo
password, the same one `deploy provision` types for `admin`; it is a disposable loopback
database and the password is published with Odoo's demo data.

Both credentials land under `.private/`, 0600, and are never printed.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from web_operation_agent_odoo.deploy import (BASE_URL, DATABASE, DEMO_PASSWORD,
                                             ODOO_SERVICE, ProvisioningError, _container,
                                             _require_loopback, _run)

ROOT = Path(__file__).resolve().parents[3]
LOGIN = "woa.reader"
NAME = "WOA held-out reader"
KEY_NAME = "woa-heldout-reader"
#: What an ordinary salesperson has, and nothing else: no Settings, no user administration.
GROUPS = ("base.group_user", "sales_team.group_sale_salesman_all_leads",
          "account.group_account_invoice")
FORBIDDEN_GROUPS = ("base.group_system", "base.group_erp_manager")
#: Odoo refuses a persistent API key for anyone who is not an administrator, so a delegated
#: ordinary user's key expires and a deployment has to refresh it before it lapses. This
#: user's group allows ninety days.
KEY_DAYS = 30

_CREATE_USER = """
import datetime

groups = [env.ref(xmlid) for xmlid in {groups!r}]
user = env["res.users"].sudo().search([("login", "=", {login!r})], limit=1)
values = {{"name": {name!r}, "login": {login!r}, "password": {password!r},
          "groups_id": [(6, 0, [group.id for group in groups])]}}
if user:
    user.write(values)
else:
    user = env["res.users"].sudo().create(values)
forbidden = [xmlid for xmlid in {forbidden!r} if user.has_group(xmlid)]
if forbidden:
    raise SystemExit("the held-out reader must not hold " + ",".join(forbidden))
env.cr.commit()
existing = env["res.users.apikeys"].sudo().search(
    [("user_id", "=", user.id), ("name", "=", {key_name!r})])
existing.unlink()
expires = datetime.datetime.now() + datetime.timedelta(days={key_days})
key = env["res.users.apikeys"].with_user(user)._generate("rpc", {key_name!r}, expires)
env.cr.commit()
print("WOA_READER_UID=%s" % user.id)
print("WOA_READER_GROUPS=%s" % ",".join(sorted(user.groups_id.mapped("full_name"))))
print("WOA_READER_KEY_EXPIRES=%s" % expires.date().isoformat())
print("WOA_API_KEY=" + key)
"""


def provision(*, database: str = DATABASE, directory: Path) -> dict:
    _require_loopback(BASE_URL)
    script = _CREATE_USER.format(groups=list(GROUPS), login=LOGIN, name=NAME,
                                 password=DEMO_PASSWORD, key_name=KEY_NAME,
                                 key_days=KEY_DAYS,
                                 forbidden=list(FORBIDDEN_GROUPS)).encode("utf-8")
    output = _run(["docker", "exec", "-i", _container(ODOO_SERVICE),
                   "odoo", "shell", "-c", "/etc/odoo/odoo.conf", "-d", database,
                   "--no-http", "--log-level=warn"], stdin=script, timeout=300)
    uid = next((line.partition("=")[2].strip() for line in output.splitlines()
                if line.startswith("WOA_READER_UID=")), "")
    groups = next((line.partition("=")[2].strip() for line in output.splitlines()
                   if line.startswith("WOA_READER_GROUPS=")), "")
    if not uid:
        raise ProvisioningError("the Odoo shell did not create the held-out reader")
    minted = [line.partition("=")[2].strip() for line in output.splitlines()
              if line.startswith("WOA_API_KEY=")]
    expires = next((line.partition("=")[2].strip() for line in output.splitlines()
                    if line.startswith("WOA_READER_KEY_EXPIRES=")), "")
    if len(minted) != 1 or not minted[0]:
        raise ProvisioningError("the Odoo shell did not return exactly one API key")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    key_path = directory / "api-key.json"
    key_path.write_text(json.dumps(
        {"database": database, "login": LOGIN, "api_key": minted[0]}, indent=2) + "\n",
        encoding="utf-8")
    key_path.chmod(0o600)
    from web_operation_agent_odoo.demo import login as login_module

    storage_state = directory / "storage-state.json"
    code = login_module.main(["--base-url", BASE_URL, "--api-key", str(key_path),
                              "--storage-state", str(storage_state), "--demo-credentials"])
    if code != 0 or not storage_state.is_file():
        raise ProvisioningError("the held-out reader's browser sign-in produced no state")
    return {"login": LOGIN, "uid": int(uid), "groups": groups.split(","),
            "api_key_expires": expires, "api_key_path": str(key_path),
            "storage_state_path": str(storage_state)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=DATABASE)
    parser.add_argument("--directory", type=Path, default=ROOT / ".private" / "odoo-reader")
    arguments = parser.parse_args()
    print(json.dumps(provision(database=arguments.database, directory=arguments.directory),
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
