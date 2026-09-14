#!/usr/bin/env python3
"""Create private single-node deployment inputs without printing their values."""
from __future__ import annotations

import argparse
import json
import os
import secrets
from pathlib import Path


def _write_private(path: Path, body: bytes) -> None:
    if path.exists():
        return
    path.write_bytes(body)
    os.chmod(path, 0o600)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--private-dir", default=".private/odoo-single-node",
        help="ignored directory for generated secrets and per-user API-key files")
    args = parser.parse_args(argv)
    root = Path(args.private_dir).expanduser()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    keys = root / "delegated-keys"
    keys.mkdir(mode=0o700, exist_ok=True)
    os.chmod(keys, 0o700)
    _write_private(root / "db-password", (secrets.token_urlsafe(36) + "\n").encode())
    _write_private(root / "session-secret", secrets.token_bytes(48))
    _write_private(
        root / "odoo-admin-login-password",
        (secrets.token_urlsafe(24) + "\n").encode())
    _write_private(
        root / "writer-login-password", (secrets.token_urlsafe(24) + "\n").encode())
    _write_private(
        root / "reader-login-password", (secrets.token_urlsafe(24) + "\n").encode())
    _write_private(root / "model.env", b"")
    _write_private(root / "delegations.json", (json.dumps({
        "schema_version": 1, "principals": {},
    }, indent=2, sort_keys=True) + "\n").encode())
    if not (root / "odoo.conf").exists():
        db_password = (root / "db-password").read_text().strip()
        master = secrets.token_urlsafe(36)
        _write_private(root / "odoo.conf", f"""[options]
addons_path = /mnt/extra-addons,/usr/lib/python3/dist-packages/odoo/addons
db_host = db
db_port = 5432
db_user = odoo
db_password = {db_password}
admin_passwd = {master}
list_db = False
proxy_mode = True
workers = 2
max_cron_threads = 1
dbfilter = ^woa_trial$
limit_time_cpu = 60
limit_time_real = 120
""".encode())
    print(json.dumps({
        "status": "READY",
        "private_dir": str(root),
        "next": "initialize Odoo, then register each user's own API key",
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
