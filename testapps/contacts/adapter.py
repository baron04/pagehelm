"""Declarative onboarding integration for the synthetic Minimal Contacts app."""
from __future__ import annotations

from datetime import timezone
import json
from pathlib import Path

from web_operation_agent.business.entities import ENTITY_READ_SCHEMAS, RESOLVE_RECORD_REFERENCE_SCHEMA
from web_operation_agent.business.errors import (
    BusinessMutationIndeterminate,
    BusinessToolError,
    BusinessToolIdentityError,
)
from web_operation_agent.business.forms import FormChoice
from web_operation_agent.business.runtime import BusinessToolResult, business_text_key, normalize_business_text
from .declarative import (
    DeclarativeAppSpec, DeclarativeApplicationAdapter, DeclarativeGoalPort,
    DeclarativeModelGoalPort, DeclarativeProcedure, FRONTEND_PRESETS, FieldSpec,
    FormSpec, HttpWhoamiIdentityPort, PageSpec, QueryFormSpec, ResolvedActor,
    StatusToggleSpec, SubmitSpec, TypedWriteSpec, build_declarative_profile, entities_from_openapi,
)
from .declarative.declarative import DeclarativeEntityReadPort, DeclarativeReadExecutor, DeclarativeRestClient
from .declarative.declarative_forms import DeclarativeDraftSteps, DeclarativeFormPort, DeclarativeMutationPrefill, DeclarativeReadActions
from .declarative.declarative_writes import DeclarativeSubmitPort, DeclarativeTypedWriteHandler
from web_operation_agent.execution.submit_contract import SubmitConventions

APPLICATION_ID = "minimal.contacts"
CREATE_CONTACT = "create_contact"
GENERIC_FORM_SCOPE = "contacts:write"
SKILLS_DIR = Path(__file__).resolve().parent / "skills"
PACKAGES_DIR = Path(__file__).resolve().parent / "packages"
SCOPES = {
    "list_contacts": "contacts:read", "discover_entities": "contacts:read",
    "describe_entity": "contacts:read", "query_entity_records": "contacts:read",
    "resolve_record_reference": "contacts:read", "find_page_entries": "contacts:read",
    "resolve_goal_candidates": "contacts:read", CREATE_CONTACT: "contacts:write",
}

def _obj(props: dict, required: list[str] = ()) -> dict:
    return {"200": {"description": "OK", "content": {"application/json": {"schema": {"type": "object", "properties": props, **({"required": required} if required else {})}}}}}

CONTACTS_OPENAPI = {
    "openapi": "3.1.0",
    "info": {"title": "Minimal Contacts", "version": "1.0.0"},
    "paths": {
        "/api/contacts": {"get": {"x-woa-entity": "contacts", "responses": _obj({"contacts": {"type": "array", "items": {"type": "object", "required": ["id", "name", "email"], "properties": {
            "id": {"type": "string", "title": "Identifier"},
            "name": {"type": "string", "title": "Name", "maxLength": 120},
            "email": {"type": "string", "title": "Email", "format": "email", "maxLength": 200},
        }}}})}},
        "/api/contacts/{id}": {
            "get": {"responses": _obj({"contact": {"type": "object", "properties": {"id": {"type": "string"}, "name": {"type": "string"}, "email": {"type": "string"}}}})},
            "put": {"responses": {"200": {"description": "Updated"}}},
        },
    },
}

CONTACTS_ENTITIES = entities_from_openapi(
    CONTACTS_OPENAPI,
    entity_overrides={"contacts": {
        "label": "Contacts", "singular_label": "Contact", "aliases": ("联系人", "通讯录"),
        "search_text": "contacts contact address book people person 联系人 通讯录 人员",
        "subject_reference": "contact", "detail_field": "email",
        "list_tool_name": "list_contacts",
        "list_tool_description": "Read actor-visible contacts from the synthetic REST collection.",
        "list_filter_param": "name_contains", "list_filter_field": "name",
        "list_filter_description": "Optional case-insensitive filter on contact name.",
        "summary_source_label": "Contacts REST collection",
        "field_overrides": {
            "id": {"summary_label": "Record ID"},
            "name": {"summary_label": "Contact name"},
            "email": {"summary_label": "Email address", "normalizer": lambda v: str(v).strip().casefold()},
        },
        "row_fetcher": lambda ex: ex._contact_rows(),
        "record_reader": lambda ex, rid: ex.client.get_record(f"/api/contacts/{rid}", detail_key="contact"),
    }},
    default_read_scope="contacts:read", default_write_scope="contacts:write",
)

