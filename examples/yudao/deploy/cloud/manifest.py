"""Record the actual release artifacts, refusing a wheel with stale browser assets."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import zipfile


def write_manifest(repo: Path, stage: Path, release: str) -> None:
    runtime = (repo / "src/web_operation_agent/web_runtime/assistant.js").read_bytes()
    digest = hashlib.sha256(runtime).hexdigest()
    wheels = list((stage / "wheels").glob("web_operation_agent-*.whl"))
    if len(wheels) != 1:
        raise ValueError("expected exactly one core wheel")
    with zipfile.ZipFile(wheels[0]) as wheel:
        if wheel.read("web_operation_agent/web_runtime/assistant.js") != runtime:
            raise ValueError("wheel contains a stale assistant runtime; stopped before upload")
    manifest = {
        "release": release,
        "commit": subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip(),
        "modified": bool(subprocess.check_output(["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=normal"])),
        "runtime_sha256": digest,
        "wheels": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (stage / "wheels").glob("*.whl")},
    }
    (stage / "release.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (stage / "runtime.sha256").write_text(digest + "\n")
    print("commit:", manifest["commit"], "(modified)" if manifest["modified"] else "(clean)")
    print("assistant SHA256:", digest)


if __name__ == "__main__":
    write_manifest(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3])
