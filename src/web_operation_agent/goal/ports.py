"""The goal port a goal-directed turn talks to, and what a resolution hands back.

The core owns the turn -- asking once, confirming page switches, dispatching generic page
commands, checking the outcome against the resolution, writing the reply -- and the
resolution itself (`goal.resolve`). The adapter owns the application's facts: its index,
its schema, the records it reads under the acting user, how a value is displayed, and
where a page or record lives (`goal.data`). `goal.resolve.service.DataGoalPort` is the
`GoalPort` over those facts; a test may stand in its own.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..state.identity import OperationContext
from .index import ApplicationIndex
from .spec import GoalSpec, Filter


@dataclass(frozen=True)
class Fact:
    subject: str
    label: str
    value: str
    #: Optional adapter schema key and exact coded choice read under this actor.
    #: Text/credentials are not copied into a second native-value channel.
    field_key: str = ""
    native_value: str = ""


@dataclass(frozen=True)
class Candidate:
    """A real value of the application offered to the model before it declares ("values"):
    a record, one of a record's lines, a linked record, a field. The core numbers them;
    `display` is the exact name a declaration is bound to."""
    #: record | line | link | field | choice | filter
    kind: str
    entity: str
    display: str
    #: What tells it apart from its neighbours: a date, an amount, a quantity.
    detail: str = ""
    id: str = ""
    #: The conditions this value stands for as (field label, exact value) pairs, when a
    #: name alone does not pick it out: one line of one record is its record and its
    #: item, since the same item is on many records' lines.
    binds: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Aggregate:
    subject: str
    count: int
    #: (label, displayed total) for each summed field.
    sums: tuple[tuple[str, str], ...] = ()
    #: (group name, count) for the groups reported.
    groups: tuple[tuple[str, int], ...] = ()
    #: Record display names when the person asked for a list.
    records: tuple[str, ...] = ()
    #: True when `count` is the whole scope, not a bounded page of it.
    complete: bool = True
    #: A qualification the reply must carry, e.g. the wider count when the scope is empty.
    note: str = ""
    #: (operation, field label, displayed value); None value means no known values.
    extreme: tuple[str, str, str | None] | None = None


@dataclass(frozen=True)
class FormPlan:
    """Exact server-resolved entry and field values for one manual-submit handoff.

    References belong to the application form port, not model or DOM selectors.
    The live descriptor must agree before any field is entered.
    """
    reference: str
    values: tuple[tuple[str, str], ...]
    open_entry: bool = False

    def __post_init__(self):
        if (not isinstance(self.open_entry, bool) or not isinstance(self.reference, str) or not self.reference.strip()
                or len(self.reference) > 300 or not isinstance(self.values, tuple)
                or not 1 <= len(self.values) <= 20
                or any(not isinstance(item, tuple) or len(item) != 2
                       or any(not isinstance(v, str) for v in item)
                       or not item[0] for item in self.values)
                or len({item[0] for item in self.values}) != len(self.values)):
            raise ValueError("form plan requires an entry and 1-20 unique field values")


@dataclass(frozen=True)
class PagePlan:
    """A page state to reach, in terms the runtime can act on and the core can check."""
    #: Same-origin path to open. Empty when the current page is meant.
    url: str
    #: How the application labels the destination, for the reply.
    label: str
    #: Path the page is expected to show once open; the URL may differ after redirects.
    expected_path: str = ""
    #: Expected rendered view name, when a view was asked for.
    view: str = ""
    facets_on: tuple[str, ...] = ()
    facets_off: tuple[str, ...] = ()
    #: Tab to select on the record's page, by its visible name.
    section: str = ""
    #: Control to highlight and leave to the person, by its visible name.
    highlight: str = ""
    #: What pressing that control does, when the adapter knows.
    highlight_effect: str = ""
    #: A view the person asked for that this page does not offer; the page opens in
    #: its own view and the reply says the asked-for one is not available.
    missing_view: str = ""
    #: A value to put into the highlighted field before handing over: what to type or
    #: choose, how ("text" typed, "choice" picked from a native select by option value,
    #: "link" typed then picked from the field's own suggestions), and how the page
    #: labels it. The person saves; the assistant never submits.
    fill: str = ""
    fill_kind: str = ""
    fill_label: str = ""
    #: The application's own identifier for that field, as the page exposes it on the
    #: input (a form's inputs are often unlabelled in the accessible tree; their field
    #: identifier is what tells them apart).
    fill_field: str = ""
    #: Opt-in application-form contract; supersedes the legacy single-field fill.
    form: FormPlan | None = None
    #: Require the application's rendered main-page identity to match expected_path.
    #: Header controls and the address alone cannot verify such a destination.
    requires_page_route: bool = False
    conditions: tuple[Filter, ...] = ()
    #: The page of the list to show (a number, or a step from the one shown) and the rows
    #: per page; 0 leaves each as it is. Settled on the page's own pagination controls.
    page_number: int = 0
    page_step: int = 0
    page_size: int = 0
    #: The record has only a collection address; opening it cannot settle a record
    #: request or authorize actions on a particular row.
    missing_record: str = ""

    def __post_init__(self):
        if not isinstance(self.requires_page_route, bool):
            raise ValueError("requires_page_route must be a boolean")
        if self.requires_page_route and not self.expected_path:
            raise ValueError("a required page route needs an expected path")
        if self.missing_record and any((self.section, self.highlight, self.form, self.fill,
                                       self.facets_on, self.facets_off, self.conditions)):
            raise ValueError("a collection fallback cannot perform record actions")


@dataclass(frozen=True)
class Resolved:
    """One deliverable after resolution. Exactly one of the payloads is present."""
    kind: str
    facts: tuple[Fact, ...] = ()
    aggregate: Aggregate | None = None
    page: PagePlan | None = None
    #: Why this deliverable could not be resolved: not found, ambiguous, unsupported.
    problem: str = ""
    #: The application's own sentence for a read it refused, when that sentence named
    #: what it needs ("请选择时间范围"): the reply says it, so the person knows what the
    #: page asks for before it shows anything.
    refused: str = ""
    #: Typed refused/unavailable read; never inferred from exception prose.
    read_failure: str = ""
    #: Requested operation and labels whose result the failed read cannot establish.
    read_operation: str = ""
    read_fields: tuple[str, ...] = ()
    #: Requested labels absent from the actor-visible schema; never guessed from errors.
    missing_fields: tuple[str, ...] = ()
    #: Explicit indexed count grouping omitted or replaced by the declaration.
    missing_group: str = ""
    #: Form fields lacking a supplied value or an explicit clearing instruction.
    input_fields: tuple[str, ...] = ()
    #: Candidate names when a reference was ambiguous, so the reply can ask.
    candidates: tuple[str, ...] = ()
    #: The reference the person gave that names no record here ("S00998"), so the
    #: reply can say plainly that it was not found, in the person's language.
    missing: str = ""
    #: Actor-scoped conditions that found no record; not an unavailable capability.
    no_matches: str = ""
    #: An operation this account may not perform on what was asked ("delete",
    #: "create", "write"), as the application's own access rules say: the reply says so
    #: rather than ask which record, since none of them could be acted on.
    denied: str = ""
    #: The optional capabilities (names in `OPTIONAL`) this deliverable needed and the
    #: port does not offer; set by the core, so the reply can say so in the person's words.
    unoffered: tuple[str, ...] = ()
    #: What the adapter would tell the declarer so that a second declaration could
    #: succeed: the fields an entity does have, the records a description matched.
    #: Empty when the problem is not one a different declaration would fix.
    repair: str = ""
    #: Entities (keys) where the name that no record of the declared entity has is one
    #: record's, so that a repair can be declared on them.
    elsewhere: tuple[str, ...] = ()
    #: The facts asked for that the record holds no value in (their labels): a fact
    #: all of whose fields are empty has not answered the question yet.
    empty: tuple[str, ...] = ()
    #: Source citations backing the facts (`path:start-end@release sha256:<hex>`), each
    #: rendered verbatim in the reply as a rule of the deployment's source, not data.
    citations: tuple[str, ...] = ()


@dataclass(frozen=True)
class Resolution:
    deliverables: tuple[Resolved, ...]
    #: Reads performed, for the audit record: (operation, target) pairs, no values.
    reads: tuple[tuple[str, str], ...] = ()


#: What every adapter must provide for the goal turn to answer questions and open
#: pages: an index of entities, fields and pages; records found by name; field values;
#: counts and lists with conditions; the address of a page or record. These have no
#: capability name because they cannot be absent.
REQUIRED = ("index", "records", "facts", "aggregates", "pages")
#: What an adapter may add, each named so that the core can say honestly what this
#: application does not offer rather than fail: page filters and groups ("facets"), a list's page and page size ("paging"),
#: tabs on a record's page ("sections"), the lines a record's form embeds ("lines"),
#: business controls to point at ("handoffs"), entering a value before handing over
#: ("fill"), free text searched on a page ("search"), rules compiled from the
#: deployment's source ("source"), and real values offered before the declaration --
#: a named record's lines, a named partner's documents -- through
#: `candidates(entities=, known=, context=)` ("values").
OPTIONAL = frozenset({"facets", "paging", "sections", "lines", "handoffs", "fill", "search", "source",
                      "values"})


class GoalPort(Protocol):
    """Read-only by construction; `DataGoalPort` is the implementation over an adapter's data.

    The contract has two levels. The required level is `index` and `resolve` for facts,
    aggregates, lists and pages of named records. The optional level is declared by
    `capabilities()`, a subset of `OPTIONAL`; a port without the method offers none of
    them, and the core answers a deliverable that needs a missing one with a plain
    statement instead of asking the port.
    """

    def index(self, *, context: OperationContext | None = None) -> ApplicationIndex: ...

    def resolve(self, spec: GoalSpec, *, context: OperationContext | None = None,
                current_path: str = "") -> Resolution: ...


def capabilities_of(port) -> frozenset[str]:
    """The optional capabilities a port declares, restricted to the known names."""
    method = getattr(port, "capabilities", None)
    if not callable(method):
        return frozenset()
    return frozenset(str(name) for name in method()) & OPTIONAL


__all__ = ["Aggregate", "Candidate", "Fact", "FormPlan", "GoalPort", "OPTIONAL", "PagePlan", "REQUIRED", "Resolution",
           "Resolved", "capabilities_of"]
