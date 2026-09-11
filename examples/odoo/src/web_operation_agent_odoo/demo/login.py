"""Renew the browser state the Odoo live gate runs under.

Odoo's session cookie is HttpOnly, so nothing in the page can hand it to a server; the only
way to obtain one is a real sign-in in a real browser. That is a deployment step, and this
script is what makes it repeatable rather than a thing someone did once by hand and could
not redo six weeks later.

Two modes, both ending in a written storage state:

* the default opens a visible window and waits for a person to sign in, which is what a
  deployment against anything but a local demo must use;
* ``--demo-credentials`` fills the local demo database's own well-known admin password,
  which exists so a gate can be re-provisioned unattended against a fixture. It refuses any
  origin that is not loopback, because sending a password to a real host is exactly the
  thing this project must never do quietly.

The written file is 0600 and is never committed. What it proves is checked before it is
written: the session must resolve to the user the API key is bound to, or the two halves of
the identity binding disagree and every later run would fail for a reason that looks like a
product defect.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from ..adapter import (OdooApiKeyStore, OdooApplicationAdapter,
                       OdooPageSessionIdentityResolver, _validated_origin)


#: The password Odoo's own demo data gives `admin`. It is not a secret: it ships in the
#: upstream fixture, and it only ever reaches a loopback origin.
DEMO_PASSWORD = "admin"


def _is_loopback(base_url: str) -> bool:
    host = urlsplit(base_url).hostname or ""
    return host in {"127.0.0.1", "::1", "localhost"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="web-operation-agent-odoo-login",
        description="Sign in to Odoo and write the browser storage state a live gate uses.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8069")
    parser.add_argument("--api-key", required=True,
                        help="the 0600 key file whose user this session must match")
    parser.add_argument("--storage-state", required=True,
                        help="where to write the browser state (0600)")
    parser.add_argument("--demo-credentials", action="store_true",
                        help="fill the demo database's own admin password; loopback only")
    parser.add_argument("--timeout-s", type=float, default=300.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    origin = _validated_origin(arguments.base_url)
    keys = OdooApiKeyStore(arguments.api_key)
    database, login = keys.database, keys.login
    if arguments.demo_credentials and not _is_loopback(origin):
        print("refusing to type a password into a non-loopback origin", file=sys.stderr)
        return 2

    from playwright.sync_api import sync_playwright

    target = Path(arguments.storage_state)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=arguments.demo_credentials)
        context = browser.new_context()
        page = context.new_page()
        page.goto(f"{origin}/web/login?db={database}", wait_until="domcontentloaded")
        if arguments.demo_credentials:
            page.fill("input[name='login']", login)
            page.fill("input[name='password']", DEMO_PASSWORD)
            page.click("button[type='submit']")
            page.wait_for_url(f"{origin}/odoo**", timeout=60_000)
        else:
            print(f"Sign in as {login} on {database}; this waits for the session cookie.")
            deadline = time.time() + arguments.timeout_s
            while time.time() < deadline:
                if any(cookie["name"] == "session_id"
                       for cookie in context.cookies(origin)):
                    break
                page.wait_for_timeout(500)
            else:
                print("no session appeared before the timeout", file=sys.stderr)
                browser.close()
                return 1
        state = context.storage_state()
        browser.close()

    # Before writing anything, insist the browser is the same actor the API key is bound
    # to. A state file for the wrong user fails later as an identity mismatch, which reads
    # as a product defect rather than as the provisioning mistake it is. The comparison is
    # the adapter's own, so this cannot pass a binding the product would reject.
    asserted = OdooPageSessionIdentityResolver(
        base_url=origin, storage_state=state).resolve()
    adapter = OdooApplicationAdapter(base_url=origin, api_key_path=arguments.api_key)
    try:
        adapter.bind_principal(asserted, delegation_id="odoo-login-check")
    except Exception as exc:                                # typed by the adapter's port
        print(f"the signed-in session does not match {database}/{login}: "
              f"{type(exc).__name__}", file=sys.stderr)
        return 1

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True) + "\n",
                      encoding="utf-8")
    os.chmod(target, 0o600)
    print(json.dumps({"storage_state": str(target), "database": database,
                      "actor": asserted.actor_id}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
