"""Provision ``p74_odoo_v1`` from an empty Compose project, reproducibly.

One command builds the whole evaluation fixture: private inputs, the pinned PageController,
images, a demo-free Odoo database, the seeded synthetic population with its per-user API keys,
the stack with the evaluation overlay, warm-up logins, the qualified baseline snapshot, the
stability proof and the fixture manifest. Provisioning twice from empty volumes must yield the
same population, the same seeded record identities and the same actor-visible projection; row
timestamps and archive bytes legitimately differ and are recorded, not compared.

    python -m evals.odoo.provision --destroy-first
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import fixture, manifest, population as population_module
from .population import digest_json
from .projection import actor_projection
from .runner_woa import ActorCredential, WoaRunnerConfig, login, logout
from .stack import REPOSITORY, Stack, StackError

ROLES = ("writer", "reader", "other")


def _private_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
    os.chmod(path, 0o600)


def keep_manifest(path: Path) -> Path | None:
    """Keep the fixture manifest at ``path`` under its own digest, beside it.

    A rebuild replaces ``fixture-manifest.json``. The kept copy is written once and never
    rewritten, so every qualification and batch that bound a manifest can still read it in full.
    """
    try:
        data = path.read_bytes()
        digest = json.loads(data)["manifest_digest"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        return None
    kept = path.parent / "manifests" / f"{digest.removeprefix('sha256:')}.json"
    if not kept.exists():
        kept.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(kept, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
    return kept


def prepare_inputs(stack: Stack, private: Path) -> None:
    subprocess.run([sys.executable, str(REPOSITORY / "deploy/odoo-single-node/prepare.py"),
                    "--private-dir", str(stack.stack_inputs)], check=True, capture_output=True)
    other = stack.stack_inputs / "other-login-password"
    if not other.exists():
        _private_write(other, (secrets.token_urlsafe(24) + "\n").encode())
    config = stack.stack_inputs / "odoo.conf"
    text = config.read_text()
    # The evaluation fixture runs no scheduled actions at all: the thread count is zero here,
    # and the seed program deactivates every scheduled action in the database as well.
    frozen = "\n".join("max_cron_threads = 0" if line.startswith("max_cron_threads")
                       else line for line in text.splitlines()) + "\n"
    _private_write(config, frozen.encode())
    stack.write_model_env("p74" + secrets.token_urlsafe(24))
    for name in ("seeds", "keys", "seeded", "recovery", "qualification"):
        (private / name).mkdir(mode=0o700, exist_ok=True)
    seed = private / "seeds" / "development.seed"
    if not seed.exists():
        _private_write(seed, (secrets.token_hex(16) + "\n").encode())


POPULATIONS = ("development", "heldout")


def population_seed(private: Path, population: str) -> str:
    """The split's private seed. The held-out seed is escrowed and never made here: a missing one
    is a lost held-out set, and a fresh seed would be a different set under the same seal."""
    if population not in POPULATIONS:
        raise StackError("a fixture holds the development or the held-out population")
    path = private / "seeds" / f"{population}.seed"
    if not path.exists():
        raise StackError(f"the {population} seed is missing; restore it from the escrow")
    return path.read_text().strip()


def seed_database(stack: Stack, private: Path, population: dict[str, Any],
                  name: str = "development") -> dict[str, Any]:
    population_file = private / f"{name}-population.json"
    _private_write(population_file, (json.dumps(population, ensure_ascii=False, indent=2,
                                                sort_keys=True) + "\n").encode())
    seeded = private / "seeded"
    for path in seeded.glob("*.json"):
        path.unlink()
    mounts = []
    for role in ROLES:
        mounts += ["-v", f"{stack.stack_inputs / (role + '-login-password')}:"
                         f"/run/woa-input/{role}-login-password:ro"]
    script = ("set -e; install -d -m 0700 -o 100 -g 101 /run/woa-private /run/woa-generated; "
              "for name in db-password odoo.conf writer-login-password reader-login-password "
              "other-login-password; do install -m 0400 -o 100 -g 101 /run/woa-input/$name "
              "/run/woa-private/$name; done; setpriv --reuid=100 --regid=101 --init-groups "
              "odoo shell --config=/run/woa-private/odoo.conf --database=woa_trial --no-http "
              "--log-level=warn < /run/p74/seed.py; test -f /run/woa-generated/truth.json; "
              "for f in /run/woa-generated/*; do install -m 0600 $f /run/woa-output/; done")
    stack.compose("run", "--rm", "--no-deps",
                  "-v", f"{REPOSITORY / 'evals/odoo/odoo_seed.py'}:/run/p74/seed.py:ro",
                  "-v", f"{population_file}:/run/p74/population.json:ro", *mounts,
                  "-v", f"{seeded}:/run/woa-output", "--entrypoint", "bash", "odoo", "-lc",
                  script, timeout=900)
    truth = json.loads((seeded / "truth.json").read_text())
    _private_write(stack.stack_inputs / "delegations.json",
                   (seeded / "delegations.json").read_bytes())
    for role in ROLES:
        key = (seeded / f"{role}.api-key.json").read_bytes()
        _private_write(stack.stack_inputs / "delegated-keys" / f"{role}.api-key.json", key)
        _private_write(private / "keys" / f"{role}.api-key.json", key)
    return truth


def warm_up(stack: Stack, private: Path) -> None:
    from playwright.sync_api import sync_playwright

    actors = {role: ActorCredential(f"p74-{role}@example.test",
                                    stack.stack_inputs / f"{role}-login-password")
              for role in ROLES}
    config = WoaRunnerConfig(base_url=stack.base_url, actors=actors,
                             evidence_dir=private / "warmup")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            for role in ROLES:
                for _ in range(2):
                    context = browser.new_context(ignore_https_errors=True,
                                                  viewport={"width": 1440, "height": 1000})
                    page = context.new_page()
                    login(page, config, role)
                    page.wait_for_timeout(4_000)
                    if not logout(page):
                        raise StackError(f"warm-up logout failed for {role}")
                    context.close()
            version = browser.version
        finally:
            browser.close()
    (private / "qualification" / "browser-version.txt").write_text(version + "\n")


def _wait_login(stack: Stack, timeout_s: float = 300) -> None:
    import ssl
    import urllib.request

    unverified = ssl._create_unverified_context()  # the fixture's own internal CA
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(stack.base_url + "/web/login", timeout=5,
                                        context=unverified) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(3)
    raise StackError("the Odoo login page did not become reachable")


def reuse_fixture(private: Path, *, build: bool = True,
                  population_name: str = "development") -> dict[str, Any]:
    """Bring the stack up on a rebuilt Agent image and keep the fixture that is already there.

    Seeding, the baseline snapshot, the stability proof and the manifest describe the fixture,
    and a rebuilt candidate leaves the fixture alone: the database is seeded through Odoo's own
    RPC and the Agent reaches it only as a trial's actor. So a candidate-only rebuild checks
    rather than repeats them -- the recorded manifest must describe the fixture that is up, and
    the live fixture must still be the recorded baseline. Either check failing means the fixture
    is not the one that was qualified, and it says to rebuild it whole.
    """
    stack = Stack(private_dir=private)
    timings: dict[str, float] = {}
    started = time.monotonic()
    prepare_inputs(stack, private)
    stack.run([sys.executable, "-m", "web_operation_agent_odoo.deploy", "assets"], timeout=600)
    if build:
        stack.build("odoo", "agent")
    timings["inputs_and_images"] = round(time.monotonic() - started, 1)
    stack.compose("up", "-d", timeout=900)
    _wait_login(stack)
    timings["stack_up"] = round(time.monotonic() - started, 1)
    recovery = json.loads((private / "recovery" / "recovery.json").read_text())
    population = population_module.generate(population_seed(private, population_name))
    target = private / "qualification" / "fixture-manifest.json"
    record = manifest.verify_manifest(
        json.loads(target.read_text()),
        inputs=manifest.fixture_inputs(stack, population=population),
        baseline_guard_digest=recovery["baseline_digest"])
    guard = fixture.FixtureGuard(stack)
    live = guard.capture()["digest"]
    timings["fixture_verified"] = round(time.monotonic() - started, 1)
    if live != recovery["baseline_digest"]:
        raise StackError(
            "the fixture is not the recorded baseline any more; provision --destroy-first")
    return {
        "kind": "p74_provision_record", "fixture_id": fixture.FIXTURE_ID, "reused_fixture": True,
        "population": population_name, "population_digest": digest_json(population),
        "baseline_guard_digest": recovery["baseline_digest"],
        "candidate": stack.verify_candidate(),
        "stability_status": record["stability_proof"]["status"],
        "manifest_digest": record["manifest_digest"], "timings_s": timings,
    }


def provision(private: Path, *, destroy_first: bool, stability_interval_s: float,
              build: bool = True, population_name: str = "development") -> dict[str, Any]:
    """Build the fixture from empty volumes, seeded with one split's population.

    The stack holds one population at a time: provisioning the held-out one replaces the
    development fixture, its keys, baseline and manifest, and a development batch after it needs
    the development population provisioned again. The manifest names the population it was
    taken with, so neither split's entry can run on the other's fixture.
    """
    seed = population_seed(private, population_name)
    stack = Stack(private_dir=private)
    timings: dict[str, float] = {}
    started = time.monotonic()

    def mark(step: str) -> None:
        timings[step] = round(time.monotonic() - started, 1)

    if destroy_first:
        stack.compose("down", "--volumes", "--remove-orphans", timeout=600)
        mark("destroyed")
    prepare_inputs(stack, private)
    stack.run([sys.executable, "-m", "web_operation_agent_odoo.deploy", "assets"], timeout=600)
    if build:
        # Only a clean checkout is built, and the images are labelled with its full commit.
        stack.build("odoo", "agent")
    mark("inputs_and_images")
    stack.compose("up", "-d", "db", timeout=600)
    stack.compose("--profile", "setup", "run", "--rm", "odoo-init", timeout=1800)
    mark("database_initialized")
    population = population_module.generate(seed)
    truth = seed_database(stack, private, population, population_name)
    mark("population_seeded")
    stack.compose("up", "-d", timeout=900)
    _wait_login(stack)
    mark("stack_up")
    warm_up(stack, private)
    mark("warmed_up")
    keys = {role: private / "keys" / f"{role}.api-key.json" for role in ROLES}
    projection = actor_projection(stack.rpc_url, keys)
    guard = fixture.FixtureGuard(stack)
    recovery = fixture.prepare_snapshot(stack, guard, private / "recovery",
                                        actor_projection=projection)
    mark("baseline_snapshot")
    stability = fixture.stability_proof(guard, interval_s=stability_interval_s)
    _private_write(private / "qualification" / "stability-proof.json",
                   (json.dumps(stability, indent=2, sort_keys=True) + "\n").encode())
    mark("stability_proof")
    record = manifest.fixture_manifest(
        stack, population=population, seed_truth=truth, recovery=recovery,
        stability=stability,
        browser_version=(private / "qualification" / "browser-version.txt").read_text().strip())
    target = private / "qualification" / "fixture-manifest.json"
    # The manifest this rebuild replaces stays readable in full under its digest.
    keep_manifest(target)
    _private_write(target, (json.dumps(record, indent=2, sort_keys=True) + "\n").encode())
    keep_manifest(target)
    mark("manifest")
    identities = {key: value for key, value in truth.items() if key != "frozen_crons"}
    return {
        "kind": "p74_provision_record", "fixture_id": fixture.FIXTURE_ID,
        "population": population_name, "population_digest": digest_json(population),
        "seed_identity_digest": digest_json(identities),
        "actor_projection_digest": recovery["actor_projection_digest"],
        "frozen_crons": len(truth["frozen_crons"]),
        "baseline_guard_digest": recovery["baseline_digest"],
        "stability_status": stability["status"],
        "manifest_digest": record["manifest_digest"], "timings_s": timings,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.odoo.provision")
    parser.add_argument("--private-dir", default=str(REPOSITORY / ".private" / "p74-odoo-v1"))
    parser.add_argument("--destroy-first", action="store_true")
    parser.add_argument("--reuse-fixture", action="store_true",
                        help="rebuild the candidate and keep the fixture that is already "
                             "provisioned, provided it is still the one the manifest describes")
    parser.add_argument("--stability-interval", type=float, default=300)
    parser.add_argument("--no-build", action="store_true")
    parser.add_argument("--population", choices=POPULATIONS, default="development",
                        help="the split the fixture is seeded with; heldout replaces the "
                             "development fixture")
    parser.add_argument("--record")
    args = parser.parse_args(argv)
    if args.reuse_fixture and args.destroy_first:
        parser.error("--reuse-fixture keeps the fixture; --destroy-first replaces it")
    result = (reuse_fixture(Path(args.private_dir), build=not args.no_build,
                            population_name=args.population)
              if args.reuse_fixture else
              provision(Path(args.private_dir), destroy_first=args.destroy_first,
                        stability_interval_s=args.stability_interval, build=not args.no_build,
                        population_name=args.population))
    if args.record:
        _private_write(Path(args.record), (json.dumps(result, indent=2, sort_keys=True)
                                           + "\n").encode())
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["stability_status"] == "READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
