"""Shared atomic lease guard for local Yudao reference environments (`env1`/`env2`)."""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import signal
import tempfile
import threading
import uuid

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LEASE_STATE_FILE = _REPO_ROOT / ".q0-cache/yudao-local/env-state.json"
DEFAULT_LEASE_LOCK_FILE = _REPO_ROOT / ".q0-cache/yudao-local/locks/env-state.lock"


class EnvLeaseGuard:
    """Enforce shared `.q0-cache/yudao-local/env-state.json` environment leases."""

    def __init__(
        self,
        envs: tuple[int, ...],
        *,
        experiment: str = "yudao-eval",
        lease_token: str | None = None,
        state_file: Path = DEFAULT_LEASE_STATE_FILE,
        lock_file: Path = DEFAULT_LEASE_LOCK_FILE,
    ):
        self.envs = tuple(dict.fromkeys(int(e) for e in envs))
        self.experiment = experiment
        self.lease_token = lease_token
        self.state_file = Path(state_file)
        self.lock_file = Path(lock_file)
        self._acquired_token: str | None = None
        self._prior_meta: dict[str, tuple[object, object]] = {}
        self._prev_handlers: dict[int, object] = {}

    @property
    def active_token(self) -> str | None:
        return self.lease_token or self._acquired_token

    @staticmethod
    def _atomic_write(path: Path, data: dict) -> None:
        text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        fd, tmp = tempfile.mkstemp(dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _install_signal_handlers(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return

        def _handle(signum: int, _frame) -> None:
            self.release()
            self._restore_signal_handlers()
            if signum == signal.SIGINT:
                raise KeyboardInterrupt
            raise SystemExit(128 + signum)

        for sig in (signal.SIGTERM, signal.SIGINT):
            self._prev_handlers[sig] = signal.getsignal(sig)
            signal.signal(sig, _handle)

    def _restore_signal_handlers(self) -> None:
        if not self._prev_handlers or threading.current_thread() is not threading.main_thread():
            return
        for sig, handler in list(self._prev_handlers.items()):
            signal.signal(sig, handler)
        self._prev_handlers.clear()

    def release(self) -> None:
        """Atomically release any lease acquired by this guard instance (idempotent)."""
        token = self._acquired_token
        if not token or not self.state_file.is_file():
            self._restore_signal_handlers()
            return
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_file.open("a", encoding="utf-8") as lock_fh:
            fcntl.flock(lock_fh, fcntl.LOCK_EX)
            try:
                state = json.loads(self.state_file.read_text(encoding="utf-8"))
                environments = state.get("environments") or {}
                stamp = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat()
                changed = False
                for env_id in self.envs:
                    key = str(env_id)
                    entry = environments.get(key)
                    if isinstance(entry, dict) and entry.get("token") == token:
                        snap, fp = self._prior_meta.get(key, (entry.get("snapshot"), entry.get("fingerprint")))
                        entry.update(
                            status="idle",
                            experiment=None,
                            last_experiment=self.experiment,
                            ended_at=stamp,
                            snapshot=snap,
                            fingerprint=fp,
                            note="lease released",
                        )
                        entry.pop("token", None)
                        entry.pop("pid", None)
                        changed = True
                if changed:
                    self._atomic_write(self.state_file, state)
            finally:
                fcntl.flock(lock_fh, fcntl.LOCK_UN)
        self._acquired_token = None
        self._restore_signal_handlers()

    def __enter__(self) -> "EnvLeaseGuard":
        if not self.envs:
            return self
        if not self.state_file.is_file():
            raise RuntimeError(f"missing_env_state_file: {self.state_file}")
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_file.open("a", encoding="utf-8") as lock_fh:
            fcntl.flock(lock_fh, fcntl.LOCK_EX)
            try:
                state = json.loads(self.state_file.read_text(encoding="utf-8"))
                environments = state.get("environments") or {}
                if self.lease_token:
                    changed = False
                    pid = os.getpid()
                    for env_id in self.envs:
                        entry = environments.get(str(env_id))
                        if (
                            not isinstance(entry, dict)
                            or entry.get("status") != "occupied"
                            or entry.get("token") != self.lease_token
                        ):
                            raise RuntimeError(
                                f"environment_lease_mismatch: env{env_id} "
                                f"status={entry.get('status') if isinstance(entry, dict) else None!r}"
                            )
                        if entry.get("pid") != pid:
                            entry["pid"] = pid
                            changed = True
                    if changed:
                        self._atomic_write(self.state_file, state)
                    return self

                for env_id in self.envs:
                    entry = environments.get(str(env_id))
                    if not isinstance(entry, dict) or entry.get("status") != "idle":
                        raise RuntimeError(
                            f"environment_occupied: env{env_id} held by "
                            f"{entry.get('experiment') if isinstance(entry, dict) else None!r} "
                            f"pid={entry.get('pid') if isinstance(entry, dict) else None}"
                        )
                    self._prior_meta[str(env_id)] = (entry.get("snapshot"), entry.get("fingerprint"))

                token = uuid.uuid4().hex
                stamp = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat()
                for env_id in self.envs:
                    snap, fp = self._prior_meta[str(env_id)]
                    environments[str(env_id)] = {
                        "status": "occupied",
                        "experiment": self.experiment,
                        "pid": os.getpid(),
                        "token": token,
                        "started_at": stamp,
                        "snapshot": snap,
                        "fingerprint": fp,
                    }
                self._atomic_write(self.state_file, state)
                self._acquired_token = token
            finally:
                fcntl.flock(lock_fh, fcntl.LOCK_UN)
        self._install_signal_handlers()
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


__all__ = [
    "DEFAULT_LEASE_LOCK_FILE",
    "DEFAULT_LEASE_STATE_FILE",
    "EnvLeaseGuard",
]
