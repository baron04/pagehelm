"""Goal handoffs and page conditions on what the entity's own page shows, under the method fence."""
from dataclasses import replace
import hashlib
import json
import re

from web_operation_agent.goal.ports import FormPlan, Resolved

from .live_filters import LivePageFilters
from .live_forms import LiveFormPort, LiveRequest, field_key
from .form_goals import NativeFormsGoalPort
from .form_entries import REVIEWED_FILES

_CREATE = frozenset({"", "create", "new", "add", "新增", "新建", "创建", "添加"})
_UPDATE = frozenset({"", "edit", "update", "编辑", "修改"})


def _names(entity, label: str) -> tuple[str, ...]:
    """The requested label first, then what the index calls the same field."""
    wanted = "".join(label.casefold().split())
    matches = [f for f in (entity.fields if entity is not None else ())
               if wanted in {"".join(str(v).casefold().split()) for v in (f.key, f.label, *f.aliases)}]
    extra = (matches[0].label, *matches[0].aliases) if len(matches) == 1 else ()
    return tuple(dict.fromkeys(name for name in (label, *extra) if name))


class LiveFormsGoalPort(NativeFormsGoalPort):
    """A requested change becomes a draft in the editor the page itself offers."""

    def __init__(self, data, forms: LiveFormPort, **kwargs):
        if not isinstance(forms, LiveFormPort):
            raise TypeError("live goal handoffs require the live form port")
        reviewed = forms._reviewed
        super().__init__(data, reviewed.goal_source, entries=reviewed.goal_entries,
                         updates=reviewed.goal_updates, **kwargs)
        self._forms = forms
        self.page_filters = LivePageFilters(self)

    def index(self, *, context=None):
        index = super().index(context=context)
        components = [(entity, component) for _, entity, _, _, _, _, component in self._entries]
        if self._updates:
            components.append(("crm/customer", REVIEWED_FILES[1]))
        labels = {}
        for entity, component in components:
            form = self._form_source.read(component)
            for field in form.fields if form else ():
                labels.setdefault((entity, field.reference), set()).add(field.label)
        # An exact application field key, not a shared suffix, establishes this alias.
        # Keep the reviewed label for link/choice fields too; the base port labels text.
        def aliases(entity, field):
            names = labels.get((entity.key, field.key), set())
            if len(names) != 1:
                return field
            label = next(iter(names))
            return replace(field, aliases=tuple(dict.fromkeys(
                (label, *field.aliases)))[:3]) if label != field.label else field
        return replace(index, entities=tuple(replace(entity, fields=tuple(
            aliases(entity, field) for field in entity.fields)) for entity in index.entities))

    def capabilities(self):
        return super().capabilities() | {"fill", "handoffs"}

    def resolve(self, spec, *, context=None, current_path=""):
        indices = [i for i, d in enumerate(spec.deliverables)
                   if d.kind == "handoff" and (d.value or d.field_values)]
        if not indices:
            return super().resolve(spec, context=context, current_path=current_path)
        altered = tuple(replace(d, kind="page", action="", value="", field_values=()) if i in indices else d
                        for i, d in enumerate(spec.deliverables))
        result = super().resolve(replace(spec, deliverables=altered), context=context,
                                 current_path=current_path)
        index = self.index(context=context)
        out = list(result.deliverables)
        for i in indices:
            d, resolved = spec.deliverables[i], out[i]
            page = resolved.page
            if resolved.problem or resolved.denied or resolved.unoffered or page is None:
                out[i] = replace(resolved, kind="handoff")
                continue
            entity = index.entity(d.entity)
            requested = d.field_values or ((d.action, d.value),)
            action = d.action.casefold() if d.field_values else ""
            record = re.search(r"/([1-9][0-9]{0,19})$", page.url or "")
            mode = "update" if d.record and record else "create"
            if (d.section or d.via or d.metric or page.fill or page.highlight
                    or (d.record and not record) or not all(label and isinstance(value, str)
                                                            for label, value in requested)
                    or action not in (_UPDATE if mode == "update" else _CREATE)
                    or len({field_key(label) for label, _ in requested}) != len(requested)):
                out[i] = Resolved(kind="handoff",
                                  problem="this page offers no editor the request can be prepared in")
                continue
            # One reference per exact request, so the reading the turn acts on finds its
            # own fields whichever reading was resolved last.
            digest = hashlib.sha256(json.dumps(requested, ensure_ascii=False).encode()).hexdigest()[:16]
            reference = (f"live:{mode}:{d.entity}:" + (record[1] if mode == "update" else "new")
                         + ":" + digest)
            request = LiveRequest(
                reference, mode, entity.label if entity is not None else "",
                tuple((field_key(label), _names(entity, label)) for label, _ in requested),
                tuple((field_key(label), value) for label, value in requested),
                entity=d.entity, route=page.url, record_id=record[1] if mode == "update" else "")
            if not self._forms.entry_reference(request):
                out[i] = Resolved(kind="handoff", problem="this target has no reviewed transient editor")
                continue
            self._forms.expect(request)
            out[i] = replace(resolved, kind="handoff", page=replace(page, form=FormPlan(
                reference, tuple((field_key(label), value) for label, value in requested),
                open_entry=True)))
        return replace(result, deliverables=tuple(out))


__all__ = ["LiveFormsGoalPort"]
