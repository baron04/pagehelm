"""Fixture manifest for ``p74_odoo_v1``: every pinned input a trial depends on, as digests.

A result is only interpretable against the exact application, addon, database, browser, page
controller, locale, roles, data scale and frozen background work it ran on. The manifest
records each of them from the running evaluation stack and the repository, so a later batch
can prove it runs on the same fixture or must be treated as a different one.

What the manifest *binds* is what made the fixture -- the application images, the addon, the
configuration and the seeded population -- and not the candidate running on it. The database is
seeded through Odoo's own RPC, and the Agent reaches it only as a trial's actor, so rebuilding
the candidate does not change what is in the fixture. The candidate is bound where it belongs:
every batch header names the Agent image it ran and the clean commit it was built from, and
every attempt compares the live fixture against this baseline before it starts.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .fixture import EXCLUSION_RULES, FIXTURE_ID, GUARDED_TABLES
from .population import digest_json
from .stack import BASE_COMPOSE, REPOSITORY, Stack


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*")
                       if item.is_file() and "__pycache__" not in item.parts):
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return "sha256:" + digest.hexdigest()


def _file_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


#: The Odoo modules the fixture is built with. A fixture with others is a different fixture.
INSTALLED_MODULES = ("account", "sale_management", "woa_runtime")
#: Everything that decides what the fixture becomes, by content rather than by a list of the
#: parts someone remembered. The review of 6376c20 found the list short by the deployment that
#: builds and initialises the application; a longer list of remembered parts is still a closure
#: nobody proved, so the list is now the import closure of the two programs that build the
#: fixture, and :mod:`evals.odoo.tests.test_manifest` recomputes that closure and refuses a
#: module the list does not name.
#:
#: The population generator is the one module the closure holds and this list leaves out: what it
#: produced is bound by its own digest, so a generator that produces the same population leaves
#: the same fixture. Nothing else claims that exemption.
FIXTURE_EXEMPT = ("evals/odoo/population.py",)
FIXTURE_SOURCES = (
    # The programs that seed the database and drive provisioning, and everything they import:
    # what writes the records, what the writes go through, and what decides the baseline.
    "evals/odoo/__init__.py", "evals/odoo/odoo_seed.py", "evals/odoo/provision.py",
    "evals/odoo/fixture.py", "evals/odoo/manifest.py", "evals/odoo/projection.py",
    "evals/odoo/revision.py", "evals/odoo/rpc.py", "evals/odoo/runner_woa.py",
    "evals/odoo/stack.py", "evals/odoo/tasks.py", "evals/odoo/compose.p74.yml",
    # The deployment they run: images, entrypoints, initialisation and the inputs it prepares.
    "deploy/odoo-single-node",
)
#: The programs the closure starts from: the seed the stack runs inside Odoo, and provisioning.
FIXTURE_ENTRY_POINTS = ("evals/odoo/odoo_seed.py", "evals/odoo/provision.py")


def _source_digests() -> dict[str, str]:
    """The digest of every fixture source, a directory as a tree digest of its files."""
    digests = {}
    for relative in FIXTURE_SOURCES:
        path = REPOSITORY / relative
        digests[relative] = _tree_digest(path) if path.is_dir() else _file_digest(path)
    return digests


#: The options in ``odoo.conf`` that say how to authenticate rather than what the application
#: becomes. They are left out of the binding so that a fresh private root with new credentials is
#: not a different fixture; every other option is bound, including any added later.
CONFIG_CREDENTIALS = frozenset({"admin_passwd", "db_password"})


def _config_digest(text: str) -> str:
    """The digest of every ``odoo.conf`` option that decides what the application does.

    The review of 6376c20 found one option bound -- ``max_cron_threads`` -- while the whole file
    is mounted: the addons path, the worker count, the database filter and the time limits all
    decide the application a trial runs against, and none of them made the manifest stale.
    """
    options = []
    for line in text.splitlines():
        name, separator, value = line.partition("=")
        name = name.strip()
        if not separator or not name or name.startswith(("#", ";", "[")):
            continue
        if name not in CONFIG_CREDENTIALS:
            options.append([name, value.strip()])
    return digest_json(sorted(options))


def fixture_inputs(stack: Stack, *, population: dict[str, Any]) -> dict[str, Any]:
    """What decides the content of the fixture, read from the stack and repository in front of us.

    A manifest is stale when one of these changes, and only then. They are read the same way when
    the manifest is written and when a batch or a rebuild checks it, so the two cannot drift.
    """
    images = _service_images(stack)
    config = (stack.stack_inputs / "odoo.conf").read_text(encoding="utf-8")
    cron_threads = re.search(r"^max_cron_threads\s*=\s*(\d+)", config, re.M)
    return {
        "odoo_base_image": _odoo_base_image(),
        "compose_pinned_images": _pinned_images(),
        # The application and its database; the Agent image is deliberately not among them.
        "image_ids": {service: images[service] for service in ("db", "odoo")},
        # Every addon the application is given, not the one this file used to name.
        "addon_digest": _tree_digest(addons_source()),
        "installed_modules": list(INSTALLED_MODULES),
        "demo_data": False,
        "odoo_conf_max_cron_threads": int(cron_threads.group(1)) if cron_threads else None,
        "odoo_conf_digest": _config_digest(config),
        "population_digest": digest_json(population),
        "population_seed_commitment": population["seed_commitment"],
        "fixture_sources": _source_digests(),
    }


def _service_images(stack: Stack) -> dict[str, str]:
    images = {}
    for service in ("db", "odoo", "agent", "gateway", "provider-relay"):
        container = stack.container(service, running=False)
        images[service] = stack.run(["docker", "inspect", "--format", "{{.Image}}",
                                     container]).stdout.decode().strip()
    return images


def _pinned_images() -> list[str]:
    compose_text = BASE_COMPOSE.read_text(encoding="utf-8")
    return sorted(set(re.findall(r"image:\s*(\S+@sha256:[0-9a-f]{64})", compose_text)))


#: Where the base Compose mounts the application's extra addons inside the container. What is
#: bound is the host tree that lands there, read from the mount rather than named again here.
ADDONS_MOUNT = "/mnt/extra-addons"


def addons_source() -> Path:
    """The host directory the base Compose mounts as the application's addons path.

    The review of 8c4025d: the manifest bound one addon, ``woa_runtime``, while Compose mounts
    the directory that holds it. A sibling addon -- an auto-installing one above all -- would
    reach the application's addons path and change what the database becomes, leaving both
    bound digests identical. The mount decides what is bound, so the two cannot drift again.
    """
    found = set()
    for line in BASE_COMPOSE.read_text(encoding="utf-8").splitlines():
        source, separator, target = line.strip().lstrip("- ").partition(":")
        if separator and target.split(":")[0] == ADDONS_MOUNT:
            found.add((BASE_COMPOSE.parent / source).resolve())
    if not found:
        raise ManifestError(f"the base Compose mounts nothing at {ADDONS_MOUNT}")
    if len(found) != 1:
        # Several services mount the addons path. One digest can stand for them only while they
        # name one tree; if they ever part, the manifest would bind whichever came first.
        raise ManifestError(f"the base Compose mounts {len(found)} different trees at "
                            f"{ADDONS_MOUNT}: " + ", ".join(sorted(str(x) for x in found)))
    return found.pop()


def _odoo_base_image() -> str:
    return re.search(r"FROM (odoo@sha256:[0-9a-f]{64})",
                     (REPOSITORY / "deploy/odoo-single-node/Dockerfile.odoo")
                     .read_text()).group(1)


def fixture_manifest(stack: Stack, *, population: dict[str, Any], seed_truth: dict[str, Any],
                     recovery: dict[str, Any], stability: dict[str, Any],
                     browser_version: str) -> dict[str, Any]:
    images = _service_images(stack)
    inputs = fixture_inputs(stack, population=population)
    page_controller = json.loads((REPOSITORY / "src/web_operation_agent/policies/"
                                  "page_controller.json").read_text(encoding="utf-8"))
    bundle = REPOSITORY / ".artifacts/evals/pageagent/pageagent-baseline.js"
    body = {
        "artifact_version": 1, "kind": "p74_fixture_manifest", "fixture_id": FIXTURE_ID,
        "compose_project": stack.project, "database": "woa_trial",
        # What makes this fixture, and what a later batch or rebuild checks it by.
        "fixture_inputs": inputs, "fixture_inputs_digest": digest_json(inputs),
        # The candidate that happened to be running when the fixture was taken. It is recorded
        # so a reviewer can follow it, and it is not part of what the manifest binds: the batch
        # header binds the candidate its own attempts ran.
        "taken_with_candidate": stack.candidate(),
        "image_ids": images,
        "application_release": "odoo@18.0-20260908",
        "page_controller": {"version": page_controller.get("version"),
                            "policy_digest": digest_json(page_controller)},
        "pageagent_bundle_digest": _file_digest(bundle) if bundle.exists() else None,
        "browser": {"engine": "chromium (Playwright)", "version": browser_version,
                    "headless": True, "viewport": [1440, 1000]},
        "locale": {"odoo_user_lang": "en_US", "timezone": "UTC",
                   "prompt_locales": ["zh-CN", "en-US"]},
        "roles": population["users"],
        "companies": {key: {"currency": value["currency"]}
                      for key, value in population["companies"].items()},
        "currencies": ["USD", "EUR"],
        "data_scale": {"customers": len(population["customers"]),
                       "products": len(population["products"]),
                       "sale_orders": len(population["orders"]),
                       "invoices": len(population["invoices"])},
        "population_seed_commitment": population["seed_commitment"],
        "population_digest": digest_json(population),
        "seed_truth_digest": digest_json(seed_truth),
        "background_work": {"odoo_conf_max_cron_threads": inputs["odoo_conf_max_cron_threads"],
                            "scheduled_actions_deactivated": len(seed_truth["frozen_crons"]),
                            "deactivated_digest": digest_json(seed_truth["frozen_crons"])},
        "guard": {"tables": list(GUARDED_TABLES) + ["ir_attachment[non-asset]"],
                  "sequences": "pg_sequences ir_sequence_NNN last_value",
                  "filestore": "sha256 of every object named by a guarded attachment",
                  "exclusions": EXCLUSION_RULES},
        "baseline": {"guard_digest": recovery["baseline_digest"],
                     "database_archive": recovery["database_archive"],
                     "filestore_archive": recovery["filestore_archive"],
                     "actor_projection_digest": recovery["actor_projection_digest"]},
        "stability_proof": {key: stability[key] for key in (
            "status", "interval_s", "required_interval_s", "changed")},
    }
    return {**body, "manifest_digest": digest_json(body)}


class ManifestError(ValueError):
    """A fixture manifest was changed, or describes another fixture or baseline."""


def verify_manifest(record: Any, *, inputs: dict[str, Any],
                    baseline_guard_digest: str) -> dict[str, Any]:
    """A recorded manifest, provided it is unchanged and describes the fixture in front of us.

    ``inputs`` is :func:`fixture_inputs` read from the running stack. A manifest goes stale when
    one of them changes -- another Odoo image, another addon, another population, cron threads
    the fixture was not frozen with -- and not when the candidate is rebuilt, which leaves the
    fixture exactly as it was.
    """
    if not isinstance(record, dict) or record.get("kind") != "p74_fixture_manifest":
        raise ManifestError("expected a P7.4 fixture manifest")
    body = {key: value for key, value in record.items() if key != "manifest_digest"}
    if record.get("manifest_digest") != digest_json(body):
        raise ManifestError("the fixture manifest was changed after it was recorded")
    if body.get("fixture_inputs_digest") != digest_json(inputs):
        changed = sorted(key for key, value in inputs.items()
                         if (body.get("fixture_inputs") or {}).get(key) != value)
        raise ManifestError("the fixture manifest describes another fixture ("
                            + ", ".join(changed or ["shape"]) + "); rebuild it")
    baseline = body.get("baseline")
    if not isinstance(baseline, dict) or baseline.get("guard_digest") != baseline_guard_digest:
        raise ManifestError("the fixture manifest names another baseline")
    return record


def batch_fixture(record: dict[str, Any]) -> dict[str, str]:
    """The fixture facts a batch header binds."""
    return {"manifest_digest": record["manifest_digest"],
            "baseline_guard_digest": record["baseline"]["guard_digest"]}
