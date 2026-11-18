"""The part a real integration writes: ports, typed tools, and Skill artifacts.

This is the file to copy. It implements the ports `ApplicationPorts` asks for -- identity,
route validation, current forms, postconditions, reconciliation and data classification -- and
exposes one read and one write over the application's own data, each with the Skill that
tells the model when it applies.

Nothing here knows about the Agent, the Runtime, or the model. That is the whole point of
the SPI: an application author describes their application, and the framework decides what
to do with it.

**The write is the part worth reading.** It used to be absent, and an example with only a
read left every author to invent the hard half for themselves. The chain is five steps and
each one exists because of a specific way writes go wrong:

1. `prepare_mutation` finds exactly one target over a scan it knows finished, and refuses
   a duplicate. A write aimed at the wrong record, or at a record that already exists, is
   not something a later check can undo.
2. It states an exact effect and exact targets. That text is what the user approves; a
   proposal that said "create a contact" would be approval of anything.
3. The framework shows it, takes one approval, and consumes it once.
4. `execute_mutation` re-checks the duplicate immediately before dispatch, closing the
   ordinary time-of-check gap without ever retrying the write itself.
5. It reads the record back **under the same actor** and compares it. Anything else --
   a create with no id, a readback that fails or disagrees -- raises
   `BusinessMutationIndeterminate`, which the framework records and never retries.

The identity port is real for the same reason: `AgentTurnConfig` refuses to enable business
mutations without a verified session identity, and it is right to. Two credentials resolve
independently and must agree, which is the smallest honest version of what two applications
both do.
"""
from __future__ import annotations

import json
import hashlib
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from datetime import datetime, timezone

from web_operation_agent.execution.events import observed_interval, turn_interval

from web_operation_agent.composition.descriptor import (
    APPLICATION_ADAPTER_VERSION,
    ApplicationDescriptor,
)
from web_operation_agent.composition.ports import (
    ApplicationPorts,
    DeclaredDataClassifier,
    DeclaredRoutePort,
    MutationReconciliation,
    ResultPostconditionVerifier,
)
from web_operation_agent.business.runtime import (
    BusinessMutationIndeterminate,
    BusinessMutationSpec,
    BusinessSkill,
    BusinessToolAuthenticationError,
    BusinessToolError,
    BusinessToolPermissionError,
    BusinessToolRequestError,
    BusinessToolResult,
    BusinessToolRuntime,
    business_text_key,
    normalize_business_text,
)
from web_operation_agent.business.forms import (
    ApplicationFormDescriptor,
    ApplicationFormField,
    ApplicationFormValue,
    FormChoice,
)
from web_operation_agent.business.tools.contracts import FormDraft, FormDraftField, ToolOperation
from web_operation_agent.business.errors import BusinessToolRequestError
from web_operation_agent.business.entities import (
    DESCRIBE_ENTITY_TOOL,
    DISCOVER_ENTITIES_TOOL,
    ENTITY_READ_SCHEMAS,
    ENTITY_READ_TOOLS,
    QUERY_ENTITY_RECORDS_TOOL,
    RESOLVE_RECORD_REFERENCE_SCHEMA,
    RESOLVE_RECORD_REFERENCE_TOOL,
    EntityCatalog,
    EntityCell,
    EntityDescriptor,
    EntityField,
    EntityPageHint,
    EntityQuery,
    EntityQueryPage,
    EntityReadTools,
    EntityRecord,
    ReferenceLookupTools,
    ReferenceMatch,
)
from web_operation_agent.business.page_entries import (
    FIND_PAGE_ENTRIES_SCHEMA, FIND_PAGE_ENTRIES_TOOL,
    PageEntry, PageEntryCatalog, PageEntryTools, PagePart)
from web_operation_agent.business.goal_candidates import (
    GoalCandidateTools, RESOLVE_GOAL_CANDIDATES_SCHEMA,
    RESOLVE_GOAL_CANDIDATES_TOOL)
from web_operation_agent.state.identity import (IdentityAssertion, PrincipalContext,
                                          PrincipalError, PRINCIPAL_CONTEXT_VERSION,
                                          bind_matching_identity)
from web_operation_agent.answer.evidence import Origin, QueryEvidence, RecordEvidence


#: Skills are artifacts, not literals. A real integration ships one file per Skill and
#: version-controls it alongside the application it describes; loading through
#: `BusinessSkill.from_bytes` is what validates its shape and its tool references.
SKILLS = Path(__file__).parent / "skills"
APPLICATION_ID = "minimal.contacts"
CREATE_CONTACT = "create_contact"
#: What a write needs the principal to carry. Declared here because scopes are the
#: application's vocabulary, not the framework's.
SCOPES = {
    "list_contacts": "contacts:read",
    **{name: "contacts:read" for name in ENTITY_READ_TOOLS},
    RESOLVE_RECORD_REFERENCE_TOOL: "contacts:read",
    FIND_PAGE_ENTRIES_TOOL: "contacts:read",
    RESOLVE_GOAL_CANDIDATES_TOOL: "contacts:read",
    CREATE_CONTACT: "contacts:write",
}
# The application already owns this permission.  Prefilling does not persist, but the form
# is useful only to an actor who may finish the create themselves; inventing a framework
# scope would bypass the application's authority vocabulary.
GENERIC_FORM_SCOPE = "contacts:write"
_READ_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_contacts",
        "description": "Read current contacts from the delegated synthetic workspace.",
        "parameters": {
            "type": "object",
            "properties": {
                "name_contains": {
                    "type": "string",
                    "description": "Optional case-insensitive contact-name filter.",
                },
            },
            "additionalProperties": False,
        },
    },
}