CONTACTS_PAGES = (
    PageSpec("contacts", "Contacts", "/contacts", entity="contacts", subject_reference="contact", views=("list",), operations=("create",), search_text="contacts address book directory people 联系人 通讯录"),
    PageSpec("workflows", "Vendor workspaces", "/contacts/workflows", search_text="workspaces workflows 协同 工作区"),
    PageSpec("desk-partner", "Partner desk", "/contacts/workflows/partner", entity="contacts", subject_reference="contact", sections=("Overview", "Terms"), section_tab_ids=("workflow-overview", "workflow-terms"), search_text="partner desk terms overview 合作伙伴"),
    PageSpec("desk-supplier", "Supplier desk", "/contacts/workflows/supplier", entity="contacts", subject_reference="contact", sections=("Overview", "Terms"), section_tab_ids=("workflow-overview", "workflow-terms"), search_text="supplier desk terms overview 供应商"),
    PageSpec("help", "Help", "/contacts/help", search_text="help docs documentation 帮助"),
    PageSpec("contacts-filter", "Filter contacts", "/contacts/filter", query_form=QueryFormSpec("/contacts/filter", "form:contact-filter", "name_contains", "Name contains", submit_label="Search contacts", api_path="/api/contacts")),
)

CONTACTS_FORMS = (FormSpec(
    reference="contacts-create-v1", title="New contact", route="/contacts/new",
    control_scope="form:contact-create", submit_label="Save contact",
    fields=(
        FieldSpec("name", "Name", kind="text", required=True, controls=("textbox",), updateable=True),
        FieldSpec("email", "Email", kind="text", form_kind="email", required=True, controls=("textbox",), updateable=True),
        FieldSpec("daily_limit", "Daily limit", kind="integer", controls=("spinbutton",), binding="label", updateable=False),
        FieldSpec("follow_up_date", "Follow-up date", kind="date", controls=("textbox",), binding="label", updateable=False),
        FieldSpec("priority", "Priority", kind="choice", controls=("combobox",), binding="label", choices=(FormChoice("normal", "Normal"), FormChoice("urgent", "Urgent"))),
        FieldSpec("contact_method", "Contact method", kind="choice", controls=("radiogroup",), binding="label", choices=(FormChoice("email", "Email only"), FormChoice("phone", "Phone call"))),
        FieldSpec("period_start", "Period start", kind="date", in_form=False, updateable=True),
        FieldSpec("period_end", "Period end", kind="date", in_form=False, updateable=True),
    ),
    scope_description="fields writable by the current actor on the new-contact page",
    required_scope="contacts:write", entry_route="/contacts", entry_label="New contact",
    entry_reference="contacts-create-entry", draft_routes=("/contacts/new", "/contacts/edit/", "/contacts/edit-locked/"),
    edit_control_scope="form:contact-edit", local_line_prefix="Local line ", add_line_button="Add local line",
    prefill_from_tool=CREATE_CONTACT,
    normalizers={"name": lambda v: normalize_business_text(v, field="contact name", maximum=120), "email": lambda v: str(v).strip().casefold()},
),)

SUBSCRIPTION_TOGGLE = StatusToggleSpec(
    entity="contact-subscriptions", label="Contact subscription",
    states=(("enabled", "enabled"), ("disabled", "disabled")),
    detail_endpoint_template="/api/contact-subscriptions/{id}", update_endpoint_template="/api/contact-subscriptions/{id}",
    detail_key="subscription", synonyms=(("开启", "enabled"), ("启用", "enabled"), ("关闭", "disabled"), ("停用", "disabled")),
    resolve_from_entity="contacts",
)


def _normalise_create(args: dict) -> dict:
    if not isinstance(args, dict) or set(args) != {"name", "email"}:
        raise BusinessToolError(f"{CREATE_CONTACT} requires only name and email")
    name = normalize_business_text(args.get("name"), field="contact name", maximum=120)
    email = args.get("email")
    if not isinstance(email, str) or "@" not in email or len(email) > 200:
        raise BusinessToolError("contact email must be a plain address")
    return {"name": name, "email": email.strip().casefold()}


