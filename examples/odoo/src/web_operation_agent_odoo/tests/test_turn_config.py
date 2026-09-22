"""Both entry points assemble the same turn (P7.5 CR-53).

The production server (`native_server.py`, what the single-node deployment runs) and the
demo (`demo/demo.py`, what `demo/qualify.py` and the README quick start run) each wrote
their own `AgentTurnConfig`. They drifted: only production passed the signed-in user's time
zone, so on the demo path a question about "today" was answered for the server's UTC day --
the defect CR-18 fixed, still live where a new integrator meets it first.
"""
from __future__ import annotations

import dataclasses
import unittest
from pathlib import Path

from web_operation_agent.composition.sources import SourceCapability
from web_operation_agent_odoo.turn import odoo_turn_config


class _BusinessTools:
    def __init__(self, zone: str):
        self._zone = zone

    def time_zone(self) -> str:
        return self._zone


class _Adapter:
    """As much of the adapter as assembling a turn touches."""

    def __init__(self, zone: str = "Asia/Shanghai"):
        self.business_tools = _BusinessTools(zone)
        self.capability_calls = 0

    def source_capability(self) -> SourceCapability:
        self.capability_calls += 1
        return SourceCapability.none()

    @staticmethod
    def click_policy():
        from web_operation_agent_odoo.click_policy import OdooReviewedClickPolicy, load_review
        return OdooReviewedClickPolicy.from_review(load_review(), {})


class TheSameTurnWhereverItStarts(unittest.TestCase):
    def test_the_two_entry_points_assemble_the_same_configuration(self):
        adapter = _Adapter()
        run_dir = Path("/tmp/woa-turn-config-test")
        # The production server's call.
        served = odoo_turn_config(adapter=adapter, run_dir=run_dir, allow_mutations=True,
                                  scripted=False, turn_timeout_s=120)
        # The demo's call, which supplies the source capability it compiled once.
        compiled = SourceCapability(source_contract=False, runtime_source=False,
                                    source_affordance_mode="off")
        demoed = odoo_turn_config(adapter=adapter, run_dir=run_dir, allow_mutations=True,
                                  scripted=False, turn_timeout_s=120, source=compiled)
        difference = {
            name: (getattr(served, name), getattr(demoed, name))
            for name in (field.name for field in dataclasses.fields(served))
            if getattr(served, name) != getattr(demoed, name)
        }
        self.assertEqual(difference, {}, "the two entry points assemble different turns")
        self.assertEqual(adapter.capability_calls, 1,
                         "the demo's own compiled capability was recompiled")

    def test_the_signed_in_user_s_time_zone_is_always_carried(self):
        adapter = _Adapter("Asia/Shanghai")
        config = odoo_turn_config(adapter=adapter, run_dir=Path("/tmp/x"),
                                  allow_mutations=False, scripted=True, turn_timeout_s=60)
        self.assertEqual(config.time_zone, "Asia/Shanghai")

    def test_what_the_caller_decides_reaches_the_configuration(self):
        adapter = _Adapter()
        read_only = odoo_turn_config(adapter=adapter, run_dir=Path("/tmp/x"),
                                     allow_mutations=False, scripted=True, turn_timeout_s=0.5)
        writing = odoo_turn_config(adapter=adapter, run_dir=Path("/tmp/x"),
                                   allow_mutations=True, scripted=False, turn_timeout_s=200)
        self.assertFalse(read_only.allow_business_mutations)
        self.assertIn("Do not modify records", read_only.supported_scope)
        self.assertTrue(writing.allow_business_mutations)
        self.assertIn("the one record change the user confirms", writing.supported_scope)
        # A scripted run keeps the explicit load/finish transitions the apparatus exercises.
        self.assertFalse(read_only.deterministic_read_navigation)
        self.assertTrue(writing.deterministic_read_navigation)
        # A budget below a second is still a budget.
        self.assertEqual(read_only.wall_budget_s, 1)
        self.assertEqual(writing.wall_budget_s, 200)
        self.assertEqual(writing.transport_timeout_s, 30)


if __name__ == "__main__":
    unittest.main()
