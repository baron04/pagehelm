"""The WOA runner's wall-clock arithmetic and the stage its faults are recorded at, without a
browser."""
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from evals.odoo import runner_woa
from evals.odoo.runner_woa import WoaRunnerConfig, turn_budget
from evals.odoo.tasks import RunnerInput


class TurnBudgetTests(unittest.TestCase):
    def test_a_turn_gets_the_smaller_of_its_own_limit_and_what_the_attempt_has_left(self):
        cases = {0: (150.0, "turn_wall"), 150: (150.0, "turn_wall"),
                 200: (100.0, "attempt_automatic_wall"), 300: (0.0, "attempt_automatic_wall"),
                 320: (0.0, "attempt_automatic_wall")}
        for used, expected in cases.items():
            with self.subTest(used=used):
                self.assertEqual(turn_budget(turn_timeout_s=150, attempt_wall_s=300,
                                             automatic_used_s=used), expected)


class Broken(RuntimeError):
    """A fault a browser call raised."""


class Page:
    """Just enough of a Playwright page for one settled turn; ``fail`` names the call that raises,
    and ``crash`` makes the page report a crash just before it does."""

    def __init__(self, fail=None, crash=False):
        self.fail, self.crash, self.handlers = fail, crash, {}
        self.root = mock.MagicMock()
        self.root.locator.return_value.count.return_value = 0

    def _maybe_fail(self, call):
        if call == self.fail:
            if self.crash:
                self.handlers["crash"](self)
            raise Broken(call)

    def on(self, event, handler):
        self.handlers[event] = handler

    def locator(self, _selector):
        return self.root

    def wait_for_function(self, script, **_kwargs):
        if script == runner_woa._SETTLED_JS:
            self._maybe_fail("turn")
        handle = mock.MagicMock()
        handle.json_value.return_value = "settled"
        return handle

    def evaluate(self, script, *_args):
        if script == runner_woa._ANSWER_JS:
            return {"kind": "plain", "text": "done"}
        return "/odoo"

    def screenshot(self, **_kwargs):
        self._maybe_fail("screenshot")


@unittest.skipIf(importlib.util.find_spec("playwright") is None, "playwright is not installed")
class FaultStageTests(unittest.TestCase):
    """A fault after every turn has ended is not losing sight of the product while it ran."""

    def run_with(self, fail=None, *, crash=False, logged_out=True, messages=1):
        page = Page(fail, crash)
        context = mock.MagicMock()
        context.new_page.return_value = page
        if fail == "context_close":
            context.close.side_effect = Broken(fail)
        browser = mock.MagicMock()
        browser.new_context.return_value = context
        if fail == "browser_close":
            browser.close.side_effect = Broken(fail)
        playwright = mock.MagicMock()
        playwright.chromium.launch.return_value = browser

        read = []

        def breadcrumb(_page):
            # The view is read before each turn too; the final page is the read after the last.
            read.append(True)
            if len(read) > len(runner_input.messages):
                page._maybe_fail("final_page")
            return {"path": "/odoo", "breadcrumb": "", "title": ""}

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        config = WoaRunnerConfig(base_url="https://odoo.test", actors={},
                                 evidence_dir=Path(directory.name))
        runner_input = RunnerInput(task_id="t", actor="writer", locale="zh-CN", entry_path=None,
                                   messages=("列出客户",) * messages)

        def settle_view(*_args):
            page._maybe_fail("settle")
            return 0

        with mock.patch.object(runner_woa, "login"), \
                mock.patch.object(runner_woa, "breadcrumb", breadcrumb), \
                mock.patch.object(runner_woa, "settle_view", settle_view), \
                mock.patch.object(runner_woa, "logout", return_value=logged_out):
            return runner_woa.run(runner_input, config, approval_hook=None,
                                  checkpoint=lambda _value: None, playwright=playwright)

    def faults(self, artifact):
        return {key: (artifact[key]["type"] if isinstance(artifact[key], dict) else artifact[key])
                for key in ("browser_exit", "control_channel_error", "final_page_error",
                            "teardown_error") if artifact[key]}

    def test_a_fault_while_the_product_runs_is_the_control_channel_or_the_browser(self):
        self.assertEqual(self.faults(self.run_with("turn")), {"control_channel_error": "Broken"})
        self.assertEqual(self.faults(self.run_with("turn", crash=True)),
                         {"browser_exit": "page_crashed"})
        # A turn still to come keeps the run going, so waiting on its view is still the run.
        two_turns = self.run_with("settle", messages=2)
        self.assertEqual((self.faults(two_turns), len(two_turns["turns"])),
                         ({"control_channel_error": "Broken"}, 1))

    def test_a_fault_after_every_turn_ended_is_recorded_at_its_own_stage(self):
        clean = self.run_with()
        self.assertEqual((self.faults(clean), clean["logged_out"], len(clean["turns"])),
                         ({}, True, 1))
        cases = {"settle": {"final_page_error": "Broken"},
                 "final_page": {"final_page_error": "Broken"},
                 "screenshot": {"teardown_error": "Broken"},
                 "context_close": {"teardown_error": "Broken"},
                 "browser_close": {"teardown_error": "close_failed"}}
        for fail, expected in cases.items():
            with self.subTest(fail=fail):
                artifact = self.run_with(fail)
                self.assertEqual(self.faults(artifact), expected)
                self.assertEqual(len(artifact["turns"]), 1)
        # A page that crashes while being torn down is not a browser lost during the run.
        self.assertEqual(self.faults(self.run_with("screenshot", crash=True)),
                         {"teardown_error": "Broken"})
        # A failed logout is left for the driver to record from ``logged_out``.
        self.assertEqual(self.faults(self.run_with(logged_out=False)), {})

    def test_a_browser_that_will_not_close_after_a_run_fault_stays_the_runs_fault(self):
        artifact = {"browser_exit": None, "control_channel_error": None,
                    "final_page_error": None, "teardown_error": None}
        runner_woa.record_fault(artifact, "run", "Broken")
        runner_woa.record_fault(artifact, "run", "close_failed")
        self.assertEqual(self.faults(artifact), {"browser_exit": "close_failed",
                                                 "control_channel_error": "Broken"})
        # After the run, the first fault of teardown is kept.
        artifact = {"browser_exit": None, "control_channel_error": None,
                    "final_page_error": None, "teardown_error": None}
        runner_woa.record_fault(artifact, "teardown", "Broken")
        runner_woa.record_fault(artifact, "teardown", "close_failed")
        self.assertEqual(self.faults(artifact), {"teardown_error": "Broken"})