def _create_dispatch(executor: "_ContactsToolExecutor", proposed: dict) -> str:
    created = executor._post("/api/contacts", proposed).get("contact")
    if not isinstance(created, dict) or not created.get("id"):
        raise BusinessMutationIndeterminate("the contacts API accepted the create but returned no record", phase="acknowledgement")
    return str(created["id"])


def _create_readback(executor: "_ContactsToolExecutor", proposed: dict, record_id: str) -> dict:
    try:
        readback = [row for row in executor._existing(proposed["name"]) if row["id"] == record_id]
    except BusinessToolError as exc:
        raise BusinessMutationIndeterminate("the contact was created but same-actor readback was unavailable", phase="readback", record_id=record_id) from exc
    if len(readback) != 1 or readback[0]["email"] != proposed["email"]:
        raise BusinessMutationIndeterminate("the contact was created but the same-actor readback did not match", phase="readback", record_id=record_id)
    return dict(readback[0])


def _create_result(record: dict, record_id: str, scope: str) -> BusinessToolResult:
    return BusinessToolResult(
        content={"created": True, "readback_verified": True, "verified": True, "entity": "contact", "reference": record["name"], "record_id": str(record_id), "contact": dict(record), "scope": scope, "effects": [{"effect": "record", "verified": True, "id": str(record_id)}]},
        audit={"application": "minimal-contacts", "operation": CREATE_CONTACT, "records_created": 1, "readback_verified": True},
        allowed_routes=(f"/contacts/{record_id}",),
    )


def _create_reconcile(executor: "_ContactsToolExecutor", business_key: dict) -> str:
    proposed = executor._normalise(business_key)
    matches = executor._existing(proposed["name"])
    return f"{len(matches)} contacts currently carry the name {proposed['name']!r}. Establish the original writer's final outcome before resolving."


CREATE_CONTACT_SPEC = TypedWriteSpec(
    name=CREATE_CONTACT,
    description="Create one new contact in the current actor's workspace after visible approval.",
    parameters={"type": "object", "properties": {"name": {"type": "string", "minLength": 1, "maxLength": 120}, "email": {"type": "string", "minLength": 3, "maxLength": 200}}, "required": ["name", "email"], "additionalProperties": False},
    entity="contact", operation_action="create", required_scope="contacts:write",
    normalizer=_normalise_create,
    precondition_check=lambda ex, p: (_ for _ in ()).throw(BusinessToolError("a contact with that name already exists")) if ex._existing(p["name"]) else None,
    effect_summary="Create exactly one contact",
    targets_fn=lambda p: (f"Name: {p['name']}", f"Email: {p['email']}"),
    reconcile_key_fn=lambda p: p["name"].casefold(),
    dispatch_fn=_create_dispatch, readback_fn=_create_readback,
    result_builder=_create_result, reconcile_fn=_create_reconcile,
    literal_inputs=("name", "email"),
    input_fields=(("name", "contacts", "name"), ("email", "contacts", "email")),
    postcondition_paths=("content.readback_verified", "audit.readback_verified"),
)


def _decode_contacts_write(status, body):
    answer = None
    if body and status is not None and 200 <= status < 300:
        try:
            rec = (json.loads(body.decode("utf-8")) or {}).get("contact")
            if isinstance(rec, dict) and rec.get("id") not in (None, ""):
                answer = rec["id"]
        except (UnicodeDecodeError, ValueError, AttributeError):
            pass
    return status, answer


CONTACTS_SUBMIT_CONVENTIONS = SubmitConventions(
    volatile_keys=frozenset({"modified", "modifiedBy"}),
    record_key="contactId", line_key="lineId", name_key="name",
    epoch_zone=timezone.utc, decode_write=_decode_contacts_write,
)
CONTACTS_SUBMIT_SPEC = SubmitSpec(
    prefix="/api/contacts", browser_prefix="/api/browser/contacts",
    collection_key="contacts", conventions=CONTACTS_SUBMIT_CONVENTIONS,
    delete_template="/api/browser/contacts/{id}/delete",
)


class _ContactsEntityReadPort(DeclarativeEntityReadPort):
    def __init__(self, executor, entities=CONTACTS_ENTITIES, *, application_id: str = APPLICATION_ID):
        super().__init__(executor, entities, application_id=application_id)


class _ContactsFormPort(DeclarativeFormPort):
    def __init__(self, forms_or_executor=CONTACTS_FORMS, *, executor=None):
        forms = forms_or_executor if isinstance(forms_or_executor, tuple) else CONTACTS_FORMS
        if not isinstance(forms_or_executor, tuple) and executor is None:
            executor = forms_or_executor
        super().__init__(forms, executor=executor, required_scopes=frozenset({GENERIC_FORM_SCOPE}))


