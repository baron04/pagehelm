"""The operator's last step: read what Odoo says, then settle the journal on purpose.

A write whose outcome was lost is never retried. It sits in the journal as `dispatching` or
`indeterminate`, and something has to close it -- otherwise the durable record of "we do not
know" grows forever and nobody can tell an old uncertainty from a new one.

`web_operation_agent.business.operations` has both halves of the machinery already: `reconcile`
performs the application's own read under the original actor, and
`SQLiteMutationJournal.resolve` applies a compare-and-swap that records who decided and on
what evidence. What was missing was the step in between -- the one that needs a real
application, a real credential and a real principal, which only a deployment has. This is
that step for Odoo.

    python -m web_operation_agent_odoo.demo.settle list   --journal ...
    python -m web_operation_agent_odoo.demo.settle inspect --journal ... --proposal <digest> \\
        --base-url ... --api-key ... --storage-state ...
    python -m web_operation_agent_odoo.demo.settle resolve --journal ... --proposal <digest> \\
        --outcome completed --operator alice --writer-stopped --apply ...

Three properties this keeps, none of them incidental:

* **`inspect` writes nothing.** It reconciles, which is a read, and saves what Odoo said as
  the evidence file `resolve` will digest. An operator who ran `inspect` has, by
  construction, the evidence for the decision they are about to make.
* **`resolve` never guesses.** `--writer-stopped` is an attestation a person makes, not
  something this script can observe: a timed-out request may still be executing somewhere,
  and no read from here can rule that out. Without it the journal refuses.
* **Neither ever redispatches.** There is no code path in this file that calls a mutation
  tool. Resolving records a human's conclusion; it does not make it true.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from pathlib import Path

from web_operation_agent.composition.binding import bind_application
from web_operation_agent.business.mutation import MutationJournalError, SQLiteMutationJournal
from web_operation_agent.business.operations import reconcile, unresolved_report

from ..adapter import OdooApplicationAdapter


def _adapter(arguments) -> OdooApplicationAdapter:
    if not (arguments.base_url and arguments.api_key and arguments.storage_state):
        raise SystemExit("an authorised read needs --base-url, --api-key and "
                         "--storage-state: it runs as the original actor, not as root")
    # Mutations are enabled only so the reconciliation port is live. Nothing in this module
    # calls a write tool, and `reconcile` is a read.
    return OdooApplicationAdapter(
        base_url=arguments.base_url, api_key_path=Path(arguments.api_key),
        allow_mutations=True,
        mutation_journal=SQLiteMutationJournal(Path(arguments.journal)))


def inspect(arguments) -> dict:
    """Ask Odoo what it currently shows, under the actor the write was made as."""
    adapter = _adapter(arguments)
    bound = bind_application(adapter)
    principal = adapter.bind_browser_session(
        Path(arguments.storage_state), delegation_id=f"odoo-settle-{uuid.uuid4().hex}")
    journal = SQLiteMutationJournal(Path(arguments.journal))
    report = reconcile(journal, arguments.proposal,
                       application=bound, principal=principal)
    intent = journal.get(arguments.proposal)
    evidence = {
        "kind": "woa_odoo_reconciliation_evidence",
        "proposal_digest": arguments.proposal,
        "observed": report,
        "intent": unresolved_report(intent, journal_path=Path(arguments.journal)),
    }
    if arguments.evidence:
        path = Path(arguments.evidence)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(evidence, ensure_ascii=False, sort_keys=True, indent=2)
                        + "\n", encoding="utf-8")
        path.chmod(0o600)
        evidence["written_to"] = str(path)
    # Always `unknown` and `requires_human`: a matching record is not proof this proposal
    # created it, and its absence cannot rule out a write still in flight.
    return evidence


def resolve(arguments) -> dict:
    journal = SQLiteMutationJournal(Path(arguments.journal))
    intent = journal.get(arguments.proposal)
    if intent is None:
        raise SystemExit("no such proposal in this journal")
    evidence_path = Path(arguments.evidence) if arguments.evidence else None
    if evidence_path is None or not evidence_path.is_file():
        raise SystemExit("resolving needs an evidence file; run `inspect --evidence` first")
    raw = evidence_path.read_bytes()
    if not raw or len(raw) > 1_048_576 or not arguments.operator.strip():
        raise SystemExit("provide an operator and an evidence file of at most 1 MiB")
    # The CAS guard: `updated_at` is read here and checked again inside the transaction, so
    # a journal that moved between reading and deciding refuses rather than overwriting
    # somebody else's conclusion.
    return journal.resolve(
        arguments.proposal, expected_updated_at=intent.updated_at,
        outcome=arguments.outcome,
        operator_digest=hashlib.sha256(arguments.operator.strip().encode()).hexdigest(),
        evidence_digest=hashlib.sha256(raw).hexdigest(),
        writer_stopped=arguments.writer_stopped, dry_run=not arguments.apply)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="web-operation-agent-odoo-settle",
        description="Close out an Odoo write whose outcome was lost.")
    parser.add_argument("--journal", required=True)
    commands = parser.add_subparsers(dest="command", required=True)

    listing = commands.add_parser("list", help="unresolved writes, with what to do next")
    listing.set_defaults(handler=lambda arguments: [
        unresolved_report(item, journal_path=Path(arguments.journal))
        for item in SQLiteMutationJournal(Path(arguments.journal)).unresolved()])

    for name, handler in (("inspect", inspect), ("resolve", resolve)):
        command = commands.add_parser(name)
        command.add_argument("--proposal", required=True)
        command.add_argument("--evidence", default="")
        command.set_defaults(handler=handler)
        if name == "inspect":
            command.add_argument("--base-url", default="http://127.0.0.1:8069")
            command.add_argument("--api-key", default="")
            command.add_argument("--storage-state", default="")
        else:
            command.add_argument("--outcome", choices=("completed", "failed"),
                                 required=True)
            command.add_argument("--operator", required=True)
            command.add_argument("--writer-stopped", action="store_true",
                                 help="attest that the original writer and its upstream "
                                      "request can no longer execute; this cannot be "
                                      "observed from here")
            command.add_argument("--apply", action="store_true",
                                 help="apply; the default is a dry run")

    arguments = parser.parse_args(argv)
    if not Path(arguments.journal).is_file():
        parser.error("journal must already exist")
    try:
        result = arguments.handler(arguments)
    except MutationJournalError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
