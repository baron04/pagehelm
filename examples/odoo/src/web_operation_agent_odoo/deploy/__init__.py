"""Bring the Odoo reference fixture up from nothing, in one command.

Until this existed, the whole fixture -- the Compose file, the Odoo configuration, the addon
that serves PageController, the database, the API key, the browser session and the snapshot
the gate restores to -- lived on one laptop in an ignored directory. `scripts/qualify.sh`
reported the live Odoo gate as `BLOCKED` for everyone else, and the honest reading of that
was not "you need an Odoo" but "nobody can reproduce this".

Six steps, each of which can be run alone because each fails differently and a person
debugging one does not want to repeat the other five:

    assets      fetch the pinned PageController and check every digest
    up          start Compose and wait for Odoo to answer
    database    create the database from Odoo's own demo data and install the addon
    credentials mint an API key and capture a browser session
    baseline    record the snapshot the gate restores to
    env         print the configuration lines the gate reads

`provision` runs all six in that order, which is the only order that works: the snapshot has
to be taken *after* the API key exists, or every restore during a gate run would delete the
credential the next run needs.

This is a fixture, and it is loopback-only by construction. It carries a database password
and a master password in the clear, signs in with Odoo's own well-known demo password, and
refuses to do any of that against a host that is not loopback. It is a disposable
application to point the agent at, not a deployment pattern to copy.
"""
from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64encode
from pathlib import Path
from urllib.parse import urlsplit

from web_operation_agent.transport.page_controller import (PINNED_PAGE_CONTROLLER, pinned_version,
                                                 require_served_directory,
                                                 verify_served_directory)


#: Checkout-only deployment resources are grouped by purpose beside the adapter.  The
#: reference fixture is disposable; the single-node stack is the deployment example; the
#: addon is shared because both serve the same browser Runtime.
INTEGRATION_ROOT = Path(__file__).resolve().parents[3]
DEPLOY_ROOT = INTEGRATION_ROOT / "deploy"
REFERENCE_DEPLOY = DEPLOY_ROOT / "reference"
SINGLE_NODE_DEPLOY = DEPLOY_ROOT / "single-node"
ADDONS_ROOT = DEPLOY_ROOT / "addons"
COMPOSE_FILE = REFERENCE_DEPLOY / "docker-compose.yml"
SERVED_ASSETS = ADDONS_ROOT / "woa_runtime" / "static" / "src"
LOCAL_PAGE_CONTROLLER = (INTEGRATION_ROOT.parents[1] / "evals" / "pageagent" /
                         "node_modules" / "@page-agent" / "page-controller")
COMPOSE_PROJECT = "woa-odoo-reference"
ODOO_SERVICE = "odoo"
DB_SERVICE = "db"
BASE_URL = "http://127.0.0.1:8069"
DATABASE = "woa_reference"
#: Odoo's own demo data ships this login and password. It is not a secret, and it only ever
#: reaches loopback -- `_require_loopback` is what makes that a rule rather than a habit.
DEMO_LOGIN = "admin"
DEMO_PASSWORD = "admin"
MASTER_PASSWORD = "woa-reference-fixture"
API_KEY_NAME = "woa-reference-gate"
PAGE_CONTROLLER_URL = f"{BASE_URL}/woa_runtime/static/src/page-controller.js"


class ProvisioningError(RuntimeError):
    """A provisioning step could not complete, with the reason a person needs."""


def _require_checkout() -> None:
    """This provisions a development fixture, so it runs from a checkout and says so.

    The Compose file and the addon live beside the example rather than inside the wheel: a
    wheel that carried a Docker Compose file and a master password would be shipping a
    fixture as if it were a product.
    """
    if not COMPOSE_FILE.is_file():
        raise ProvisioningError(
            f"{COMPOSE_FILE} is not there. The reference fixture is provisioned from a "
            "checkout of this repository, not from an installed wheel -- the Compose file "
            "and the Runtime addon are deliberately not packaged.")


def _require_loopback(base_url: str) -> None:
    host = urlsplit(base_url).hostname or ""
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise ProvisioningError(
            f"{base_url} is not loopback. This provisioning step signs in with a well-known "
            "demo password and writes a master password in the clear; sending either to a "
            "host anyone else can reach is exactly what it must never do quietly.")


