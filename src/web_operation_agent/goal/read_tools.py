"""Same-agent, actor-bound readonly access to the existing deterministic resolver."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import date
from typing import Callable

from ..business.errors import (BusinessToolContractError, BusinessToolIdentityError,
                               BusinessToolPermissionError, BusinessToolRequestError,
                               BusinessToolUnavailableError)
from ..business.runtime import BusinessToolResult
from ..state.identity import OperationContext, PrincipalError
from .coverage import missing_group
from .data import DataRefused, Group, Ref, Row
from .index import ApplicationIndex, conventions_named
from .narrow import candidate_entities
from .resolve.read_evidence import ReadEvidence, validate_number
from .resolve.resolver import Resolver
from .resolve.service import GoalData
from .ports import capabilities_of
from .read_schemas import DATA_READ_SCHEMAS, SIMPLE_DATA_READ_SCHEMAS
from .read_simple import SimpleReadResolver, bind_read, compile_read
from .spec import GoalSpec

DATA_READ_NAMES = frozenset({"inspect_data", "resolve_data"})
SIMPLE_DATA_READ_NAMES = frozenset({"inspect_data", "read_data"})
_DELIVERABLE_KEYS = {"kind", "entity", "record", "via", "fields", "metric", "page",
                     "sort", "descending", "limit"}
_METRIC_KEYS = {"op", "field", "filters", "page_filters", "group_by", "top", "total"}
_FILTER_KEYS = {"field", "values", "op"}


def _object(value, allowed, name):
    if not isinstance(value, dict) or set(value) - allowed:
        raise BusinessToolRequestError(f"{name} contains unsupported properties")


def _declarations(args):
    _object(args, {"deliverables"}, "resolve_data")
    raw = args.get("deliverables")
    if not isinstance(raw, list) or not 1 <= len(raw) <= 4:
        raise BusinessToolRequestError("resolve_data needs one to four readonly deliverables")
    for item in raw:
        _object(item, _DELIVERABLE_KEYS, "deliverable")
        if item.get("kind") not in {"fact", "list", "aggregate"}:
            raise BusinessToolRequestError("resolve_data offers facts, lists and aggregates only")
        if "page" in item:
            _object(item["page"], {"key", "search"}, "readonly page scope")
        if "descending" in item and not isinstance(item["descending"], bool):
            raise BusinessToolRequestError("descending must be boolean")
        if "metric" in item:
            metric = item["metric"]
            _object(metric, _METRIC_KEYS, "metric")
            if "total" in metric and not isinstance(metric["total"], bool):
                raise BusinessToolRequestError("total must be boolean")
            filters = metric.get("filters", [])
            if not isinstance(filters, list):
                raise BusinessToolRequestError("filters must be an array")
            for condition in filters:
                _object(condition, _FILTER_KEYS, "condition")
    try:
        spec = GoalSpec.from_json(args)
    except (ValueError, TypeError) as exc:
        raise BusinessToolRequestError(str(exc)) from exc
    for i, item in enumerate(spec.deliverables):
        if item.via and not item.record and not (item.kind == "fact" and i > 0
                and spec.deliverables[i-1].kind == "list"):
            raise BusinessToolRequestError("via names a link from a named record or the preceding "
                "list, not a customer/owner restriction. Use metric.filters with the indexed "
                "owner/link field and its exact record name. No scope was read or broadened.")
        if item.kind == "list" and item.metric and item.metric.op == "sum":
            raise BusinessToolRequestError("a list does not deliver a sum; declare a separate "
                "aggregate with op=sum and the same scope, or compute on a complete list")
    return spec


@dataclass(frozen=True)
class DataReadTools:
    """Opt-in deployment binding, not an application resolver or model runner.

    ``data_for(context, checkpoint)`` returns a readonly GoalData bound to this operation's actual
    delegated actor and immutable credential identity. The application owns this
    verification, including checking credential renewal during each read. The core
    checks principal, scope, cancellation and deadline around every port call.

    ``surface="simple"`` exposes ``inspect_data`` and one-operation ``read_data``.
    The default declaration surface preserves ``resolve_data`` callers. Hosts must
    select this instance's ``names`` and ``schemas`` together.
    """

    data_for: Callable[[OperationContext, Callable[[], None]], GoalData]
    application_id: str
    required_scope: str
    today: date | None = None
    surface: str = "declaration"

    def __post_init__(self):
        if (not callable(self.data_for) or not isinstance(self.application_id, str)
                or not self.application_id or not isinstance(self.required_scope, str)
                or not self.required_scope):
            raise ValueError("data reads require an application, scope and actor-bound factory")
        if self.today is not None and type(self.today) is not date:
            raise TypeError("today must be a date or None")
        if not isinstance(self.surface, str) or self.surface not in {"declaration", "simple"}:
            raise ValueError("data read surface must be declaration or simple")

    @property
    def names(self):
        return SIMPLE_DATA_READ_NAMES if self.surface == "simple" else DATA_READ_NAMES

    @property
    def schemas(self):
        return SIMPLE_DATA_READ_SCHEMAS if self.surface == "simple" else DATA_READ_SCHEMAS

    def execute(self, name, args, *, context, checkpoint, user_request):
        try:
            return self._execute(name, args, context=context, checkpoint=checkpoint,
                                 user_request=user_request)
        except PrincipalError as exc:
            raise BusinessToolIdentityError("the application data actor binding is invalid") from exc
        except DataRefused as exc:
            if exc.permission_denied:
                raise BusinessToolPermissionError("the application denied this data read") from exc
            raise BusinessToolUnavailableError("the application data read is unavailable") from exc

    def _execute(self, name, args, *, context, checkpoint, user_request):
        if name not in self.names:
            raise BusinessToolRequestError("unknown deterministic data tool")
        if not isinstance(context, OperationContext):
            raise BusinessToolIdentityError("data reads require a verified operation identity")

        def check():
            checkpoint()
            try:
                context.require_live(application_id=self.application_id)
            except PrincipalError as exc:
                raise BusinessToolIdentityError("the data operation identity is no longer valid") from exc
            if self.required_scope not in context.principal.scopes:
                raise BusinessToolPermissionError("this actor lacks the data read scope",
                    layer="tool_authorization", required_scope=self.required_scope)

        # Validate the declaration before application dispatch. Metadata never grants
        # navigation, form entry, writes, source access or extra query capability.
        spec = (_declarations(args) if name == "resolve_data" else
                compile_read(args) if name == "read_data" else None)
        if name == "inspect_data":
            _object(args, {"query", "entity"}, name)
            if (not isinstance(args.get("query"), str) or not args["query"].strip()
                    or len(args["query"]) > 500 or not isinstance(args.get("entity", ""), str)
                    or len(args.get("entity", "")) > 120):
                raise BusinessToolRequestError("inspect_data needs a bounded query and optional entity key")
        check()
        data = _CheckedData(self.data_for(context, check), check)
        check()
        index = data.index(context=context)
        if not isinstance(index, ApplicationIndex):
            raise BusinessToolContractError("the application returned an invalid data index")
        if name == "inspect_data":
            content = _inspect(index, args, read_tool="read_data" if self.surface == "simple" else "resolve_data")
            proof = ()
            reads = ()
        else:
            spec = replace(spec, conventions=(() if name == "read_data" else
                           conventions_named(index.conventions, user_request)),
                           language="zh" if any("\u3400" <= c <= "\u9fff" for c in user_request) else "en")
            offered = capabilities_of(data)
            for d in spec.deliverables:
                needs = ({"facets"} if d.metric and d.metric.page_filters else set())
                needs |= {"search"} if d.page.search else set()
                if needs - offered:
                    raise BusinessToolRequestError("the application does not offer " +
                                                   ", ".join(sorted(needs - offered)))
                if d.entity and index.entity(d.entity) is None:
                    raise BusinessToolRequestError("the entity is absent from this actor's index")
                if d.page.key and index.page(d.page.key) is None:
                    raise BusinessToolRequestError("the query page is absent from this actor's index")
                if missing_group(d, user_request, index):
                    raise BusinessToolRequestError("the request's explicit count grouping was not declared")
            evidence = ReadEvidence(context.binding_digest)
            schema = data.schema()
            if name == "read_data":
                spec = bind_read(spec, index, schema)
            resolver_type = SimpleReadResolver if name == "read_data" else Resolver
            resolver = resolver_type(data, index, schema, today=self.today, evidence=evidence)
            resolution = resolver.resolve(spec)
            content = {"deliverables": [{
                "kind": result.kind, "problem": result.problem, "repair": result.repair,
                "missing_fields": list(result.missing_fields), "read_failure": result.read_failure,
                "no_matches": result.no_matches, "choices": list(result.candidates),
                "evidence_refs": list(refs),
                "read_succeeded": bool(refs) and not (result.problem or result.read_failure),
            } for result, refs in zip(resolution.deliverables, evidence.refs, strict=True)],
                "evidence": [_projection(item) for item in evidence.items],
                "instructions": "Result-local data# refs are mapped to turn-owned refs in the appended evidence index; cite those. "
                    "Cite typed evidence. A query scope is not proof that it answers the raw user request. "
                    "Incomplete lists/groups and unread fields cannot prove full completion. "
                    "Data reads grant no page action, navigation route or form authority."}
            proof = tuple(evidence.items)
            reads = resolution.reads
        check()
        try:
            return BusinessToolResult(content=content, evidence=proof,
                audit={"identity": context.audit_projection(), "tool": name,
                       "reads": [list(r) for r in reads], "index_complete": index.complete},
                data_classes=("business.metadata",) if name == "inspect_data" else ("business.records",))
        except (ValueError, TypeError) as exc:
            raise BusinessToolContractError("the data projection is too large or invalid; narrow the query") from exc


class _CheckedData:
    """Every deterministic subread uses the turn's existing deadline and principal."""
    def __init__(self, data, check):
        self._data, self._check = data, check

    def __getattr__(self, name):
        method = getattr(self._data, name)
        if not callable(method):
            return method
        def call(*args, **kwargs):
            self._check()
            try:
                value = method(*args, **kwargs)
            except DataRefused as exc:
                self._check()
                if exc.permission_denied:
                    raise BusinessToolPermissionError("the application denied this data read",
                                                       layer="application") from exc
                raise
            self._check()
            if name == "count" and (type(value) is not int or value < 0):
                raise BusinessToolContractError("the application returned an invalid count")
            if name == "total":
                if not isinstance(value, tuple) or len(value) != 2:
                    raise BusinessToolContractError("the application returned an invalid total")
                validate_number(value[0])
                if not isinstance(value[1], str) or value[1] == "unknown-currency":
                    raise BusinessToolContractError("the application did not bind the aggregate unit")
            if name in ("rows", "find", "groups"):
                cls = {"rows": Row, "find": Ref, "groups": Group}[name]
                if not isinstance(value, tuple) or any(not isinstance(r, cls) for r in value):
                    raise BusinessToolContractError("the application returned an invalid collection")
                ids = [r.key if name == "groups" else r.id for r in value]
                if len(set(ids)) != len(ids) or any(not isinstance(i, str) for i in ids):
                    raise BusinessToolContractError("the application returned duplicate or invalid read identities")
                if name != "groups" and any(not i or i == "None" for i in ids):
                    raise BusinessToolContractError("the application returned missing read identities")
                if name == "groups" and any(type(r.count) is not int or r.count < 0 for r in value):
                    raise BusinessToolContractError("the application returned invalid group counts")
                if len(value) > kwargs.get("limit", 80 if name == "rows" else 12):
                    raise BusinessToolContractError("the application exceeded the requested collection limit")
            if name == "read" and (not isinstance(value, Row) or value.id != args[1]):
                raise BusinessToolContractError("the application returned another record")
            return value
        return call


