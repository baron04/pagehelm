import unittest

from web_operation_agent.schemas import ElementRef, PageObservation
from web_operation_agent_yudao.native_tabs import NativeTabPolicy


class NativeTabPolicyTests(unittest.TestCase):
    def test_only_enabled_native_tabs_on_deployed_record_routes_are_allowed(self):
        policy = NativeTabPolicy(("/crm/customer/detail/{id}",))
        page = PageObservation(schema_version=1, epoch=1,
            url="https://fixture.test/crm/customer/detail/1", title="Customer",
            landmarks=[], elements=[], content="Synthetic customer")
        tab = ElementRef(ref="e1", role="tab", name="回款", tag="div", epoch=1,
                         attrs={"id": "tab-payments", "aria-controls": "pane-payments"})
        self.assertEqual(policy.decide(page, tab), "")
        from dataclasses import replace
        self.assertTrue(policy.decide(page, replace(tab, role="button", name="确定")))
        self.assertTrue(policy.decide(page, replace(tab, attrs={"aria-disabled": "true"})))
        self.assertTrue(policy.decide(page, replace(tab, attrs={})))
        page.url = "https://fixture.test/crm/customer"
        self.assertTrue(policy.decide(page, tab))


if __name__ == "__main__": unittest.main()
