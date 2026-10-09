"""Timeline pages survive restart without granting recovered execution authority."""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from web_operation_agent.state import SQLiteSessionStore
from web_operation_agent.transport import RemoteProtocolError, RemoteSessionRegistry
from web_operation_agent.tests.transport.support import ORIGIN, hello, principal


class TimelinePagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SQLiteSessionStore(Path(self.temp.name) / "sessions.db")

    def connect(self, sid="history", *, durable=True):
        registry = RemoteSessionRegistry(durable_store=self.store if durable else None)
        actor = principal(int(time.time() * 1000))
        _, channel = registry.create(allowed_origins=[ORIGIN], session_id=sid, principal=actor)
        self.addCleanup(channel.close)
        lease = channel.accept_hello(hello(sid, application_id="sample",
                                          principal_digest=actor.binding_digest))["lease_id"]
        return channel, lease

    def publish(self, channel, start, stop):
        for n in range(start, stop):
            channel.publish("progress", {"state": "finished", "message": f"answer {n}"},
                            turn_id=f"turn-{n}")

    def history(self, channel, lease):
        pages = []
        page = channel.panel_timeline(lease_id=lease)
        while True:
            pages.append(page)
            if not page["has_more"]:
                break
            self.assertTrue(page["oldest"])
            self.assertLess(len(pages), 10, "history cursor did not advance")
            page = channel.panel_timeline(lease_id=lease, before=page["oldest"])
        return pages

    def test_default_pages_reach_the_beginning_before_and_after_restart(self):
        first, lease = self.connect()
        self.publish(first, 0, 235)
        original = self.history(first, lease)
        self.assertEqual([len(p["entries"]) for p in original], [100, 100, 35])
        original_ids = [e["id"] for p in reversed(original) for e in p["entries"]]
        first.close()
        recovered, lease = self.connect()
        with patch.object(self.store, "history", wraps=self.store.history) as reads:
            pages = self.history(recovered, lease)
        self.assertTrue(all(call.kwargs["limit"] <= 100 for call in reads.call_args_list))
        self.assertTrue(all("before" in call.kwargs for call in reads.call_args_list))
        entries = [e for p in reversed(pages) for e in p["entries"]]
        self.assertEqual([e["id"] for e in entries], original_ids)
        self.assertEqual([e["text"] for e in entries], [f"answer {n}" for n in range(235)])
        self.assertTrue(all(e["replayed"] for e in entries))

    def test_recovered_pages_cross_into_live_events_and_stay_stable_when_new_events_arrive(self):
        first, lease = self.connect()
        self.publish(first, 0, 205)
        first.close()
        channel, lease = self.connect()
        self.publish(channel, 205, 245)
        newest = channel.panel_timeline(lease_id=lease)
        self.assertEqual(len(newest["entries"]), 100)
        self.publish(channel, 245, 250)
        second = channel.panel_timeline(lease_id=lease, before=newest["oldest"])
        third = channel.panel_timeline(lease_id=lease, before=second["oldest"])
        entries = third["entries"] + second["entries"] + newest["entries"]
        self.assertEqual([e["text"] for e in entries], [f"answer {n}" for n in range(245)])
        newer = channel.panel_timeline(lease_id=lease, after=newest["cursor"])
        self.assertEqual([e["text"] for e in newer["entries"]],
                         [f"answer {n}" for n in range(245, 250)])
        self.assertFalse(newer["has_newer"])
        self.assertFalse(third["has_more"])

    def test_invisible_rows_cannot_strand_visible_history(self):
        channel, lease = self.connect()
        self.publish(channel, 0, 1)
        for _ in range(320):
            channel.publish("progress", {"state": "new_conversation"})
        channel.close()
        channel, lease = self.connect()
        page = channel.panel_timeline(lease_id=lease)
        self.assertEqual([e["text"] for e in page["entries"]], ["answer 0"])
        self.assertFalse(page["has_more"])

    def test_live_catchup_is_bounded_and_projects_only_new_events(self):
        channel, lease = self.connect(durable=False)
        cursor = channel.latest_event_id
        self.publish(channel, 0, 235)
        from web_operation_agent.transport.channel_panel import project_event
        with patch("web_operation_agent.transport.channel_panel.project_event",
                   wraps=project_event) as project:
            first = channel.panel_timeline(lease_id=lease, after=cursor)
            calls = project.call_count
            second = channel.panel_timeline(lease_id=lease, after=first["cursor"])
            self.assertEqual(project.call_count, calls)
        third = channel.panel_timeline(lease_id=lease, after=second["cursor"])
        self.assertEqual([len(p["entries"]) for p in (first, second, third)], [100, 100, 35])
        self.assertTrue(first["has_newer"])
        self.assertFalse(third["has_newer"])
        entries = first["entries"] + second["entries"] + third["entries"]
        self.assertEqual([e["text"] for e in entries], [f"answer {n}" for n in range(235)])

    def test_history_cursor_is_scoped_to_session_and_requires_valid_lease(self):
        channel, lease = self.connect()
        self.publish(channel, 0, 1)
        cursor = channel.panel_timeline(lease_id=lease)["oldest"]
        other, other_lease = self.connect("other")
        with self.assertRaises(RemoteProtocolError):
            other.panel_timeline(lease_id=other_lease, before=cursor)
        with self.assertRaises(RemoteProtocolError):
            channel.panel_timeline(lease_id="invalid", before=cursor)
        for invalid in (channel._history_prefix() + "00000000", cursor + "x"):
            with self.assertRaises(RemoteProtocolError):
                channel.panel_timeline(lease_id=lease, before=invalid)

    def test_cached_projection_is_not_mutated_by_a_caller(self):
        channel, lease = self.connect()
        self.publish(channel, 0, 1)
        first = channel.panel_timeline(lease_id=lease)
        first["entries"][0]["text"] = "changed"
        first["entries"][0]["position"]["sequence"] = -1
        second = channel.panel_timeline(lease_id=lease)
        self.assertEqual(second["entries"][0]["text"], "answer 0")
        self.assertGreater(second["entries"][0]["position"]["sequence"], 0)

    def test_incremental_projection_keeps_existing_denoising(self):
        channel, lease = self.connect(durable=False)
        channel.publish("progress", {"state": "step", "message": "Reading"}, turn_id="t")
        first = channel.panel_timeline(lease_id=lease)
        channel.publish("progress", {"state": "step", "message": "Reading"}, turn_id="t")
        second = channel.panel_timeline(lease_id=lease)
        self.assertEqual(len(first["entries"]), 1)
        self.assertEqual(len(second["entries"]), 1)
        self.assertGreater(second["entries"][0]["sequence"], first["entries"][0]["sequence"])

    def test_store_rejects_unbounded_or_invalid_pages(self):
        channel, _ = self.connect()
        for limit in (True, 0, 501):
            with self.assertRaises(ValueError):
                self.store.history(channel._owner, limit=limit)
        for before in (True, 0, "1"):
            with self.assertRaises(ValueError):
                self.store.history(channel._owner, before=before)

    def test_repeated_progress_across_page_boundary_is_not_repeated(self):
        channel, lease = self.connect()
        for n in range(110):
            for _ in range(2):
                channel.publish("progress", {"state": "step", "message": f"Reading {n}"},
                                turn_id=f"t{n}")
        channel.close()
        channel, lease = self.connect()
        pages = self.history(channel, lease)
        entries = [e for p in reversed(pages) for e in p["entries"]]
        self.assertEqual([e["text"] for e in entries], [f"Reading {n}" for n in range(110)])
