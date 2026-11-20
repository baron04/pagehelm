"""Native deployment arrival verification over the existing neutral goal port."""
from dataclasses import replace
import re
from urllib.parse import urlsplit

from web_operation_agent.goal.resolve.service import DataGoalPort
from web_operation_agent.goal.data import DataRefused
from web_operation_agent.goal.scoped import ScopedData
from web_operation_agent.goal.ports import Candidate, Resolution


class NativePageGoalPort(DataGoalPort):
    """A native page is reached only when its controller proves the live mount.

    This opts the deployment into the existing PagePlan observation contract;
    it grants no navigation, form, tab or API capability.
    """

    page_reader = None
    page_filters = None
    allow_updates = False

    def write_route_capability(self):
        fn = getattr(self.data, "write_route_capability", None)
        if callable(fn):
            return fn()
        from ..writes.convention_writes import YUDAO_WRITE_ROUTE_CAPABILITY

        return YUDAO_WRITE_ROUTE_CAPABILITY

    def capabilities(self):
        # Paging is settled on the page's own pagination controls and list reads, which the
        # page integration reports for every list; it needs no review of its own.
        return (
            super().capabilities()
            | {"paging"}
            | ({"updates"} if self.allow_updates else set())
            | ({"facets"} if self.page_filters is not None else set())
        )

    def prepare_page_filters(self, plan, observation, *, context):
        from web_operation_agent.business.errors import BusinessToolRequestError
        if self.page_filters is None:
            raise BusinessToolRequestError("page query controls are not enabled")
        from ..reads.client import YudaoReadRefused
        from web_operation_agent.business.errors import BusinessToolPermissionError, BusinessToolUnavailableError
        try:
            return self.page_filters.prepare(plan, observation, context=context)
        except YudaoReadRefused as exc:
            if exc.permission_denied:
                raise BusinessToolPermissionError("the application refused the query read") from exc
            raise BusinessToolUnavailableError("the application query read failed") from exc
        except (OSError, TimeoutError) as exc:
            raise BusinessToolUnavailableError("the application query read is unavailable") from exc

    def candidates(self, *, entities=(), known=(), dates=(), context=None):
        """Prefer an actor-read current record within the existing values capability.

        Route identity only selects a read; it cannot establish the display name.
        The host validates the active document lease for both snapshots. Ordinary
        known-record candidates remain available on unqualified/list pages.
        """
        ordinary = super().candidates(entities=entities, known=known, dates=dates, context=context)
        if self.page_reader is None or "values" not in self.capabilities():
            return ordinary
        observation = self.page_reader(context)
        observed_epoch, observed_digest = observation.epoch, observation.content_digest
        path = urlsplit(observation.url).path
        if (observation.render_state != "ready" or observation.record_route != path
                or observation.page_route != path):
            return ordinary
        # Menu revocation is checked by the current actor's index, not by the URL.
        index = self.index(context=context)
        possible = []
        for page in index.pages:
            if page.entity not in entities:
                continue
            template = self.data._ui.records.get(page.key, "")
            if template.count("{id}") != 1:
                continue
            match = re.fullmatch(re.escape(template).replace(r"\{id\}", r"([0-9]+)"), path)
            if match:
                possible.append((page.entity, match.group(1)))
        possible = tuple(dict.fromkeys(possible))
        if len(possible) != 1:
            return ordinary
        entity, identifier = possible[0]
        row = self.data.read(entity, identifier, ())
        fresh = self.page_reader(context)
        if (row.id != identifier or not row.display
                or self.data.record_address(entity, row.id) != path
                or fresh.epoch != observed_epoch
                or fresh.content_digest != observed_digest):
            return ordinary
        reference = f"{row.display}（ID={row.id}）"
        current = Candidate(kind="record", entity=entity, display=reference,
                            detail="Currently rendered record; name read under the active actor. "
                                   "Use this candidate for current-record references, not the URL identifier.")
        return (current, *(c for c in ordinary if (c.entity, c.display) != (entity, reference)))

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
