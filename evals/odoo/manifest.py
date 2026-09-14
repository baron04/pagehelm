"""Fixture manifest for ``p74_odoo_v1``: every pinned input a trial depends on, as digests.

A result is only interpretable against the exact application, addon, database, browser, page
controller, locale, roles, data scale and frozen background work it ran on. The manifest
records each of them from the running evaluation stack and the repository, so a later batch
can prove it runs on the same fixture or must be treated as a different one.
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


def fixture_manifest(stack: Stack, *, population: dict[str, Any], seed_truth: dict[str, Any],
                     recovery: dict[str, Any], stability: dict[str, Any],
                     browser_version: str) -> dict[str, Any]:
    images = {}
    for service in ("db", "odoo", "agent", "gateway", "provider-relay"):
        container = stack.container(service, running=False)
        images[service] = stack.run(["docker", "inspect", "--format", "{{.Image}}",
                                     container]).stdout.decode().strip()
    compose_text = BASE_COMPOSE.read_text(encoding="utf-8")
    pinned = sorted(set(re.findall(r"image:\s*(\S+@sha256:[0-9a-f]{64})", compose_text)))
    config = (stack.stack_inputs / "odoo.conf").read_text(encoding="utf-8")
    cron_threads = re.search(r"^max_cron_threads\s*=\s*(\d+)", config, re.M)
    page_controller = json.loads((REPOSITORY / "src/web_operation_agent/policies/"
                                  "page_controller.json").read_text(encoding="utf-8"))
    bundle = REPOSITORY / ".artifacts/evals/pageagent/pageagent-baseline.js"
    body = {
        "artifact_version": 1, "kind": "p74_fixture_manifest", "fixture_id": FIXTURE_ID,
        "compose_project": stack.project, "database": "woa_trial",
        # The running Agent image must carry this clean checkout's commit as its revision.
        "candidate": stack.verify_candidate(),
        "image_ids": images, "compose_pinned_images": pinned,
        "odoo_base_image": re.search(r"FROM (odoo@sha256:[0-9a-f]{64})",
                                     (REPOSITORY / "deploy/odoo-single-node/Dockerfile.odoo")
                                     .read_text()).group(1),
        "application_release": "odoo@18.0-20260908",
        "installed_modules": ["account", "sale_management", "woa_runtime"],
        "demo_data": False,
        "addon_digest": _tree_digest(REPOSITORY / "examples/odoo/deploy/addons/woa_runtime"),
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
        "background_work": {"odoo_conf_max_cron_threads": int(cron_threads.group(1))
                            if cron_threads else None,
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
    """A fixture manifest was changed, or describes another candidate or baseline."""


def verify_manifest(record: Any, *, candidate: dict[str, Any],
                    baseline_guard_digest: str) -> dict[str, Any]:
    """A recorded manifest, provided it is unchanged and describes this candidate and baseline.

    The manifest names the candidate it was taken with, so rebuilding the Agent image makes every
    earlier manifest stale: a batch cannot bind a fixture recorded for another candidate.
    """
    if not isinstance(record, dict) or record.get("kind") != "p74_fixture_manifest":
        raise ManifestError("expected a P7.4 fixture manifest")
    body = {key: value for key, value in record.items() if key != "manifest_digest"}
    if record.get("manifest_digest") != digest_json(body):
        raise ManifestError("the fixture manifest was changed after it was recorded")
    if body.get("candidate") != candidate:
        raise ManifestError("the fixture manifest was taken with another candidate; refresh it")
    baseline = body.get("baseline")
    if not isinstance(baseline, dict) or baseline.get("guard_digest") != baseline_guard_digest:
        raise ManifestError("the fixture manifest names another baseline")
    return record


def batch_fixture(record: dict[str, Any]) -> dict[str, str]:
    """The fixture facts a batch header binds."""
    return {"manifest_digest": record["manifest_digest"],
            "baseline_guard_digest": record["baseline"]["guard_digest"]}
