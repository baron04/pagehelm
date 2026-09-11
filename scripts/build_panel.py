#!/usr/bin/env python3
"""Build the React panel and place it where the Python package can serve it.

The built panel is a generated artifact, so it is not committed. It is produced here and
copied into ``web_operation_agent/web_runtime/panel/`` with a manifest the gateway reads.
A checkout without Node has no panel; the gateway then refuses to serve the panel document
with a named prerequisite rather than falling back to some older copy -- the same rule the
PageAgent bundle gate already applies.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
DIST = FRONTEND / "dist" / "panel"
TARGET = ROOT / "src" / "web_operation_agent" / "web_runtime" / "panel"
SERVABLE = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
            ".woff2": "font/woff2", ".svg": "image/svg+xml"}


def run(*command: str) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=FRONTEND, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true",
                        help="run npm ci first (CI and fresh checkouts)")
    parser.add_argument("--skip-build", action="store_true",
                        help="only sync an already built frontend/dist/panel")
    args = parser.parse_args()

    if not shutil.which("npm"):
        print("npm is unavailable; the panel cannot be built here", file=sys.stderr)
        return 2
    if args.install:
        run("npm", "ci")
    if not args.skip_build:
        run("npm", "run", "build")
    if not DIST.is_dir():
        print(f"no build output at {DIST}", file=sys.stderr)
        return 1

    if TARGET.exists():
        shutil.rmtree(TARGET)
    TARGET.mkdir(parents=True)
    assets: dict[str, dict] = {}
    for source in sorted(DIST.rglob("*")):
        if not source.is_file():
            continue
        relative = source.relative_to(DIST).as_posix()
        media_type = SERVABLE.get(source.suffix)
        if media_type is None:
            print(f"refusing to ship {relative}: {source.suffix} is not a servable panel asset",
                  file=sys.stderr)
            return 1
        destination = TARGET / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        body = source.read_bytes()
        destination.write_bytes(body)
        assets[relative] = {
            "media_type": media_type,
            "bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
        }
    if "panel.js" not in assets:
        print("the panel build produced no panel.js", file=sys.stderr)
        return 1
    (TARGET / "manifest.json").write_text(
        json.dumps({"schema_version": 1, "assets": assets}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    print(f"panel -> {TARGET} ({len(assets)} assets)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
