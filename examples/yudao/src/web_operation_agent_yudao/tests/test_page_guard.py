"""No models, tokens or production accounts; verify native API admission and fencing."""
import threading
import types
import unittest
import tempfile
from pathlib import Path

from web_operation_agent_yudao.host.page_guard import GuardRefused, NativePageGuard


A, B, OWNER, OTHER = "a" * 64, "b" * 64, "c" * 64, "d" * 64
READ = "/admin-api/crm/customer/get"


class NativePageGuardTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.guard = NativePageGuard(frozenset({READ}), clock=lambda: self.now)

    def test_window_only_admits_exact_reviewed_gets(self):
        self.guard.begin(A, OWNER)
        with self.guard.request(A, "GET", READ + "?id=1"):
            pass
        for method, path in (("POST", READ), ("GET", "/admin-api/crm/customer/delete"),
                             ("GET", "https://upstream.test" + READ),
                             ("GET", READ + "/"), ("GET", "/admin-api/crm/customer/%67et")):
            with self.subTest(method=method, path=path), self.assertRaises(GuardRefused):
                with self.guard.request(A, method, path):
                    self.fail("a refused request was admitted")

    def test_actor_isolation_and_multiple_browser_windows(self):
        self.guard.begin(A, OWNER)
        self.guard.begin(A, OTHER)
        self.guard.end(A, OWNER)
        self.assertFalse(self.guard.active(A, OWNER))
        self.assertTrue(self.guard.active(A, OTHER))
        with self.guard.request(B, "POST", "/admin-api/crm/customer/create"):
            pass
        with self.assertRaises(GuardRefused):
            with self.guard.request(A, "POST", "/admin-api/crm/customer/create"):
                pass
        self.guard.end(A, OTHER)
        with self.guard.request(A, "POST", "/admin-api/crm/customer/create"):
            pass

    def test_begin_waits_for_an_earlier_request_and_blocks_new_writes_while_waiting(self):
        entered, release, opening, finished = (threading.Event() for _ in range(4))
        errors = []
        def earlier():
            with self.guard.request(A, "POST", "/admin-api/crm/customer/create"):
                entered.set()
                release.wait(2)
        def begin():
            opening.set()
            try:
                self.guard.begin(A, OWNER, wait_s=2)
            except Exception as exc:
                errors.append(exc)
            finished.set()
        prior = threading.Thread(target=earlier)
        prior.start(); self.assertTrue(entered.wait(2))
        waiter = threading.Thread(target=begin)
        waiter.start(); self.assertTrue(opening.wait(2))
        # Wait for the actual condition waiter, not merely the thread's intention.
        with self.guard._condition:
            self.guard._condition.wait_for(lambda: self.guard._actors[A].opening > 0, timeout=2)
        self.assertFalse(finished.is_set())
        with self.assertRaises(GuardRefused):
            with self.guard.request(A, "POST", "/admin-api/crm/customer/update"):
                pass
        release.set(); prior.join(2); waiter.join(2)
        self.assertFalse(prior.is_alive()); self.assertFalse(waiter.is_alive())
        self.assertFalse(errors)
        self.assertTrue(self.guard.active(A, OWNER))

    def test_a_pending_or_uncertain_request_cannot_open_a_window(self):
        with self.guard.request(A, "POST", "/admin-api/crm/customer/create"):
            with self.assertRaises(GuardRefused) as pending:
                self.guard.begin(A, OWNER, wait_s=0)
            self.assertEqual(pending.exception.code, "prior_request_pending")
            self.assertFalse(self.guard.active(A, OWNER))
        with self.assertRaises(TimeoutError):
            with self.guard.request(A, "POST", "/admin-api/crm/customer/create"):
                raise TimeoutError("lost upstream result")
        with self.assertRaises(GuardRefused) as uncertain:
            self.guard.begin(A, OWNER)
        self.assertEqual(uncertain.exception.code, "prior_request_uncertain")
        with self.assertRaises(GuardRefused):
            self.guard.end(A, OWNER)
        with self.assertRaises(GuardRefused):
            with self.guard.request(A, "GET", READ):
                pass

    def test_expiry_and_refresh_are_bounded(self):
        self.guard.begin(A, OWNER, ttl_s=2)
        self.now = 1
        self.guard.begin(A, OWNER, ttl_s=2)
        self.now = 2
        self.assertTrue(self.guard.active(A, OWNER))
        self.now = 3
        self.assertFalse(self.guard.active(A, OWNER))
        for ttl in (True, 0, 121, float("nan")):
            with self.assertRaises(GuardRefused):
                self.guard.begin(A, OWNER, ttl_s=ttl)

    def test_capacity_never_discards_active_or_uncertain_actors(self):
        guard = NativePageGuard(frozenset({READ}), actor_limit=1)
        guard.begin(A, OWNER)
        with self.assertRaises(GuardRefused):
            guard.begin(B, OWNER)
        guard.end(A, OWNER)
        guard.begin(B, OWNER)
        self.assertTrue(guard.active(B, OWNER))

    def test_uncertain_admission_survives_a_proxy_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "guard.sqlite3"
            first = NativePageGuard(frozenset({READ}), journal=journal)
            with first.request(A, "POST", "/admin-api/crm/customer/update"):
                pass
            restored = NativePageGuard(frozenset({READ}), journal=journal)
            restored.begin(A, OWNER)
            restored.end(A, OWNER)
            with self.assertRaises(TimeoutError):
                with restored.request(A, "POST", "/admin-api/crm/customer/update"):
                    raise TimeoutError("unknown upstream completion")
            restarted = NativePageGuard(frozenset({READ}), journal=journal)
            with self.assertRaises(GuardRefused) as lost:
                restarted.begin(A, OWNER)
            self.assertEqual(lost.exception.code, "prior_request_uncertain")

    def test_a_window_open_at_a_restart_is_held_for_the_longest_a_window_lasts(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "guard.sqlite3"
            first = NativePageGuard(frozenset({READ}), journal=journal)
            first.begin(A, OWNER)
            now = [0]
            restarted = NativePageGuard(frozenset({READ}), journal=journal, clock=lambda: now[0])
            with self.assertRaises(GuardRefused) as held:
                with restarted.request(A, "POST", "/admin-api/crm/customer/update"):
                    self.fail("restart must not release an acknowledged window")
            self.assertEqual(held.exception.code, "page_write_blocked")
            self.assertTrue(restarted.active(A, OWNER))
            # The same browser, or another of the same person, can go on working.
            restarted.begin(A, OTHER, ttl_s=5)
            now[0] = 6
            self.assertTrue(restarted.active(A, OWNER))
            now[0] = 121
            self.assertFalse(restarted.protected(A))
            with restarted.request(A, "POST", "/admin-api/crm/customer/update"):
                pass
            # Ended by its own clock, it is not found again by the next restart.
            again = NativePageGuard(frozenset({READ}), journal=journal)
            self.assertFalse(again.protected(A))

    def test_a_window_nobody_closed_before_a_restart_does_not_lock_its_actor_out(self):
        """It ran out long before the restart, and nothing touched the actor in between."""
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "guard.sqlite3"
            now = [0]
            first = NativePageGuard(frozenset({READ}), journal=journal, clock=lambda: now[0])
            first.begin(A, OWNER)
            now[0] = 600
            restarted = NativePageGuard(frozenset({READ}), journal=journal)
            restarted.begin(A, OWNER)
            restarted.end(A, OWNER)

    def test_a_read_that_was_never_answered_leaves_the_actor_certain(self):
        with self.assertRaises(TimeoutError):
            with self.guard.request(A, "GET", READ):
                raise TimeoutError("the reply was lost")
        self.guard.begin(A, OWNER)
        self.assertTrue(self.guard.active(A, OWNER))

    def test_released_or_expired_window_does_not_poison_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "guard.sqlite3"
            now = [0]
            guard = NativePageGuard(frozenset({READ}), journal=journal, clock=lambda: now[0])
            guard.begin(A, OWNER)
            guard.begin(B, OWNER)
            guard.end(A, OWNER)
            now[0] = 121
            self.assertFalse(guard.active(B, OWNER))
            restarted = NativePageGuard(frozenset({READ}), journal=journal)
            restarted.begin(A, OWNER)
            restarted.begin(B, OWNER)


class MethodFenceTests(unittest.TestCase):
    WRITING = frozenset({"/admin-api/pms/pm/project-group/list",
                         "/admin-api/infra/file/{configId}/get/**"})

    def setUp(self):
        self.guard = NativePageGuard(frozenset(), writing_gets=self.WRITING)

    def test_window_admits_unreviewed_plain_gets_and_refuses_every_other_method(self):
        self.guard.begin(A, OWNER)
        for path in (READ + "?id=1", "/admin-api/hrm/employee/page", "/admin-api/system/dict-data/simple-list"):
            with self.subTest(path=path), self.guard.request(A, "GET", path):
                pass
        for method in ("POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
            with self.subTest(method=method), self.assertRaises(GuardRefused):
                with self.guard.request(A, method, READ):
                    self.fail("a refused request was admitted")

    def test_scanned_writing_gets_and_alternate_spellings_stay_refused(self):
        self.guard.begin(A, OWNER)
        for path in ("/admin-api/pms/pm/project-group/list", "/admin-api/PMS/pm/project-group/list",
                     "/admin-api/pms/pm/project-group/list/", "/admin-api/pms/pm//project-group/list",
                     "/admin-api/pms/pm/project-group/list;v=1", "/admin-api/pms/pm/../pm/project-group/list",
                     "/admin-api/pms/pm/project-group/%6cist", "/admin-api/infra/file/4/get",
                     "/admin-api/infra/file/4/get/a", "/admin-api/infra/file/4/get/a/b.png",
                     "https://upstream.test/admin-api/crm/customer/get", "/app-api/trade/order/page",
                     "/admin-api", "/admin-api/"):
            with self.subTest(path=path), self.assertRaises(GuardRefused):
                with self.guard.request(A, "GET", path):
                    self.fail("a refused request was admitted")
        with self.guard.request(A, "GET", "/admin-api/pms/pm/project/page"):
            pass

    def test_refusals_inside_a_window_are_bounded_evidence_without_queries(self):
        with self.guard.request(A, "POST", "/admin-api/crm/customer/create"):
            pass
        self.assertEqual(self.guard.refused_since(A, 0), ())
        self.guard.begin(A, OWNER)
        mark = self.guard.refusal_mark(A)
        for method, path in (("PUT", "/admin-api/crm/customer/update?id=7"),
                             ("GET", "/admin-api/pms/pm/project-group/list?token=secret")):
            with self.assertRaises(GuardRefused):
                with self.guard.request(A, method, path):
                    pass
        self.assertEqual(self.guard.refused_since(A, mark),
                         (("PUT", "/admin-api/crm/customer/update"),
                          ("GET", "/admin-api/pms/pm/project-group/list")))
        self.assertEqual(self.guard.refused_since(A, self.guard.refusal_mark(A)), ())
        self.assertEqual(self.guard.refused_since(B, 0), ())
        for _ in range(70):
            with self.assertRaises(GuardRefused):
                with self.guard.request(A, "DELETE", "/admin-api/crm/customer/delete"):
                    pass
        with self.assertRaises(GuardRefused):
            self.guard.refused_since(A, mark)
        with self.assertRaises(GuardRefused):
            self.guard.refused_since(A, 10_000)

    def test_fence_and_reviewed_reads_cannot_be_combined_and_templates_are_exact(self):
        with self.assertRaises(ValueError):
            NativePageGuard(frozenset({READ}), writing_gets=frozenset())
        for bad in ("/crm/customer/get", "/admin-api/**/get", "/admin-api/a b", "/admin-api/x/{id", 7):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                NativePageGuard(frozenset(), writing_gets=frozenset({bad}))
        reviewed = NativePageGuard(frozenset({READ}))
        reviewed.begin(A, OWNER)
        with self.assertRaises(GuardRefused):
            with reviewed.request(A, "GET", "/admin-api/hrm/employee/page"):
                self.fail("the reviewed window must not widen")


class WindowIsolationTests(unittest.TestCase):
    """Two open pages of one actor keep their own evidence: reads, refusals and the
    page-load pass are each the window's own, never shared or taken."""

    def setUp(self):
        self.now = 0
        self.guard = NativePageGuard(frozenset(), clock=lambda: self.now,
                                     writing_gets=frozenset({"/admin-api/project/project/{id}"}))
        self.guard.begin(A, OWNER)
        self.guard.begin(A, OTHER)

    def test_each_window_s_reads_and_refusals_are_its_own(self):
        with self.guard.request(A, "GET", READ + "?id=1", window=OWNER):
            pass
        with self.guard.request(A, "GET", READ + "?id=2", window=OTHER):
            pass
        with self.assertRaises(GuardRefused):
            with self.guard.request(A, "POST", "/admin-api/crm/customer/create", window=OWNER):
                pass
        self.assertEqual(self.guard.recent_reads(A, window=OWNER), ((READ, "id=1"),))
        self.assertEqual(self.guard.recent_reads(A, window=OTHER), ((READ, "id=2"),))
        self.assertEqual(self.guard.refused_since(A, 0, window=OTHER), ())
        self.assertEqual(self.guard.refused_since(A, 0, window=OWNER),
                         (("POST", "/admin-api/crm/customer/create"),))
        # Actor-wide queries still see everything the actor's windows did.
        self.assertEqual(len(self.guard.recent_reads(A)), 2)
        self.assertEqual(len(self.guard.refused_since(A, 0)), 1)

    def test_a_page_load_pass_belongs_to_the_window_it_was_armed_for(self):
        self.guard.arm_page_load(A, window=OWNER)
        with self.assertRaises(GuardRefused):
            with self.guard.request(A, "GET", "/admin-api/project/project/17", window=OTHER):
                pass
        with self.guard.request(A, "GET", "/admin-api/project/project/17", window=OWNER):
            pass
        self.assertEqual([path for _n, path in self.guard.page_loads_since(A, 0, window=OWNER)],
                         ["/admin-api/project/project/17"])
        self.assertEqual(self.guard.page_loads_since(A, 0, window=OTHER), ())

    def test_ending_one_window_clears_only_its_page_load_arm(self):
        self.guard.arm_page_load(A, window=OWNER)
        self.guard.arm_page_load(A, window=OTHER)
        self.guard.end(A, OWNER)
        # The ended window's pass is gone: a request in its name is fenced again.
        with self.assertRaises(GuardRefused):
            with self.guard.request(A, "GET", "/admin-api/project/project/17", window=OWNER):
                pass
        # The surviving window kept its own arm.
        with self.guard.request(A, "GET", "/admin-api/project/project/17", window=OTHER):
            pass

    def test_unnamed_requests_merge_into_every_window_s_view_but_not_into_each_other(self):
        with self.guard.request(A, "GET", READ + "?id=0"):
            pass
        with self.guard.request(A, "GET", READ + "?id=1", window=OWNER):
            pass
        self.assertEqual(self.guard.recent_reads(A, window=OWNER),
                         ((READ, "id=0"), (READ, "id=1")))
        self.assertEqual(self.guard.recent_reads(A, window=OTHER), ((READ, "id=0"),))

    def test_a_page_instance_name_with_a_dot_is_a_real_window(self):
        # The browser's page instance id -- "<tab id>.<conversation epoch>" -- is the
        # name the runtime actually sends; refusing it would close every guarded page
        # action in production.
        instance = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3.2"
        self.guard.begin(A, instance)
        self.assertTrue(self.guard.active(A, instance))
        with self.guard.request(A, "GET", READ + "?id=1", window=instance):
            pass
        self.assertEqual(self.guard.recent_reads(A, window=instance), ((READ, "id=1"),))
        self.guard.end(A, instance)
        self.assertFalse(self.guard.active(A, instance))

    def test_a_request_naming_a_window_that_is_not_open_is_unattributed_evidence(self):
        # `X-WOA-Window` is self-asserted: a well-formed name no page opened must not get
        # a bucket of its own that no window query would ever merge.
        with self.assertRaises(GuardRefused):
            with self.guard.request(A, "POST", "/admin-api/crm/customer/create",
                                    window="ghost-window"):
                pass
        for window in (OWNER, OTHER):
            with self.subTest(window=window):
                self.assertEqual(self.guard.refused_since(A, 0, window=window),
                                 (("POST", "/admin-api/crm/customer/create"),))


class _StubPermit:
    """The least a permit does for the guard: decide, carry a ttl, be settled."""
    def __init__(self):
        self.ttl_s = 30
        self.settled = []

    def admit(self, method, path, _query, _body):
        return types.SimpleNamespace(
            allowed=(method == "PUT" and path == "/admin-api/crm/customer/update"),
            sequence=1)

    def settle(self, sequence, status, _response):
        self.settled.append((sequence, status))


class PermitWindowTests(unittest.TestCase):
    """An approved click's permit belongs to the window the approval was given in."""

    def setUp(self):
        self.guard = NativePageGuard(frozenset({READ}))
        self.permit = _StubPermit()

    def test_a_sibling_page_cannot_claim_the_pending_permit(self):
        self.guard.arm_permit(A, self.permit, owner=OWNER)
        with self.assertRaises(GuardRefused) as refused:
            self.guard.begin(A, OTHER)
        self.assertEqual(refused.exception.code, "other_window_open")
        self.guard.begin(A, OWNER)
        self.assertTrue(self.guard.permit_active(A))

    def test_while_the_permit_decides_other_windows_writes_are_refused(self):
        self.guard.begin(A, OTHER)
        self.guard.arm_permit(A, self.permit, owner=OWNER)
        # The permit's exclusivity still holds: its owner cannot claim while a sibling is open.
        with self.assertRaises(GuardRefused):
            self.guard.begin(A, OWNER)
        self.guard.end(A, OTHER)
        self.guard.begin(A, OWNER)
        self.assertTrue(self.guard.permit_active(A))
        # A sibling may open while the permit decides, but its marked writes are refused.
        self.guard.begin(A, OTHER)
        with self.assertRaises(GuardRefused):
            with self.guard.request(A, "PUT", "/admin-api/crm/customer/update", window=OTHER):
                pass
        with self.guard.request(A, "PUT", "/admin-api/crm/customer/update", window=OWNER) as ticket:
            ticket.status = 200
        self.assertEqual(self.permit.settled, [(1, 200)])

    def test_only_the_permit_s_own_window_releases_it(self):
        self.guard.arm_permit(A, self.permit, owner=OWNER)
        self.guard.begin(A, OWNER)
        self.guard.begin(A, OTHER)
        self.guard.end(A, OTHER)
        self.assertTrue(self.guard.permit_active(A))
        self.guard.end(A, OWNER)
        self.assertFalse(self.guard.permit_active(A))
        self.assertIs(self.guard.finished_permit(A), self.permit)

    def test_disarming_the_permit_frees_the_pending_owner(self):
        self.guard.arm_permit(A, self.permit, owner=OWNER)
        self.assertIsNotNone(self.guard.disarm_permit(A))
        self.guard.begin(A, OTHER)
        self.assertTrue(self.guard.active(A, OTHER))


if __name__ == "__main__":
    unittest.main()