if __name__ == "__main__":
    unittest.main()


class CardPage(Page):
    """A turn that shows one confirmation card of ``kind`` before it settles."""

    def __init__(self, kind):
        super().__init__()
        self.states = ["approval", "settled"]
        self.card = {"kind": kind, "effect": "离开当前页面，打开另一个页面",
                     "targets": ["将打开：/odoo/customers/13"]}

    def wait_for_function(self, script, **kwargs):
        handle = mock.MagicMock()
        handle.json_value.return_value = (self.states.pop(0) if script == runner_woa._SETTLED_JS
                                          else True)
        return handle

    def evaluate(self, script, *args):
        if script == runner_woa._APPROVAL_JS:
            return dict(self.card)
        return super().evaluate(script, *args)

    def wait_for_timeout(self, _ms):
        pass


@unittest.skipIf(importlib.util.find_spec("playwright") is None, "playwright is not installed")
class NavigationCardTests(unittest.TestCase):
    """The product asks before it leaves the page (P7.5 D7); the runner opens the page the task
    asked for, and the write oracle never sees that card."""

    def run_card(self, kind):
        page = CardPage(kind)
        context = mock.MagicMock()
        context.new_page.return_value = page
        browser = mock.MagicMock()
        browser.new_context.return_value = context
        playwright = mock.MagicMock()
        playwright.chromium.launch.return_value = browser
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        config = WoaRunnerConfig(base_url="https://odoo.test", actors={},
                                 evidence_dir=Path(directory.name), approval_visible_wait_ms=0)
        hook = mock.MagicMock(return_value={"decision": "approve", "reasons": [],
                                            "safety_events": []})
        checkpoints = []
        with mock.patch.object(runner_woa, "login"), \
                mock.patch.object(runner_woa, "breadcrumb",
                                  return_value={"path": "/odoo", "breadcrumb": "", "title": ""}), \
                mock.patch.object(runner_woa, "settle_view", return_value=0), \
                mock.patch.object(runner_woa, "logout", return_value=True):
            artifact = runner_woa.run(
                RunnerInput(task_id="t", actor="writer", locale="zh-CN", entry_path=None,
                            messages=("打开 azur",)),
                config, approval_hook=hook, checkpoint=checkpoints.append,
                playwright=playwright)
        return artifact, hook, page, checkpoints

    def test_a_page_switch_card_is_confirmed_and_kept_apart_from_write_approvals(self):
        artifact, hook, page, checkpoints = self.run_card("navigation")
        hook.assert_not_called()
        page.root.get_by_test_id.assert_called_once_with("woa-approve")
        self.assertEqual(artifact["approvals"], [])
        self.assertEqual([(item["turn"], item["card"]["kind"])
                          for item in artifact["navigation_confirmations"]], [(0, "navigation")])
        self.assertTrue(any(value.get("navigation") for value in checkpoints))
        self.assertEqual(artifact["turns"][0]["answer_kind"], "plain")

    def test_a_write_card_still_goes_to_the_oracle(self):
        artifact, hook, page, _ = self.run_card("write")
        hook.assert_called_once()
        page.root.get_by_test_id.assert_called_once_with("woa-approve")
        self.assertEqual(artifact["navigation_confirmations"], [])
        self.assertEqual(len(artifact["approvals"]), 1)


class NavigationTerminalTests(unittest.TestCase):
    def test_an_unconfirmed_page_switch_is_its_own_scored_ending(self):
        from evals.odoo import outcomes, trace
        self.assertEqual(trace.WOA_TERMINALS["navigation_not_confirmed"],
                         "navigation_not_confirmed")
        self.assertIn("navigation_not_confirmed", outcomes.PRODUCT_TERMINALS)
        self.assertNotEqual(trace.WOA_TERMINALS["navigation_not_confirmed"], "approval_expired")
