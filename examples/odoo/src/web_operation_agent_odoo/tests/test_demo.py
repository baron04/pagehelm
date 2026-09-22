"""The two properties the demo's new screenshots must not cost it.

A demo is the one artifact whose job is to be looked at, and until now it produced nothing
to look at: whether the panel had rendered could only be settled by a person being in front
of the window. Adding screenshots is worth doing only if it cannot break the run it is
photographing, and only if the wait it now shares with the approval card keeps the budget it
replaced. Neither needs a browser to check.
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from web_operation_agent_odoo.demo.demo import (OdooDemoConfig, _await_outcome,
                                                _browser_window_options, _capture,
                                                _notify_turn_boundary)


class _Page:
    """A page that answers the two questions `_capture` and the poll ask of it."""

    def __init__(self, *, shoots: bool = True, approval: bool = False):
        self.shoots, self.approval, self.shots = shoots, approval, []

    def screenshot(self, *, path: str) -> None:
        if not self.shoots:
            raise RuntimeError("the page went away mid-screenshot")
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\n")
        self.shots.append(path)

    def locator(self, selector: str):
        page = self

        class _Locator:
            @staticmethod
            def is_visible() -> bool:
                return page.approval

        return _Locator()


class _Conversation:
    """A conversation whose outcome arrives after a set number of polls, or never."""

    def __init__(self, *, after_polls: int | None):
        self.after_polls, self.polls = after_polls, 0

    def next_outcome(self, timeout_s: float | None = None):
        self.polls += 1
        if self.after_polls is not None and self.polls >= self.after_polls:
            return "outcome"
        time.sleep(min(0.02, timeout_s or 0.02))
        raise TimeoutError("timed out waiting for a completed conversation turn")


class ACapturedScreenshotCannotEndTheRun(unittest.TestCase):
    def test_a_screenshot_that_fails_is_reported_by_its_absence(self):
        """The run is the deliverable; the picture of it is not worth failing for."""
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(
                _capture(_Page(shoots=False), Path(directory) / "screens", "ready"), "")

    def test_a_screenshot_that_works_names_the_file_it_wrote(self):
        with tempfile.TemporaryDirectory() as directory:
            shots = Path(directory) / "screens"
            page = _Page()
            self.assertEqual(_capture(page, shots, "ready"), "ready.png")
            self.assertTrue((shots / "ready.png").is_file())


class ThePollingWaitKeepsTheContractItReplaced(unittest.TestCase):
    def test_the_outcome_is_returned_as_soon_as_it_arrives(self):
        with tempfile.TemporaryDirectory() as directory:
            taken: list[str] = []
            outcome = _await_outcome(
                _Conversation(after_polls=3), _Page(), Path(directory),
                timeout_s=10, taken=taken, turn=1)
        self.assertEqual(outcome, "outcome")

    def test_the_budget_is_the_same_one_and_still_raises_timeout(self):
        """Splitting the wait into polls must not extend it, and must not swallow the end."""
        with tempfile.TemporaryDirectory() as directory:
            started = time.monotonic()
            with self.assertRaisesRegex(TimeoutError, "completed conversation turn"):
                _await_outcome(_Conversation(after_polls=None), _Page(), Path(directory),
                               timeout_s=0.4, taken=[], turn=1)
            self.assertLess(time.monotonic() - started, 2.0)

    def test_an_approval_on_screen_is_photographed_once_while_waiting(self):
        """The card is the one thing a person is here to see, and it is on screen only
        while the turn is blocked -- which is exactly when nothing else was looking."""
        with tempfile.TemporaryDirectory() as directory:
            shots, taken = Path(directory) / "screens", []
            page = _Page(approval=True)
            with self.assertRaises(TimeoutError):
                _await_outcome(_Conversation(after_polls=None), page, shots,
                               timeout_s=0.4, taken=taken, turn=2)
        self.assertEqual(taken, ["turn-2-approval.png"])
        self.assertEqual(len(page.shots), 1, "the card is photographed once, not once per poll")

    def test_no_card_means_no_file(self):
        with tempfile.TemporaryDirectory() as directory:
            taken: list[str] = []
            with self.assertRaises(TimeoutError):
                _await_outcome(_Conversation(after_polls=None), _Page(approval=False),
                               Path(directory) / "screens", timeout_s=0.3, taken=taken,
                               turn=1)
        self.assertEqual(taken, [])


class TheFixtureHookRunsOnlyBetweenTurns(unittest.TestCase):
    def test_the_hook_gets_a_copy_after_a_non_final_turn(self):
        calls = []
        record = {"status": "finished"}

        def hook(turn, projected):
            calls.append((turn, projected))
            projected["status"] = "rewritten"

        _notify_turn_boundary(hook, turn=1, total_turns=2, record=record)

        self.assertEqual(calls, [(1, {"status": "rewritten"})])
        self.assertEqual(record, {"status": "finished"})

    def test_the_hook_never_runs_after_the_final_turn(self):
        calls = []
        _notify_turn_boundary(
            lambda turn, record: calls.append((turn, record)),
            turn=2, total_turns=2, record={"status": "finished"})
        self.assertEqual(calls, [])


class TheDemoViewportFitsItsWindow(unittest.TestCase):
    def test_interactive_window_uses_the_available_screen(self):
        launch, context = _browser_window_options(headless=False)
        self.assertEqual(launch, {"headless": False, "args": ["--start-maximized"]})
        self.assertEqual(context, {"no_viewport": True})

    def test_headless_capture_retains_a_deterministic_size(self):
        launch, context = _browser_window_options(headless=True)
        self.assertEqual(launch, {"headless": True})
        self.assertEqual(context, {"viewport": {"width": 1440, "height": 1000}})


class QualificationOnlyDemoSeamsFailClosed(unittest.TestCase):
    def _config(self, **changes):
        values = {
            "base_url": "https://localhost:20443",
            "api_key_path": Path("api-key.json"),
            "storage_state_path": Path("state.json"),
            "page_controller_module": "https://localhost:20443/controller.js",
            "scripted": True,
        }
        values.update(changes)
        return OdooDemoConfig(**values)

    def test_private_ca_bypass_is_limited_to_loopback(self):
        self._config(browser_ignore_https_errors=True)
        with self.assertRaisesRegex(ValueError, "loopback qualification"):
            self._config(base_url="https://odoo.example.test",
                         browser_ignore_https_errors=True)

    def test_dynamic_chat_factory_requires_scripted_mode(self):
        with self.assertRaisesRegex(ValueError, "requires scripted mode"):
            self._config(scripted=False, scripted_chat_factory=lambda _policy: object())

    def test_final_probe_must_be_callable(self):
        with self.assertRaisesRegex(TypeError, "final_page_probe"):
            self._config(final_page_probe={})

    def test_probe_and_tls_bypass_are_not_available_to_provider_runs(self):
        with self.assertRaisesRegex(ValueError, "scripted qualification mode"):
            self._config(scripted=False, final_page_probe=lambda _page: {})
        with self.assertRaisesRegex(ValueError, "scripted qualification"):
            self._config(scripted=False, browser_ignore_https_errors=True)

    def test_evaluation_seams_are_read_only_provider_runs(self):
        config = self._config(scripted=False, evaluation=True, evaluation_page_click=True,
                              confirm_navigation_in_page=True,
                              final_page_probe=lambda _page: {})
        self.assertTrue(config.evaluation_page_click)
        with self.assertRaisesRegex(ValueError, "requires an evaluation run"):
            self._config(scripted=False, evaluation_page_click=True)
        with self.assertRaisesRegex(ValueError, "read-only provider run"):
            self._config(scripted=True, evaluation=True)
        with self.assertRaisesRegex(ValueError, "read-only provider run"):
            self._config(scripted=False, evaluation=True, allow_mutations=True,
                         mutation_journal_path="/tmp/journal.sqlite3")
        with self.assertRaisesRegex(ValueError, "scripted run"):
            self._config(scripted=False, confirm_navigation_in_page=True)
        with self.assertRaisesRegex(ValueError, "require an evaluation run"):
            self._config(scripted=False, extra_skills=(object(),))
        with self.assertRaisesRegex(ValueError, "require an evaluation run"):
            self._config(scripted=False, source_override=object())


if __name__ == "__main__":
    unittest.main()
