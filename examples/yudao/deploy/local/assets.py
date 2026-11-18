"""Build the checkout's frontend and a dependency-only development image."""
from __future__ import annotations

import fcntl
import hashlib
import json
import subprocess
import sys

import configuration as cfg


def build_frontend() -> None:
    directory = cfg.ROOT / ".q0-cache/yudao-local"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "compose-build.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        marker = directory / "compose-build.json"
        try:
            previous = marker if marker.is_file() else directory / "build.json"
            before = json.loads(previous.read_text()) if previous.is_file() else {}
        except (ValueError, OSError):
            before = {}
        if not isinstance(before, dict):
            before = {}
        source = cfg.source_digest(frontend_only=True)
        runtime = hashlib.sha256(cfg.RUNTIME.read_bytes()).hexdigest() if cfg.RUNTIME.is_file() else ""
        yudao_dir = cfg.ROOT / "frontend/dist/integrations/yudao"
        yudao_built = (
            not (cfg.ROOT / "frontend/scripts/build-yudao.mjs").is_file()
            or all((yudao_dir / n).is_file() for n in ("page-facts.js", "yudao-page-controller.js", "form-surface.js"))
        )
        if before.get("frontend") == source and before.get("runtime") == runtime and yudao_built:
            if not marker.is_file():
                marker.write_text(json.dumps(before))
                marker.chmod(0o600)
            print("前端未变化，复用已核验的构建。", flush=True)
            return
        installed = cfg.ROOT / "frontend/node_modules/.package-lock.json"
        def dependencies():
            return hashlib.sha256((cfg.ROOT / "frontend/package-lock.json").read_bytes() + b"\0" +
                                  (installed.read_bytes() if installed.is_file() else b"")).hexdigest()
        install = [] if installed.is_file() and before.get("dependencies") == dependencies() else ["--install"]
        print("构建当前目录的助手前端。", flush=True)
        for script, options in (("build_panel.py", install), ("build_runtime.py", ["--skip-build"])):
            subprocess.run([sys.executable, str(cfg.ROOT / "scripts" / script), *options],
                           cwd=cfg.ROOT, env=cfg.process_environment(), check=True)
        marker.write_text(json.dumps({"frontend": source, "runtime": hashlib.sha256(cfg.RUNTIME.read_bytes()).hexdigest(),
                                      "dependencies": dependencies()}))
        marker.chmod(0o600)


def ensure_image(config: cfg.Configuration) -> None:
    result = subprocess.run(["docker", "image", "ls", "-q", "--filter", "reference=" + cfg.image_name()],
                            capture_output=True, text=True, check=True)
    if result.stdout.strip():
        return
    print("构建助手依赖镜像；普通源码更新复用此镜像。", flush=True)
    cfg.compose(config, "build", "agent")
