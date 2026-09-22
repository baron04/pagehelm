"""Turn-owned page facts from an already observed, redacted browser snapshot."""
from __future__ import annotations

import time

from ..answer import answer_protocol, page_answer
from ..privacy import redact_sensitive_assignments
from ..schemas import PageObservation


def capture_page_snapshot(session, observation: PageObservation, *, step: int,
                          reused_observation: bool = True
                          ) -> tuple[PageObservation, str]:
    """Mint bounded refs without dispatching a second browser command.

    The caller must supply the latest observation from a successful browser receipt.
    A later changed page invalidates these refs; they describe this snapshot, not an
    application read or a permanently current state.
    """
    if session._refused_reads:
        raise ValueError("an authorization refusal closed the page as an answer source")
    clean = PageObservation.from_json(
        redact_sensitive_assignments(session.session_handoffs.redact(
            observation.to_json())))
    session.evidence.discard_pages()
    session._page_answer_digest = observation.content_digest
    refs = session.evidence.capture(page_answer.collect(clean, observed_at=str(time.time())),
                                    namespace=f"obs{step}")
    session.ledger.append("page_answer_evidence", step=step, epoch=observation.epoch,
                          recorded=len(refs), digest=observation.content_digest,
                          reused_observation=reused_observation)
    return clean, (answer_protocol.ANSWER_INSTRUCTIONS + "\n" +
                   answer_protocol.evidence_index(session.evidence, refs))
