"""Transport conformance: zero model, real browser, a fixture page.

These tests prove that a command the server sends and a receipt the browser returns lose
nothing on the way: no action name, argument, navigation lifecycle, or failure mode. They
also prevent an argument key from overwriting the action selected by the command envelope.

Everything here runs against a page served from a temporary directory, so it needs neither
OFBiz nor a model.
"""
import http.server
import socket
import tempfile
import threading
import unittest
from functools import partial
from pathlib import Path

from web_operation_agent.browser import ThinBrowserClient, TransportError
from web_operation_agent.browser_worker import _redact_session_handoffs
from web_operation_agent.schemas import BROWSER_ACTIONS, RECEIPT_ERRORS

PAGE_ONE = """<!doctype html><title>Fixture One</title>
<h1>Fixture One</h1>
<div class="eventMessage">nothing has happened yet</div>
<a id="go" href="/two.html">Second page</a>
<a id="away" href="http://example.invalid/x">Off site</a>
<a id="session" href="/two.html?externalLoginKey=raw-session-secret">Session link</a>
<a id="encoded" href="/one.html%3FexternalLoginKey%3Dencoded-session-secret%26amp%3BsortField%3Ddescription">Encoded session link</a>
<a id="misspelled" href="/two.html?externaLoginKey=misspelled-session-secret">Misspelled session link</a>
<a id="short" href="/one.html%253FexternLoginKey%253Dshort-session-secret%2526x%253D1">Short session link</a>
<form id="f" action="/two.html" method="get">
  <label for="who">Who</label><input id="who" name="who" type="text" value="start">
  <input id="pw" name="PASSWORD" type="password" value="hunter2">
  <select id="colour" name="colour"><option value="r">Red</option><option value="g">Green</option></select>
  <input id="send" type="submit" value="Send">
</form>
"""
PAGE_TWO = """<!doctype html><title>Fixture Two</title><h1>Fixture Two</h1>
<a id="back" href="/one.html">First page</a>
"""


