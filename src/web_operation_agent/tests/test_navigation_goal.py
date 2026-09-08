import unittest

from web_operation_agent.navigation_goal import resolve_visible_navigation_path
from web_operation_agent.schemas import ElementRef, PageObservation


def observation(*links):
    return PageObservation(
        schema_version=1, epoch=1, url="http://app.test/sfa/control/main", title="SFA",
        content="SFA", landmarks=[],
        elements=[ElementRef(
            ref=f"e{i}", role="link", name=name, tag="a", epoch=1,
            attrs={"href": href}) for i, (name, href) in enumerate(links)],
    )


class VisibleNavigationGoalTests(unittest.TestCase):
    def test_binds_a_named_top_level_application(self):
        page = observation(("SFA", "/sfa/control/main"),
                           ("Party", "/partymgr/control/main"))
        self.assertEqual(resolve_visible_navigation_path("跳转 party 页面", page),
                         "/partymgr/control/main")

    def test_create_intent_prefers_the_visible_new_entity_link(self):
        page = observation(("Accounts", "/sfa/control/FindAccounts"),
                           ("New Accounts", "/sfa/control/NewAccount"))
        self.assertEqual(resolve_visible_navigation_path("创建一个 account", page),
                         "/sfa/control/NewAccount")

    def test_refuses_an_ambiguous_or_cross_origin_target(self):
        page = observation(("Accounts", "/sfa/control/FindAccounts"),
                           ("Accounts", "/sfa/control/MyAccounts"),
                           ("WebSites", "https://other.test/content/control/FindWebSite"))
        self.assertIsNone(resolve_visible_navigation_path("打开 accounts", page))
        self.assertIsNone(resolve_visible_navigation_path("打开 websites", page))
