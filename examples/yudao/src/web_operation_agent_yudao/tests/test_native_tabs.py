import unittest

from web_operation_agent.schemas import ElementRef, PageObservation
from web_operation_agent_yudao.native_tabs import NativeTabPolicy


class NativeTabPolicyTests(unittest.TestCase):
    def test_only_enabled_native_tabs_on_deployed_record_routes_are_allowed(self):
        policy = NativeTabPolicy(("/crm/customer/detail/{id}",))
        page = PageObservation(schema_version=1, epoch=1,
            url="https://fixture.test/crm/customer/detail/1", title="Customer",
            landmarks=[], elements=[], content="Synthetic customer", render_state="ready",
            record_route="/crm/customer/detail/1")
        tab = ElementRef(ref="e1", role="tab", name="回款", tag="div", epoch=1,
                         attrs={"id": "tab-payments", "aria-controls": "pane-payments"})
        self.assertEqual(policy.decide(page, tab), "")
        from dataclasses import replace
        self.assertTrue(policy.decide(page, replace(tab, role="button", name="确定")))
        self.assertTrue(policy.decide(page, replace(tab, attrs={"aria-disabled": "true"})))
        self.assertTrue(policy.decide(page, replace(tab, attrs={})))
        self.assertTrue(policy.decide(replace(page, record_route="/crm/customer/detail/2"), tab))
        self.assertTrue(policy.decide(replace(page, render_state="blocked"), tab))
        page.url = "https://fixture.test/crm/customer"
        self.assertTrue(policy.decide(page, tab))


class NativeNavigationTests(unittest.TestCase):
    def test_actor_catalog_and_fresh_default_menu_facts_are_both_required(self):
        from dataclasses import replace
        from web_operation_agent.goal.index import ApplicationIndex, IndexPage
        from web_operation_agent_yudao.native_tabs import NativeNavigationPolicy
        index = ApplicationIndex((), (IndexPage('/crm/customer', '客户', 'CRM / 客户'),))
        policy = NativeNavigationPolicy('https://fixture.test', lambda: index)
        item = ElementRef(ref='e1',role='menuitem',name='客户',tag='li',epoch=1,
            attrs={'navigation_kind':'page','navigation_path':'/crm/customer','href':'/crm/customer'})
        page = PageObservation(schema_version=1,epoch=1,url='https://fixture.test/index',title='',
            landmarks=[],elements=[item],content='',render_state='ready',page_route='/index')
        self.assertEqual(policy.decide(page,item),'')
        branch = replace(item,attrs={'navigation_kind':'branch','navigation_path':'/crm'})
        self.assertEqual(policy.decide(replace(page,elements=[branch]),branch),'')
        for changed in (replace(page,render_state='pending'),replace(page,page_route='/other'),
                        replace(page,url='https://foreign.test/index'),replace(page,elements=[])):
            self.assertTrue(policy.decide(changed,item))
        for path in ('/system/user','//foreign.test/a','/crm/../system/user','/crm/customer?delete=1',
                     '/crm/%63ustomer','/'):
            changed = replace(item,attrs={**item.attrs,'navigation_path':path,'href':path})
            self.assertTrue(policy.decide(replace(page,elements=[changed]),changed))
        self.assertTrue(policy.decide(page, replace(item,epoch=2)))
        for changed in (replace(item,role='button'),
            replace(item,attrs={**item.attrs,'form_scope':'dialog:save'}),
            replace(item,attrs={**item.attrs,'disabled':'true'}),
            replace(item,attrs={**item.attrs,'href':'/other'})):
            self.assertTrue(policy.decide(replace(page,elements=[changed]),changed))

    def test_default_configuration_requires_a_reviewed_write_fence(self):
        from web_operation_agent_yudao.native_server import YudaoHostConfig
        from pathlib import Path
        with self.assertRaises(ValueError):
            YudaoHostConfig('http://fixture.test','http://fixture.test',Path('/tmp/unused'),native_navigation=True)


if __name__ == "__main__": unittest.main()
