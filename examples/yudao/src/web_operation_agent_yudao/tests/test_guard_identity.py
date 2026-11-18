import unittest
from unittest.mock import patch

from web_operation_agent_yudao.host.guard_identity import GuardIdentity
from web_operation_agent_yudao.host.page_guard import GuardRefused


class GuardIdentityTests(unittest.TestCase):
    def test_cache_binds_tenant_token_and_verified_actor_without_retaining_credentials(self):
        now = [0.0]
        identity = GuardIdentity("http://fixture.test", clock=lambda: now[0], limit=2)
        with patch("web_operation_agent_yudao.host.guard_identity.YudaoClient.for_token") as client:
            client.return_value.get.return_value = {"user": {"id": 7}}
            first = identity("synthetic-private-token", "1")
            self.assertEqual(identity("synthetic-private-token", "1"), first)
            self.assertEqual(client.call_count, 1)
            self.assertNotEqual(identity("synthetic-private-token", "2"), first)
            self.assertEqual(identity("synthetic-new-token", "1"), first)
            self.assertEqual(len(identity._cache), 2)
            now[0] = 61
            identity("synthetic-new-token", "1")
            self.assertEqual(client.call_count, 4)
        self.assertNotIn("synthetic-private-token", str(identity._cache))
        self.assertNotIn("synthetic-new-token", str(identity._cache))

    def test_identity_failures_are_never_cached(self):
        identity = GuardIdentity("http://fixture.test")
        with patch("web_operation_agent_yudao.host.guard_identity.YudaoClient.for_token") as client:
            client.return_value.get.side_effect = RuntimeError("synthetic unavailable")
            for _ in range(2):
                with self.assertRaises(GuardRefused): identity("synthetic", "1")
            self.assertEqual(client.call_count, 2)
            self.assertFalse(identity._cache)


if __name__ == "__main__": unittest.main()
