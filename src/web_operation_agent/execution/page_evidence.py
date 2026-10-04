"""Turn-owned page facts from an already observed, redacted browser snapshot."""
from __future__ import annotations

import json
import time

from ..answer import answer_protocol, page_answer
from ..privacy import redact_sensitive_assignments
from ..schemas import PageObservation


def capture_page_snapshot(session, observation: PageObservation, *, step: int,
                          reused_observation: bool = True,
                          query: str = "", offset: int = 0
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
    query = redact_sensitive_assignments(session.session_handoffs.redact(query))
    window = page_answer.collect_window(clean, observed_at=str(time.time()),
                                       query=query, offset=offset)
    refs = session.evidence.capture(window.items,
                                    namespace=f"obs{step}")
    index, published = answer_protocol.evidence_index_window(session.evidence, refs)
    presented = len(published)
    if window.items and not presented:
        raise ValueError("the page evidence index cannot fit even one excerpt")
    following = offset + presented
    next_offset = following if following < window.matched_items else None
    session.ledger.append("page_answer_evidence", step=step, epoch=observation.epoch,
                          recorded=len(refs), digest=observation.content_digest,
                          reused_observation=reused_observation, query=query,
                          offset=offset, presented=presented, matched_items=window.matched_items,
                          next_offset=next_offset)
    paging = json.dumps({"query": query, "offset": offset,
                         "matched_items": window.matched_items,
                         "next_offset": next_offset}, ensure_ascii=False)
    return clean, (answer_protocol.ANSWER_INSTRUCTIONS + "\n" +
                   index +
                   "\nPAGE EVIDENCE WINDOW: " + paging +
                   "\nThis searches only bounded excerpts of the current redacted observation, "
                   "not all application data. No match does not establish absence. "
                   "To continue, observe with answer_evidence=true, the same answer_query "
                   "and next_offset as answer_offset. Each refresh replaces page refs; "
                   "offsets are local to the fresh snapshot, not a stable collection cursor. "
                   "If page content changes restart at offset 0. Cite only evidence that "
                   "answers the requested fact; a selected tab is only UI state.")
