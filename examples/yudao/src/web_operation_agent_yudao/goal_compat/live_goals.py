"""Goal handoffs and page conditions on what the entity's own page shows, under the method fence."""
from dataclasses import replace
import hashlib
import json
import re

from web_operation_agent.goal.data import DataRefused
from web_operation_agent.goal.ports import FormPlan, Resolved

from ..reads.live_filters import LivePageFilters
from ..writes.live_forms import LiveFormPort, LiveRequest, field_key
from .page_goals import NativePageGoalPort

_CREATE = frozenset({"", "create", "new", "add", "新增", "新建", "创建", "添加"})
_UPDATE = frozenset({"", "edit", "update", "编辑", "修改"})


def _field(entity, label: str):
    """The one index field a requested label names, if exactly one does."""
    wanted = "".join(label.casefold().split())
    matches = [f for f in (entity.fields if entity is not None else ())
               if wanted in {"".join(str(v).casefold().split()) for v in (f.key, f.label, *f.aliases)}]
    return matches[0] if len(matches) == 1 else None


def _names(entity, label: str) -> tuple[str, ...]:
    """The requested label first, then what the index calls the same field."""
    found = _field(entity, label)
    extra = (found.label, *found.aliases) if found is not None else ()
    return tuple(dict.fromkeys(name for name in (label, *extra) if name))


class LiveFormsGoalPort(NativePageGoalPort):
    """A requested change becomes a draft in the editor the page itself offers."""

    def _as_shown(self, entity, label, value):
        """A linked record given by its number is chosen on a form by its name: the form
        lists names. The name is read under the same account; anything else stays as given."""
        found = _field(entity, label) if isinstance(label, str) else None
        if (found is None or found.kind != "link" or not found.relation
                or not isinstance(value, str) or not value.isdigit()):
            return value
        try:
            return self.data.read(found.relation, value, ()).display or value
        except (DataRefused, LookupError, ValueError):
            return value

    def __init__(self, data, forms: LiveFormPort, **kwargs):
        if not isinstance(forms, LiveFormPort):
            raise TypeError("live goal handoffs require the live form port")
        super().__init__(data, **kwargs)
        self._forms = forms
        self.page_filters = LivePageFilters(self)

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
            requested = tuple((label, self._as_shown(entity, label, value))
                              for label, value in (d.field_values or ((d.action, d.value),)))
            action = d.action.casefold() if d.field_values else ""
            record = re.search(r"/([1-9][0-9]{0,19})$", page.url or "")
            # A record that has only its list's address is edited from its row there.
            listed = bool(d.record and not record and page.missing_record)
            # Under one of a record's tabs the request adds an entry of that tab's kind (a
            # follow-up, a contact): a new form, opened from the tab once it is shown.
            under_tab = bool(d.section and d.record and record and page.section)
            mode = "create" if under_tab else "update" if d.record and (record or listed) else "create"
            if ((d.section and not under_tab) or d.via or d.metric or page.fill or page.highlight
                    or (d.record and not record and not listed)
                    or not all(label and isinstance(value, str) for label, value in requested)
                    or action not in (_UPDATE if mode == "update" else _CREATE)
                    or len({field_key(label) for label, _ in requested}) != len(requested)):
                out[i] = Resolved(kind="handoff",
                                  problem="this page offers no editor the request can be prepared in")
                continue
            # One reference per exact request, so the reading the turn acts on finds its
            # own fields whichever reading was resolved last.
            digest = hashlib.sha256(json.dumps(requested, ensure_ascii=False).encode()).hexdigest()[:16]
            reference = (f"live:{mode}:{d.entity}:" + (record[1] if record else "row" if listed else "new")
                         + (":tab" if under_tab else "") + ":" + digest)
            named = (page.missing_record or d.record) if mode == "update" else ""
            title = page.section if under_tab else entity.label if entity is not None else ""
            self._forms.expect(LiveRequest(
                reference, mode, title,
                tuple((field_key(label), _names(entity, label)) for label, _ in requested),
                tuple((field_key(label), value) for label, value in requested), record=named))
            out[i] = replace(resolved, kind="handoff", page=replace(page, missing_record="", form=FormPlan(
                reference, tuple((field_key(label), value) for label, value in requested),
                open_entry=True, title=title[:200], record=named[:200])))
        return replace(result, deliverables=tuple(out))


__all__ = ["LiveFormsGoalPort"]
