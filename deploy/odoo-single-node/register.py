#!/usr/bin/env python3
"""Atomically register one Odoo actor's own API-key delegation."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path


DATABASE = re.compile(r"[A-Za-z0-9_.-]{1,63}\Z")
ACTOR = re.compile(r"[1-9][0-9]{0,18}\Z")
LOGIN = re.compile(r"[^\x00-\x1f\x7f]{1,254}\Z")
API_KEY = re.compile(r"[A-Za-z0-9_-]{16,256}\Z")
READ_SCOPES = frozenset({
    "customers:read", "invoices:read", "products:read", "sale_orders:read",
})
WRITE_SCOPES = frozenset({
    "customers:write", "invoices:write", "sale_orders:write",
})
ALL_SCOPES = READ_SCOPES | WRITE_SCOPES


class RegistrationError(RuntimeError):
    pass


def _private_write(path: Path, body: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as target:
            target.write(body)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _read_key(path: Path) -> dict[str, str]:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
            raise RegistrationError("API-key input must be a small regular file")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise RegistrationError("API-key input is unreadable or invalid JSON") from exc
    if not isinstance(value, dict) or set(value) != {"database", "login", "api_key"}:
        raise RegistrationError("API-key input must contain database, login, and api_key")
    for field, pattern in (("database", DATABASE), ("login", LOGIN), ("api_key", API_KEY)):
        if not isinstance(value[field], str) or not pattern.fullmatch(value[field]):
            raise RegistrationError(f"API-key input has an invalid {field}")
    return value


def _verify_identity(api_key_file: Path, rpc_base_url: str, actor_id: str) -> None:
    from web_operation_agent_odoo.adapter import (OdooApiKeyStore,
                                                   OdooBusinessToolExecutor)
    executor = OdooBusinessToolExecutor(
        base_url=rpc_base_url, keys=OdooApiKeyStore(api_key_file),
        allow_private_http=True)
    identity = executor.resolve_delegated_identity(lifetime_s=60)
    if identity.actor_id != actor_id:
        raise RegistrationError(
            "the API key authenticates a different actor than --actor-id")


def register(*, private_dir: str | Path, actor_id: str, api_key_file: str | Path,
             scopes: tuple[str, ...], allow_mutations: bool, replace: bool = False,
             rpc_base_url: str = "") -> dict:
    if not ACTOR.fullmatch(actor_id):
        raise RegistrationError("actor id must be the positive numeric id resolved by Odoo")
    if (not scopes or len(scopes) != len(set(scopes))
            or not set(scopes) <= ALL_SCOPES):
        raise RegistrationError("scopes must be a unique non-empty supported subset")
    if set(scopes) & WRITE_SCOPES and not allow_mutations:
        raise RegistrationError("write scopes require --allow-mutations")
    if allow_mutations and not set(scopes) & WRITE_SCOPES:
        raise RegistrationError("--allow-mutations requires at least one write scope")
    root = Path(private_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    source = Path(api_key_file).expanduser()
    key = _read_key(source)
    if rpc_base_url:
        try:
            _verify_identity(source, rpc_base_url, actor_id)
        except RegistrationError:
            raise
        except Exception as exc:
            raise RegistrationError(
                "the delegated API identity could not be verified against Odoo") from exc
    principal = f"{key['database']}:{actor_id}"
    filename = "actor-" + hashlib.sha256(principal.encode()).hexdigest()[:20] \
        + ".api-key.json"
    lock_path = root / ".delegations.lock"
    lock_path.touch(mode=0o600, exist_ok=True)
    os.chmod(lock_path, 0o600)
    with lock_path.open("r+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        manifest_path = root / "delegations.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise RegistrationError("delegation manifest is unreadable") from exc
        if (not isinstance(manifest, dict) or set(manifest) != {
                "schema_version", "principals"} or manifest.get("schema_version") != 1
                or not isinstance(manifest.get("principals"), dict)):
            raise RegistrationError("delegation manifest does not match schema 1")
        if principal in manifest["principals"] and not replace:
            raise RegistrationError("actor is already registered; use --replace to rotate it")
        normalized = json.dumps(key, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        _private_write(root / filename, normalized)
        manifest["principals"][principal] = {
            "api_key_file": filename,
            "scopes": sorted(scopes),
            "allow_mutations": allow_mutations,
        }
        _private_write(
            manifest_path,
            (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode())
    return {
        "status": "READY", "principal": principal,
        "credential_file": filename, "scopes": sorted(scopes),
        "allow_mutations": allow_mutations,
        "online_identity_check": ("passed" if rpc_base_url else
                                  "deferred_until_signed_browser_bootstrap"),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--private-dir", default=".private/odoo-single-node")
    parser.add_argument("--actor-id", required=True)
    parser.add_argument("--api-key-file", required=True)
    parser.add_argument("--rpc-base-url", required=True)
    parser.add_argument("--scope", action="append", required=True, dest="scopes")
    parser.add_argument("--allow-mutations", action="store_true")
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = register(
            private_dir=args.private_dir, actor_id=args.actor_id,
            api_key_file=args.api_key_file, scopes=tuple(args.scopes),
            allow_mutations=args.allow_mutations, replace=args.replace,
            rpc_base_url=args.rpc_base_url)
    except RegistrationError as exc:
        print(json.dumps({"status": "REFUSED", "reason": str(exc)}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
