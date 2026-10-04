"""Native deployment arrival verification over the existing neutral goal port."""
from dataclasses import replace

from web_operation_agent.goal.resolve.service import DataGoalPort
from web_operation_agent.goal.data import DataRefused
from web_operation_agent.goal.scoped import ScopedData
from web_operation_agent.goal.ports import Resolution


class NativePageGoalPort(DataGoalPort):
    """A native page is reached only when its controller proves the live mount.

    This opts the deployment into the existing PagePlan observation contract;
    it grants no navigation, form, tab or API capability.
    """

    page_reader = None

    def requires_page_reading(self, spec, *, current_path=""):
        page = self.data.schema().page(current_path)
        entity = page.entity if page else ""
        info = self.data._entities.get(entity)
        return bool(info and "id" not in info.props and info.required and any(
            d.entity == entity and d.kind in {"fact", "list", "aggregate"} for d in spec.deliverables))

    def resolve(self, spec, *, context=None, current_path=""):
        result = (self._resolve_scoped(spec, context=context, current_path=current_path)
                  if self.page_reader is not None else
                  super().resolve(spec, context=context, current_path=current_path))
        return replace(result, deliverables=tuple(
            replace(item, page=replace(item.page, requires_page_route=True))
            if item.page is not None else item for item in result.deliverables))

    def _resolve_scoped(self, spec, *, context=None, current_path=""):
        resolver = self._resolver(self.index(context=context))
        schema = resolver.schema
        page = schema.page(current_path)
        entity = page.entity if page else ""
        scoped_indices = [i for i, d in enumerate(spec.deliverables)
                          if d.entity == entity and d.kind in {"fact", "list", "aggregate"}]
        needs_scope = self.requires_page_reading(spec, current_path=current_path)
        failure = None
        if needs_scope and self.page_reader is not None:
            try:
                observation = self.page_reader(context)
                scope = self.data.scope_from_page(entity, observation, current_path=current_path)
                resolver.port = ScopedData(resolver.port, {entity: scope})
            except DataRefused as exc:
                failure = exc
        if failure is None:
            result = resolver.resolve(spec)
        else:
            # Preserve unrelated answers; never turn a failed scoped read into an
            # unscoped fallback. The core supplies the typed failure projection.
            indices = scoped_indices
            partial = resolver.resolve(replace(spec, deliverables=tuple(
                d for i, d in enumerate(spec.deliverables) if i not in indices)))
            results = iter(partial.deliverables)
            result = Resolution(tuple(resolver._refused_read(d, failure) if i in indices else next(results)
                                      for i, d in enumerate(spec.deliverables)), partial.reads)
        return result
