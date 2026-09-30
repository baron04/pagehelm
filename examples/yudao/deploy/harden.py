"""Prepare a seeded yudao database for public exposure.

Run against the deployment before its site is published (for example through
an SSH tunnel). It gives `admin` and the seeded users new random passwords and
disables every other admin account in the tenant, which includes the upstream
demo accounts with published passwords. Tenant and token cleanup is SQL and
lives in harden.sql.

The new passwords are written to --out (mode 600) and never printed.

Usage: harden.py --base http://127.0.0.1:48080 --out credentials [--tenant 1]
Environment: YUDAO_ADMIN_PASSWORD (current admin password), YUDAO_SEED_PASSWORD
(current password of the seeded users).
"""
from __future__ import annotations

import argparse
import os
import secrets

from seed import USERS, Client


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--tenant", default="1")
    args = ap.parse_args()

    c = Client(args.base, args.tenant)
    c.login("admin", os.environ.get("YUDAO_ADMIN_PASSWORD", "admin123"))
    keep = {"admin"} | {u[0] for u in USERS}
    users = c.page("system/user/page", pageSize=100)
    creds = {}
    for u in users:
        if u["username"] in keep:
            creds[u["username"]] = secrets.token_urlsafe(12)  # 16 characters, the upper bound yudao accepts
            c.put("system/user/update-password", {"id": u["id"], "password": creds[u["username"]]})
        elif u["status"] == 0:
            c.put("system/user/update-status", {"id": u["id"], "status": 1})
    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"# yudao {args.base} tenant {args.tenant}\n")
        for name, pw in sorted(creds.items()):
            f.write(f"{name}\t{pw}\n")
    disabled = len([u for u in users if u["username"] not in keep])
    print(f"new passwords for {len(creds)} accounts written to {args.out}; {disabled} other accounts disabled")


if __name__ == "__main__":
    main()