_WRITE_SCHEMA = {
    "type": "function",
    "function": {
        "name": CREATE_CONTACT,
        "description": (
            "Propose creation of exactly one contact for explicit user confirmation. The "
            "server re-checks for a duplicate immediately before writing and reads the "
            "created record back under the same actor."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1, "maxLength": 120,
                         "description": "Exact contact name supplied by the user."},
                "email": {"type": "string", "minLength": 3, "maxLength": 200,
                          "description": "Contact email address."},
            },
            "required": ["name", "email"],
            "additionalProperties": False,
        },
    },
}


def _skill(filename: str) -> BusinessSkill:
    path = SKILLS / filename
    return BusinessSkill.from_bytes(path.read_bytes(), artifact_uri=f"file://{filename}")


class _ContactsToolExecutor:
    def __init__(self, origin: str, *, api_token: str = "", allow_mutations: bool = False):
        self.origin = origin.rstrip("/")
        #: The delegated credential. It never leaves this object: it is not in a tool
        #: schema, not in a result, and not in an audit record.
        self._api_token = api_token
        self.allow_mutations = bool(allow_mutations)
        if self.allow_mutations and not api_token:
            raise ValueError("a write needs a delegated credential to write with")
        entity_port = _ContactsEntityReadPort(self)
        self._entity_reads = EntityReadTools(entity_port, application_id=APPLICATION_ID)
        self._reference_lookup = ReferenceLookupTools(
            entity_port, entity_port, application_id=APPLICATION_ID)
        self._page_entries = PageEntryTools(
            _ContactsPageEntryPort(self), application_id=APPLICATION_ID)
        self._goal_candidates = GoalCandidateTools(
            self._entity_reads, self._reference_lookup, self._page_entries,
            application_id=APPLICATION_ID)

    def tool_schemas(self) -> tuple[dict, ...]:
        # A read-only composition does not merely refuse the write; it never offers it. A
        # tool in the surface that the run will always refuse just invites a wasted turn.
        return (_READ_SCHEMA,) + ENTITY_READ_SCHEMAS + (
            RESOLVE_RECORD_REFERENCE_SCHEMA, FIND_PAGE_ENTRIES_SCHEMA,
            RESOLVE_GOAL_CANDIDATES_SCHEMA) + (
            (_WRITE_SCHEMA,) if self.allow_mutations else ())

    def tool_effects(self) -> dict[str, str]:
        effects = {"list_contacts": "read"}
        effects.update({name: "read" for name in ENTITY_READ_TOOLS})
        effects[RESOLVE_RECORD_REFERENCE_TOOL] = "read"
        effects[FIND_PAGE_ENTRIES_TOOL] = "read"
        effects[RESOLVE_GOAL_CANDIDATES_TOOL] = "read"
        if self.allow_mutations:
            effects[CREATE_CONTACT] = "write"
        return effects

    @staticmethod
    def _scope(context) -> str:
        """What a read covered: the actor's own workspace, never the application at large."""
        principal = getattr(context, "principal", None)
        if principal is not None:
            return f"workspace {principal.workspace_id}: contacts the current actor may read"
        return "contacts the delegated actor may read"

    def execute(self, name: str, args: dict, *, context=None) -> BusinessToolResult:
        if name in ENTITY_READ_TOOLS:
            return self._entity_reads.execute(name, args, context=context)
        if name == RESOLVE_RECORD_REFERENCE_TOOL:
            return self._reference_lookup.execute(args, context=context)
        if name == FIND_PAGE_ENTRIES_TOOL:
            return self._page_entries.execute(args, context=context)
        if name == RESOLVE_GOAL_CANDIDATES_TOOL:
            return self._goal_candidates.execute(args, context=context)
        if name != "list_contacts" or set(args) - {"name_contains"}:
            raise BusinessToolError("invalid contact query")
        query = args.get("name_contains", "")
        if (not isinstance(query, str) or len(query) > 120
                or re.search(r"[\x00-\x1f\x7f]", query)):
            raise BusinessToolError("name_contains must be a string of at most 120 characters")
        source = self._contact_rows()
        matched = []
        for raw in source:
            if query and query.casefold() not in str(raw["name"]).casefold():
                continue
            matched.append({
                **raw,
                "ui_path": f"/contacts/{raw['id']}",
            })
        routes = tuple(item["ui_path"] for item in matched)
        conditions = (f"name case-insensitive substring {query!r}",) if query else ("no filter",)
        scope = self._scope(context)
        origin = Origin(kind="typed", reference="list_contacts", observed_at=datetime.now(timezone.utc).isoformat(),
                        scope=scope + "; " + "; ".join(conditions))
        evidence = [QueryEvidence(evidence_id="query#1", origin=origin, collection="contacts",
                                  looked_for=query,
                                  conditions=conditions, matched_count=len(matched),
                                  records_scanned=len(source), covers_whole_scope=True)]
        for row in matched:
            for field in ("name", "email"):
                evidence.append(RecordEvidence(
                    evidence_id=f"record#{len(evidence)}", origin=origin,
                    record_id=f"contact/{row['id']}", field_name=field, field_meaning=field,
                    field_was_read=True, value=row[field] or None, value_type="text"))
        return BusinessToolResult(
            content={
                "contacts": matched,
                "matched_count": len(matched),
                "source_total": len(source),
                "scope": scope,
                "completeness": {
                    "source_complete": True,
                    "result_truncated": False,
                    "incomplete_reason": "",
                },
            },
            audit={
                "application": "minimal-contacts",
                "operation": name,
                "returned_count": len(matched),
                "source_complete": True,
            },
            allowed_routes=routes,
            evidence=tuple(evidence),
        )

    @observed_interval("application")
    def _contact_rows(self) -> list[dict]:
        """Read and validate the complete actor-visible synthetic collection once."""
        # Reads go out under the delegated actor too. What comes back is what the
        # application lets that actor see, which is the only scope an answer may claim.
        request = urllib.request.Request(
            self.origin + "/api/contacts",
            headers={"Authorization": f"Bearer {self._api_token}"})
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise BusinessToolAuthenticationError(
                    "the delegated credential was refused") from exc
            if exc.code == 403:
                raise BusinessToolPermissionError(
                    "the application does not let this actor read contacts") from exc
            raise BusinessToolError("the contacts API is unavailable") from exc
        except (OSError, ValueError, urllib.error.URLError) as exc:
            raise BusinessToolError("the contacts API is unavailable") from exc
        source = payload.get("contacts") if isinstance(payload, dict) else None
        if not isinstance(source, list):
            raise BusinessToolError("the contacts API returned an invalid document")
        if len(source) > 10_000:
            raise BusinessToolError("the contacts API returned too many records")
        rows = []
        identities = set()
        for raw in source:
            if not isinstance(raw, dict) or set(raw) != {"id", "name", "email"}:
                raise BusinessToolError("the contacts API returned an invalid record")
            if (any(not isinstance(raw[key], str) for key in ("id", "name", "email"))
                    or not re.fullmatch(r"[a-z0-9-]{1,120}", raw["id"])
                    or raw["id"] in identities):
                raise BusinessToolError("the contacts API returned an invalid record identity or field")
            identities.add(raw["id"])
            rows.append({
                "id": str(raw["id"]),
                "name": str(raw["name"]),
                "email": str(raw["email"]),
            })
        return rows

    def get(self, path: str, **params) -> dict:
        """One read under the delegated actor; the write port reads back through this."""
        if params:
            path += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(
            self.origin + path,
            headers={"Authorization": f"Bearer {self._api_token}"})
        with urllib.request.urlopen(request, timeout=3) as response:
            payload = json.load(response)
        if not isinstance(payload, dict):
            raise BusinessToolError("the contacts API returned an invalid document")
        return payload

    # -- the write -----------------------------------------------------------------------
    def _post(self, path: str, payload: dict) -> dict:
        from web_operation_agent.business.dispatch_evidence import mutation_transport
        request = urllib.request.Request(
            self.origin + path, json.dumps(payload).encode("utf-8"), method="POST")
        request.add_header("Content-Type", "application/json")
        request.add_header("Authorization", f"Bearer {self._api_token}")
        with mutation_transport() as transport:
            return self._post_transport(request, transport)

    def _post_transport(self, request, transport):
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                result = json.load(response)
                if not isinstance(result, dict) or not isinstance(result.get("contact"), dict) or not result["contact"].get("id"):
                    raise BusinessMutationIndeterminate("contacts create returned no acknowledgement", phase="acknowledgement")
                transport.applied()
                return result
        except urllib.error.HTTPError as exc:
            # The application's own refusal, classified rather than passed through: its
            # body may quote record content, and only the shape of the answer crosses.
            if exc.code == 409:
                transport.rejected()
                raise BusinessToolError("a contact with that name already exists") from exc
            if exc.code == 401:
                transport.rejected()
                raise BusinessToolAuthenticationError(
                    "the delegated credential was refused") from exc
            if exc.code == 403:
                transport.rejected()
                # Definite: the application turned the request away before storing anything,
                # so this is an authorization result and not an uncertain write.
                raise BusinessToolPermissionError(
                    "the application does not let this actor create contacts") from exc
            raise BusinessMutationIndeterminate("contacts create returned no safe outcome", phase="dispatch") from exc
        except (OSError, ValueError) as exc:
            # Unlike the refusals above, this may have arrived and been lost on the way
            # back. The caller decides; it must not be reported as a plain failure.
            raise BusinessMutationIndeterminate(
                "the contacts API did not report an outcome", phase="dispatch") from exc

    @staticmethod
    def _normalise(args: dict) -> dict:
        if not isinstance(args, dict) or set(args) != {"name", "email"}:
            raise BusinessToolError(f"{CREATE_CONTACT} requires only name and email")
        # `normalize_business_text` folds the Unicode tricks that make two different names
        # look identical to a person and different to a duplicate check.
        name = normalize_business_text(args.get("name"), field="contact name", maximum=120)
        email = args.get("email")
        if not isinstance(email, str) or "@" not in email or len(email) > 200:
            raise BusinessToolError("contact email must be a plain address")
        return {"name": name, "email": email.strip().casefold()}

    def _existing(self, name: str) -> list[dict]:
        """Over the whole collection, read the same way the read tool reads it."""
        result = self.execute("list_contacts", {})
        if not result.content["completeness"]["source_complete"]:
            raise BusinessToolError("the duplicate check could not read every contact")
        key = business_text_key(name)
        return [row for row in result.content["contacts"]
                if business_text_key(row["name"]) == key]

    def prepare_mutation(self, name: str, args: dict, *,
                         context=None) -> BusinessMutationSpec:
        if not self.allow_mutations or name != CREATE_CONTACT:
            raise BusinessToolError("this composition has no such write")
        proposed = self._normalise(args)
        if self._existing(proposed["name"]):
            raise BusinessToolError("a contact with that name already exists")
        return BusinessMutationSpec(
            args=proposed,
            # What reconciliation will look for if the outcome is ever lost. It has to be
            # something the application can be asked about later, not a local id.
            reconcile_key=proposed["name"].casefold(),
            effect="Create exactly one contact",
            targets=(f"Name: {proposed['name']}", f"Email: {proposed['email']}"),
        )

    def execute_mutation(self, name: str, args: dict, *,
                         context=None) -> BusinessToolResult:
        from web_operation_agent.business.write_pipeline import execute_verified_handler_write

        if not self.allow_mutations or name != CREATE_CONTACT:
            raise BusinessToolError("this composition has no such write")
        proposed = self._normalise(args)

        def pre_dispatch_reread(_actor):
            # Again, immediately before dispatch. The proposal may have waited on a person.
            if self._existing(proposed["name"]):
                raise BusinessToolError("a contact with that name already exists")
            return proposed

        def dispatch_once(_actor, _reread_state):
            created = self._post("/api/contacts", proposed).get("contact")
            if not isinstance(created, dict) or not created.get("id"):
                raise BusinessMutationIndeterminate(
                    "the contacts API accepted the create but returned no record",
                    phase="acknowledgement")
            return str(created["id"])

        def verify_readback(_actor, _reread_state, record_id, _uncertain):
            try:
                readback = [row for row in self._existing(proposed["name"])
                            if row["id"] == record_id]
            except BusinessToolError as exc:
                raise BusinessMutationIndeterminate(
                    "the contact was created but same-actor readback was unavailable",
                    phase="readback", record_id=record_id) from exc
            if len(readback) != 1 or readback[0]["email"] != proposed["email"]:
                # Created, and not what was approved. Not a failure -- a failure would invite a
                # retry, and something is already there.
                raise BusinessMutationIndeterminate(
                    "the contact was created but the same-actor readback did not match",
                    phase="readback", record_id=record_id)
            ui_path = f"/contacts/{record_id}"
            return BusinessToolResult(
                content={
                    "created": True, "readback_verified": True,
                    "contact": {**readback[0]},
                    "scope": self._scope(context),
                },
                audit={
                    "application": "minimal-contacts", "operation": name,
                    "records_created": 1, "readback_verified": True,
                },
                allowed_routes=(ui_path,),
            )

        return execute_verified_handler_write(
            verify_actor=lambda: None,
            pre_dispatch_reread=pre_dispatch_reread,
            dispatch_once=dispatch_once,
            verify_readback=verify_readback,
            allow_readback_recovery=False,
            use_verification_interval=True,
        )


