"""Opt-in wall-clock offset for isolated local test containers only."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
import time


class ClockError(ValueError):
    pass


def settings(target: str, library: str, previous=None):
    try:
        start = dt.datetime.fromisoformat(target)
    except ValueError as exc:
        raise ClockError("测试时钟必须是带时区的ISO时间。") from exc
    if start.utcoffset() != dt.timedelta(hours=8):
        raise ClockError("测试时钟仅支持上海时区（+08:00）。")
    source = Path(library).resolve(strict=True)
    if not source.is_file() or source.read_bytes()[:4] != b"\x7fELF":
        raise ClockError("测试时钟需要与容器架构兼容的libfaketime共享库。")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    identity = {"target": start.isoformat(), "timezone": "Asia/Shanghai", "library": str(source),
                "library_sha256": digest}
    if previous and all(previous.get(k) == v for k, v in identity.items()):
        return previous
    real_ms = time.time_ns() // 1_000_000
    return {**identity, "activated_at_ms": real_ms,
            "offset_ms": round(start.timestamp() * 1000) - real_ms,
            "mode": "running-wall-clock-offset", "monotonic": "real"}


def composition(clock):
    label = hashlib.sha256(json.dumps(clock, sort_keys=True).encode()).hexdigest() if clock else "real"
    services = {}
    for name in ("server", "mysql"):
        service = {"labels": {"org.web-operation-agent.test-clock": label}}
        if clock:
            # Preload only after selecting MySQL's existing unprivileged actor.
            # The image entrypoint otherwise creates root-owned faketime semaphores
            # before gosu switches uid, and the child cannot reopen them.
            if name == "mysql":
                service["user"] = "mysql"
            service["environment"] = {"LD_PRELOAD": "/opt/woa-test-clock/libfaketimeMT.so.1",
                "FAKETIME": f"{clock['offset_ms'] / 1000:+.3f}s", "FAKETIME_DONT_FAKE_MONOTONIC": "1",
                "TZ": "Asia/Shanghai"}
            service["volumes"] = [{"type": "bind", "source": clock["library"],
                "target": "/opt/woa-test-clock/libfaketimeMT.so.1", "read_only": True,
                "bind": {"create_host_path": False}}]
        services[name] = service
    return services
