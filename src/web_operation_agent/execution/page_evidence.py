"""Turn-owned page facts from an already observed, redacted browser snapshot."""
from __future__ import annotations

import json
import time

from ..answer import answer_protocol, page_answer
from ..privacy import redact_sensitive_assignments
from ..schemas import PageObservation
from ..schemas.common import digest


def page_window_digest(observation: PageObservation) -> str:
    """Compare complete observations except their transport freshness counters.

    Only consecutive successful answer-window observations may reuse references.
    Keep every value, route, query revision, control identity and render state in
    this comparison. The resulting digest grants no browser action authority.
    """
    projection = observation.to_json()
    projection.pop("epoch")
    for element in projection["elements"]:
        element.pop("epoch")
    return digest(projection)


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
    window_digest = page_window_digest(observation)
    retained = (bool(session._page_answer_digest)
                and session._page_answer_window_digest == window_digest)
    if not retained:
        session.evidence.discard_pages()
    query = redact_sensitive_assignments(session.session_handoffs.redact(query))
    window = page_answer.collect_window(clean, observed_at=str(time.time()),
                                       query=query, offset=offset)
    try:
        refs = session.evidence.capture(window.items, namespace=f"obs{step}")
    except (ValueError, TypeError):
        session.evidence.discard_pages()
        session._page_answer_digest = session._page_answer_window_digest = ""
        raise
    session._page_answer_digest = observation.content_digest
    session._page_answer_window_digest = window_digest
    index, published = answer_protocol.evidence_index_window(session.evidence, refs)
    presented = len(published)
    if window.items and not presented:
        raise ValueError("the page evidence index cannot fit even one excerpt")
    following = offset + presented
    next_offset = following if following < window.matched_items else None
    session.ledger.append("page_answer_evidence", step=step, epoch=observation.epoch,
                          recorded=len(refs), digest=observation.content_digest,
                          reused_observation=reused_observation, query=query,
                          retained_previous_windows=retained,
                          offset=offset, presented=presented, matched_items=window.matched_items,
                          next_offset=next_offset)
    paging = json.dumps({"query": query, "offset": offset,
                         "matched_items": window.matched_items,
                         "next_offset": next_offset}, ensure_ascii=False)
    return clean, (index +
                   "\nPAGE EVIDENCE WINDOW: " + paging +
                   "\nThis searches only bounded excerpts of the current redacted observation, "
                   "not all application data. No match does not establish absence. "
                   "To continue, observe with answer_evidence=true, the same answer_query "
                   "and next_offset as answer_offset. Successful answer-window reads retain "
                   "earlier page refs only while the complete observed page is unchanged "
                   "apart from epoch counters. A changed page, an ordinary observation, "
                   "a failed refresh or an authorization refusal expires those refs; "
                   "offsets are local to the fresh snapshot, not a stable collection cursor. "
                   "If page content changes restart at offset 0. Cite only evidence that "
                   "answers the requested fact; a selected tab is only UI state.")