class _ContactsToolExecutor(DeclarativeReadExecutor):
    """Contacts read/write executor delegating generic transport & queries to ``DeclarativeReadExecutor``."""

    def __init__(self, origin: str = "", *, api_token: str = "", allow_mutations: bool = False, **kwargs):
        if "client" not in kwargs:
            kwargs = {
                "application_id": APPLICATION_ID, "client": DeclarativeRestClient(origin, api_token=api_token),
                "entities": CONTACTS_ENTITIES, "pages": CONTACTS_PAGES, "write_specs": (CREATE_CONTACT_SPEC,),
                "allow_mutations": allow_mutations,
                "scope_formatter": lambda _ctx: "contacts in the synthetic workspace the delegated actor may read",
                "entity_port_cls": _ContactsEntityReadPort,
            }
        else:
            kwargs.setdefault("allow_mutations", allow_mutations)
        super().__init__(**kwargs)

    def set_identity_port(self, identity_port) -> None:
        self.identity_port = identity_port

        class _LiveActorIdentity:
            def __getattr__(self, name):
                return getattr(identity_port, name)

            def verify_actor_for_write(self, ctx, *, required_scope: str) -> str:
                del required_scope
                identity_port.verify_live_actor(
                    ctx,
                    mismatch_error_cls=BusinessToolIdentityError,
                    mismatch_message="delegated API token no longer matches bound actor",
                )
                return ctx.principal.actor_id

        self._write_handlers = {
            w.name: DeclarativeTypedWriteHandler(
                w, self, application_id=self.application_id,
                identity_port=_LiveActorIdentity(), require_context=False,
            )
            for w in self._write_specs.values()
        }

    def _contact_rows(self) -> list[dict]:
        return self.client.get_collection("/api/contacts", collection_key="contacts", id_field="id", required_keys=("id", "name", "email"))

    def _post(self, path: str, payload: dict) -> dict:
        return self.client.post_mutation(path, payload, record_key="contact", conflict_message="a contact with that name already exists", forbidden_message="the application does not let this actor create contacts")

    @staticmethod
    def _normalise(args: dict) -> dict:
        return _normalise_create(args)

    def _existing(self, name: str) -> list[dict]:
        result = self.execute("list_contacts", {})
        if not result.content["completeness"]["source_complete"]:
            raise BusinessToolError("the duplicate check could not read every contact")
        key = business_text_key(name)
        return [row for row in result.content["contacts"] if business_text_key(row["name"]) == key]


class MinimalContactsAdapter(DeclarativeApplicationAdapter):
    """Minimal Contacts adapter built directly on the Declarative Onboarding SDK."""

    def __init__(self, origin: str, *, api_token: str = "", allow_mutations: bool = False,
                 mutation_journal=None, extra_skills=(), standard_skills: bool = False,
                 convention_updates: bool = False):
        spec = DeclarativeAppSpec(
            application_id=APPLICATION_ID, display_name="Minimal Contacts",
            entities=CONTACTS_ENTITIES, pages=CONTACTS_PAGES, forms=CONTACTS_FORMS,
            page_resolver=lambda _c: ResolvedActor(""),
            navigation_mode=FRONTEND_PRESETS["plain-html"].navigation_mode,
            tool_route_patterns=(r"/contacts", r"/contacts/new", r"/contacts/[a-z0-9-]+", r"/contacts/workflows/(?:partner|supplier)"),
            skills_dir=PACKAGES_DIR,
            legacy_skill_files=(SKILLS_DIR / "contacts-query.json", SKILLS_DIR / "runtime-records-query.json"),
            legacy_write_skill_files=(SKILLS_DIR / "contacts-create.json",),
            write_specs=(CREATE_CONTACT_SPEC,), status_toggles=(SUBSCRIPTION_TOGGLE,),
            default_read_scope="contacts:read", default_write_scope="contacts:write",
        )
        super().__init__(
            spec, origin, api_token=api_token, allow_mutations=allow_mutations,
            mutation_journal=mutation_journal, extra_skills=extra_skills,
            standard_skills=standard_skills, convention_updates=convention_updates,
            menu_fetcher=lambda _ctx: self._executor.client.get_json("/api/pages"),
            scope_formatter=lambda _ctx: "contacts in the synthetic workspace the delegated actor may read",
            executor_cls=_ContactsToolExecutor, entity_port_cls=_ContactsEntityReadPort,
            form_port_cls=_ContactsFormPort,
            operations_factory=lambda ex, idp: contact_operations(
                ex, allow_mutations, convention_updates=convention_updates, identity=idp
            ),
            identity_port_cls=lambda **kw: HttpWhoamiIdentityPort(
                origin=origin, api_token=api_token, default_tenant_id="synthetic",
                endpoint_label="contacts", **kw,
            ),
        )


