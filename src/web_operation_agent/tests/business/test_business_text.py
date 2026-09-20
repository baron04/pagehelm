"""Two records a person cannot tell apart must not both exist.

`twenty.py:696` used to reject only C0 controls, so a zero-width space, a bidirectional
override, a non-breaking space or one Cyrillic letter produced a second company whose
confirmation card rendered identically to the first (review finding P2-a).
"""
import unittest

from web_operation_agent.business.runtime import (BusinessToolRequestError, business_text_key,
                                          normalize_business_text)


def _normalize(value):
    return normalize_business_text(value, field="company name", maximum=200)


class InvisibleAndDirectionalCharactersAreRefused(unittest.TestCase):
    def test_zero_width_and_bidirectional_overrides_are_refused(self):
        for value in ("Acme​Corp", "Acme Corp‮", "Acme⁠Corp", "Acme﻿Corp"):
            with self.assertRaisesRegex(BusinessToolRequestError, "invisible or directional"):
                _normalize(value)

    def test_an_ordinary_space_is_not_refused(self):
        """U+0020 is category Zs; refusing that category would refuse every real name."""
        self.assertEqual(_normalize("Acme Corp"), "Acme Corp")


class VisuallyIdenticalNamesFoldToOneKey(unittest.TestCase):
    def test_compatibility_forms_and_exotic_spaces_fold_to_the_plain_name(self):
        for value in ("Acme Corp", "Ａｃｍｅ Corp", "  Acme   Corp  ", "Acme　Corp"):
            self.assertEqual(_normalize(value), "Acme Corp", value)
            self.assertEqual(business_text_key(value), business_text_key("Acme Corp"), value)

    def test_a_latin_cyrillic_homograph_is_refused_outright(self):
        with self.assertRaisesRegex(BusinessToolRequestError, "visually identical alphabet"):
            _normalize("Аcme Corp")

    def test_a_name_written_in_one_alphabet_is_accepted(self):
        for value in ("Акме", "Acme 中国", "Ακμη"):
            self.assertEqual(_normalize(value), value)

    def test_the_comparison_key_never_raises_on_a_record_we_did_not_create(self):
        for value in (None, 42, "Acme​Corp", ""):
            business_text_key(value)


class BoundsStillHold(unittest.TestCase):
    def test_empty_and_oversized_names_are_refused(self):
        for value in ("", "   ", " "):
            with self.assertRaisesRegex(BusinessToolRequestError, "must not be empty"):
                _normalize(value)
        with self.assertRaisesRegex(BusinessToolRequestError, "1 to 200"):
            _normalize("A" * 201)

    def test_a_non_string_is_refused(self):
        with self.assertRaisesRegex(BusinessToolRequestError, "must be a string"):
            _normalize(None)


if __name__ == "__main__":
    unittest.main()
