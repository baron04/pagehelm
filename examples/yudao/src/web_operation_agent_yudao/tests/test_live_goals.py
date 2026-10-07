"""No models, source or accounts: a requested change becomes a plan for the page's own editor."""
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from web_operation_agent.goal.index import ApplicationIndex, IndexEntity, IndexField
from web_operation_agent.goal.ports import PagePlan, Resolution, Resolved
from web_operation_agent.goal.spec import GoalSpec
from web_operation_agent_yudao.live_forms import FenceEvidence, LiveFormPort, field_key
from web_operation_agent_yudao.live_goals import LiveFormsGoalPort
from web_operation_agent_yudao.page_goals import NativePageGoalPort
from web_operation_agent_yudao.page_guard import NativePageGuard
from web_operation_agent_yudao.form_goals import NativeFormsGoalPort

INDEX = ApplicationIndex(entities=(IndexEntity("crm/contact", "联系人", fields=(
    IndexField("customerId", "客户", "link", aliases=("所属客户",)), IndexField("mobile", "手机", "text"))),),
    pages=(), source_digest="synthetic")


class LiveGoalTests(unittest.TestCase):
    def setUp(self):
        self.forms = LiveFormPort(FenceEvidence(NativePageGuard(frozenset(), writing_gets=frozenset()),
                                                lambda: "a" * 64), SimpleNamespace(
            goal_entries=(("contact-create", "crm/contact", "/crm/contact"),), goal_updates=False,
            **{name: lambda *a, **k: None for name in ("plan_entry", "accept_entry", "discard_entry",
                                                      "describe_form", "prepare_form")}))
        self.port = LiveFormsGoalPort.__new__(LiveFormsGoalPort)
        self.port._forms = self.forms
        self.port.index = lambda **k: INDEX
        self.pages = {}
        self.asked = []

        def resolved(_, spec, *, context=None, current_path=""):
            self.asked.append(spec)
            return Resolution(tuple(self.pages.get(i, Resolved(kind=d.kind, problem="not resolved"))
                                    for i, d in enumerate(spec.deliverables)))
        patcher = patch.object(NativePageGoalPort, "resolve", resolved)
        patcher.start()
        self.addCleanup(patcher.stop)

    def resolve(self, *deliverables):
        return self.port.resolve(GoalSpec.from_json({"deliverables": list(deliverables)})).deliverables

    def test_reviewed_source_key_supplies_link_label_alias_without_suffix_guessing(self):
        del self.port.index
        self.port._updates = False
        self.port._entries = (("contact-create", "crm/contact", "/crm/contact", "新增", "create",
                               "Parent.vue", "Contact.vue"),)
        self.port._form_source = SimpleNamespace(read=lambda path: SimpleNamespace(fields=(
            SimpleNamespace(reference="customerId", label="客户名称"),)))
        with patch.object(NativeFormsGoalPort, "index", return_value=INDEX):
            entity = self.port.index().entity("crm/contact")
        self.assertIn("客户名称", entity.fields[0].aliases)
        self.assertNotIn("客户编号", entity.fields[0].aliases)

    def test_a_create_with_values_is_planned_on_the_entity_page_under_one_reference(self):
        self.pages[0] = Resolved(kind="page", page=PagePlan("/crm/contact", "联系人", expected_path="/crm/contact"))
        asked = {"kind": "handoff", "entity": "crm/contact", "action": "create",
                 "field_values": [{"field": "客户", "value": "晨岚机电"}, {"field": "手机", "value": "13800001001"}]}
        first, = self.resolve(asked)
        # The page was resolved as a page: no value was handed to the base port.
        self.assertEqual((self.asked[0].deliverables[0].kind, self.asked[0].deliverables[0].field_values),
                         ("page", ()))
        self.assertEqual((first.kind, first.page.url, first.page.form.open_entry), ("handoff", "/crm/contact", True))
        self.assertEqual(first.page.form.values, ((field_key("客户"), "晨岚机电"), (field_key("手机"), "13800001001")))
        self.assertTrue(first.page.form.reference.startswith("live:create:crm/contact:new:"))
        request = self.forms._requests[first.page.form.reference]
        self.assertEqual((request.mode, request.entity_label), ("create", "联系人"))
        # The index's own names for the field ride along; the page decides which it shows.
        self.assertEqual(dict(request.fields), {field_key("客户"): ("客户", "所属客户"),
                                                field_key("手机"): ("手机",)})
        # The same request resolves to the same reference; another one to its own.
        again, = self.resolve(asked)
        other, = self.resolve({**asked, "field_values": [{"field": "手机", "value": "1"}]})
        self.assertEqual(again.page.form.reference, first.page.form.reference)
        self.assertNotEqual(other.page.form.reference, first.page.form.reference)

    def test_an_unreviewed_update_is_refused_even_with_a_record_page(self):
        self.pages[0] = Resolved(kind="page", page=PagePlan("/crm/contact/detail/12", "顾溪",
                                                            expected_path="/crm/contact/detail/12"))
        done, = self.resolve({"kind": "handoff", "entity": "crm/contact", "record": "顾溪", "action": "edit",
                              "field_values": [{"field": "手机", "value": ""}]})
        self.assertIsNone(done.page)
        self.assertIn("reviewed transient", done.problem)
        self.pages[0] = Resolved(kind="page", page=PagePlan("/crm/contact", "联系人", expected_path="/crm/contact"))
        lost, = self.resolve({"kind": "handoff", "entity": "crm/contact", "record": "顾溪",
                              "field_values": [{"field": "手机", "value": "1"}]})
        self.assertEqual((lost.kind, lost.page), ("handoff", None))
        self.assertTrue(lost.problem)

    def test_what_is_not_a_plain_form_request_is_refused_or_left_to_the_base_port(self):
        self.pages[0] = Resolved(kind="page", page=PagePlan("/crm/contact", "联系人", expected_path="/crm/contact"))
        base = {"kind": "handoff", "entity": "crm/contact"}
        for extra in ({"action": "approve", "field_values": [{"field": "手机", "value": "1"}]},
                      {"section": "团队成员", "field_values": [{"field": "手机", "value": "1"}]},
                      {"field_values": [{"field": "手机", "value": "1"}, {"field": "手 机", "value": "2"}]}):
            with self.subTest(extra=extra):
                refused, = self.resolve({**base, **extra})
                self.assertEqual((refused.kind, refused.page), ("handoff", None))
                self.assertTrue(refused.problem)
        # A problem the base port found is kept as it is.
        self.pages[0] = Resolved(kind="page", problem="no 联系人 record matches")
        kept, = self.resolve({**base, "field_values": [{"field": "手机", "value": "1"}]})
        self.assertEqual((kept.kind, kept.problem), ("handoff", "no 联系人 record matches"))
        # A handoff without values is not this port's: it goes down unchanged.
        self.asked.clear()
        self.resolve({**base, "record": "顾溪", "section": "团队成员"})
        self.assertEqual(self.asked[0].deliverables[0].kind, "handoff")


if __name__ == "__main__":
    unittest.main()
