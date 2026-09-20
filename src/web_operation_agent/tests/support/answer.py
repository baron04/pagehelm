"""Shared evidence and answer fixtures; contains no test cases."""
from web_operation_agent.answer.evidence import Origin, QueryEvidence, RecordEvidence, TurnEvidence


AT = "2026-09-12T10:04:07+00:00"
SCOPE = "open quotations, neither confirmed nor cancelled"
QUOTATIONS = (
    ("sale.order/1", "1740.00"), ("sale.order/2", "2947.50"),
    ("sale.order/3", "377.50"), ("sale.order/5", "405.00"),
    ("sale.order/19", "1740.00"),
)


def _turn(*, whole: bool = True, records=QUOTATIONS, unit: str = "USD",
          scope: str = SCOPE, looked_for: str = "",
          exact_matches: int | None = None) -> TurnEvidence:
    turn = TurnEvidence()
    turn.record(QueryEvidence(
        evidence_id="query#1",
        origin=Origin(kind="typed", reference="query_quotations#1", observed_at=AT,
                      scope=scope),
        collection="quotations", conditions=("state in draft,sent",),
        matched_count=len(records), records_scanned=len(records),
        covers_whole_scope=whole, looked_for=looked_for,
        exact_matches=exact_matches,
    ))
    for index, (record_id, amount) in enumerate(records, start=1):
        turn.record(RecordEvidence(
            evidence_id=f"record#{index}",
            origin=Origin(kind="typed", reference="query_quotations#1", observed_at=AT,
                          scope=scope),
            record_id=record_id, field_name="amount_total",
            field_meaning="order total", field_was_read=True, value=amount,
            value_type="money", unit=unit,
        ))
    return turn


def _answer(kind, ref, **extra):
    return {"outcome": "answered", "answer": {
        "version": 1, "coverage": "complete",
        "conclusions": [{"kind": kind, "evidence_ref": ref, **extra}],
    }}
