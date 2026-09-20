"""The feed and the guard, driven directly -- which the session class never allowed."""
from __future__ import annotations

import unittest

from web_operation_agent.schemas import PageObservation
from web_operation_agent.execution.turnwatch import ProgressFeed, RepeatGuard


def _page(url: str = "https://app.test/orders", epoch: int = 1,
          content: str = "Orders") -> PageObservation:
    return PageObservation(schema_version=1, epoch=epoch, url=url, title="t",
                           landmarks=[], elements=[], content=content, messages=[])


class AProgressFeedSaysWhatIsHappening(unittest.TestCase):
    def test_it_publishes_the_facts_it_is_given(self):
        published: list[dict] = []
        ProgressFeed(published.append).note(stage="thinking", step=2)
        self.assertEqual(published, [{"stage": "thinking", "step": 2}])

    def test_nobody_listening_is_not_a_failure(self):
        ProgressFeed(None).note(stage="thinking")   # no exception

    def test_a_listener_that_raises_is_recorded_and_the_turn_goes_on(self):
        recorded: list[tuple] = []

        def broken(_facts):
            raise RuntimeError("panel is gone")

        ProgressFeed(broken, on_failure=lambda event, **facts: recorded.append((event, facts))
                     ).note(stage="acting")
        self.assertEqual(recorded, [("progress_publish_failed", {"error_type": "RuntimeError"})])


class ARepeatGuardEndsATurnThatIsNotMoving(unittest.TestCase):
    def test_the_same_decision_on_the_same_page_warns_once_then_ends_it(self):
        guard = RepeatGuard(2)
        self.assertEqual(guard.decide("click", {"ref": "e1"}, _page()), "")
        self.assertEqual(guard.decide("click", {"ref": "e1"}, _page()), "warn")
        self.assertEqual(guard.decide("click", {"ref": "e1"}, _page()), "end")
        self.assertEqual(guard.repeats, 3)

    def test_a_different_decision_or_a_changed_page_is_progress(self):
        guard = RepeatGuard(2)
        guard.decide("click", {"ref": "e1"}, _page())
        self.assertEqual(guard.decide("click", {"ref": "e2"}, _page()), "")
        guard.decide("click", {"ref": "e2"}, _page())
        self.assertEqual(guard.decide("click", {"ref": "e2"}, _page(content="Orders (2)")), "")

    def test_a_new_observation_of_the_same_page_is_still_the_same_page(self):
        """Only the epoch changed, and it changes with every observation."""
        guard = RepeatGuard(2)
        guard.decide("observe", {}, _page(epoch=1))
        self.assertEqual(guard.decide("observe", {}, _page(epoch=2)), "warn")

    def test_finishing_and_asking_are_never_repeats(self):
        guard = RepeatGuard(2)
        for _ in range(5):
            self.assertEqual(guard.decide("finish", {"done": True}, _page()), "")
            self.assertEqual(guard.decide("ask_user", {"question": "which?"}, _page()), "")

    def test_no_limit_means_no_guard(self):
        guard = RepeatGuard(None)
        for _ in range(5):
            self.assertEqual(guard.decide("click", {"ref": "e1"}, _page()), "")

    def test_arguments_that_are_not_json_still_fingerprint(self):
        guard = RepeatGuard(2)
        args = {"value": float("nan")}
        self.assertEqual(guard.decide("fill", args, _page()), "")
        self.assertEqual(guard.decide("fill", args, _page()), "warn")

    def test_a_reset_forgets_what_was_repeated(self):
        guard = RepeatGuard(2)
        guard.decide("click", {"ref": "e1"}, _page())
        guard.reset()
        self.assertEqual(guard.decide("click", {"ref": "e1"}, _page()), "")


if __name__ == "__main__":
    unittest.main()
