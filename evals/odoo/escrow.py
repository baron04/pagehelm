"""Encrypted, digest-verified escrow of the held-out task set and both population seeds.

The held-out set and the two seeds exist in one ignored directory, and losing it retires the
held-out set. ``create`` packs exactly those files with a manifest of their SHA-256 digests,
encrypts the pack with OpenSSL (AES-256-CBC, PBKDF2-SHA256 key derivation) under a key derived
from a random 256-bit secret, authenticates header and ciphertext with HMAC-SHA256, and writes
the archive and the secret to two different directories outside the repository. ``verify``
checks the MAC before decrypting, recomputes every digest and, given the private directory,
compares each file byte for byte; ``restore`` writes the files only into a new, empty
directory. Nothing here enters the repository or a model's context.

Two directories on one machine are not custody. ``custody`` restore-verifies a copy and writes
a record, holding neither paths nor secrets, of three failure domains: where the original lives,
where the archive copy lives and where the secret copy lives. All three must differ. A domain
the machine can prove is a physical device, identified from the operating system's disk
description and refused for disk images, or a network volume served by another host. Where the
machine cannot prove a domain, the record carries an explicit owner attestation instead.

    python -m evals.odoo.escrow create --archive PATH --secret PATH --record PATH
    python -m evals.odoo.escrow verify --archive PATH --secret PATH [--private-dir DIR]
    python -m evals.odoo.escrow restore --archive PATH --secret PATH --destination DIR
    python -m evals.odoo.escrow custody --archive PATH --secret PATH --record PATH \
        [--archive-attested LABEL] [--secret-attested LABEL] [--attested-by NAME]
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import io
import json
import os
import plistlib
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .population import digest_json
from .revision import REPOSITORY

ESCROW_FILES = ("heldout/p74-odoo-heldout-v1.json", "seeds/development.seed",
                "seeds/heldout.seed")
MAGIC = b"P74-ESCROW-1\n"
CIPHER = "aes-256-cbc"
PBKDF2_ITERATIONS = 200_000
MAC_BYTES = 32


class EscrowError(RuntimeError):
    """The escrow cannot be created, opened, verified or restored as asked."""


def _openssl() -> str:
    path = shutil.which("openssl")
    if path is None:
        raise EscrowError("openssl is required to encrypt or decrypt the escrow")
    return path


def _derive(secret: bytes, label: bytes) -> bytes:
    return hmac.new(secret, label, hashlib.sha256).digest()


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _cipher(data: bytes, secret: bytes, *, decrypt: bool) -> bytes:
    """Run OpenSSL with the passphrase on a pipe, never on the command line."""
    read_end, write_end = os.pipe()
    try:
        os.write(write_end, _derive(secret, b"p74-escrow/encrypt").hex().encode() + b"\n")
    finally:
        os.close(write_end)
    try:
        completed = subprocess.run(
            [_openssl(), "enc", *(["-d"] if decrypt else []), f"-{CIPHER}", "-pbkdf2",
             "-iter", str(PBKDF2_ITERATIONS), "-md", "sha256", "-pass", f"fd:{read_end}"],
            input=data, capture_output=True, pass_fds=(read_end,), timeout=120, check=False)
    finally:
        os.close(read_end)
    if completed.returncode != 0:
        raise EscrowError("openssl could not process the escrow")
    return completed.stdout


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                         0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _pack(private_dir: Path) -> tuple[bytes, dict[str, Any]]:
    buffer = io.BytesIO()
    files: dict[str, dict[str, Any]] = {}
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for relative in ESCROW_FILES:
            try:
                data = (private_dir / relative).read_bytes()
            except OSError as exc:
                raise EscrowError(f"{relative} is missing from the private directory") from exc
            files[relative] = {"sha256": _sha256(data), "bytes": len(data)}
            archive.addfile(_member(relative, len(data)), io.BytesIO(data))
        manifest = {"artifact_version": 1, "kind": "p74_escrow_manifest", "files": files}
        encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        archive.addfile(_member("manifest.json", len(encoded)), io.BytesIO(encoded))
    return buffer.getvalue(), manifest


def _member(name: str, size: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.mtime = size, 0o600, 0
    return info


def create(private_dir: str | Path, archive_path: str | Path,
           secret_path: str | Path) -> dict[str, Any]:
    """Write a new encrypted archive and its secret, then verify the archive against the source."""
    private_dir, archive_path, secret_path = Path(private_dir), Path(archive_path), Path(secret_path)
    for path in (archive_path, secret_path):
        if _inside(path, REPOSITORY):
            raise EscrowError("escrow files must live outside the repository")
        if path.exists():
            raise EscrowError(f"{path.name} already exists; the escrow never overwrites")
    if archive_path.parent.resolve() == secret_path.parent.resolve():
        raise EscrowError("keep the archive and its secret in different directories")
    pack, manifest = _pack(private_dir)
    secret = secrets.token_bytes(32)
    ciphertext = _cipher(pack, secret, decrypt=False)
    header = {"artifact_version": 1, "kind": "p74_escrow_archive", "cipher": CIPHER,
              "kdf": "pbkdf2-sha256", "iterations": PBKDF2_ITERATIONS, "mac": "hmac-sha256",
              "files": len(ESCROW_FILES), "manifest_digest": digest_json(manifest),
              "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    header_line = json.dumps(header, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    mac = hmac.new(_derive(secret, b"p74-escrow/authenticate"), MAGIC + header_line + ciphertext,
                   hashlib.sha256).digest()
    _write_private(secret_path, secret.hex().encode() + b"\n")
    _write_private(archive_path, MAGIC + header_line + ciphertext + mac)
    return verify(archive_path, secret_path, private_dir=private_dir)


def _open(archive_path: str | Path, secret_path: str | Path) -> tuple[dict[str, Any],
                                                                    dict[str, bytes]]:
    try:
        data = Path(archive_path).read_bytes()
        secret = bytes.fromhex(Path(secret_path).read_text(encoding="ascii").strip())
    except (OSError, ValueError) as exc:
        raise EscrowError("the escrow archive or its secret cannot be read") from exc
    if len(secret) != 32 or not data.startswith(MAGIC):
        raise EscrowError("this is not a P7.4 escrow archive and secret")
    rest = data[len(MAGIC):]
    newline = rest.find(b"\n")
    if newline < 0 or len(rest) <= newline + 1 + MAC_BYTES:
        raise EscrowError("the escrow archive is truncated")
    header_line, body = rest[:newline + 1], rest[newline + 1:]
    ciphertext, mac = body[:-MAC_BYTES], body[-MAC_BYTES:]
    expected = hmac.new(_derive(secret, b"p74-escrow/authenticate"),
                        MAGIC + header_line + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected):
        raise EscrowError("the escrow archive or its secret is wrong, or the archive was altered")
    header = json.loads(header_line)
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(_cipher(ciphertext, secret, decrypt=True)),
                      mode="r") as archive:
        for member in archive.getmembers():
            if not member.isfile() or member.name not in (*ESCROW_FILES, "manifest.json") \
                    or member.name in files:
                raise EscrowError("the escrow pack holds an unexpected member")
            files[member.name] = archive.extractfile(member).read()
    manifest = json.loads(files.pop("manifest.json", b"null"))
    if (set(files) != set(ESCROW_FILES) or not isinstance(manifest, dict)
            or digest_json(manifest) != header.get("manifest_digest")
            or any(manifest["files"][name]["sha256"] != _sha256(content)
                   for name, content in files.items())):
        raise EscrowError("the escrowed files do not match their manifest")
    return header, files


def verify(archive_path: str | Path, secret_path: str | Path, *,
           private_dir: str | Path | None = None) -> dict[str, Any]:
    header, files = _open(archive_path, secret_path)
    matches = None
    if private_dir is not None:
        try:
            matches = all((Path(private_dir) / name).read_bytes() == content
                          for name, content in files.items())
        except OSError:
            matches = False
        if not matches:
            raise EscrowError("the escrowed copy differs from the private files")
    return {"artifact_version": 1, "kind": "p74_escrow_verification", "status": "READY",
            "manifest_digest": header["manifest_digest"], "files": len(files),
            "created_at": header["created_at"], "matches_private_files": matches}


def restore(archive_path: str | Path, secret_path: str | Path,
            destination: str | Path) -> dict[str, Any]:
    """Recover the escrowed files into a new, empty directory and verify the result."""
    destination = Path(destination)
    if destination.exists() and any(destination.iterdir()):
        raise EscrowError("restore into a new, empty directory")
    _, files = _open(archive_path, secret_path)
    for name, content in files.items():
        _write_private(destination / name, content)
    return verify(archive_path, secret_path, private_dir=destination)


# -- custody in independent failure domains --------------------------------------------------------
CUSTODY_KIND = "p74_escrow_custody"
NETWORK_FILESYSTEMS = frozenset({"smbfs", "nfs", "nfs4", "afpfs", "webdav", "cifs", "smb3",
                                 "fuse.sshfs", "sshfs", "macfuse", "osxfuse"})
DOMAIN_KINDS = ("physical_device", "network_volume", "owner_attested")
_MOUNT_LINES = (re.compile(r"^(?P<source>.+?) on (?P<mount>.+?) type (?P<fstype>\S+) \("),
                re.compile(r"^(?P<source>.+?) on (?P<mount>.+?) \((?P<fstype>[^,)]+)"))


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def manifest_digest(private_dir: str | Path) -> str:
    """The digest of the manifest an escrow of the current private files carries."""
    return digest_json(_pack(Path(private_dir))[1])


def parse_mount_line(line: str) -> dict[str, str] | None:
    """Source, mount point and filesystem type from one line of ``mount`` (Linux or macOS)."""
    for pattern in _MOUNT_LINES:
        match = pattern.match(line)
        if match:
            return match.groupdict()
    return None


def network_host(source: str) -> str:
    """The server named by a network mount source such as ``//user@host/share`` or ``host:/x``."""
    if "://" in source:
        return (urllib.parse.urlsplit(source).hostname or "").lower()
    if source.startswith("//"):
        return source[2:].split("/", 1)[0].rsplit("@", 1)[-1].split(":", 1)[0].lower()
    if ":" in source:
        return source.split(":", 1)[0].rsplit("@", 1)[-1].lower()
    return ""


