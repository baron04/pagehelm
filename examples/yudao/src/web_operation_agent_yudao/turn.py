"""One production turn configuration, reused by qualification runs."""
from pathlib import Path

from web_operation_agent.composition.sources import SourceCapability
from web_operation_agent.execution.product import AgentTurnConfig


def yudao_turn_config(run_dir: Path, *, timeout_s: int,
                      source: SourceCapability, click_policy=None, form_prefill: bool = False,
                      page_filtering: bool = False, deterministic_reads: bool = False,
                      native_writes: bool = False) -> AgentTurnConfig:
    reads = ("Use inspect_data to see current entities and fields, then resolve_data for "
        "business facts, filtered counts/sums and complete lists. Keep every owner, date and "
        "status condition in metric.filters. Numeric highest/lowest uses min/max, including "
        "every tie. Select the requested business field using its actual label or UI alias; "
        "tax alone is not a tax-inclusive total. Use native aggregate evidence or compute, "
        "never statement arithmetic. resolve_record_reference supplies exact page addresses "
        "when navigation is requested. " if deterministic_reads else
        "Prefer resolve_goal_candidates for a business read: it combines entity, field and "
        "named-record discovery in one call; use separate discovery only if its result needs "
        "refinement. For counts, including filtered counts, query with field_ids=[] and bind "
        "filters to discovered fields; cite its ready count evidence without fetching record "
        "values or computing again. ")
    return AgentTurnConfig(
        supported_scope=(
            "Answer source and documentation questions from cited repository evidence; "
            "read current business records as the signed-in actor and open exact pages "
            "returned by read tools. " + reads + "Pure source "
            "questions need no business metadata or page navigation. A request may combine "
            "source questions, data reads and page navigation. Source describes the "
            "deployed implementation, not current records or execution authority. "
            "Do not substitute business operation logs for Git history, or record "
            "owners for declared engineering ownership. " +
            ("Reviewed typed mutations may be proposed only for execute intent with a loaded supporting Skill; "
             "each effect requires its own fresh approval and application readback. Never submit a form. " if native_writes else
             "No record mutations or form submission are enabled. ") +
            "For source search, start with the user's own terms and language; this "
            "lexical index does not translate queries. Use identifiers when known. "
            "For a pure page-opening request, find_page_entries returns current "
            "application paths; keep those paths relative rather than adding an origin."),
        ledger_path=run_dir / "audit.jsonl", wall_budget_s=timeout_s,
        transport_timeout_s=30, require_identity=True, observe_before_reads=True, source=source,
        disabled_actions=frozenset({"click", "submit", "back", "apply_facet"}
                                  | (set() if form_prefill or page_filtering else {"fill", "select"})),
        allow_form_prefill=form_prefill,
        allow_business_mutations=native_writes,
        click_policy=click_policy,
        plain_business_answer_min_page_actions=0, time_zone="Asia/Shanghai", step_budget=12)


__all__ = ["yudao_turn_config"]