class _ContactsEntityReadPort:
    """REST/list translation proving the entity contract is not tied to an ORM."""

    _FIELDS = (
        EntityField(reference="id", label="Identifier", kind="text"),
        EntityField(reference="name", label="Name", kind="text"),
        EntityField(reference="email", label="Email", kind="text"),
    )

    def __init__(self, executor: _ContactsToolExecutor):
        self._executor = executor

    @staticmethod
    def _actor_binding(context) -> str:
        principal = getattr(context, "principal", None)
        return (principal.actor_authority_digest if principal is not None
                else "unverified-synthetic-actor")

    def catalog(self, *, context=None) -> EntityCatalog:
        binding = hashlib.sha256(
            ("minimal-contacts-entity-v1\0" + self._actor_binding(context)).encode("utf-8")
        ).hexdigest()
        return EntityCatalog(
            binding_digest=binding,
            entities=(EntityDescriptor(
                reference="contacts",
                label="Contacts",
                search_text="contacts contact address book people person 联系人 通讯录 人员",
                page_hints=(EntityPageHint(
                    name="Contacts", breadcrumbs=("Contacts",), views=("list", "detail")),),
            ),),
            complete=True,
            scope="current actor's readable entities in the synthetic workspace",
        )

    def fields(self, entity: EntityDescriptor, *, context=None) -> tuple[EntityField, ...]:
        if entity.reference != "contacts":
            raise BusinessToolError("the synthetic entity is no longer available")
        return self._FIELDS

    def lookup(self, entity: EntityDescriptor, reference: str, *, limit: int,
               context=None) -> tuple[tuple[ReferenceMatch, ...], bool]:
        if entity.reference != "contacts":
            raise BusinessToolError("the synthetic entity is no longer available")
        source = self._executor._contact_rows()
        rows = [row for row in source if row["name"].casefold() == reference.casefold()]
        if not rows:
            rows = [row for row in source
                    if reference.casefold() in row["name"].casefold()]
        rows.sort(key=lambda row: (row["name"].casefold() != reference.casefold(),
                                   row["id"]))
        return tuple(ReferenceMatch(
            record_id=str(row["id"]), label=row["name"],
            route=f"/contacts/{row['id']}") for row in rows[:limit]), len(rows) > limit

    @staticmethod
    def _matches(row: dict, condition) -> bool:
        actual = row[condition.field.reference]
        expected = condition.value
        if condition.operator == "=":
            return actual == expected
        if condition.operator == "!=":
            return actual != expected
        if condition.operator == "contains":
            return str(expected).casefold() in actual.casefold()
        if condition.operator == "in":
            return actual in expected
        if condition.operator == "not_in":
            return actual not in expected
        raise BusinessToolError("the synthetic REST adapter cannot apply this filter")

    def query(self, request: EntityQuery, *, context=None) -> EntityQueryPage:
        if request.entity.reference != "contacts":
            raise BusinessToolError("the synthetic entity is no longer available")
        source = sorted(self._executor._contact_rows(), key=lambda row: row["id"])
        matched = [row for row in source
                   if all(self._matches(row, condition) for condition in request.filters)]
        page = matched[request.offset:request.offset + request.limit]
        records = tuple(EntityRecord(
            reference=f"contact/{row['id']}",
            display_id=row["id"],
            cells=tuple(EntityCell(
                field_reference=field.reference,
                value=row[field.reference],
            ) for field in request.fields),
        ) for row in page)
        return EntityQueryPage(
            records=records,
            matched_count=len(matched),
            source_total=len(source),
            source="delegated synthetic REST API (Contacts)",
            scope=self._executor._scope(context) +
                  f"; {len(request.filters)} AND filters; stable identity ascending",
            consistency="one complete actor-visible REST list filtered and paged in the adapter",
        )


