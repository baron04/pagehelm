"""A closed browser tab does not keep the host from pruning (P7.5 CR-16).

The host keeps one entry per browser tab until the actor logs out. A tab closed without
logging out is reaped by the server after its idle timeout, but its entry stayed in the
host, so the hourly maintenance -- which prunes run ledgers only while no session is active --
never pruned them again, and `active_sessions` counted every tab ever closed.
"""
from __future__ import annotations

import os
import time
import unittest
from unittest.mock import patch

from web_operation_agent_odoo.native import OdooSessionEvent, sign_session_event
from web_operation_agent_odoo.native_server import OdooNativeConfig, OdooNativeHost
from web_operation_agent_odoo.tests import test_native


class AClosedTabIsNotAnActiveSession(unittest.TestCase):
    # The ordinary-browser host fixture, borrowed rather than inherited so its own tests
    # do not run twice.
    setUp = test_native.OrdinaryBrowserHostContract.setUp
    tearDown = test_native.OrdinaryBrowserHostContract.tearDown
    _post = staticmethod(test_native.OrdinaryBrowserHostContract._post)
    _bound_adapter = test_native.OrdinaryBrowserHostContract._bound_adapter

    def test_maintenance_prunes_old_runs_after_the_tab_is_gone(self):
        now = int(time.time() * 1000)
        config = OdooNativeConfig(
            rpc_base_url="http://127.0.0.1:8069", public_origin="http://127.0.0.1:18069",
            public_agent_url="http://127.0.0.1:18766/assistant", delegation_dir=self.root,
            session_secret_path=self.secret_path, state_dir=self.root / "state",
            gateway_port=0, bootstrap_port=0, scripted=True)
        host = OdooNativeHost(config)
        with patch.object(host, "_adapter_and_principal",
                          return_value=self._bound_adapter(now)):
            with host:
                bootstrap = OdooSessionEvent(
                    schema_version=1, event="bootstrap", database="trial", company_id="7",
                    actor_id="42", runtime_instance_id="a" * 32, issued_at=now,
                    expires_at=now + 30_000, nonce="c" * 32)
                status, _ = self._post(host.bootstrap_server.base_url + "/bootstrap",
                                       sign_session_event(bootstrap, test_native.SECRET))
                self.assertEqual(status, 200)
                old = config.state_dir / "runs" / "20260801-000000-00000000"
                old.mkdir(parents=True)
                forty_days_ago = time.time() - 40 * 24 * 3600
                os.utime(old, (forty_days_ago, forty_days_ago))

                host._maintenance_once()
                self.assertTrue(old.exists(), "a live tab keeps the run ledgers")
                self.assertEqual(host.operational_snapshot()["active_sessions"], 1)

                # The tab is closed; the server reaps its session after the idle timeout.
                (active,) = host._sessions.values()
                active.session.close("browser_inactive")
                host._maintenance_once()

                self.assertFalse(old.exists())
                self.assertEqual(host.operational_snapshot()["active_sessions"], 0)
                self.assertEqual(host._sessions, {})

                # The same tab comes back: it gets a fresh session.
                again = OdooSessionEvent(
                    schema_version=1, event="bootstrap", database="trial", company_id="7",
                    actor_id="42", runtime_instance_id="a" * 32, issued_at=now + 1,
                    expires_at=now + 30_001, nonce="e" * 32)
                status, _ = self._post(host.bootstrap_server.base_url + "/bootstrap",
                                       sign_session_event(again, test_native.SECRET))
                self.assertEqual(status, 200)
                self.assertEqual(host.operational_snapshot()["active_sessions"], 1)


if __name__ == "__main__":
    unittest.main()
