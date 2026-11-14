"""The page the assistant opens may load itself, even through a scan-listed GET; nothing else may."""
import unittest

from web_operation_agent_yudao.page_guard import GuardRefused, NativePageGuard

A, OWNER = "a" * 64, "c" * 64
LISTED = "/admin-api/project/project/{id}"          # a GET the release scan says may write
LOAD = "/admin-api/project/project/17?x=1"
OTHER_WRITING_GET = "/admin-api/project/project/17"


class PageLoadTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.guard = NativePageGuard(frozenset(), clock=lambda: self.now, writing_gets=frozenset({LISTED}))
        self.guard.begin(A, OWNER)

    def get(self, target, method="GET"):
        with self.guard.request(A, method, target):
            pass

    def test_a_scan_listed_get_is_refused_without_a_page_opening(self):
        with self.assertRaises(GuardRefused):
            self.get(LOAD)
        self.assertEqual(self.guard.page_loads_since(A), ())

    def test_it_passes_while_the_page_opens_and_is_kept_in_the_private_record(self):
        mark = self.guard.page_load_mark(A)
        self.guard.arm_page_load(A)
        self.get(LOAD)
        self.assertEqual([path for _n, path in self.guard.page_loads_since(A, mark)], ["/admin-api/project/project/17"])

    def test_the_window_for_it_is_short(self):
        self.guard.arm_page_load(A, 5)
        self.now = 6.0
        with self.assertRaises(GuardRefused):
            self.get(LOAD)

    def test_writes_stay_refused_while_the_page_loads(self):
        self.guard.arm_page_load(A)
        for method, target in (("POST", "/admin-api/project/project/create"), ("PUT", "/admin-api/project/project/17"),
                               ("GET", "/admin-api/project/project/%31%37")):
            with self.subTest(method=method), self.assertRaises(GuardRefused):
                self.get(target, method)

    def test_an_ordinary_get_is_not_recorded_as_a_pass(self):
        mark = self.guard.page_load_mark(A)
        self.guard.arm_page_load(A)
        self.get("/admin-api/crm/customer/get?id=1")                 # admitted by the method fence as ever
        self.assertEqual(self.guard.page_loads_since(A, mark), ())

    def test_no_open_window_no_pass(self):
        self.guard.end(A, OWNER)
        self.guard.arm_page_load(A)
        self.get(LOAD)                                               # no window: nothing to pass through
        self.assertEqual(self.guard.page_loads_since(A), ())

    def test_an_unanswered_pass_makes_the_actor_uncertain_like_any_write(self):
        self.guard.arm_page_load(A)
        with self.assertRaises(RuntimeError):
            with self.guard.request(A, "GET", LOAD):
                raise RuntimeError("upstream lost")
        with self.assertRaises(GuardRefused) as refused:
            self.get(LOAD)
        self.assertEqual(refused.exception.code, "prior_request_uncertain")


if __name__ == "__main__":
    unittest.main()
