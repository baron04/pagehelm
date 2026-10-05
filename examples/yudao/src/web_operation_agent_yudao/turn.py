"""One production turn configuration, reused by qualification runs."""
from pathlib import Path

from web_operation_agent.composition.sources import SourceCapability
from web_operation_agent.execution.product import AgentTurnConfig


def yudao_turn_config(run_dir: Path, *, timeout_s: int,
                      source: SourceCapability, click_policy=None, form_prefill: bool = False,
                      page_filtering: bool = False) -> AgentTurnConfig:
    return AgentTurnConfig(
        supported_scope=(
            "Answer source and documentation questions from cited repository evidence; "
            "read current business records as the signed-in actor and open exact pages "
            "returned by read tools. Prefer resolve_goal_candidates for a business "
            "read: it combines entity, field and named-record discovery in one call; "
            "use separate discovery only if its result needs refinement. Pure source "
            "questions need no business metadata or page navigation. A request may combine "
            "source questions, data reads and page navigation. Source describes the "
            "deployed implementation, not current records or execution authority. "
            "Do not substitute business operation logs for Git history, or record "
            "owners for declared engineering ownership. No record mutations or "
            "form submission are enabled. For counts, including filtered counts, query "
            "with field_ids=[] and bind filters to discovered fields; cite its ready "
            "count evidence without fetching record values or computing again. "
            "For source search, start with the user's own terms and language; this "
            "lexical index does not translate queries. Use identifiers when known. "
            "For a pure page-opening request, find_page_entries returns current "
            "application paths; keep those paths relative rather than adding an origin."),
        ledger_path=run_dir / "audit.jsonl", wall_budget_s=timeout_s,
        transport_timeout_s=30, require_identity=True, observe_before_reads=True, source=source,
        disabled_actions=frozenset({"click", "submit", "back", "apply_facet"}
                                  | (set() if form_prefill or page_filtering else {"fill", "select"})),
        allow_form_prefill=form_prefill,
        click_policy=click_policy,
        plain_business_answer_min_page_actions=0, time_zone="Asia/Shanghai", step_budget=12)


__all__ = ["yudao_turn_config"]
