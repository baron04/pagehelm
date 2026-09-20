"""How this deployment assembles one Agent turn, in one place.

The production server and the demo each used to write their own `AgentTurnConfig`, by hand,
from the same intent. They drifted: the demo -- which `demo/qualify.py` and the README's
quick start both run -- never got the signed-in user's time zone, so a question about
"today" asked between midnight and 08:00 in Shanghai was answered for the server's UTC day.
That is P7.5 CR-18 all over again, still present on the path a new integrator follows
(CR-53). Both entry points call this now, and a test compares what they produce.
"""
from __future__ import annotations

from pathlib import Path

from web_operation_agent.composition.sources import SourceCapability
from web_operation_agent.execution.product import AgentTurnConfig

#: Page actions this deployment does not offer: Odoo is driven through its typed API, and
#: the one write a turn may make goes through a confirmed typed tool.
DISABLED_PAGE_ACTIONS = frozenset({"click", "fill", "select", "submit", "back"})

_READ_ONLY_SCOPE = (
    "Read current Odoo records through the typed API and open only an exact route "
    "returned by that API. Do not modify records or submit forms.")
_WRITE_SCOPE = (
    "Read current Odoo records through the typed API, and make at most the one record "
    "change the user confirms, through a typed write tool. Do not submit page forms. "
    "Open only an exact route returned by the API.")


def odoo_turn_config(*, adapter, run_dir: Path, allow_mutations: bool, scripted: bool,
                     turn_timeout_s: float,
                     source: SourceCapability | None = None) -> AgentTurnConfig:
    """The configuration every Odoo turn runs with, wherever the turn was started.

    `source` is the compiled source capability when the deployment has one; without it the
    adapter's own answer is used, which is the ordinary case.
    """
    return AgentTurnConfig(
        supported_scope=_WRITE_SCOPE if allow_mutations else _READ_ONLY_SCOPE,
        ledger_path=run_dir / "audit.jsonl",
        step_budget=10,
        wall_budget_s=max(1, int(turn_timeout_s)),
        transport_timeout_s=min(30, turn_timeout_s),
        disabled_actions=DISABLED_PAGE_ACTIONS,
        scope_first_policy=True,
        policy_block_limit=3,
        plain_business_answer_min_page_actions=0,
        # The scripted apparatus deliberately exercises the explicit load/finish
        # transitions. Provider mode may remove those two model round trips once the server
        # has enough trusted evidence to do so.
        deterministic_read_navigation=not scripted,
        require_identity=True,
        allow_business_mutations=allow_mutations,
        # The adapter may issue an exact transient customer-form draft. Direct model
        # fill/select/submit actions remain unavailable.
        allow_form_prefill=True,
        deployment_profile="production",
        # The adapter's whole answer, not a selection from it: with a source root configured
        # this is what turns on cited runtime lookup as well as source affordance candidates.
        source=adapter.source_capability() if source is None else source,
        # The signed-in Odoo user's own zone, so "today" is their day and every time the
        # assistant reports matches what Odoo shows them.
        time_zone=adapter.business_tools.time_zone(),
        max_user_answer_chars=8_000,
    )
