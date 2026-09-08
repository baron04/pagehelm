"""Spawn the thin browser runtime and exchange typed JSON with it.

The client launches the worker from the same installed Python environment as the caller.
It does not add a source checkout to ``PYTHONPATH`` or route through another launcher, so
the launch contract is the same in development and in an installed wheel.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from collections.abc import Mapping
from os import PathLike
from pathlib import Path

from .privacy import SENSITIVE_CREDENTIAL_NAME
from .schemas import ActionCommand, ActionReceipt, PageObservation

DEFAULT_PYTHON = sys.executable


class TransportError(Exception):
    """Any failure of the channel itself. Callers map this to terminal reason `transport_error`."""


class TurnCancelled(TransportError):
    """The authenticated user cancelled before the browser claimed the next action."""


class ThinBrowserClient:
    def __init__(self, base_url: str, *, python: str | PathLike[str] | None = None,
                 headless: bool = True, env: Mapping[str, str] | None = None):
        self.base_url = base_url
        self.python = os.fspath(python) if python is not None else DEFAULT_PYTHON
        self.headless = headless
        self.env = dict(env) if env is not None else None
        self.proc: subprocess.Popen | None = None
        # Kept as a read-only compatibility signal for older remote browser peers. The
        # packaged worker never performs credential-based session recovery.
        self.relogins = 0
        self.command_count = 0

    # -- process -----------------------------------------------------------------------
    def _argv(self) -> list[str]:
        return [self.python, "-u", "-m", "web_operation_agent.browser_worker"]

    def _child_env(self) -> dict[str, str]:
        """Return a worker environment with server credentials and import hooks removed."""
        env = dict(os.environ if self.env is None else self.env)
        # The worker must resolve from the selected interpreter's installed packages.
        # Inheriting an ambient import hook can accidentally make a local checkout win.
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
        # The server must not carry any credential-shaped environment value into the
        # browser process, regardless of provider or application naming conventions.
        for key in tuple(env):
            if SENSITIVE_CREDENTIAL_NAME.search(key):
                env.pop(key, None)
        return env

    def start(self, *, entry_path: str = "/",
              storage_state_path: str | PathLike[str] | None = None) -> dict:
        """Start one browser context, optionally from application-provided auth state.

        ``storage_state_path`` is deliberately a path rather than a username/password
        pair. Authentication remains an application-owned setup step and the generic
        worker never guesses that a page is a login form or submits credentials.
        """
        storage_state = None
        if storage_state_path is not None:
            storage_state = os.fspath(storage_state_path)
            path = Path(storage_state)
            if not path.is_absolute():
                raise ValueError("storage_state_path must be absolute")
            if not path.is_file():
                raise FileNotFoundError("storage-state file does not exist")
            try:
                parsed_state = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                raise ValueError("storage-state file is not valid UTF-8 JSON") from None
            if not isinstance(parsed_state, dict):
                raise ValueError("storage-state file must contain a JSON object")
            del parsed_state
        self.proc = subprocess.Popen(self._argv(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, bufsize=1,
                                     env=self._child_env())
        message = {"kind": "start", "base_url": self.base_url,
                   "entry_path": entry_path, "headless": self.headless}
        if storage_state is not None:
            message["storage_state_path"] = storage_state
        try:
            return self._roundtrip(message, expect="started")
        except BaseException:
            self._reap_process(self.proc, timeout=5)
            raise

    def close(self) -> None:
        if self.proc is None:
            return
        proc = self.proc
        try:
            self._roundtrip({"kind": "close"}, expect="closed", timeout_note="close")
        except Exception:
            pass
        self._reap_process(proc, timeout=20)

    def _reap_process(self, proc: subprocess.Popen, *, timeout: int) -> None:
        """Close pipes and ensure a failed or completed worker cannot remain orphaned."""
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=timeout)
        except Exception:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except Exception:
                    pass
        finally:
            for stream in (proc.stdout, proc.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except Exception:
                    pass
            if self.proc is proc:
                self.proc = None

    # -- protocol ----------------------------------------------------------------------
    def _roundtrip(self, msg: dict, *, expect: str, timeout_note: str = "") -> dict:
        if self.proc is None or self.proc.poll() is not None:
            raise TransportError(f"browser process is not running ({timeout_note or msg.get('kind')})")
        try:
            self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
        except Exception as e:
            raise TransportError(f"could not send {msg.get('kind')!r}: {e}\n{self._stderr()}")
        while True:
            line = self.proc.stdout.readline()
            if line == "":
                raise TransportError(f"browser process ended while awaiting {expect!r}\n{self._stderr()}")
            line = line.strip()
            if not line or not line.startswith("{"):
                # Ignore incidental non-protocol output from the runtime or its dependencies.
                continue
            try:
                reply = json.loads(line)
            except json.JSONDecodeError:
                continue
            if reply.get("kind") == "error":
                raise TransportError(f"browser error: {reply.get('error')}\n"
                                     f"{reply.get('traceback', '')}")
            if reply.get("kind") != expect:
                raise TransportError(f"expected {expect!r}, got {reply.get('kind')!r}")
            return reply

    def _stderr(self) -> str:
        if self.proc is None or self.proc.stderr is None:
            return ""
        if self.proc.poll() is not None:
            try:
                return self.proc.stderr.read()[-4000:]
            except Exception:
                return ""
        try:
            self.proc.stderr.flush()
        except Exception:
            pass
        return ""

    # -- api ---------------------------------------------------------------------------
    def observe(self) -> PageObservation:
        r = self._roundtrip({"kind": "observe"}, expect="observation")
        return PageObservation.from_json(r["observation"])

    def execute(self, action: str, args: dict | None = None, *,
                expect_navigation: bool = False) -> ActionReceipt:
        self.command_count += 1
        cmd = ActionCommand(command_id=f"c{self.command_count:04d}", action=action,
                            args=args or {}, expect_navigation=expect_navigation)
        r = self._roundtrip({"kind": "command", "command": cmd.to_json()}, expect="receipt")
        receipt = ActionReceipt.from_json(r["receipt"])
        if receipt.command_id != cmd.command_id or receipt.action != cmd.action:
            raise TransportError(
                f"receipt does not match the command: sent {cmd.command_id}/{cmd.action}, "
                f"got {receipt.command_id}/{receipt.action}")
        return receipt

    def console(self) -> list[str]:
        """Return browser console lines."""
        reply = self._roundtrip({"kind": "console"}, expect="console")
        # Older peers may still report this field. New workers omit it and never log in.
        self.relogins = int(reply.get("relogins") or 0)
        return reply.get("lines", [])

    def __enter__(self) -> "ThinBrowserClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def describe_launch(client: ThinBrowserClient) -> str:
    return " ".join(shlex.quote(a) for a in client._argv())
