"""Run the product test suite with a spend-ledger integrity guard.

Synthetic usage is indistinguishable from real usage once written to the production ledger.
The whole suite therefore runs between two fingerprints of that file and fails if any test
changes it. Individual meter tests must always use a temporary ledger path.

    python -m web_operation_agent.tests.run_all [-k pattern]
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

from web_operation_agent.model import DEFAULT_LEDGER      # noqa: E402


def fingerprint(path: str) -> str:
    p = Path(path)
    if not p.exists():
        return "absent"
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return f"{h.hexdigest()}:{p.stat().st_size}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-k", "--pattern", default="test_*.py")
    ap.add_argument("-v", "--verbose", action="count", default=1)
    a = ap.parse_args(argv)

    before = fingerprint(DEFAULT_LEDGER)
    # Any test that forgets to redirect its meter now writes into a scratch file instead of
    # the project's books; the fingerprint below is the backstop, not the only defence.
    os.environ.setdefault("WOA_SPEND_LEDGER", str(HERE / ".scratch-spend.jsonl"))

    suite = unittest.TestLoader().discover(str(HERE), pattern=a.pattern,
                                           top_level_dir=str(HERE.parents[1]))
    result = unittest.TextTestRunner(verbosity=a.verbose).run(suite)

    after = fingerprint(DEFAULT_LEDGER)
    scratch = HERE / ".scratch-spend.jsonl"
    wrote_scratch = scratch.exists() and scratch.stat().st_size > 0
    scratch.unlink(missing_ok=True)

    untouched = before == after
    print(f"\nspend ledger untouched: {untouched}  ({DEFAULT_LEDGER})")
    if wrote_scratch:
        print("note: a test wrote usage lines; they went to the scratch ledger, as intended")
    if not untouched:
        print("FAILED: the test suite modified the real spend ledger")
        return 2
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
