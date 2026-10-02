"""Distribution-side scalar editor goals over the neutral data and form contracts."""
from dataclasses import replace
import re

from web_operation_agent.goal.ports import FormPlan, Resolved
from web_operation_agent.goal.resolve.service import DataGoalPort
from .form_entries import REVIEWED_FILES


class NativeFormsGoalPort(DataGoalPort):
    def __init__(self, data, source, **kwargs):
        super().__init__(data, **kwargs)
        self._form_source = source

    def capabilities(self):
        return super().capabilities() | {"fill", "handoffs"}

    def resolve(self, spec, *, context=None, current_path=""):
        indices = [i for i, d in enumerate(spec.deliverables) if d.kind == "handoff" and d.value]
        if not indices:
            return super().resolve(spec, context=context, current_path=current_path)
        altered = tuple(replace(d, kind="page", action="", value="") if i in indices else d
                        for i, d in enumerate(spec.deliverables))
        result = super().resolve(replace(spec, deliverables=altered), context=context, current_path=current_path)
        source = self._form_source.read(REVIEWED_FILES[1])
        fields = {f.reference for f in source.fields if not f.conditional and not f.handlers} if source else set()
        index = self.index(context=context)
        out = list(result.deliverables)
        for i in indices:
            d, resolved = spec.deliverables[i], out[i]
            entity = index.entity(d.entity)
            field = entity.field_by_label(d.action) if entity else None
            page = resolved.page
            match = re.fullmatch(r"/crm/customer/detail/([1-9][0-9]{0,19})", page.url) if page else None
            if resolved.problem or resolved.denied or resolved.unoffered:
                out[i] = replace(resolved, kind="handoff")
            elif (not match or not field or field.key not in fields or d.section or not d.record
                  or page.fill or page.highlight):
                out[i] = Resolved(kind="handoff", problem="this reviewed editor does not offer the requested field or target")
            else:
                out[i] = replace(resolved, kind="handoff", page=replace(page,
                    form=FormPlan("customer-update:"+match[1], ((field.key, d.value),), open_entry=True)))
        return replace(result, deliverables=tuple(out))