class _ContactsPageEntryPort:
    """The application's current actor menu, without an ORM action table or source."""

    _PAGES = {"contacts": ("/contacts", "contact"), "help": ("/contacts/help", ""),
              "workflows": ("/contacts/workflows", ""),
              "desk-partner": ("/contacts/workflows/partner", "contact"),
              "desk-supplier": ("/contacts/workflows/supplier", "contact")}

    def __init__(self, executor: _ContactsToolExecutor):
        self._executor = executor

    def catalog(self, *, context=None) -> PageEntryCatalog:
        actor_binding = _ContactsEntityReadPort._actor_binding(context)
        request = urllib.request.Request(self._executor.origin + "/api/pages",
            headers={"Authorization": f"Bearer {self._executor._api_token}"})
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise BusinessToolAuthenticationError("the delegated credential was refused") from exc
            if exc.code == 403:
                raise BusinessToolPermissionError("the application does not let this actor read pages") from exc
            raise BusinessToolError("the page catalog is unavailable") from exc
        except (OSError, ValueError, urllib.error.URLError) as exc:
            raise BusinessToolError("the page catalog is unavailable") from exc
        if (not isinstance(payload, dict) or not isinstance(payload.get("pages"), list)
                or not isinstance(payload.get("complete"), bool)):
            raise BusinessToolContractError("the application returned an invalid page catalog")
        principal = getattr(context, "principal", None)
        if principal is not None and (payload.get("actor") != principal.actor_id
                                     or payload.get("workspace") != principal.workspace_id):
            raise BusinessToolPermissionError("the page catalog actor differs from the bound actor")
        entries = []
        for item in payload["pages"]:
            if (not isinstance(item, dict) or set(item) != {"key", "label", "path"}
                    or any(not isinstance(value, str) or not value.strip() for value in item.values())):
                raise BusinessToolContractError("the application returned an invalid page entry")
            reviewed = self._PAGES.get(item["key"])
            route = item["path"] if reviewed is not None and item["path"] == reviewed[0] else ""
            subject = reviewed[1] if route else ""
            entries.append(PageEntry(reference=item["key"], label=item["label"],
                search_text=item["label"] + (" address book people 联系人 通讯录" if item["key"] == "contacts" else ""),
                subject_reference=subject, breadcrumbs=(item["label"],),
                views=("list",) if item["key"] == "contacts" else (), route=route,
                unavailable_reason="" if route else "unreviewed_route"))
        digest = hashlib.sha256(
            ("contacts-page-entries-v2\0" + actor_binding + "\0"
             + json.dumps(payload, sort_keys=True)).encode()).hexdigest()
        return PageEntryCatalog(
            binding_digest=digest,
            entries=tuple(entries), complete=payload["complete"],
            scope=self._executor._scope(context) + "; current actor menu",
        )

    def parts(self, entry: PageEntry, *, context=None) -> tuple[PagePart, ...]:
        if entry.reference != "contacts":
            return ()
        return (PagePart(reference="name", label="Name", kind="field",
                         search_text="contact name 姓名"),
                PagePart(reference="email", label="Email", kind="field",
                         search_text="email address 邮箱"))


