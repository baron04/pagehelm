#!/usr/bin/env python3
"""Build the frontend, and place the trace replay page where the Python package can serve it.

The panel ships inside ``assistant.js`` (``scripts/build_runtime.py`` places that). What this
script places is the trace replay page, built by the same frontend with the design tokens
inlined, as ``web_runtime/trace_replay.html`` -- the page ``traceview.py`` fills with a run's
data. It is a generated artifact, so it is not committed. It also removes the separate panel
directory earlier builds wrote, so nothing stale ships in the wheel.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
#: Where earlier builds put the panel as a module of its own. It is part of `assistant.js` now.
RETIRED_PANEL = ROOT / "src" / "web_operation_agent" / "web_runtime" / "panel"
TRACE_DIST = FRONTEND / "dist" / "trace" / "trace_replay.html"
TRACE_TARGET = ROOT / "src" / "web_operation_agent" / "web_runtime" / "trace_replay.html"


def run(*command: str) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=FRONTEND, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true",
                        help="run npm ci first (CI and fresh checkouts)")
    parser.add_argument("--skip-build", action="store_true",
                        help="only sync an already built frontend/dist")
    args = parser.parse_args()

    if (args.install or not args.skip_build) and not shutil.which("npm"):
        print("npm is unavailable; the frontend cannot be built here", file=sys.stderr)
        return 2
    if args.install:
        run("npm", "ci")
    if not args.skip_build:
        run("npm", "run", "build")
    if RETIRED_PANEL.exists():
        shutil.rmtree(RETIRED_PANEL)
    if not TRACE_DIST.is_file():
        print(f"the frontend build produced no trace replay page at {TRACE_DIST}",
              file=sys.stderr)
        return 1
    shutil.copyfile(TRACE_DIST, TRACE_TARGET)
    print(f"trace replay -> {TRACE_TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
