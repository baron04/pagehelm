"""Black-box assertions shared by structurally different application form adapters."""
from __future__ import annotations

import json
from dataclasses import replace

from web_operation_agent.business.forms import ApplicationFormTools
from web_operation_agent.business.errors import BusinessToolIdentityError


def assert_current_form_contract(case, port, *, application_id: str, observation,
                                 context, field_label: str, native_field: str,
                                 value: str, expected_value: str | None = None) -> None:
    tools = ApplicationFormTools(
        port, application_id=application_id, binding_secret=b"contract-form-binding-key")
    described = tools.describe(observation, {"field_query": field_label}, context=context)
    case.assertTrue(described["form_available"])
    case.assertEqual(described["submission"], "user_only")
    field = next(item for item in described["fields"] if item["label"] == field_label)
    case.assertRegex(described["form_id"], r"^form_[0-9a-f]{32}$")
    case.assertRegex(field["field_id"], r"^form_field_[0-9a-f]{32}$")
    case.assertNotIn(native_field, json.dumps(described, ensure_ascii=False))

    draft, prepared = tools.prepare(observation, {
        "form_id": described["form_id"],
        "values": [{"field_id": field["field_id"], "value": value}],
    }, context=context)
    case.assertEqual(prepared["submission"], "user_only")
    case.assertEqual(prepared["fields"], [field_label])
    case.assertEqual(len(draft.fields), 1)
    case.assertEqual(draft.fields[0].field, native_field)
    case.assertEqual(draft.fields[0].value,
                     value if expected_value is None else expected_value)
    verification = tools.take_verification(draft)
    fresh = replace(observation, epoch=observation.epoch + 1,
                    elements=[replace(e, epoch=observation.epoch + 1) for e in observation.elements])
    verification.check(draft, fresh, context=context)
    if context is not None:
        with case.assertRaises(BusinessToolIdentityError):
            verification.check(draft, fresh, context=replace(context, turn_id="other-turn"))


__all__ = ["assert_current_form_contract"]