def _inspect(index, args, *, read_tool="resolve_data"):
    entity_key = args.get("entity", "")
    if entity_key:
        entity = index.entity(entity_key)
        if entity is None:
            raise BusinessToolRequestError("the entity is absent from this actor's index")
        entities = (entity,)
    else:
        entities = candidate_entities(index, args["query"], ())[:6]
    out = []
    for entity in entities:
        fields = (*entity.fields, *entity.held)
        pages = [p for p in index.pages if p.entity == entity.key]
        out.append({"key": entity.key, "label": entity.label, "aliases": list(entity.aliases),
            "fields": [{"key": f.key, "label": f.label, "kind": f.kind, "relation": f.relation,
                "aliases": list(f.aliases), "choices": list(f.choices[:20]),
                "choices_complete": len(f.choices) <= 20} for f in fields[:40]],
            "fields_complete": len(fields) <= 40,
            "pages": [{"key": p.key, "label": p.label, "scope": list(p.scope),
                       "filters": list(p.filters), "groups": list(p.groups)} for p in pages[:12]],
            "pages_complete": len(pages) <= 12})
    return {"entities": out, "index_complete": index.complete,
            "total_entities": len(index.entities), "shown_entities": len(out),
            "sample_complete": len(out) == len(index.entities),
            "instructions": "Metadata only; this is a ranked bounded sample, not business data or proof of absence. "
                f"Use entity keys and indexed field labels in {read_tool}; page keys select query scope only. "
                "Ask again for a focused entity; omitted vocabulary is not an unsupported capability."}


def _projection(item):
    # Include actual typed values in the content digest used for repeated-read detection.
    # Per-call timestamps and operation identities belong in audit/evidence, not that digest.
    row = asdict(item)
    row.pop("origin")
    row["kind"] = type(item).__name__
    return row
