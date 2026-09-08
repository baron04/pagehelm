import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote

from web_operation_agent.ledger import SessionLedger
from web_operation_agent.privacy import (
    SESSION_HANDOFF_KEYS,
    has_unredacted_sensitive_assignment,
    has_unredacted_session_handoff,
    redact_sensitive_assignments,
    redact_session_handoffs,
)
from web_operation_agent.session import _safe


class SessionHandoffPrivacy(unittest.TestCase):
    def test_every_exact_name_is_redacted_through_three_url_encodings(self):
        for key in SESSION_HANDOFF_KEYS:
            for depth in range(4):
                with self.subTest(key=key, depth=depth):
                    value = f"/x?{key}=session-secret&businessId=42"
                    for _ in range(depth):
                        value = quote(value, safe="")
                    self.assertTrue(
                        has_unredacted_session_handoff(value, url_decode_depth=3))
                    cleaned = redact_session_handoffs(value)
                    self.assertNotIn("session-secret", cleaned)
                    self.assertIn("[redacted]", cleaned)
                    self.assertFalse(
                        has_unredacted_session_handoff(cleaned, url_decode_depth=3))
                    self.assertEqual(redact_session_handoffs(cleaned), cleaned)

    def test_matching_is_case_insensitive_and_preserves_suffixes(self):
        value = "/x?ExTeRnALoGiNkEy=secret&businessId=42"
        cleaned = redact_session_handoffs(value)
        self.assertEqual(
            cleaned, "/x?ExTeRnALoGiNkEy=[redacted]&businessId=42")

    def test_structured_key_and_name_value_forms_are_symmetric(self):
        value = {
            "externaLoginKey": "structured-secret",
            "element": {"name": "externLoginKey", "value": "control-secret"},
        }
        self.assertTrue(has_unredacted_session_handoff(value))
        cleaned = redact_session_handoffs(value)
        self.assertEqual(cleaned["externaLoginKey"], "[redacted]")
        self.assertEqual(cleaned["element"]["value"], "[redacted]")
        self.assertFalse(has_unredacted_session_handoff(cleaned))

    def test_near_miss_business_names_are_not_redacted(self):
        value = {
            "externalLoginKeyLabel": "business-value",
            "url": "/x?externalLoginKeyLabel=business-value",
            "control": {"name": "externalLoginKeyLabel", "value": "business-value"},
        }
        self.assertEqual(redact_session_handoffs(value), value)
        self.assertFalse(has_unredacted_session_handoff(value, url_decode_depth=3))

    def test_encoded_key_falls_back_to_whole_leaf_redaction(self):
        value = "/x?external%254CoginKey%253DHANDOFF_SECRET%2526next%253D1"
        self.assertTrue(has_unredacted_session_handoff(value, url_decode_depth=3))
        self.assertEqual(redact_session_handoffs(value), "[redacted]")


class GenericCredentialPrivacy(unittest.TestCase):
    def test_literal_and_repeated_encoded_assignments_are_detected(self):
        unsafe = (
            "/callback?access_token=plain-secret&next=1",
            "/callback?access_token%3Dencoded-secret%26next%3D1",
            "/callback%253Frefresh-token%253Ddouble-secret%2526next%253D1",
            "/callback?access%255Ftoken%253Dencoded-key-secret%2526next%253D1",
            "authorization=Bearer%20secret",
        )
        for value in unsafe:
            with self.subTest(value=value):
                self.assertTrue(has_unredacted_sensitive_assignment(
                    value, url_decode_depth=3))

    def test_direct_keys_and_control_value_siblings_are_detected_recursively(self):
        unsafe = {
            "events": [{"api_key": "direct-secret"}],
            "elements": [
                {"name": "access_token", "value": "control-secret"},
                {"id": "current-password", "value": "password-secret"},
                {"autocomplete": "new-password", "value": "new-secret"},
            ],
        }
        self.assertTrue(has_unredacted_sensitive_assignment(unsafe))

    def test_redacted_values_and_business_assignments_are_allowed(self):
        safe = {
            "url": "/orders?status=pending&access_token%253D%255Bredacted%255D",
            "access_token": "[redacted]",
            "control": {"name": "refresh_token", "value": "[REDACTED]"},
            "business": {"tokenLabel": "Gold", "value": "ordinary"},
        }
        self.assertFalse(has_unredacted_sensitive_assignment(
            safe, url_decode_depth=3))

    def test_generic_redactor_is_recursive_encoded_and_idempotent(self):
        unsafe = {
            "url": "/callback?access_token=plain-secret&status=pending",
            "encoded": "/callback?api_key%253Dencoded-secret%2526next%253D1",
            "encoded_key": "/callback?access%255Ftoken%253Dkey-secret%2526next%253D1",
            "nested": [{"authorization": "Bearer direct-secret"}],
            "control": {"id": "current-password", "value": "control-secret"},
            "business": "/orders?status=pending&orderTokenLabel=gold",
        }
        cleaned = redact_sensitive_assignments(unsafe)
        wire = json.dumps(cleaned)
        for secret in ("plain-secret", "encoded-secret", "key-secret", "direct-secret",
                       "control-secret"):
            self.assertNotIn(secret, wire)
        self.assertEqual(cleaned["business"], unsafe["business"])
        self.assertEqual(redact_sensitive_assignments(cleaned), cleaned)
        self.assertFalse(has_unredacted_sensitive_assignment(
            cleaned, url_decode_depth=3))

    def test_session_safe_redacts_assignments_before_tool_call_is_persisted(self):
        cleaned = _safe({
            "url": "/next?access_token=URL_SECRET",
            "note": {"text": "api_key%253DINNER_SECRET%2526next%253D1"},
            "encoded_key": "access%255Ftoken%253DKEY_SECRET%2526next%253D1",
            "api_key": "DIRECT_SECRET",
            "business": "/orders?status=pending&tokenLabel=gold",
        })
        wire = json.dumps(cleaned)
        for secret in ("URL_SECRET", "INNER_SECRET", "KEY_SECRET", "DIRECT_SECRET"):
            self.assertNotIn(secret, wire)
        self.assertEqual(cleaned["business"],
                         "/orders?status=pending&tokenLabel=gold")

    def test_ledger_is_the_final_recursive_privacy_boundary(self):
        fields = {
            "url": "/next?access_token=LEDGER_SECRET&status=pending",
            "detail": "api_key%253DINNER_SECRET%2526next%253D1",
            "encoded_key": "access%255Ftoken%253DKEY_SECRET%2526next%253D1",
            "encoded_handoff": (
                "external%254CoginKey%253DHANDOFF_SECRET%2526next%253D1"),
            "nested": {"token": "DIRECT_SECRET"},
            "control": {"autocomplete": "new-password", "value": "FORM_SECRET"},
            "business": "/orders?status=pending&tokenLabel=gold",
        }
        with tempfile.TemporaryDirectory() as tmp:
            ledger = SessionLedger(Path(tmp) / "session.jsonl", "privacy-test")
            first = ledger.append("browser_event", **fields)
            second = ledger.append("already_clean", payload=first)
            disk = (Path(tmp) / "session.jsonl").read_text()
        for secret in ("LEDGER_SECRET", "INNER_SECRET", "KEY_SECRET", "HANDOFF_SECRET",
                       "DIRECT_SECRET", "FORM_SECRET"):
            self.assertNotIn(secret, disk)
        self.assertEqual(first["business"], fields["business"])
        self.assertEqual(second["payload"]["url"], first["url"])
        self.assertEqual(len(disk.splitlines()), 2)


if __name__ == "__main__":
    unittest.main()
