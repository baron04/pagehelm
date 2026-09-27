#!/usr/bin/env python3
"""Build ``assistant.js`` and place it where the Python package can serve it.

``assistant.js`` is the one script a host page includes to have the assistant: the boot
supervisor, the Runtime and the panel, built from ``frontend/``. This script is the only
thing that turns it into ``web_operation_agent/web_runtime/assistant.js``, the generated
artifact the gateway reads from disk. It is not committed: a checkout without Node has no
assistant, reported as a named prerequisite rather than served from whatever happens to be
lying in the directory -- the same rule the PageAgent bundle gate applies.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
DIST = FRONTEND / "dist" / "runtime"
TARGET = ROOT / "src" / "web_operation_agent" / "web_runtime" / "assistant.js"
#: Names earlier builds wrote. A leftover would ship in the wheel (`web_runtime/*.js`)
#: as code nothing serves, so a build removes it.
RETIRED = ("embedded.js", "panel_host.js")


def run(*command: str) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=FRONTEND, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true",
                        help="run npm ci first (CI and fresh checkouts)")
    parser.add_argument("--skip-build", action="store_true",
                        help="only sync an already built frontend/dist/runtime")
    args = parser.parse_args()

    if (args.install or not args.skip_build) and not shutil.which("npm"):
        print("npm is unavailable; the runtime cannot be built here", file=sys.stderr)
        return 2
    if args.install:
        run("npm", "ci")
    if not args.skip_build:
        # The design tokens and the protocol types are generated inputs of the build; a
        # fresh checkout has neither, and the bundle does not compile without them.
        run("npm", "run", "tokens")
        run("npm", "run", "build:runtime")
    built = DIST / "assistant.js"
    if not built.is_file():
        print(f"no build output at {built}", file=sys.stderr)
        return 1

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_bytes(built.read_bytes())
    for name in RETIRED:
        (TARGET.parent / name).unlink(missing_ok=True)
    print(f"runtime -> {TARGET} ({TARGET.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
