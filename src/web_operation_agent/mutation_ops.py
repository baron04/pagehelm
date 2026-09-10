"""Read-only adapter reconciliation and explicit, audited operator resolution."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from .application import BoundApplication
from .identity import PrincipalContext
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
        result = [{"proposal_digest": item.proposal_digest, "state": item.state,
                   "updated_at": item.updated_at, "business_key": item.business_key}
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