class _ContactsFormPort:
    """Translate the synthetic app's create page to the generic current-form contract.

    This intentionally shares no implementation with Odoo.  It proves the core contract
    needs only application metadata plus the current page, not an ORM or a particular web
    framework.
    """

    required_scopes = frozenset({GENERIC_FORM_SCOPE})

    def __init__(self, executor: _ContactsToolExecutor):
        self._executor = executor
        self._planned = None

    def form_entries(self, observation, *, context):
        from web_operation_agent.business.form_entries import FormEntryCandidate
        if (observation.render_state != "ready" or observation.page_route != "/contacts"
                or urllib.parse.urlsplit(observation.url).path != "/contacts"):
            return ()
        controls = [e for e in observation.elements if e.role == "link" and e.name == "New contact"
                    and e.attrs.get("href") == "/contacts/new"]
        return (FormEntryCandidate("contacts-create-entry", "New contact", "create"),) if len(controls) == 1 else ()

    def plan_entry(self, reference, observation, *, context):
        from web_operation_agent.business.forms import ApplicationFormEntry
        self._planned = None
        if reference != "contacts-create-entry" or not self.form_entries(observation, context=context):
            raise BusinessToolRequestError("the contact create entry is not current")
        control = next(e for e in observation.elements if e.role == "link" and e.name == "New contact"
                       and e.attrs.get("href") == "/contacts/new")
        entry = ApplicationFormEntry(reference, control)
        self._planned = entry, context.principal.binding_digest, context.session_id, context.turn_id
        return entry

    def accept_entry(self, entry, before, receipt, observation, *, context):
        planned, self._planned = self._planned, None
        if (planned != (entry, context.principal.binding_digest, context.session_id, context.turn_id)
                or not receipt.ok or receipt.action != "click"
                or receipt.args_echo != {"ref": entry.control.ref, "epoch": before.epoch}
                or observation.epoch <= before.epoch or observation.render_state != "ready"
                or observation.page_route != "/contacts/new"
                or urllib.parse.urlsplit(observation.url).path != "/contacts/new"):
            raise BusinessToolRequestError("the contact form entry receipt was not verified")
        return "contacts-create-v1"

    def discard_entry(self, entry, *, context):
        if self._planned is not None and self._planned[0] == entry:
            self._planned = None

    def decide(self, observation, element):
        if (self._planned is not None and self._planned[0].control == element
                and element in observation.elements and element.epoch == observation.epoch):
            return ""
        return "only a current reviewed contact form opener may be clicked"

    def describe_form(self, observation, *, context=None):
        if urllib.parse.urlsplit(observation.url).path != "/contacts/new":
            return None
        return ApplicationFormDescriptor(
            reference="contacts-create-v1",
            title="New contact",
            route="/contacts/new",
            fields=(
                ApplicationFormField(
                    "name", "Name", controls=("textbox",), required=True),
                ApplicationFormField(
                    "email", "Email", kind="email", controls=("textbox",),
                    required=True),
                # Local preferences never enter the save payload.
                ApplicationFormField("daily_limit", "Daily limit", kind="integer",
                                     controls=("spinbutton",), binding="label"),
                ApplicationFormField("follow_up_date", "Follow-up date", kind="date", binding="label"),
                ApplicationFormField("priority", "Priority", kind="choice", controls=("combobox",),
                    choices=(FormChoice("normal", "Normal"), FormChoice("urgent", "Urgent")), binding="label"),
                ApplicationFormField("contact_method", "Contact method", kind="choice", controls=("radiogroup",),
                    choices=(FormChoice("email", "Email only"), FormChoice("phone", "Phone call")), binding="label"),
            ),
            scope="fields writable by the current actor on the new-contact page",
            control_scope="form:contact-create", submit_label="Save contact",
        )

    def prepare_form(self, form, values: tuple[ApplicationFormValue, ...], *,
                     context=None) -> FormDraft:
        raw = {item.field.reference: item.value for item in values}
        try:
            normalized = {
                **raw,
                **({"name": normalize_business_text(
                    raw["name"], field="contact name", maximum=120)}
                   if "name" in raw else {}),
                **({"email": raw["email"].strip().casefold()}
                   if "email" in raw else {}),
            }
        except BusinessToolError as exc:
            raise BusinessToolRequestError(str(exc)) from exc
        return FormDraft(
            route=form.route,
            fields=tuple(FormDraftField(
                item.field.reference, item.field.label,
                normalized[item.field.reference], item.control, binding=item.field.binding,
                option_label=next((c.label for c in item.field.choices if c.value == item.value), ""))
                for item in values),
            control_scope=form.control_scope, submit_label=form.submit_label,
        )


