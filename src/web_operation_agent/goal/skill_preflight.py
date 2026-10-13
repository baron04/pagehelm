"""Read-only whole-plan admission before any Skill write approval."""
from dataclasses import dataclass, replace
import json

from ..business.errors import BusinessToolRequestError, BusinessToolReferenceError
from ..business.tools.registry import validate_input
from .skill_plan import reference_dependencies
from .spec import GoalSpec


@dataclass
class CorrectionBudget:
    used: bool = False

    def consume(self):
        if self.used:
            return False
        self.used = True
        return True


class PreparationError(BusinessToolRequestError):
    """One proposed operation failed while preparation was still read-only."""
    code = "skill_preparation"

    def __init__(self, position, cause):
        super().__init__(str(cause))
        self.position = position
        self.cause = cause


def preflight_plan(plan, definitions, *, prepare, check_goal=None, recover_references=False):
    """Check all schema/dependencies and independently preparable effects.

    Dependent effects cannot be prepared before their referenced write is verified.
    They are still schema-checked now and prepared afresh at their actual position.
    Preparations confer no authority and are never dispatched by this function.
    """
    if plan.get("question") or plan.get("unsupported"):
        return
    tools = {definition.name: definition for definition in definitions}
    steps = plan["steps"]
    for position, step in enumerate(steps):
        if step["kind"] == "goal":
            goal = GoalSpec.from_json(step["goal"])
            if goal.unsupported:
                raise BusinessToolRequestError("a declared goal has an unmet requirement")
            if check_goal:
                check_goal(goal)
            continue
        definition = tools.get(step["operation"])
        if definition is None:
            raise BusinessToolRequestError("plan names an unavailable operation")
        try:
            validate_input(json.loads(definition.input_json), step["inputs"])
            dependencies = reference_dependencies(step["inputs"], definition.reference_inputs)
            if any(prior >= position or steps[prior]["kind"] != "operation" for prior in dependencies):
                raise BusinessToolRequestError("dependency must name a preceding operation")
            if not dependencies:
                proposal = prepare(position, step)
                if definition.effect_contract is not None:
                    if proposal.effect_projection is None:
                        raise BusinessToolRequestError("application did not project the prepared effect")
                    expected = definition.effect_contract.project_inputs(step["inputs"])
                    if step.get("preserve"):
                        expected = replace(expected, preserve=tuple(step["preserve"]))
                    proposal.effect_projection.require(expected)
        except BusinessToolReferenceError as exc:
            if not recover_references:
                raise
            raise PreparationError(position, exc) from exc
        except BusinessToolRequestError as exc:
            raise PreparationError(position, exc) from exc