def _run(argv: list[str], *, timeout: int = 600, stdin: bytes | None = None) -> str:
    try:
        done = subprocess.run(argv, input=stdin, capture_output=True, timeout=timeout,
                              check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProvisioningError(f"{argv[0]} could not be run: {exc}") from exc
    if done.returncode != 0:
        detail = (done.stderr or done.stdout).decode("utf-8", "replace").strip()
        raise ProvisioningError(f"{' '.join(argv[:3])} failed: {detail[:800]}")
    return done.stdout.decode("utf-8", "replace")


def _compose(*arguments: str, timeout: int = 600) -> str:
    return _run(["docker", "compose", "-f", str(COMPOSE_FILE), *arguments], timeout=timeout)


def _container(service: str) -> str:
    """The container's *name*, not its id.

    The id changes every time Compose recreates the service, and one of these is written
    into a configuration file a gate reads later. A name survives a restart; an id turns
    into a line that was correct when it was printed and is wrong the next morning.
    """
    listed = _compose("ps", "--format", "{{.Service}}\t{{.Name}}").strip().splitlines()
    names = [row.split("\t", 1)[1].strip() for row in listed
             if "\t" in row and row.split("\t", 1)[0].strip() == service]
    if not names:
        raise ProvisioningError(
            f"the {service} container is not running; run the `up` step first")
    return names[-1]


# ------------------------------------------------------------------------ assets

def fetch_assets(*, force: bool = False) -> dict:
    """Put the pinned PageController where Odoo can serve it, or refuse.

    The tarball's own integrity is checked first and each extracted file's sha256 second.
    Both, because they answer different questions: the first says the registry gave us the
    release the lockfile names, the second says the files inside it are the ones this
    Runtime was qualified against.
    """
    _require_checkout()
    SERVED_ASSETS.mkdir(parents=True, exist_ok=True)
    if not force and verify_served_directory(SERVED_ASSETS)["verified"]:
        return {"step": "assets", "fetched": False,
                "detail": f"{pinned_version()} is already served and every digest matches"}

    if not force and LOCAL_PAGE_CONTROLLER.is_dir():
        package_file = LOCAL_PAGE_CONTROLLER / "package.json"
        try:
            installed_version = json.loads(package_file.read_text())["version"]
        except (OSError, KeyError, json.JSONDecodeError):
            installed_version = ""
        bodies = {}
        if installed_version == pinned_version():
            for name, entry in PINNED_PAGE_CONTROLLER["served_files"].items():
                relative = entry["tarball_path"].removeprefix("package/")
                source = LOCAL_PAGE_CONTROLLER / relative
                try:
                    body = source.read_bytes()
                except OSError:
                    bodies = {}
                    break
                if hashlib.sha256(body).hexdigest() != entry["sha256"]:
                    bodies = {}
                    break
                bodies[name] = body
        if bodies:
            for name, body in bodies.items():
                (SERVED_ASSETS / name).write_bytes(body)
            report = require_served_directory(SERVED_ASSETS)
            return {"step": "assets", "fetched": True, "source": "installed_package",
                    "version": report["version"], "files": sorted(report["files"]),
                    "detail": f"served {len(report['files'])} verified files from local npm"}

    url = PINNED_PAGE_CONTROLLER["registry_tarball"]
    try:
        with urllib.request.urlopen(url, timeout=120) as response:
            archive = response.read(64 * 1024 * 1024)
    except (urllib.error.URLError, OSError) as exc:
        raise ProvisioningError(f"could not download {url}: {exc}") from exc

    algorithm, _, expected = PINNED_PAGE_CONTROLLER["integrity"].partition("-")
    observed = b64encode(hashlib.new(algorithm, archive).digest()).decode()
    if observed != expected:
        raise ProvisioningError(
            f"{url} does not match the pinned {algorithm} integrity. Refusing to serve it.")

    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for name, entry in PINNED_PAGE_CONTROLLER["served_files"].items():
            try:
                member = tar.extractfile(entry["tarball_path"])
            except KeyError:
                member = None
            if member is None:
                raise ProvisioningError(
                    f"the {pinned_version()} tarball has no {entry['tarball_path']}")
            # Written by name chosen here, never by the name inside the archive: an archive
            # member cannot decide where this writes.
            (SERVED_ASSETS / name).write_bytes(member.read(64 * 1024 * 1024))

    report = require_served_directory(SERVED_ASSETS)
    return {"step": "assets", "fetched": True, "version": report["version"],
            "files": sorted(report["files"]),
            "detail": f"served {len(report['files'])} files from the pinned tarball"}


# ------------------------------------------------------------------------ compose

def _odoo_answers(timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(BASE_URL + "/web/database/selector", timeout=5) as r:
                if r.status < 500:
                    return True
        except (urllib.error.HTTPError, urllib.error.URLError, OSError):
            pass
        time.sleep(2)
    return False


def _service_logs(service: str, lines: int = 40) -> str:
    """The tail of a service's own log, for an error that would otherwise say nothing.

    Telling a person to go and run `docker compose logs` is a worse answer than showing
    them the log: the container is often gone by the time they read the message, and then
    the reason is gone with it.
    """
    try:
        return _compose("logs", "--no-color", f"--tail={lines}", service, timeout=120).strip()
    except ProvisioningError:
        return "(the container is gone; its log went with it)"


def start_stack(*, timeout_s: float = 420) -> dict:
    """Start Compose and wait until Odoo answers, rather than until Compose returns.

    A first boot on an empty volume initialises the data directory and can take minutes on a
    busy machine, so the wait is generous. What it is not is silent: whichever way this
    fails, the failure carries the log that explains it.
    """
    _require_checkout()
    try:
        _compose("up", "-d", "--wait", timeout=900)
    except ProvisioningError as exc:
        raise ProvisioningError(
            f"{exc}\n\n--- docker compose logs odoo ---\n{_service_logs('odoo')}") from None
    if not _odoo_answers(timeout_s):
        raise ProvisioningError(
            f"Odoo did not answer on {BASE_URL} within {timeout_s:.0f}s.\n\n"
            f"--- docker compose logs odoo ---\n{_service_logs('odoo')}")
    return {"step": "up", "base_url": BASE_URL, "project": COMPOSE_PROJECT}


def stop_stack(*, destroy: bool = False) -> dict:
    """Stop the fixture, optionally discarding its volumes as well.

    With `destroy`, this does not return until the volumes are actually gone. Compose reports
    the removal it asked for rather than the removal that finished, and provisioning that
    started while one was still in flight would fail on a volume nobody could see.
    """
    _compose("down", *(["-v"] if destroy else []), timeout=300)
    if not destroy:
        return {"step": "down", "volumes_removed": False}
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        listed = _run(["docker", "volume", "ls", "--quiet", "--filter",
                       f"label=com.docker.compose.project={COMPOSE_PROJECT}"],
                      timeout=60).split()
        if not listed:
            return {"step": "down", "volumes_removed": True}
        time.sleep(2)
    raise ProvisioningError(
        f"the {COMPOSE_PROJECT} volumes are still present two minutes after `down -v`; "
        "provisioning now would build on data that is about to disappear")


# ------------------------------------------------------------------------ database

def _jsonrpc(service: str, method: str, args: list) -> object:
    body = json.dumps({"jsonrpc": "2.0", "method": "call",
                       "params": {"service": service, "method": method,
                                  "args": args}}).encode("utf-8")
    request = urllib.request.Request(BASE_URL + "/jsonrpc", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            payload = json.loads(response.read(16 * 1024 * 1024))
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise ProvisioningError(f"Odoo JSON-RPC {service}.{method} failed: {exc}") from exc
    if isinstance(payload, dict) and payload.get("error"):
        detail = json.dumps(payload["error"].get("data", payload["error"]))[:600]
        raise ProvisioningError(f"Odoo refused {service}.{method}: {detail}")
    return payload.get("result") if isinstance(payload, dict) else None


def create_database(*, database: str = DATABASE, timeout_s: float = 1200) -> dict:
    """Create the database from Odoo's own demo data, and install the Runtime addon.

    Demo data rather than an empty database on purpose: every Skill, every gate case and
    every citation in this distribution is written against records Odoo itself ships, so a
    fixture without them would qualify nothing.
    """
    _require_loopback(BASE_URL)
    existing = _jsonrpc("db", "list", [])
    if isinstance(existing, list) and database in existing:
        report = {"step": "database", "created": False, "database": database}
    else:
        form = urllib.parse.urlencode({
            "master_pwd": MASTER_PASSWORD, "name": database, "login": DEMO_LOGIN,
            "password": DEMO_PASSWORD, "phone": "", "lang": "en_US", "country_code": "us",
            "demo": "1",
        }).encode()
        request = urllib.request.Request(
            BASE_URL + "/web/database/create", data=form, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            # Creating a demo database takes minutes. Odoo redirects to /web on success and
            # renders the manager with an error message on failure, so the status alone
            # cannot be trusted; the database list below is the actual check.
            urllib.request.urlopen(request, timeout=timeout_s).read(1 << 20)
        except urllib.error.HTTPError as exc:
            if exc.code not in {302, 303, 200}:
                raise ProvisioningError(f"Odoo refused to create {database}: {exc}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise ProvisioningError(f"Odoo refused to create {database}: {exc}") from exc
        created = _jsonrpc("db", "list", [])
        if not isinstance(created, list) or database not in created:
            raise ProvisioningError(
                f"Odoo did not create {database}; check the master password in odoo.conf")
        report = {"step": "database", "created": True, "database": database}
    report["modules"] = install_modules(database=database)
    return report


def install_modules(*, database: str = DATABASE) -> list[str]:
    """Install the Runtime addon and the business modules the Skills are written against."""
    require_served_directory(SERVED_ASSETS)
    uid = _jsonrpc("common", "login", [database, DEMO_LOGIN, DEMO_PASSWORD])
    if not isinstance(uid, int) or uid <= 0:
        raise ProvisioningError(f"could not sign in to {database} as {DEMO_LOGIN}")
    wanted = ("woa_runtime",) + BUSINESS_MODULES
    _jsonrpc("object", "execute_kw",
             [database, uid, DEMO_PASSWORD, "ir.module.module", "update_list", []])
    found = _jsonrpc("object", "execute_kw",
                     [database, uid, DEMO_PASSWORD, "ir.module.module", "search_read",
                      [[["name", "in", list(wanted)]]], {"fields": ["id", "name", "state"]}])
    if not isinstance(found, list):
        raise ProvisioningError("Odoo did not answer with a module list")
    missing = sorted(set(wanted) - {row["name"] for row in found})
    if missing:
        raise ProvisioningError(
            f"Odoo cannot see {missing}; check that ./addons is mounted at /mnt/extra-addons")
    pending = [row["id"] for row in found if row["state"] != "installed"]
    if pending:
        # One call, so Odoo resolves the dependency order itself rather than this guessing it.
        _jsonrpc("object", "execute_kw",
                 [database, uid, DEMO_PASSWORD, "ir.module.module",
                  "button_immediate_install", [pending]])
    after = _jsonrpc("object", "execute_kw",
                     [database, uid, DEMO_PASSWORD, "ir.module.module", "search_read",
                      [[["name", "in", list(wanted)]]], {"fields": ["name", "state"]}])
    unfinished = sorted(row["name"] for row in after if row["state"] != "installed")
    if unfinished:
        raise ProvisioningError(f"these modules did not install: {unfinished}")
    return sorted(wanted)


def _serves_the_pinned_asset() -> None:
    """Ask the running Odoo for the asset and check the bytes it actually returns.

    The files on disk matching the pin is not the claim. What the Runtime imports is whatever
    the origin serves, so that is what gets hashed.
    """
    try:
        with urllib.request.urlopen(PAGE_CONTROLLER_URL, timeout=30) as response:
            served = response.read(16 * 1024 * 1024)
    except (urllib.error.URLError, OSError) as exc:
        raise ProvisioningError(
            f"{PAGE_CONTROLLER_URL} is not being served: {exc}. Is woa_runtime installed?"
        ) from exc
    expected = PINNED_PAGE_CONTROLLER["served_files"]["page-controller.js"]["sha256"]
    observed = hashlib.sha256(served).hexdigest()
    if observed != expected:
        raise ProvisioningError(
            f"{PAGE_CONTROLLER_URL} serves sha256:{observed[:16]}…, not the pinned "
            f"{pinned_version()} build sha256:{expected[:16]}…")


# ------------------------------------------------------------------------ credentials

#: Minted through the container's own Odoo shell rather than over the network. Odoo's key
#: wizard re-confirms the password inside an authenticated web request, so there is no RPC
#: for it; and whoever can `docker exec` into this container is already the fixture's owner,
#: which is the same person the product's own rule says must issue the key.
_MINT_KEY = """
owner = env["res.users"].sudo().search([("login", "=", {login!r})], limit=1)
if not owner:
    raise SystemExit("no such user: " + {login!r})
existing = env["res.users.apikeys"].sudo().search(
    [("user_id", "=", owner.id), ("name", "=", {name!r})])
existing.unlink()
key = env["res.users.apikeys"].with_user(owner)._generate("rpc", {name!r}, False)
env.cr.commit()
print("WOA_API_KEY=" + key)
"""


def mint_api_key(*, database: str = DATABASE, key_path: Path,
                 login: str = DEMO_LOGIN, name: str = API_KEY_NAME) -> dict:
    """Issue one API key for the fixture's own user and write it 0600.

    Any previous key of the same name is revoked first, so re-provisioning replaces a
    credential rather than accumulating credentials nobody can account for.
    """
    _require_loopback(BASE_URL)
    script = _MINT_KEY.format(login=login, name=name).encode("utf-8")
    output = _run(["docker", "exec", "-i", _container(ODOO_SERVICE),
                   "odoo", "shell", "-c", "/etc/odoo/odoo.conf", "-d", database,
                   "--no-http", "--log-level=warn"], stdin=script, timeout=300)
    minted = [line.partition("=")[2].strip() for line in output.splitlines()
              if line.startswith("WOA_API_KEY=")]
    if len(minted) != 1 or not minted[0]:
        raise ProvisioningError("the Odoo shell did not return exactly one API key")
    key_path = Path(key_path)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"database": database, "login": login, "api_key": minted[0]}
    key_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    key_path.chmod(0o600)
    return {"step": "api_key", "path": str(key_path), "name": name, "login": login,
            "revoked_previous": True}


def capture_browser_session(*, storage_state: Path, key_path: Path) -> dict:
    """Reuse the existing sign-in helper rather than growing a second way to do this.

    `demo.login` already refuses a non-loopback origin for the unattended path, and it takes
    the key file rather than a database name because the session it writes has to resolve to
    the user that key is bound to -- checking that is the helper's whole point, and a second
    implementation here would be a second thing to keep correct.
    """
    from ..demo import login as login_module

    storage_state = Path(storage_state)
    storage_state.parent.mkdir(parents=True, exist_ok=True)
    code = login_module.main([
        "--base-url", BASE_URL, "--api-key", str(key_path),
        "--storage-state", str(storage_state), "--demo-credentials",
    ])
    if code != 0 or not storage_state.is_file():
        raise ProvisioningError("the browser sign-in did not produce a storage state")
    return {"step": "browser_session", "path": str(storage_state)}


# ------------------------------------------------------------------------ baseline

def warm_up(*, storage_state: Path, entry_path: str = "/odoo/customers") -> None:
    """Open the page the gate opens, so whatever Odoo writes on first use has happened.

    A fresh database has work it has not done yet. The first time a real browser renders the
    web client, Odoo settles company-level defaults and writes `res_company` once -- and a
    baseline recorded before that lands leaves the gate blocked on drift it caused itself,
    on the very first run, with a message about a library someone had written to.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(storage_state=str(storage_state))
            page = context.new_page()
            # Not `networkidle`: Odoo's bus holds a long-poll open, so the network is never
            # idle and waiting for it only ever times out. The web client's own root is the
            # signal that it rendered, which is the thing being waited for.
            page.goto(BASE_URL + entry_path, wait_until="domcontentloaded", timeout=120_000)
            page.wait_for_selector(".o_web_client", timeout=120_000)
            page.wait_for_timeout(3_000)
        finally:
            browser.close()


def settle(*, database: str = DATABASE, attempts: int = 6, pause_s: float = 3.0) -> dict:
    """Wait until nothing but the application's own bookkeeping is still moving.

    Two consecutive captures have to agree everywhere outside `SELF_WRITTEN_TABLES`; that set
    is excluded because `mail_message` grows on an idle server and waiting for it would never
    return. Failing loudly after a bounded wait is the point -- a fixture that never settles
    is one whose baseline would mean nothing, and finding that out here is much cheaper than
    finding it out from a gate that blocks.
    """
    from ..demo.fixture import SELF_WRITTEN_TABLES, OdooPostgresFixture

    fixture = OdooPostgresFixture(container=_container(DB_SERVICE), database=database,
                                  db_user="odoo")

    def watched() -> dict:
        return {name: row for name, row in fixture.capture()["tables"].items()
                if name not in SELF_WRITTEN_TABLES}

    previous = watched()
    for attempt in range(1, attempts + 1):
        time.sleep(pause_s)
        current = watched()
        if current == previous:
            return {"step": "settle", "attempts": attempt,
                    "detail": "the application stopped writing to anything but its own log"}
        previous = current
    raise ProvisioningError(
        f"{database} was still changing after {attempts} checks; a baseline taken now would "
        "not be one. Look at what is writing to it before provisioning again.")


def record_baseline(*, snapshot: Path, record: Path, storage_state: Path,
                    database: str = DATABASE) -> dict:
    """Take the snapshot the gate restores to -- after the API key exists, never before.

    `restore_snapshot` puts the whole database back, so a snapshot taken before the key was
    minted would delete that key on the first restore and every later run would fail on a
    credential the provisioning step thought it had written.

    Warmed up and settled first, for the same class of reason: a baseline is a claim that the
    application is at rest, and a fresh one is not until it has been used once.
    """
    from ..demo.fixture import OdooPostgresFixture, prepare_snapshot

    warm_up(storage_state=Path(storage_state))
    settled = settle(database=database)
    fixture = OdooPostgresFixture(container=_container(DB_SERVICE), database=database,
                                  db_user="odoo")
    result = prepare_snapshot(fixture=fixture, snapshot_path=Path(snapshot),
                              record_path=Path(record))
    return {"step": "baseline", "snapshot": str(snapshot), "record": str(record),
            "settle_attempts": settled["attempts"],
            "digest": result.get("baseline_digest", "")}


# ------------------------------------------------------------------------ configuration

def environment_lines(*, key_path: Path, storage_state: Path, snapshot: Path,
                      record: Path, database: str = DATABASE) -> list[str]:
    """The exact configuration `scripts/qualify.sh` reads, with nothing left to guess."""
    return [
        f"WOA_ODOO_BASE_URL={BASE_URL}",
        f"WOA_ODOO_API_KEY_FILE={Path(key_path).resolve()}",
        f"WOA_ODOO_STORAGE_STATE={Path(storage_state).resolve()}",
        f"WOA_ODOO_PAGE_CONTROLLER_MODULE={PAGE_CONTROLLER_URL}",
        f"WOA_ODOO_FIXTURE_CONTAINER={_container(DB_SERVICE)}",
        f"WOA_ODOO_FIXTURE_DATABASE={database}",
        f"WOA_ODOO_FIXTURE_SNAPSHOT={Path(snapshot).resolve()}",
        f"WOA_ODOO_FIXTURE_RECORD={Path(record).resolve()}",
    ]


# ------------------------------------------------------------------------ source tree

#: The addons the compiled `SourceMap` is built from. Not "all of them": G3 measured the
#: difference, and this set is what makes every citation in the packaged Skills resolve --
#: adding `product`, `sales_team`, `uom`, `analytic`, `stock_account` and `sale_stock` to the
#: original four took the map from 11 882 relations to 14 011 and turned the last unresolved
#: citation green. Removing one will turn Skills red in the live gate, which is the point.
SOURCE_MODULES = ("account", "analytic", "product", "sale", "sale_management", "sale_stock",
                  "sales_team", "stock", "stock_account", "uom")
#: The business modules the fixture installs. These two pull what the five typed collections
#: need (`sale`, `sales_team`, `product`, `uom`, `analytic`), and because the database is
#: created with demo data their own demo records -- S00001, the customer invoices, Acme --
#: arrive with them. A fixture without these answers every gate case with "no such model".
#:
#: `stock` is deliberately absent, and the reason is measured rather than stylistic. With it
#: installed, 22 of the 36 saleable demo products switch to the `delivery` invoicing policy
#: and nothing has been delivered, so *no* order is left invoiceable and the gate's invoicing
#: case has no target. Without it the fixture lands exactly where the qualified baseline was:
#: S00001/S00002/S00003/S00005 draft, S00007 the one order with something to invoice, Acme as
#: partner 10, four customer invoices. Warehouse operations are not part of what this
#: integration claims, so carrying the module would only move the fixture away from the state
#: the cases were verified against.
BUSINESS_MODULES = ("sale_management", "account")
#: Where Odoo's own code lives inside the official image. The Debian package flattens the
#: upstream layout: `addons/` there holds both the community modules and `base`, while the
#: compiler reads the upstream shape, so the extraction maps one onto the other.
_IMAGE_PACKAGE = "/usr/lib/python3/dist-packages/odoo"
#: Structured extractor directories. P8.5 additionally admits narrowly selected security
#: declarations and authored static/src JS/XML below; policy still applies at read time.
_SOURCE_SUBDIRS = ("views", "wizard", "report", "models")


def image_release() -> str:
    """The exact release string the compiled map is bound to, read from the image itself.

    A `SourceMap` records the build it was compiled from, and a citation is only checkable
    against the same one. Deriving it from the image digest rather than writing it down means
    a fixture rebuilt on a newer `odoo:18` gets a different release string, and the citation
    gate notices instead of resolving against bytes nobody looked at.
    """
    digest = _run(["docker", "image", "inspect", "odoo:18",
                   "--format", "{{index .RepoDigests 0}}"], timeout=60).strip()
    _, _, sha = digest.partition("@")
    if not sha.startswith("sha256:"):
        raise ProvisioningError(
            "docker does not know a digest for odoo:18; pull the image before provisioning")
    built = _run(["docker", "image", "inspect", "odoo:18", "--format", "{{.Created}}"],
                 timeout=60).strip()[:10].replace("-", "")
    return f"18.0-{built}+image:{sha}"


def extract_source_tree(*, source_root: Path) -> dict:
    """Copy the source providers' allowlisted reading material out of the image.

    Streamed through `tar` rather than `docker cp` so the member list is filtered here: the
    result is a tree whose every file is one the compiler may read, which is also the tree a
    reviewer can diff against the image if they doubt it.
    """
    source_root = Path(source_root)
    container = _container(ODOO_SERVICE)
    # Image path -> path in the tree the compiler reads. `base` moves because the compiler
    # is written against Odoo's upstream layout and the image ships the Debian one.
    destinations = {f"addons/{module}": f"addons/{module}" for module in SOURCE_MODULES}
    destinations["addons/base"] = "odoo/addons/base"
    keep_suffixes = (".py", ".xml", ".js", ".csv")

    def destination_of(name: str) -> str:
        parts = Path(name).parts
        if len(parts) < 3 or not name.endswith(keep_suffixes):
            return ""
        prefix = "/".join(parts[:2])
        target = destinations.get(prefix)
        if target is None:
            return ""
        # High-precision compiler inputs plus the P8.5 raw-code surface.  Authored browser
        # code is only static/src JS/XML; vendored libraries, built assets and frontend
        # tests stay out.  Security contributes declarations, not arbitrary seed CSV data.
        compiler_source = (parts[2] == "__manifest__.py"
                           or (parts[2] in _SOURCE_SUBDIRS
                               and name.endswith((".py", ".xml"))))
        security_source = (parts[2] == "security" and (
            name.endswith(".xml") or parts[-1] == "ir.model.access.csv"))
        frontend_source = (parts[2:4] == ("static", "src")
                           and name.endswith((".js", ".xml"))
                           and "tests" not in parts[4:])
        if not (compiler_source or security_source or frontend_source):
            return ""
        return "/".join((target, *parts[2:]))

    stream = subprocess.run(
        ["docker", "exec", container, "tar", "-cf", "-", "-C", _IMAGE_PACKAGE,
         *sorted(destinations)],
        capture_output=True, timeout=900, check=False)
    if stream.returncode != 0:
        raise ProvisioningError(
            "could not read Odoo's own source out of the image: "
            + stream.stderr.decode("utf-8", "replace")[:400])

    written = 0
    if source_root.exists():
        shutil.rmtree(source_root)
    source_root.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(stream.stdout), mode="r:") as tar:
        for member in tar:
            relative = destination_of(member.name) if member.isfile() else ""
            if not relative:
                continue
            # Resolved under the root and compared to it: a member name never decides where
            # this writes, however the archive spells it.
            target = (source_root / relative).resolve()
            if not target.is_relative_to(source_root.resolve()):
                raise ProvisioningError(f"refusing an archive member outside the root: "
                                        f"{member.name}")
            extracted = tar.extractfile(member)
            if extracted is None:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(extracted.read(16 * 1024 * 1024))
            written += 1
    if not written:
        raise ProvisioningError("the extracted Odoo source tree is empty")
    return {"step": "source", "root": str(source_root), "files": written,
            "modules": len(SOURCE_MODULES), "release": image_release()}
