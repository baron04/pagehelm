"""Read-only adapter reconciliation and explicit, audited operator resolution.

The unresolved half of a write's life. A mutation whose outcome was lost sits in the
journal as `dispatching` or `indeterminate` and is never retried by anything, because no
finite read proves an old request stopped executing. Somebody has to look, and this is what
they look with.

`list` is written for that person rather than for a machine: it carries the phase the write
reached, the record id the adapter managed to capture before it lost the answer, and the
exact command to run once they hold evidence. An operator who has to reconstruct a CAS
protocol from memory at two in the morning will get it wrong.

What this module deliberately cannot do is resolve anything on its own. `reconcile` needs a
bound application and a live principal -- an authorised read under the original actor -- and
only a deployment has both, which is why the closing step lives in an application's own
package (see `web_operation_agent_odoo.demo.settle`).
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from ..composition.binding import BoundApplication
from ..state.identity import PrincipalContext
from .mutation import MutationJournalError, SQLiteMutationJournal


def reconcile(journal: SQLiteMutationJournal, proposal_digest: str, *,
              application: BoundApplication, principal: PrincipalContext) -> dict:
    intent = journal.get(proposal_digest)
    if intent is None or intent.state not in {"dispatching", "indeterminate"}:
        raise MutationJournalError("reconciliation requires an unresolved intent")
    application.ports.identity.validate(
        principal, application_id=application.descriptor.application_id)
    metadata = json.loads(intent.reconciliation_json)
    identity = principal.audit_projection()
    expected_identity = {key: identity[key] for key in
                         ("tenant_digest", "workspace_digest", "actor_digest")}
    if (metadata.get("application_id") != application.descriptor.application_id
            or metadata.get("identity") != expected_identity):
        raise MutationJournalError("reconciliation identity does not match the original actor")
    verdict = application.ports.reconciliation.reconcile(
        metadata["tool"], metadata["args"], evidence={"result_digest": intent.result_digest},
        principal=principal)
    # A read can describe current state but cannot establish that an old HTTP request
    # stopped executing. This report never resolves the journal or retries the mutation.
    return {"proposal_digest": proposal_digest, "updated_at": intent.updated_at,
            "outcome": verdict.outcome, "requires_human": verdict.requires_human,
            "reason": verdict.reason, "dispatches": 0, "journal_changed": False}


def unresolved_report(intent, *, journal_path: Path) -> dict:
    """One unresolved write, as the person who has to settle it needs to see it.

    `phase` and `record_id` are the difference between "a write may have happened" and "go
    and look at record 42": the adapter records them on the way down precisely so that the
    person arriving later does not have to guess which record to inspect.
    """
    try:
        metadata = json.loads(intent.reconciliation_json)
    except json.JSONDecodeError:
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    return {
        "proposal_digest": intent.proposal_digest,
        "state": intent.state,
        "updated_at": intent.updated_at,
        "business_key": intent.business_key,
        "tool": str(metadata.get("tool") or ""),
        # How far the write got before its answer was lost. `dispatch` means it may never
        # have been sent; `acknowledgement` and `readback` mean it certainly was.
        "phase": str(metadata.get("phase") or ""),
        # The application's own identifier for what was written, when the adapter had one.
        "record_id": str(metadata.get("record_id") or ""),
        "resolve_command": (
            f"python -m web_operation_agent.business.operations --journal {journal_path} "
            f"resolve {intent.proposal_digest} "
            f"--expected-updated-at {intent.updated_at} "
            "--outcome completed|failed --operator <you> --evidence <file> "
            "--writer-stopped --apply"),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", required=True, type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    resolve = sub.add_parser("resolve")
    resolve.add_argument("proposal")
    resolve.add_argument("--expected-updated-at", type=int, required=True)
    resolve.add_argument("--outcome", choices=("completed", "failed"), required=True)
    resolve.add_argument("--operator", required=True)
    resolve.add_argument("--evidence", required=True, type=Path)
    resolve.add_argument("--writer-stopped", action="store_true",
                         help="attest that the old writer and upstream request can no longer execute")
    resolve.add_argument("--apply", action="store_true", help="apply; default is dry-run")
    args = parser.parse_args(argv)
    if not args.journal.is_file():
        parser.error("journal must already exist")
    journal = SQLiteMutationJournal(args.journal)
    if args.command == "list":
        result = [unresolved_report(item, journal_path=args.journal)
                  for item in journal.unresolved()]
    else:
        raw = args.evidence.read_bytes()
        if not raw or len(raw) > 1_048_576 or not args.operator.strip():
            parser.error("provide a nonempty operator and evidence file of at most 1 MiB")
        result = journal.resolve(
            args.proposal, expected_updated_at=args.expected_updated_at,
            outcome=args.outcome,
            operator_digest=hashlib.sha256(args.operator.strip().encode()).hexdigest(),
            evidence_digest=hashlib.sha256(raw).hexdigest(),
            writer_stopped=args.writer_stopped, dry_run=not args.apply)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
