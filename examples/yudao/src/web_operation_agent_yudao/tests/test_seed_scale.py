"""Local scale fixture must not duplicate records hidden beyond page one or target a site."""
from contextlib import redirect_stderr
import importlib.util
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch


DEPLOY = Path(__file__).resolve().parents[3] / "deploy"


def load_scale():
    spec = importlib.util.spec_from_file_location("scale_fixture", DEPLOY / "seed_scale.py")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(DEPLOY))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


class ScaleFixtureTests(unittest.TestCase):
    def test_rerun_finds_an_existing_record_past_page_one_without_mutation(self):
        module = load_scale()
        client = Mock()
        client.get.side_effect = lambda _path, pageNo, pageSize: {
            "list": [{"id": i, "name": f"Synthetic {i}"} for i in
                     (range(100) if pageNo == 1 else range(100, 125))], "total": 125}
        fixture = module.Scale(client, 120)
        self.assertEqual((124, False), fixture.ensure("fixture", {"name": "Synthetic 124"}))
        client.post.assert_not_called()
        self.assertEqual([1, 2], [call.kwargs["pageNo"] for call in client.get.call_args_list])

    def test_an_incomplete_index_stops_before_creating_a_possible_duplicate(self):
        module = load_scale()
        client = Mock()
        client.get.return_value = {"list": [], "total": 120}
        with self.assertRaisesRegex(RuntimeError, "incomplete fixture index"):
            module.Scale(client, 120).ensure("fixture", {"name": "Synthetic"})
        client.post.assert_not_called()

    def test_a_failed_mutation_is_not_retried(self):
        module = load_scale()
        client = Mock()
        client.get.return_value = {"list": [], "total": 0}
        client.post.side_effect = TimeoutError("unknown outcome")
        with self.assertRaises(TimeoutError):
            module.Scale(client, 120).ensure("fixture", {"name": "Synthetic"})
        client.post.assert_called_once()

    def test_remote_targets_are_rejected_before_reading_accounts_or_logging_in(self):
        module = load_scale()
        args = ["seed_scale.py", "--base", "https://example.com", "--account", "unused",
                "--backup", "unused", "--report", "unused", "--apply"]
        with patch.object(sys, "argv", args), patch.object(module, "Client") as client, \
                redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            module.main()
        client.assert_not_called()