class _Server:
    def __init__(self, root: Path):
        handler = partial(_Quiet, directory=str(root))
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", _free_port()), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def stop(self):
        self.httpd.shutdown()


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TransportConformance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        (root / "one.html").write_text(PAGE_ONE)
        (root / "two.html").write_text(PAGE_TWO)
        cls.server = _Server(root)
        cls.client = ThinBrowserClient(cls.server.url)
        cls.client.start(entry_path="/one.html")

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.server.stop()
        cls.tmp.cleanup()

    def setUp(self):
        self.obs = self.client.execute("goto", {"url": "/one.html"},
                                       expect_navigation=True).observation

    def _ref(self, **q):
        for e in self.obs.elements:
            if all(str(e.attrs.get(k, "")) == v if k != "role" else e.role == v
                   for k, v in q.items()):
                return e.ref
        self.fail(f"fixture page has no element matching {q}: "
                  f"{[(e.ref, e.role, e.attrs) for e in self.obs.elements]}")

    # -- the core property -------------------------------------------------------------
    def test_the_action_name_and_arguments_come_back_verbatim(self):
        ref = self._ref(id="who")
        args = {"ref": ref, "value": "changed", "epoch": self.obs.epoch,
                "click": "a decoy key that is not the action"}
        r = self.client.execute("fill", args)
        self.assertTrue(r.ok, r.error_detail)
        self.assertEqual(r.action, "fill")
        self.assertEqual(r.args_echo["value"], "changed")
        self.assertEqual(r.args_echo["click"], "a decoy key that is not the action")
        self.assertEqual(r.args_echo["ref"], ref)

    def test_every_exposed_action_round_trips(self):
        """No action in the published surface may be silently unimplemented."""
        seen = set()
        for action, args, nav in (
                ("observe", {}, False),
                ("goto", {"url": "/two.html"}, True),
                ("back", {}, True),
                ("wait_for", {"ms": 50}, False),
                ("click", {"ref": self._ref(id="go")}, True),
        ):
            r = self.client.execute(action, dict(args, epoch=self.obs.epoch) if "ref" in args
                                    else args, expect_navigation=nav)
            self.assertEqual(r.action, action)
            self.assertTrue(r.ok, f"{action} failed: {r.error_code} {r.error_detail}")
            self.assertIsNotNone(r.observation, f"{action} returned no observation")
            seen.add(action)
            self.obs = r.observation
            if action in ("goto", "click"):
                self.setUp()
        for action, args in (("fill", {"ref": self._ref(id="who"), "value": "x"}),
                             ("select", {"ref": self._ref(id="colour"), "value": "g"}),
                             ("submit", {"ref": self._ref(id="f")})):
            r = self.client.execute(action, dict(args, epoch=self.obs.epoch),
                                    expect_navigation=(action == "submit"))
            self.assertEqual(r.action, action)
            self.assertTrue(r.ok, f"{action} failed: {r.error_code} {r.error_detail}")
            seen.add(action)
            self.setUp()
        self.assertEqual(seen, BROWSER_ACTIONS,
                         f"actions never exercised: {sorted(BROWSER_ACTIONS - seen)}")

    def test_command_ids_pair_up(self):
        r = self.client.execute("observe", {})
        self.assertTrue(r.command_id.startswith("c"))
        r2 = self.client.execute("observe", {})
        self.assertNotEqual(r.command_id, r2.command_id)

    def test_session_handoffs_never_cross_the_browser_boundary(self):
        by_id = {e.attrs.get("id"): e for e in self.obs.elements}
        self.assertEqual(
            by_id["session"].attrs["href"],
            "/two.html?externalLoginKey=[redacted]",
        )
        self.assertEqual(
            by_id["encoded"].attrs["href"],
            "/one.html%3FexternalLoginKey%3D[redacted]%26amp%3BsortField%3Ddescription",
        )
        self.assertEqual(
            by_id["misspelled"].attrs["href"],
            "/two.html?externaLoginKey=[redacted]",
        )
        self.assertEqual(
            by_id["short"].attrs["href"],
            "/one.html%253FexternLoginKey%253D[redacted]%2526x%253D1",
        )

        receipt = self.client.execute(
            "goto", {"url": "/two.html?externalLoginKey=command-session-secret"},
            expect_navigation=True,
        )
        wire = str(receipt.to_json())
        self.assertNotIn("command-session-secret", wire)
        self.assertIn("externalLoginKey=[redacted]", wire)

    def test_session_redaction_preserves_business_url_suffixes(self):
        cases = {
            "/p;jsessionid=raw-secret?businessId=1":
                "/p;jsessionid=[redacted]?businessId=1",
            "/p;jsessionid=raw-secret/child#tab":
                "/p;jsessionid=[redacted]/child#tab",
            "/p%3Bjsessionid%3Dencoded-secret%3FbusinessId%3D1":
                "/p%3Bjsessionid%3D[redacted]%3FbusinessId%3D1",
            "/p%253Bjsessionid%253Dtwice-secret%253FbusinessId%253D1":
                "/p%253Bjsessionid%253D[redacted]%253FbusinessId%253D1",
            "/p?externaLoginKey=misspelled-secret&businessId=1":
                "/p?externaLoginKey=[redacted]&businessId=1",
            "/p%3FexternLoginKey%3Dshort-secret%26businessId%3D1":
                "/p%3FexternLoginKey%3D[redacted]%26businessId%3D1",
        }
        for value, expected in cases.items():
            self.assertEqual(_redact_session_handoffs(value), expected)

    def test_session_redaction_covers_structured_aliases_without_fuzzy_matches(self):
        value = {
            "externaLoginKey": "structured-secret",
            "control": {"name": "ExTeRnLoGiNkEy", "value": "control-secret"},
            "externalLoginKeyLabel": "business-value",
            "near": "/x?externalLoginKeyLabel=business-value",
        }
        expected = {
            "externaLoginKey": "[redacted]",
            "control": {"name": "ExTeRnLoGiNkEy", "value": "[redacted]"},
            "externalLoginKeyLabel": "business-value",
            "near": "/x?externalLoginKeyLabel=business-value",
        }
        cleaned = _redact_session_handoffs(value)
        self.assertEqual(cleaned, expected)
        self.assertEqual(_redact_session_handoffs(cleaned), expected)

    # -- navigation lifecycle ----------------------------------------------------------
    def test_navigation_is_reported_not_guessed(self):
        r = self.client.execute("click", {"ref": self._ref(id="go"), "epoch": self.obs.epoch},
                                expect_navigation=True)
        self.assertTrue(r.navigated)
        self.assertIn("one.html", r.url_before)
        self.assertIn("two.html", r.url_after)
        self.assertEqual(r.observation.title, "Fixture Two")

    def test_an_action_that_does_not_navigate_says_so(self):
        r = self.client.execute("fill", {"ref": self._ref(id="who"), "value": "y",
                                         "epoch": self.obs.epoch})
        self.assertFalse(r.navigated)
        self.assertEqual(r.url_before, r.url_after)

    def test_expecting_a_navigation_that_does_not_happen_is_not_an_error(self):
        r = self.client.execute("click", {"ref": self._ref(id="who"), "epoch": self.obs.epoch},
                                expect_navigation=True)
        self.assertTrue(r.ok, r.error_detail)
        self.assertFalse(r.navigated)

    # -- references --------------------------------------------------------------------
    def test_a_reference_from_an_older_epoch_is_refused(self):
        stale = self._ref(id="who")
        stale_epoch = self.obs.epoch
        self.client.execute("observe", {})
        r = self.client.execute("fill", {"ref": stale, "value": "z", "epoch": stale_epoch})
        self.assertFalse(r.ok)
        self.assertEqual(r.error_code, "stale_ref")

    def test_an_unknown_reference_is_refused(self):
        r = self.client.execute("click", {"ref": "e9999", "epoch": self.obs.epoch})
        self.assertFalse(r.ok)
        self.assertEqual(r.error_code, "unknown_ref")

    def test_epoch_advances_on_every_observation(self):
        a = self.client.execute("observe", {}).observation.epoch
        b = self.client.execute("observe", {}).observation.epoch
        self.assertEqual(b, a + 1)

    # -- refusals ----------------------------------------------------------------------
    def test_leaving_the_origin_is_refused(self):
        r = self.client.execute("goto", {"url": "http://example.invalid/x"})
        self.assertFalse(r.ok)
        self.assertEqual(r.error_code, "origin_refused")

    def test_missing_arguments_are_refused(self):
        for action, args in (("fill", {"ref": self._ref(id="who"), "epoch": self.obs.epoch}),
                             ("goto", {}), ("click", {})):
            r = self.client.execute(action, args)
            self.assertFalse(r.ok, f"{action} with {args} should have been refused")
            self.assertEqual(r.error_code, "bad_arguments")

    def test_an_action_outside_the_surface_cannot_even_be_sent(self):
        from web_operation_agent.schemas import SchemaError
        with self.assertRaises(SchemaError):
            self.client.execute("evaluate_javascript", {"script": "1"})

    def test_every_error_code_it_can_produce_is_in_the_published_set(self):
        codes = {self.client.execute("click", {"ref": "e9999", "epoch": self.obs.epoch}).error_code,
                 self.client.execute("goto", {}).error_code,
                 self.client.execute("goto", {"url": "http://example.invalid/"}).error_code,
                 self.client.execute("observe", {}).error_code}
        self.assertTrue(codes <= RECEIPT_ERRORS, f"undeclared error codes: {codes - RECEIPT_ERRORS}")

    # -- masking -----------------------------------------------------------------------
    def test_a_password_field_never_leaves_the_browser(self):
        blob = str(self.obs.to_json())
        self.assertNotIn("hunter2", blob, "a password value reached the server")
        pw = [e for e in self.obs.elements if e.role == "password"]
        self.assertTrue(pw, "the fixture has a password field; the observation must show it exists")
        self.assertNotIn("hunter2", str(pw[0].to_json()))

    def test_a_secret_argument_is_masked_in_the_receipt(self):
        r = self.client.execute("fill", {"ref": self._ref(id="pw"), "value": "s3cret",
                                         "password": "s3cret", "epoch": self.obs.epoch})
        self.assertEqual(r.args_echo["password"], "***")


class TransportFailures(unittest.TestCase):
    def test_a_dead_browser_is_a_transport_error_not_a_silent_stall(self):
        client = ThinBrowserClient("http://127.0.0.1:1")
        with self.assertRaises(TransportError):
            client.start(entry_path="/")
        client.close()


if __name__ == "__main__":
    unittest.main()
