"""`python -m web_operation_agent_odoo.deploy` -- the fixture, from nothing, in one command.

Every step prints one line saying what it did, because the slow ones (a demo database is
minutes) are otherwise indistinguishable from a hang, and because a person re-running a
single step needs to see which one they are in.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from . import (BASE_URL, DATABASE, ProvisioningError, capture_browser_session,
               create_database, environment_lines, extract_source_tree, fetch_assets,
               image_release, mint_api_key, record_baseline, start_stack, stop_stack,
               _serves_the_pinned_asset)


#: Where the credentials and the snapshot go. Ignored by Git, 0600 where it matters, and
#: outside the package so a wheel can never pick any of it up.
DEFAULT_STATE = Path(".private/odoo")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m web_operation_agent_odoo.deploy",
        description="Provision the Odoo 18 reference fixture the live gate runs against.")
    parser.add_argument("command", choices=(
        "provision", "assets", "up", "database", "source", "credentials", "baseline",
        "env", "down"))
    parser.add_argument("--database", default=DATABASE)
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE),
                        help="where the API key, browser session and snapshot are written")
    parser.add_argument("--source-root", default=".fixture/odoo-source",
                        help="where Odoo's own source is extracted for the compiler")
    parser.add_argument("--force-assets", action="store_true",
                        help="re-download the pinned PageController even if it verifies")
    parser.add_argument("--destroy", action="store_true",
                        help="down: remove the volumes as well, discarding the database")
    return parser


def _say(report: dict) -> None:
    step = report.pop("step", "")
    detail = report.pop("detail", "")
    rest = " ".join(f"{key}={json.dumps(value, ensure_ascii=False)}"
                    for key, value in sorted(report.items()))
    print(f"[{step}] {detail or rest}".rstrip(), flush=True)
    if detail and rest:
        print(f"        {rest}", flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    state = Path(arguments.state_dir)
    paths = {
        "key_path": state / "api-key.json",
        "storage_state": state / "storage-state.json",
        "snapshot": state / "baseline.pgdump",
        "record": state / "recovery.json",
    }
    # The compiled source is reading material, not a credential, so it lives beside the
    # fixture rather than in the private state directory.
    source_root = Path(arguments.source_root)
    database = arguments.database

    def assets() -> None:
        _say(fetch_assets(force=arguments.force_assets))

    def up() -> None:
        _say(start_stack())

    def database_step() -> None:
        _say(create_database(database=database))
        # Ask the running server for the asset, not the disk: what the Runtime imports is
        # whatever the origin answers with.
        _serves_the_pinned_asset()
        _say({"step": "assets", "detail": "the running Odoo serves the pinned build"})

    def source() -> None:
        _say(extract_source_tree(source_root=source_root))

    def credentials() -> None:
        _say(mint_api_key(database=database, key_path=paths["key_path"]))
        _say(capture_browser_session(storage_state=paths["storage_state"],
                                     key_path=paths["key_path"]))

    def baseline() -> None:
        _say(record_baseline(snapshot=paths["snapshot"], record=paths["record"],
                             database=database))

    def env() -> None:
        lines = environment_lines(database=database, **paths)
        if source_root.is_dir():
            # Optional by design -- source retrieval is never a prerequisite for ordinary
            # operation -- so it is offered only when a tree is actually there.
            lines += [f"WOA_ODOO_SOURCE_ROOT={source_root.resolve()}",
                      f"WOA_ODOO_SOURCE_RELEASE={image_release()}"]
        print("\n# Append these to .env, or export them, and scripts/qualify.sh will run "
              "the live Odoo gate.", flush=True)
        for line in lines:
            print(line, flush=True)

    steps = {"assets": assets, "up": up, "database": database_step, "source": source,
             "credentials": credentials, "baseline": baseline, "env": env}
    try:
        if arguments.command == "down":
            _say(stop_stack(destroy=arguments.destroy))
            return 0
        if arguments.command == "provision":
            # This order is the only one that works: the snapshot has to include the API
            # key, or the gate's first restore deletes the credential the next run needs.
            for name in ("assets", "up", "database", "source", "credentials",
                         "baseline", "env"):
                steps[name]()
            return 0
        steps[arguments.command]()
        return 0
    except ProvisioningError as exc:
        print(f"provisioning stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
