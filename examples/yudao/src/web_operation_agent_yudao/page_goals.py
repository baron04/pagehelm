"""Native deployment arrival verification over the existing neutral goal port."""
from dataclasses import replace

from web_operation_agent.goal.resolve.service import DataGoalPort


class NativePageGoalPort(DataGoalPort):
    """A native page is reached only when its controller proves the live mount.

    This opts the deployment into the existing PagePlan observation contract;
    it grants no navigation, form, tab or API capability.
    """

    def resolve(self, spec, *, context=None, current_path=""):
        result = super().resolve(spec, context=context, current_path=current_path)
        return replace(result, deliverables=tuple(
            replace(item, page=replace(item.page, requires_page_route=True))
            if item.page is not None else item for item in result.deliverables))
