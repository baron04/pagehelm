"""The p87 judge reads only the hidden projection and fails closed on every gap."""
from __future__ import annotations

import unittest

from evals.odoo.p87_gate import _operation_probe, _tasks, judge


def _row(**changes):
    row = {"terminal_reason": "agent_finished", "write_detected": False, "answer": "",
           "probe": {}}
    row.update(changes)
    return row


class P87Judge(unittest.TestCase):
    def setUp(self):
        self.tasks = {task["id"]: task for task in _tasks()["tasks"]}

    def test_frozen_taskset_has_a_probe_for_every_operation(self):
        self.assertEqual(len(self.tasks), 17)
        for task in self.tasks.values():
            if task["kind"] == "operation":
                self.assertIsNotNone(_operation_probe(task["probe"]), task["id"])

    def test_arm_d_skills_are_frozen_read_only_and_one_per_task(self):
        from evals.odoo.p87_gate import _skills
        skills = _skills()
        self.assertEqual({skill.skill_id.removeprefix("eval.p87.") for skill in skills},
                         set(self.tasks))
        self.assertTrue(all(skill.read_only for skill in skills))

    def test_navigation_needs_the_exact_last_crumb_and_a_normal_end(self):
        task = self.tasks["nav-sale-order-tags"]
        good = {"breadcrumb_last": "Tags", "path": "/odoo/action-195"}
        self.assertEqual(judge(task, _row(probe=good)), [])
        self.assertIn("wrong_breadcrumb",
                      judge(task, _row(probe={**good, "breadcrumb_last": "Product Tags"})))
        self.assertIn("wrong_path", judge(task, _row(probe={**good, "path": "/odoo/x"})))
        self.assertIn("terminal:step_budget",
                      judge(task, _row(probe=good, terminal_reason="step_budget")))
        self.assertIn("write_detected", judge(task, _row(probe=good, write_detected=True)))

    def test_encoded_select_values_compare_by_value(self):
        task = self.tasks["op-select-invoice-policy"]
        probe = {"invoice_policy": '"delivery"', "save_dialog_open": False}
        self.assertEqual(judge(task, _row(probe=probe)), [])
        self.assertIn("probe_mismatch:invoice_policy",
                      judge(task, _row(probe={**probe, "invoice_policy": '"order"'})))

    def test_count_and_concepts_come_from_the_answer_text(self):
        count = self.tasks["data-count-quotations"]
        self.assertEqual(judge(count, _row(answer="You can see 4 quotations.")), [])
        self.assertIn("wrong_number", judge(count, _row(answer="You can see 14 or 4.5.")))
        concepts = self.tasks["src-release-order-invoice-status"]
        full = "Upselling Opportunity, Fully Invoiced, To Invoice, Nothing to Invoice"
        self.assertEqual(judge(concepts, _row(answer=full)), [])
        self.assertTrue(judge(concepts, _row(answer="Fully Invoiced only")))


if __name__ == "__main__":
    unittest.main()
