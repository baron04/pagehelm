"""No models, tokens or production accounts; verify native API admission and fencing."""
import threading
import unittest
import tempfile
from pathlib import Path

from web_operation_agent_yudao.page_guard import GuardRefused, NativePageGuard


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

    def test_acknowledged_window_survives_restart_until_reconciled(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "guard.sqlite3"
            first = NativePageGuard(frozenset({READ}), journal=journal)
            first.begin(A, OWNER)
            restarted = NativePageGuard(frozenset({READ}), journal=journal)
            with self.assertRaises(GuardRefused):
                with restarted.request(A, "POST", "/admin-api/crm/customer/update"):
                    self.fail("restart must not release an acknowledged window")
            with self.assertRaises(GuardRefused):
                restarted.end(A, OWNER)
            with self.assertRaises(GuardRefused):
                restarted.begin(A, OWNER)
            again = NativePageGuard(frozenset({READ}), journal=journal)
            with self.assertRaises(GuardRefused):
                again.begin(A, OWNER)

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


if __name__ == "__main__":
    unittest.main()
