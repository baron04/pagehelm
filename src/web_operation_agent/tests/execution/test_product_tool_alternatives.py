"""Closed scalar/array operands for the common summary tool in two applications."""
import unittest

from web_operation_agent.business.errors import BusinessToolRequestError
from web_operation_agent.business.tools.registry import _validate_schema, validate_input


class ToolAlternativesTests(unittest.TestCase):
    def test_each_scalar_shape_and_a_bounded_scalar_array_is_validated(self):
        scalar = {"anyOf": [{"type": "string", "maxLength": 4}, {"type": "number"},
                             {"type": "boolean"}, {"type": "null"}]}
        schema = {"anyOf": [scalar, {"type": "array", "maxItems": 2, "items": scalar}]}
        _validate_schema(schema)
        for value in ("name", 0, 1.2, True, None, [1, "two"], [None, False]):
            with self.subTest(value=value):
                validate_input(schema, value)
        for value in ({"private": "secret"}, "too long", [1, 2, 3], [[1]], float("nan")):
            with self.subTest(value=value), self.assertRaises(BusinessToolRequestError) as caught:
                validate_input(schema, value)
            self.assertNotIn("secret", str(caught.exception))

    def test_invalid_alternatives_fail_at_installation_including_unknown_branch_keywords(self):
        for schema in ({"anyOf": []}, {"anyOf": [{"type": "string"}]},
                {"anyOf": [{"type": "null"}] * 7},
                {"anyOf": [{"type": "string"}, {"type": "null"}], "type": "string"},
                {"anyOf": [{"type": "string"}, {"type": "object", "patternProperties": {}}]}):
            with self.subTest(schema=schema), self.assertRaises(ValueError):
                _validate_schema(schema)

    def test_number_and_null_branches_do_not_accept_boolean_as_a_number(self):
        schema = {"anyOf": [{"type": "number"}, {"type": "null"}]}
        _validate_schema(schema)
        with self.assertRaises(BusinessToolRequestError):
            validate_input(schema, True)
        with self.assertRaises(BusinessToolRequestError):
            validate_input({"type": "null"}, "null")


if __name__ == "__main__":
    unittest.main()