class _ContactsIdentityPort:
    """Two credentials, resolved independently, that have to agree.

    This is the smallest honest identity port. The browser holds a session it cannot read
    out of itself, the server holds a delegated token the page never sees, and each is asked
    *the application* who it is. `bind_matching_identity` then refuses unless both answers
    name the same actor -- which is what stops a page from borrowing the server's authority
    or a stolen token from acting as somebody who is not at the keyboard.

    An application with no authenticated actor uses `AnonymousIdentityPort` instead and
    cannot enable writes: `AgentTurnConfig` refuses business mutations without a verified
    session identity.
    """

    required = True

    def __init__(self, origin: str, *, api_token: str, scopes: frozenset[str],
                 lifetime_s: int = 300):
        self.origin = origin.rstrip("/")
        self._api_token = api_token
        # Exactly the scopes the tool catalog needs -- the framework compares them, and it
        # is right to: a principal carrying a write scope a read-only composition never uses
        # is authority nobody asked for.
        self.required_scopes = frozenset(scopes)
        self.lifetime_s = lifetime_s

    def _whoami(self, headers: dict,
                expected_source: str) -> tuple[IdentityAssertion, frozenset[str]]:
        request = urllib.request.Request(self.origin + "/api/whoami", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                payload = json.load(response)
        except (OSError, ValueError, urllib.error.URLError) as exc:
            raise PrincipalError("the contacts identity endpoint is unavailable") from exc
        if not isinstance(payload, dict) or payload.get("source") != expected_source:
            raise PrincipalError("the contacts identity answer is not the expected side")
        permissions = payload.get("permissions")
        if not isinstance(permissions, list) or any(
                not isinstance(item, str) for item in permissions):
            raise PrincipalError("the contacts identity answer carries no permissions")
        import time
        issued = int(time.time() * 1000)
        return IdentityAssertion(
            schema_version=PRINCIPAL_CONTEXT_VERSION, source=expected_source,
            application_id=APPLICATION_ID, tenant_id="synthetic",
            workspace_id=str(payload["workspace"]), actor_id=str(payload["actor"]),
            issued_at=issued, expires_at=issued + self.lifetime_s * 1000,
        ), frozenset(permissions)

    def page_identity(self, session_cookie: str) -> IdentityAssertion:
        """What the *browser* is, asked of the application with the browser's own cookie."""
        return self._whoami({"Cookie": session_cookie}, "page_session")[0]

    def bind(self, page_identity: IdentityAssertion, *, delegation_id: str,
             lifetime_s: int = 300, now_ms: int | None = None) -> PrincipalContext:
        delegated, permissions = self._whoami(
            {"Authorization": f"Bearer {self._api_token}"}, "delegated_api")
        # The principal carries what the catalog needs *and* the application grants this
        # actor -- never the catalog alone. A read-only user in a composition that offers a
        # write gets a principal the framework's per-tool scope check refuses the write on.
        granted = self.required_scopes & permissions
        if not granted:
            raise PrincipalError("the actor holds none of the scopes this integration uses")
        return bind_matching_identity(
            page_identity, delegated, delegation_id=delegation_id,
            scopes=tuple(sorted(granted)), now_ms=now_ms)

    def validate(self, principal: PrincipalContext | None, *,
                 application_id: str) -> None:
        if principal is None:
            raise PrincipalError("this application requires a verified principal")
        principal.require_live(application_id=application_id)


class _ContactsReconciler:
    """What the application can still say about a write whose outcome was lost.

    Never a verdict, always evidence. A matching record is not proof that *this* proposal
    created it -- a person may have created the same contact by hand in between -- and its
    absence cannot rule out a write still in flight. So the answer is always `unknown` with
    `requires_human`, carrying the counts an operator needs to decide. An adapter that
    answered "completed" here would be guessing on the one question this project refuses to
    guess about.
    """

    supported_tools = frozenset({CREATE_CONTACT})

    def __init__(self, executor: "_ContactsToolExecutor", enabled: bool,
                 extra_tools: frozenset[str] = frozenset()):
        self._executor = executor
        self.supported_tools = (frozenset({CREATE_CONTACT}) | extra_tools) if enabled else frozenset()

    def reconcile(self, name, business_key, *, evidence, principal):
        if name not in self.supported_tools or principal is None:
            raise ValueError("reconciliation requires an enabled tool and principal")
        principal.require_live(application_id=APPLICATION_ID)
        if SCOPES["list_contacts"] not in principal.scopes:
            raise PrincipalError("reconciliation requires the read scope")
        if name != CREATE_CONTACT:
            return MutationReconciliation(
                outcome="unknown", requires_human=True,
                reason=f"Indeterminate {name} outcome; inspect workspace records under the acting user.")
        proposed = self._executor._normalise(business_key)
        matches = self._executor._existing(proposed["name"])
        return MutationReconciliation(
            outcome="unknown", requires_human=True,
            reason=(f"{len(matches)} contacts currently carry the name "
                    f"{proposed['name']!r}. Establish the original writer's final outcome "
                    "before resolving."))


class MinimalContactsAdapter:
    """A second application adapter implemented only through the public SPI."""

    def __init__(self, origin: str, *, api_token: str = "",
                 allow_mutations: bool = False, mutation_journal=None,
                 extra_skills: tuple[BusinessSkill, ...] = (), standard_skills: bool = False,
                 convention_updates: bool = False):
        if allow_mutations and mutation_journal is None:
            raise ValueError("allow_mutations=True requires an explicit mutation journal")
        if (not isinstance(extra_skills, tuple)
                or any(not isinstance(skill, BusinessSkill) or not skill.read_only
                       for skill in extra_skills)):
            raise TypeError("extra synthetic Skills must be read-only artifacts")
        skills = [_skill("contacts-query.json"), _skill("runtime-records-query.json")]
        if allow_mutations:
            skills.append(_skill("contacts-create.json"))
        skills.extend(extra_skills)
        executor = _ContactsToolExecutor(origin, api_token=api_token,
                                         allow_mutations=allow_mutations)
        self._executor = executor
        forms = _ContactsFormPort(executor)
        base_scopes = ({tool: scope for tool, scope in SCOPES.items()
                        if allow_mutations or tool != CREATE_CONTACT})
        if convention_updates and allow_mutations:
            base_scopes["update_record"] = "contacts:write"
            base_scopes["toggle_record_status"] = "contacts:write"
        self.identity = _ContactsIdentityPort(
            origin, api_token=api_token,
            scopes=(frozenset(base_scopes.values()) | forms.required_scopes))
        operations = None
        if standard_skills:
            if extra_skills:
                raise ValueError("standard packages use their independent installed registry")
            from .packages import contact_operations
            operations = contact_operations(executor, allow_mutations,
                                            convention_updates=convention_updates,
                                            identity=self.identity)
            skills = []
        declarations = [("list_contacts", ("synthetic.contact", "business.identifier",
                                           "application.route"))]
        declarations.extend((
            (DISCOVER_ENTITIES_TOOL, ("application.metadata",)),
            (DESCRIBE_ENTITY_TOOL, ("application.metadata",)),
            (QUERY_ENTITY_RECORDS_TOOL,
             ("business.generic_record", "business.identifier")),
            (RESOLVE_RECORD_REFERENCE_TOOL,
             ("business.generic_record", "business.identifier", "application.route")),
            (FIND_PAGE_ENTRIES_TOOL, ("application.metadata", "application.route")),
            (RESOLVE_GOAL_CANDIDATES_TOOL,
             ("application.metadata", "application.route", "business.identifier")),
        ))
        if allow_mutations:
            declarations.append(
                (CREATE_CONTACT, ("synthetic.contact", "business.identifier",
                                  "application.route", "business.mutation_result")))
            if convention_updates:
                declarations.append(
                    ("update_record", ("synthetic.contact", "business.identifier", "business.mutation_result")))
                declarations.append(
                    ("toggle_record_status", ("synthetic.contact", "business.identifier", "business.mutation_result")))
        data = DeclaredDataClassifier(tuple(sorted(declarations)))
        # A write that cannot say the application confirmed it is refused by the framework
        # before it can be reported as done. An empty tuple here is what a read-only
        # integration declares, and it is not the same thing as having no opinion.
        post_rules = [((CREATE_CONTACT, ("content.readback_verified", "audit.readback_verified")))] if allow_mutations else []
        if allow_mutations and convention_updates:
            post_rules.extend((
                ("update_record", ("content.verified", "audit.verified")),
                ("toggle_record_status", ("content.verified", "audit.verified")),
            ))
        postconditions = ResultPostconditionVerifier(tuple(post_rules))
        routes = DeclaredRoutePort(
            application_id=APPLICATION_ID,
            tool_route_patterns=(r"/contacts", r"/contacts/new", r"/contacts/[a-z0-9-]+",
                                 r"/contacts/workflows/(?:partner|supplier)"),
        )
        if operations is None:
            self.business_tools = BusinessToolRuntime(
                tuple(skills), executor, application_id=APPLICATION_ID,
                required_scopes={tool: scope for tool, scope in SCOPES.items()
                                 if allow_mutations or tool != CREATE_CONTACT},
                mutation_journal=mutation_journal,
                data_classifier=data, postcondition_verifier=postconditions,
                route_validator=routes)
        else:
            from .packages import ContactsTools
            # The narrow adapter bridge reuses existing reads and the reviewed REST
            # handler; new operation declarations and step tools share one registry.
            self.business_tools = BusinessToolRuntime((), ContactsTools(executor, operations.tools), application_id=APPLICATION_ID,
                required_scopes=base_scopes, mutation_journal=mutation_journal,
                data_classifier=data, postcondition_verifier=postconditions, route_validator=routes,
                skill_registry=operations.skills, require_skill_for_writes=True,
                operations={
                    **{name: ToolOperation("contacts", "read") for name in SCOPES if name != CREATE_CONTACT},
                    **{d.name: d.operation for d in operations.tools.definitions},
                })
        extra_reconcile = frozenset({"update_record", "toggle_record_status"}) if convention_updates else frozenset()
        self.ports = ApplicationPorts(
            identity=self.identity,
            routes=routes,
            postconditions=postconditions,
            reconciliation=_ContactsReconciler(executor, allow_mutations, extra_reconcile),
            data=data,
            forms=forms,
            operations=operations,
        )
        self.descriptor = ApplicationDescriptor(
            schema_version=APPLICATION_ADAPTER_VERSION,
            application_id=APPLICATION_ID,
            display_name="Minimal Contacts",
            origin=origin,
            navigation_mode="spa",
            page_controller_module=origin + "/woa/page-controller.js",
            catalog_sha256=self.business_tools.catalog_sha256,
        )

    def get(self, path: str, **params) -> dict:
        """One read under the delegated actor; the write port reads back through this."""
        return self._executor.get(path, **params)

    def health(self) -> dict:
        try:
            with urllib.request.urlopen(self.descriptor.origin + "/healthz", timeout=2) as response:
                payload = json.load(response)
            passed = response.status == 200 and payload == {"ready": True}
        except (OSError, ValueError, urllib.error.URLError):
            passed = False
        return {"passed": passed, "application_id": self.descriptor.application_id}
