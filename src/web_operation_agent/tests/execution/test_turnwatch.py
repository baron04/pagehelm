"""The feed and the guard, driven directly -- which the session class never allowed."""
from __future__ import annotations

import unittest

from web_operation_agent.schemas import PageObservation
from web_operation_agent.execution.turnwatch import (ProgressFeed, RepeatGuard,
                                                     SourcePageProgress)


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
    def test_a_refused_a_b_a_loop_warns_then_stops_without_dispatching_a(self):
        guard = RepeatGuard(2)
        guard.bind_scope("session:actor-a:turn-a")
        guard.decide("read", {"field": "bad"}, _page())
        guard.refused("read", {"field": "bad"}, _page())
        guard.decide("read", {"field": "other bad"}, _page(epoch=2))
        guard.refused("read", {"field": "other bad"}, _page(epoch=2))
        self.assertEqual(guard.decide("read", {"field": "bad"}, _page(epoch=3)), "warn")
        guard.decide("observe", {}, _page(epoch=4))
        self.assertEqual(guard.decide("read", {"field": "bad"}, _page(epoch=5)), "end")

    def test_a_changed_page_successful_read_or_new_actor_invalidates_refusals(self):
        for changed in ("page", "facts", "actor"):
            with self.subTest(changed=changed):
                guard = RepeatGuard(2)
                guard.bind_scope("session:actor-a:turn-a")
                guard.refused("read", {"field": "bad"}, _page())
                page = _page(content="Changed") if changed == "page" else _page()
                if changed == "facts":
                    guard.facts_changed()
                if changed == "actor":
                    guard.bind_scope("session:actor-b:turn-b")
                self.assertEqual(guard.decide("read", {"field": "bad"}, page), "")

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


class SourceAssistedProgressIsAboutNewFactsNotNewToolWording(unittest.TestCase):
    def test_repeated_page_and_source_facts_warn_then_end(self):
        guard = SourcePageProgress()
        self.assertEqual(guard.note(_page(epoch=1), ("source#1",)), "")
        self.assertEqual(guard.note(_page(epoch=2), ("source#1",)), "")
        self.assertEqual(guard.note(_page(epoch=3), ("source#1",)), "")
        self.assertEqual(guard.note(_page(epoch=4), ("source#1",)), "warn")
        self.assertEqual(guard.note(_page(epoch=5), ("source#1",)), "")
        self.assertEqual(guard.note(_page(epoch=6), ("source#1",)), "end")

    def test_new_fact_or_page_resets_stall_but_a_revisited_page_does_not(self):
        guard = SourcePageProgress(warn_after=2, stop_after=4)
        self.assertEqual(guard.note(_page(content="Overview"), ("source#1",)), "")
        self.assertEqual(guard.note(_page(content="Terms"), ("source#1",)), "")
        self.assertEqual(guard.note(_page(content="Overview"), ("source#1",)), "")
        self.assertEqual(guard.note(_page(content="Terms"), ("source#1",)), "warn")
        self.assertEqual(guard.note(_page(content="Terms"),
                                    ("source#1", "source#2")), "")


if __name__ == "__main__":
    unittest.main()
