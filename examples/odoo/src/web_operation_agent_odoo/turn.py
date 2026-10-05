"""How this deployment assembles one Agent turn, in one place.

The production server and the demo each used to write their own `AgentTurnConfig`, by hand,
from the same intent. They drifted: the demo -- which `demo/qualify.py` and the README's
quick start both run -- never got the signed-in user's time zone, so a question about
"today" asked between midnight and 08:00 in Shanghai was answered for the server's UTC day.
That is P7.5 CR-18 all over again, still present on the path a new integrator follows
(CR-53). Both entry points call this now, and a test compares what they produce.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from web_operation_agent.composition.sources import SourceCapability
from web_operation_agent.execution.product import AgentTurnConfig
from web_operation_agent.schemas import PageObservation

from .click_policy import OdooReviewedClickPolicy

#: Page actions this deployment does not offer: Odoo is driven through its typed API, and
#: the one write a turn may make goes through a confirmed typed tool.
DISABLED_PAGE_ACTIONS = frozenset({"click", "fill", "select", "submit", "back"})

#: What every Odoo turn may click, stated to the model; the adapter's policy enforces it.
_CLICK_SCOPE = (
    " You may click links, tabs, view switches, pagers and search facets on pages reviewed "
    "as read-only; the server refuses every other click before it reaches the page. "
    "If the page lists filter or group candidates, use apply_facet with the listed ref; "
    "the result must be verified in the next page state.")
_READ_ONLY_SCOPE = (
    "Read current Odoo records through the typed API and open only an exact route "
    "returned by that API. Do not modify records or submit forms." + _CLICK_SCOPE)
_WRITE_SCOPE = (
    "Read current Odoo records through the typed API, and make at most the one record "
    "change the user confirms, through a typed write tool. Do not submit page forms. "
    "Open only an exact route returned by the API." + _CLICK_SCOPE)


@dataclass(frozen=True)
class _ReviewedFacetPolicy:
    click_policy: OdooReviewedClickPolicy

    def __call__(self, observation: PageObservation) -> bool:
        return self.click_policy.reviewed(urlsplit(observation.url).path)


def odoo_turn_config(*, adapter, run_dir: Path, allow_mutations: bool, scripted: bool,
                     turn_timeout_s: float,
                     source: SourceCapability | None = None) -> AgentTurnConfig:
    """The configuration every Odoo turn runs with, wherever the turn was started.

    `source` is the compiled source capability when the deployment has one; without it the
    adapter's own answer is used, which is the ordinary case.
    """
    reviewed = adapter.click_policy()
    return AgentTurnConfig(
        supported_scope=_WRITE_SCOPE if allow_mutations else _READ_ONLY_SCOPE,
        ledger_path=run_dir / "audit.jsonl",
        step_budget=10,
        wall_budget_s=max(1, int(turn_timeout_s)),
        transport_timeout_s=min(30, turn_timeout_s),
        disabled_actions=DISABLED_PAGE_ACTIONS,
        # Click stays disabled as an action; the adapter's reviewed policy re-allows the
        # read-only controls on reviewed pages, element by element (owner decision
        # 2026-09-22). Everything it does not name is refused before dispatch.
        click_policy=reviewed,
        scope_first_policy=True,
        policy_block_limit=3,
        plain_business_answer_min_page_actions=0,
        # The scripted apparatus deliberately exercises the explicit load/finish
        # transitions. Provider mode may remove those two model round trips once the server
        # has enough trusted evidence to do so.
        deterministic_read_navigation=not scripted,
        require_identity=True,
        allow_business_mutations=allow_mutations,
        # Odoo forms can save on leaving. Typed drafts therefore need the separately
        # enabled write-confirmation channel before input. Generic prefill is unqualified.
        # Direct model fill/select/submit actions remain unavailable.
        allow_form_prefill=True,
        allow_page_facets=True,
        page_facet_policy=_ReviewedFacetPolicy(reviewed),
        deployment_profile="production",
        # The adapter's whole answer, not a selection from it: with a source root configured
        # this is what turns on cited runtime lookup as well as source affordance candidates.
        source=adapter.source_capability() if source is None else source,
        # The signed-in Odoo user's own zone, so "today" is their day and every time the
        # assistant reports matches what Odoo shows them.
        time_zone=adapter.business_tools.time_zone(),
        max_user_answer_chars=8_000,
    )