def _output(argv: list[str]) -> bytes:
    try:
        completed = subprocess.run(argv, capture_output=True, check=False, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EscrowError(f"{argv[0]} could not describe the volume") from exc
    if completed.returncode != 0:
        raise EscrowError(f"{argv[0]} could not describe the volume")
    return completed.stdout


def _diskutil(target: str) -> dict[str, Any]:
    return plistlib.loads(_output(["diskutil", "info", "-plist", target]))


def probe_volume(path: Path) -> dict[str, Any]:
    """What the operating system reports about the volume that holds ``path``."""
    lines = _output(["df", "-P", str(path)]).decode("utf-8", "replace").splitlines()
    fields = lines[-1].split(None, 5) if len(lines) >= 2 else []
    if len(fields) < 6:
        raise EscrowError("df could not describe the volume")
    facts: dict[str, Any] = {"source": fields[0], "mount": fields[5], "fstype": "", "disks": [],
                             "virtual": False}
    for line in _output(["mount"]).decode("utf-8", "replace").splitlines():
        parsed = parse_mount_line(line)
        if parsed and parsed["mount"] == facts["mount"]:
            facts["source"], facts["fstype"] = parsed["source"], parsed["fstype"]
    if sys.platform == "darwin" and facts["source"].startswith("/dev/"):
        volume = _diskutil(facts["mount"])
        stores = [item.get("APFSPhysicalStore") for item in volume.get("APFSPhysicalStores") or []]
        virtual = (volume.get("VirtualOrPhysical") == "Virtual"
                   or volume.get("BusProtocol") == "Disk Image")
        for store in stores or [volume.get("DeviceIdentifier")]:
            whole = _diskutil(_diskutil(store)["ParentWholeDisk"])
            virtual = virtual or (whole.get("VirtualOrPhysical") == "Virtual"
                                  or whole.get("BusProtocol") == "Disk Image")
            facts["disks"].append({key: whole.get(field) for key, field in (
                ("media", "MediaName"), ("registry", "IORegistryEntryName"),
                ("bus", "BusProtocol"), ("size", "TotalSize"), ("device_tree", "DeviceTreePath"))})
        facts["virtual"] = virtual
    return facts


def _domain_identifier(*parts: Any) -> str:
    encoded = json.dumps(["p74-custody-domain", *parts], sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def _this_machine() -> set[str]:
    names = {"localhost", "127.0.0.1", "::1"}
    for name in (socket.gethostname(), socket.getfqdn()):
        names |= {name.lower(), name.lower().split(".")[0]}
    return names


def failure_domain(path: str | Path, *,
                   probe: Callable[[Path], dict[str, Any]] = probe_volume) -> dict[str, Any]:
    """The failure domain the machine can prove a file lives in; the path never leaves here."""
    facts = probe(Path(path))
    if facts.get("fstype") in NETWORK_FILESYSTEMS:
        host = network_host(facts.get("source") or "")
        if not host or host in _this_machine() or host.split(".")[0] in _this_machine():
            raise EscrowError("a network volume this machine serves is not a separate failure "
                              "domain")
        return {"kind": "network_volume", "identifier": _domain_identifier("network", host),
                "machine_verified": True, "attested_by": None}
    disks = facts.get("disks") or []
    if facts.get("virtual") or not disks:
        raise EscrowError("this machine cannot prove which physical device holds the file; "
                          "record an owner custody attestation for it instead")
    return {"kind": "physical_device", "machine_verified": True, "attested_by": None,
            "identifier": _domain_identifier("device", sorted(
                json.dumps(disk, sort_keys=True) for disk in disks))}


def attested_domain(label: Any, attested_by: Any) -> dict[str, Any]:
    """A domain the owner vouches for; only a digest of its label is kept."""
    if not isinstance(label, str) or not label.strip() or len(label) > 200:
        raise EscrowError("an attested custody domain needs a label of at most 200 characters")
    if not isinstance(attested_by, str) or not attested_by.strip() or len(attested_by) > 80:
        raise EscrowError("an owner custody attestation names who attests it")
    return {"kind": "owner_attested", "machine_verified": False,
            "identifier": _domain_identifier("attested", label.strip().casefold()),
            "attested_by": attested_by.strip()}


def custody_record(*, private_dir: str | Path, archive: str | Path, secret: str | Path,
                   archive_attestation: str | None = None, secret_attestation: str | None = None,
                   original_attestation: str | None = None, attested_by: str | None = None,
                   probe: Callable[[Path], dict[str, Any]] = probe_volume,
                   clock: Callable[[], str] = _utc) -> dict[str, Any]:
    """Restore-verify an escrow copy and record, without paths or secrets, where its parts are."""
    private_dir = Path(private_dir)

    def domain(path: str | Path, attestation: str | None) -> dict[str, Any]:
        if attestation is not None:
            return attested_domain(attestation, attested_by)
        return failure_domain(path, probe=probe)

    domains = {"original": domain(private_dir, original_attestation),
               "archive": domain(archive, archive_attestation),
               "secret": domain(secret, secret_attestation)}
    if len({item["identifier"] for item in domains.values()}) != 3:
        raise EscrowError("the original, the archive and its secret must sit in three different "
                          "failure domains")
    with tempfile.TemporaryDirectory() as directory:
        restored = restore(archive, secret, Path(directory) / "restored")
    verification = verify(archive, secret, private_dir=private_dir)
    body = {
        "artifact_version": 1, "kind": CUSTODY_KIND,
        "manifest_digest": verification["manifest_digest"], "files": verification["files"],
        "archive_sha256": _sha256(Path(archive).read_bytes()), "verified_at": clock(),
        "restore": {"status": restored["status"], "files": restored["files"],
                    "matches_private_files": verification["matches_private_files"]},
        "domains": domains,
    }
    return {**body, "custody_digest": digest_json(body)}


def verify_custody(record: Any, *, private_dir: str | Path) -> dict[str, Any]:
    """A custody record that is unchanged, restore-verified, spread over three failure domains
    and about the current held-out set and seeds."""
    if not isinstance(record, dict) or record.get("kind") != CUSTODY_KIND:
        raise EscrowError("expected an escrow custody record")
    body = {key: value for key, value in record.items() if key != "custody_digest"}
    if record.get("custody_digest") != digest_json(body):
        raise EscrowError("the custody record was changed after it was written")
    if set(body) != {"artifact_version", "kind", "manifest_digest", "files", "archive_sha256",
                     "verified_at", "restore", "domains"}:
        raise EscrowError("the custody record has unexpected fields")
    if body["restore"] != {"status": "READY", "files": len(ESCROW_FILES),
                           "matches_private_files": True}:
        raise EscrowError("the custody record does not show a successful restore")
    if not isinstance(body["verified_at"], str) or not body["verified_at"].endswith("Z"):
        raise EscrowError("the custody record names when it was verified, in UTC")
    domains = body["domains"]
    if not isinstance(domains, dict) or set(domains) != {"original", "archive", "secret"}:
        raise EscrowError("the custody record names the original, archive and secret domains")
    for name, item in domains.items():
        attested = isinstance(item, dict) and item.get("kind") == "owner_attested"
        if (not isinstance(item, dict)
                or set(item) != {"kind", "identifier", "machine_verified", "attested_by"}
                or item["kind"] not in DOMAIN_KINDS
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(item["identifier"]))
                or item["machine_verified"] is attested
                or isinstance(item["attested_by"], str) is not attested):
            raise EscrowError(f"the {name} custody domain is malformed")
    if len({item["identifier"] for item in domains.values()}) != 3:
        raise EscrowError("the original, the archive and its secret must sit in three different "
                          "failure domains")
    if body["manifest_digest"] != manifest_digest(private_dir):
        raise EscrowError("the custody record does not describe the current held-out set and "
                          "seeds")
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.odoo.escrow")
    commands = parser.add_subparsers(dest="command", required=True)
    custody = commands.add_parser("custody")
    custody.add_argument("--archive", required=True)
    custody.add_argument("--secret", required=True)
    custody.add_argument("--private-dir", default=str(REPOSITORY / ".private" / "p74-odoo-v1"))
    custody.add_argument("--record", required=True, help="new file for the path-free record")
    for part in ("original", "archive", "secret"):
        custody.add_argument(f"--{part}-attested", metavar="LABEL",
                             help=f"owner-attested domain of the {part} copy")
    custody.add_argument("--attested-by")
    for name in ("create", "verify", "restore"):
        command = commands.add_parser(name)
        command.add_argument("--archive", required=True)
        command.add_argument("--secret", required=True)
        if name == "create":
            command.add_argument("--private-dir",
                                 default=str(REPOSITORY / ".private" / "p74-odoo-v1"))
            command.add_argument("--record", help="private file that records where the escrow is")
        elif name == "verify":
            command.add_argument("--private-dir")
        else:
            command.add_argument("--destination", required=True)
    args = parser.parse_args(argv)
    if args.command == "custody":
        result = custody_record(private_dir=args.private_dir, archive=args.archive,
                                secret=args.secret, archive_attestation=args.archive_attested,
                                secret_attestation=args.secret_attested,
                                original_attestation=args.original_attested,
                                attested_by=args.attested_by)
        _write_private(Path(args.record),
                       (json.dumps(result, indent=2, sort_keys=True) + "\n").encode())
    elif args.command == "create":
        result = create(args.private_dir, args.archive, args.secret)
        if args.record:
            pointer = {**result, "archive": str(Path(args.archive).resolve()),
                       "secret": str(Path(args.secret).resolve())}
            _write_private(Path(args.record),
                           (json.dumps(pointer, indent=2, sort_keys=True) + "\n").encode())
    elif args.command == "verify":
        result = verify(args.archive, args.secret, private_dir=args.private_dir)
    else:
        result = restore(args.archive, args.secret, args.destination)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
