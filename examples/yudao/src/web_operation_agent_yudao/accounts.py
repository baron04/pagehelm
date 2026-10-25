"""Named yudao login accounts in one private JSON map kept outside Git.

Each key is the name a tool is given with `--account`; each value is
{"username", "password", "tenant"}. Any other file shape is refused, never guessed at.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

ACCOUNTS_FILE = Path(__file__).resolve().parents[4] / ".private/yudao-accounts.json"


def add_account_arguments(parser) -> None:
    parser.add_argument("--account", required=True, help="entry name in the accounts file")
    parser.add_argument("--accounts", type=Path, default=ACCOUNTS_FILE,
                        help="private name-to-account JSON map (default: %(default)s)")


def _valid(entry) -> bool:
    return isinstance(entry, dict) and entry.keys() >= {"username", "password"}


def read_accounts(path) -> dict:
    accounts = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(accounts, dict) or not all(map(_valid, accounts.values())):
        raise ValueError(f"{path} is not a map of named accounts")
    return accounts


def load_account(name: str, path) -> dict:
    accounts = read_accounts(path)
    if name not in accounts:
        raise LookupError(f"{path} has no account {name!r}; it lists {sorted(accounts)}")
    return accounts[name]


def save_account(name: str, entry: dict, path) -> None:
    """Add a new entry beside the others, replacing the file atomically and privately."""
    path = Path(path)
    accounts = read_accounts(path) if path.exists() else {}
    if name in accounts or not _valid(entry):
        raise ValueError(f"refusing to write account {name!r} to {path}")
    accounts[name] = entry
    staged = path.with_name(path.name + ".tmp")
    fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.chmod(staged, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        json.dump(accounts, out, ensure_ascii=False, indent=2)
        out.write("\n")
        out.flush()
        os.fsync(out.fileno())
    os.replace(staged, path)