from .packages import contact_operations


def ContactsModelGoalPort(
    adapter: MinimalContactsAdapter, *, drafts: bool = False, filters: bool = False, updates: bool = False
):
    from .goal import ContactsGoalPort

    return DeclarativeModelGoalPort(
        adapter,
        goal_port_cls=ContactsGoalPort,
        drafts=drafts,
        filters=filters,
        updates=updates,
    )


def __getattr__(name: str):
    if name == "ContactsGoalPort":
        from .goal import ContactsGoalPort

        return ContactsGoalPort
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class ContactsProcedure(DeclarativeProcedure):
    default_entities = CONTACTS_ENTITIES
    default_forms = CONTACTS_FORMS


def ContactsReadActions(adapter: MinimalContactsAdapter, *, filters: bool = False) -> DeclarativeReadActions:
    return DeclarativeReadActions.from_adapter(
        adapter,
        allowed_routes=frozenset({"/contacts", "/contacts/help", "/contacts/workflows", "/contacts/workflows/partner", "/contacts/workflows/supplier", "/contacts/new"}),
        filters=filters,
    )


def ContactsDraftSteps(adapter: MinimalContactsAdapter) -> DeclarativeDraftSteps:
    return DeclarativeDraftSteps.from_adapter(adapter)


def ContactsMutationPrefill(adapter: MinimalContactsAdapter) -> DeclarativeMutationPrefill:
    return DeclarativeMutationPrefill.from_adapter(adapter)


class ContactsSubmitPort(DeclarativeSubmitPort):
    allow_form_submit = True

    def __init__(self, client, journal=(), reads=None):
        super().__init__(CONTACTS_SUBMIT_SPEC, client, journal, reads)

    def prefixes(self, reads: tuple, *, mode: str, record_id: str, source: str = "") -> tuple[str, ...]:
        if not isinstance(source, str) or source != self.spec.prefix or not any(path == source for path, _ in reads):
            return ()
        if mode == "update" and not record_id:
            return ()
        return (self.spec.prefix, self.spec.browser_prefix) if self.spec.browser_prefix else (self.spec.prefix,)

    def catalog(self, prefixes: tuple[str, ...]) -> frozenset:
        if self.spec.prefix not in prefixes:
            return frozenset()
        found = {("POST", self.spec.prefix), ("PUT", self.spec.prefix)}
        if self.spec.browser_prefix:
            found |= {("POST", self.spec.browser_prefix), ("PUT", self.spec.browser_prefix)}
        return frozenset(found) | super().catalog(prefixes)


def contacts_profile(adapter: MinimalContactsAdapter, goal_port, *, drafts: bool = False, filters: bool = False):
    return build_declarative_profile(
        adapter, goal_port, procedure_cls=ContactsProcedure, drafts=drafts, filters=filters,
        draft_steps=ContactsDraftSteps(adapter) if drafts else None,
        mutation_prefill=(ContactsMutationPrefill(adapter) if drafts and adapter.ports.operations is not None else None),
    )


__all__ = [
    "APPLICATION_ID", "CONTACTS_ENTITIES", "CONTACTS_FORMS", "CONTACTS_OPENAPI",
    "CONTACTS_PAGES", "CONTACTS_SUBMIT_CONVENTIONS", "CREATE_CONTACT",
    "ContactsDraftSteps", "ContactsGoalPort", "ContactsModelGoalPort",
    "ContactsMutationPrefill", "ContactsProcedure", "ContactsReadActions",
    "ContactsSubmitPort", "ENTITY_READ_SCHEMAS", "MinimalContactsAdapter",
    "RESOLVE_RECORD_REFERENCE_SCHEMA", "SCOPES", "_ContactsEntityReadPort",
    "_ContactsFormPort", "_ContactsToolExecutor", "contact_operations", "contacts_profile",
]
