"""Distribution-side scalar editor goals over the neutral data and form contracts."""
from dataclasses import replace
import re

from web_operation_agent.goal.ports import FormPlan, Resolved
from .page_goals import NativePageGoalPort
from .form_entries import REVIEWED_FILES


class NativeFormsGoalPort(NativePageGoalPort):
    def __init__(self, data, source, *, entries=(), updates=True, **kwargs):
        super().__init__(data, **kwargs)
        self._form_source = source
        self._entries = tuple(entries)
        self._updates = updates

    def index(self, *, context=None):
        index = super().index(context=context)
        source = self._form_source.read(REVIEWED_FILES[1]) if self._updates else None
        all_labels = {"crm/customer": {f.reference:f.label for f in source.fields} if source else {}}
        for _, entity, _, _, _, _, component in self._entries:
            form = self._form_source.read(component)
            all_labels[entity] = {f.reference:f.label for f in form.fields} if form else {}
        labels = {e.key:all_labels.get(e.key,{}) for e in index.entities}
        # Only the reviewed editor's existing API fields gain their source labels;
        # this does not add fields, routes, controls or write authority.
        entities = tuple(replace(e, fields=tuple(replace(f, label=labels[e.key][f.key],
            aliases=tuple(dict.fromkeys((*f.aliases, f.label)))[:3])
            if f.key in labels[e.key] and f.kind == "text"
            and labels[e.key][f.key] != f.label else f for f in e.fields)) for e in index.entities)
        return replace(index, entities=entities)

    def _resolver(self, index):
        resolver = super()._resolver(index)
        entities = []
        for e in resolver.schema.entities:
            indexed = index.entity(e.key)
            fields = {f.key: f for f in indexed.fields} if indexed else {}
            entities.append(replace(e, fields=tuple(replace(f, label=fields[f.key].label,
                aliases=fields[f.key].aliases) if f.key in fields else f for f in e.fields)))
        resolver.schema = replace(resolver.schema, entities=tuple(entities))
        return resolver

    def capabilities(self):
        return super().capabilities() | {"fill", "handoffs"}

    def resolve(self, spec, *, context=None, current_path=""):
        indices = [i for i, d in enumerate(spec.deliverables) if d.kind == "handoff" and (d.value or d.field_values)]
        if not indices:
            return super().resolve(spec, context=context, current_path=current_path)
        altered = tuple(replace(d, kind="page", action="", value="", field_values=()) if i in indices else d
                        for i, d in enumerate(spec.deliverables))
        result = super().resolve(replace(spec, deliverables=altered), context=context, current_path=current_path)
        index = self.index(context=context)
        out = list(result.deliverables)
        for i in indices:
            d, resolved = spec.deliverables[i], out[i]
            entity = index.entity(d.entity)
            entry = next((e for e in self._entries if e[1] == d.entity and not d.record and d.action.casefold() in {"create", "new"} and spec.write_intent
                          and (not d.page.key or d.page.key == e[2])), None)
            component = entry[6] if entry else REVIEWED_FILES[1]
            form_source = self._form_source.read(component)
            fields = {f.reference for f in form_source.fields if not f.conditional and not f.handlers} if form_source else set()
            requested = d.field_values or ((d.action, d.value),)
            mapped = tuple((entity.field_by_label(label), value) for label, value in requested) if entity else ()
            page = resolved.page
            match = re.fullmatch(r"/crm/customer/detail/([1-9][0-9]{0,19})", page.url) if page else None
            if resolved.problem or resolved.denied or resolved.unoffered:
                out[i] = replace(resolved, kind="handoff")
            elif (entry and page and page.url == entry[2] and mapped
                    and len({f.key for f, _ in mapped if f}) == len(mapped) and all(
                    f is not None and f.key in fields for f, _ in mapped) and not (
                    d.section or d.via or d.metric or page.fill or page.highlight)):
                out[i] = replace(resolved, kind="handoff", page=replace(page,
                    form=FormPlan(entry[0],tuple((f.key,v) for f,v in mapped),open_entry=True)))
            elif ((d.field_values and d.action.casefold() not in {"", "edit", "update", "编辑", "修改"})
                  or not self._updates or d.entity != "crm/customer" or d.via or d.metric or not match or not mapped or len({field.key for field, _ in mapped if field}) != len(mapped) or any(not field or field.key not in fields for field, _ in mapped) or d.section or not d.record
                  or page.fill or page.highlight):
                out[i] = Resolved(kind="handoff", problem="this reviewed editor does not offer the requested field or target")
            else:
                out[i] = replace(resolved, kind="handoff", page=replace(page,
                    form=FormPlan("customer-update:"+match[1], tuple((field.key, value) for field, value in mapped), open_entry=True)))
        return replace(result, deliverables=tuple(out))
