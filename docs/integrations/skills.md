# Skills and typed operations

A Skill is application-owned knowledge and workflow guidance. A tool is an installed
capability with typed input, actor scope, effects and an application handler. They are
registered independently. Runtime application facts take precedence over Skill prose.

Each installed package contains `SKILL.md` with YAML `name` and `description`, followed
by Markdown instructions. Use descriptions for discovery and the body for business
meaning, missing-input handling, queries, page workflows and stop conditions. Put
longer material under `references/`; the Agent can load installed text resources on
demand. The host snapshots and hashes the package at installation. It rejects scripts,
symlinks, URLs, path escapes and oversized packages. Content remains untrusted input.

The goal planner offers `read_skill_resource` only when a loaded package has readable
text attachments. Its schema lists their installed Skill identities and paths;
dispatch checks the path against that package's snapshot. The loaded card lists text
attachments under `references/` and `assets/`, excluding images and invalid UTF-8.
`SKILL.md` instructions are already loaded and are not a resource-read target.

```text
customer-maintenance/
  SKILL.md
  agents/woa.toml
  references/business-meaning.md
```

`agents/woa.toml` declares reviewed application identities for knowledge selection and
the proposed-write boundary without copying tool schemas:

```toml
schema_version = 1
scope_entities = ["customers"]
scope_pages = ["customer-help"]
write_operations = ["create_customer", "update_customer"]
```

`scope_entities` and `scope_pages` are optional exact application-index identities,
not natural-language keywords. A knowledge-only package may declare them with no
write operations. Query/page knowledge is selected after ordinary scope interpretation. Write
supporters also inherit their installed operations' entity identities; additional
target entities never replace the entities described by their input bindings.

With business mutations enabled and effect-contract Skills installed, the same
scope model call offers both select_skills and the existing declare_scope.
Execution selects installed identities by package descriptions; selection contains
no business fields, values, targets, steps or write intent. The native proposal Agent
receives the complete original request, knowledge, schemas and current actor facts.
Ordinary queries/pages/preparation/source questions use the original scope parser;
query knowledge can still be attached after it. Legacy operations without effect
contracts keep their admission. No keyword table is required. Invalid routing gets
one structural correction and then stops before preparation. Selection never grants
authority. Write-disabled deployments retain their existing messages and recovery.
A source mixture can return selection and existing source scope together, preserving
separate live reads for current comparisons; plain execution needs no source slots.

The optional standard `allowed-tools` field is descriptive. It grants no authority.
An operation list neither installs a tool nor changes deployment enablement, actor
permissions, page policies or approval. Reads and pages retain their separate policies.
Integrations using `require_skill_for_writes=True` bind each write to one explicitly
loaded supporting Skill and its immutable digest. Missing or changed support fails closed.

Define a `ToolDefinition` for each reviewed operation and register it in `ToolRegistry`.
The definition owns input schema, read/write effect, scope, entity/operation, handler,
version, permitted result-reference inputs and optional reviewed input-to-field bindings. Do not expose arbitrary endpoints,
methods or raw native request bodies as Agent arguments. The application handler
resolves business names under the actor, prepares exact effects and full approval
details, performs conflict checks, dispatches once and verifies every promised effect.

`BusinessWriteService` shares this chain between goal declarations and the bounded
Agent loop. Execute intent is required before a proposal; each independent write
requires fresh visible one-use approval. Earlier verified results can bind later
declared reference inputs. Earlier success survives a later refusal or uncertain
write; the workflow is not a transaction and does not retry or roll back automatically.
The goal declaration receives the application's date and time zone. A known
unsupported constraint on a declared write stops the workflow before approval;
the server does not create a smaller effect and then report the missing constraint.
The existing optional candidate-value port also supplies current actor-read names
before the Skill declaration. Candidate values do not authorize a write or resolve
ambiguity by themselves. Only operations supported by the loaded packages appear
in this declaration surface, further filtered by deployment enablement and the
actor's scopes. Selecting a Skill admits native proposals, but only the full request
and a valid execute proposal can request a business change. Static explanations offer no
operations. Selection and availability never authorize dispatch.

Compound operations extend this lookup and their planning index with the reviewed
entity scopes of the tools actually offered under the current deployment, actor and
loaded packages. Unavailable operations contribute no scopes. This uses the full
actor-visible index; it does not change ordinary non-Skill lookup or grant access.
Unresolved scoped names can also seed the existing candidate-value port. Ambiguous
results are offered with their complete actor-read displays, including owner and ID
suffixes; they are not promoted to uniquely located references. Reference inputs
use those exact displays and the application resolves them again before approval.
An adapter may raise `BusinessToolReferenceError` with bounded current candidate
displays during read-only preparation. In the single path with registered reads,
this error can use the shared correction before dispatch: the Agent may read current
facts before replacing the rejected operation. Unresolved ambiguity returns the
existing single-choice clarification before any mutation. Without planning reads,
or on the anchored path, reference errors retain direct clarification. A user reply
begins a fresh declaration, actor read and approval. No dispatched or uncertain write
is retried. Permission errors bypass correction; missing records without candidate
choices retain their refusal behavior.

Loading a Skill for an ordinary query or page request adds knowledge to the existing
goal interpreter. It retains independent readings, bounded resolution repairs and
source obligations. Selecting knowledge does not switch every request into a write
planner. Static Skill answers cite exact installed passages.

An operation may install an application-owned `effect_contract` whose `project_inputs`
returns a `RequestedEffect` in application entity/field vocabulary. This vocabulary is
separate from `ToolOperation` policy identifiers. Yudao and the synthetic Contacts REST
adapter implement the contract.

The default `complete_request_check="single"` directly proposes native operations and
existing read/page declarations from the complete request, loaded knowledge, current
actor-read candidates and installed schemas. Preliminary scope selects knowledge and
tools; its inferred categories do not become mandatory proposal steps. A record lookup
supporting a change is not a separate user-requested read. Complete-request evaluation
must detect genuine omissions from the proposal. The call explicitly requests tools;
ordinary calls retain automatic tool selection. One or several declarations may arrive
per decision, and accepted declarations accumulate until `finish_skill_task`. Finish
ends a proposal, not business completion. Native proposals use the planning envelope
`{"request_intent": "execute", "inputs": {...}}`. An operation can register
`ToolDefinition.preservation_fields` and `preserves_unmentioned` alongside its effect
contract. Only then does the planning schema offer `preserve`, using those exact field
identities and, when supported, `"*"`. Display labels and unregistered identities fail
validation. Ordinary patch preservation is application-owned and needs no model-added
constraint. The envelope never reaches handlers or widens authority.

An empty finish declaration is invalid. Schema, source and read-only preparation
failures share one correction budget. Exhausting it retains a declaration failure
in the terminal state and audit; it does not imply nonexecution intent or yield
to the ordinary path. An explicit nonexecution or capability-limit declaration
may still yield before approval. Permission failures never enter correction.

The same Agent may call existing registered read tools that are available under the
actor and deployment. Read calls must form a separate batch from proposals and
finish, and all arguments are checked before any read executes. Read argument checks
use the published JSON Schema, including selector unions, opaque
ID patterns and dynamic filter values. Schema references must resolve locally; the
application still validates current identities, field semantics and actor authority.
Actual results return in tool messages before the next decision; they are facts, not approval. A safe read
request error preserves earlier results and marks the rest of that batch unrun. Reads
share a bounded count across initial planning and preparation correction, plus the
existing turn time, token and spend limits. They do not enter the declared task steps
or change `@result:N` positions. Skill files cannot grant read access.

For the shared entity-read surface, the planner offers reference lookup by entity
description before any opaque identity has been read. Entity descriptions become
available with returned entity IDs; field queries become available with returned
field IDs. Schemas constrain IDs to those facts and keep each field bound to its
described entity. Query record bodies cannot issue new catalog identities. Custom
read tools keep their installed contracts. This narrows the planning surface only;
application catalog freshness, actor checks and the ordinary non-Skill tools remain
independent. A batch cannot depend on identities its own earlier reads will return.

Before the first approval, the server checks every declared schema, capability and
reference dependency, and performs read-only preparation of independently bound
operations. Dependencies must name earlier operations; scalar, nested and reference-set
inputs bind only verified results. Text outside registered reference paths remains
literal data, even when it resembles dependency syntax. Later dependent operations are
prepared only after their inputs become known, with separate fresh approval.

Schema, literal-source and read-only preparation errors share one correction budget.
Preparation feedback replaces only the failed operation, retaining the other accepted
steps. Safe reference feedback may use registered reads first; unresolved target
ambiguity asks the person. Permission, provider, approval and uncertain write failures
never enter this correction path. The server cannot prove that a model
identified the right field or retained every requirement in the original natural-language
request. Source fidelity and matching effect projections establish narrower facts.

The optional existing `complete_request_check="anchored"` performs an additional
independent reading of supplied facts and compares it with the proposal. It is not the
default delivery path and does not establish semantic completeness or reliability.

Application preparation returns `BusinessMutationSpec.effect_projection`. The immutable `EffectProjection` contains the semantic change decoded from the actual prepared native body, exact application bindings, preserved inputs, derived/default/compound effects, and required receipt codes. The server checks the projected change and requested preservation before showing approval. The projection participates in `BusinessMutationProposal.content_digest`, so changing it invalidates that approval. After dispatch, the shared runtime checks required application verification receipts before journal completion. An acknowledged but incomplete result is indeterminate, retains reconciliation evidence, and cannot be automatically replayed. Reads/pages resolve at their planned position, using refreshed facts after writes; prior verified results alone can bind `@result:N` dependencies.

Registered `literal_inputs` use `{"source": 0, "text": "complete exact value"}` in planning. Source 0 is the current request; later sources contain bounded earlier user requests only. The server checks exact source copying and the original installed schema before preparation. This proves provenance, not semantic completeness. Neither matching effects nor an optional second model reading proves that every original requirement was retained. Body/notes text remains data even when it contains imperatives or negation. Missing information discards draft effects and asks the person.

Loading a Skill for an ordinary query/page/source answer keeps the existing interpreter and its repair budget. Integrations without an effect contract retain the existing declaration path and its limitations: literal copying and field coverage do not prove full-request correctness. The optional effect contract does not grant writes, install tools, widen actor scope or add browser capabilities. It checks the declared parameters against actual effects. Model interpretation errors remain possible and require independently scored complete-request evaluation.

Approval details include user changes, necessary preserved values, derived values,
side effects, full bodies and exact rule targets. The server bounds this projection
without truncating it. A browser without `approval.details.v1` cannot approve such a
write. The application verifies the postcondition under the same actor. Uncertain
dispatch or readback stays indeterminate; a later page-observation failure preserves
an already verified mutation receipt.

The goal and loop strategies are experimental. Scripted contract coverage does not
establish model success rate, performance or which strategy should be the default.

Hosts may pass a `TurnEvents` collector to either executor. Its optional callback
receives only lifecycle metadata from one thread-safe monotonic clock. Model,
application, page and verification intervals close on exceptions too. The existing
conversation host binds the collector before accepting messages; `request_received`
is emitted by the normal session channel after protocol/identity acceptance and
before queuing the message. Queue time is included. Protocol-invalid or rejected
HTTP requests never become accepted turns. Direct executor users have only their
executor ingress; they must not describe it as HTTP acceptance. Returned event lists
and callbacks are copies of the same append-only metadata sequence; callback errors
permanently mark delivery incomplete rather than inventing missing timestamps.
The existing browser observer supplies visible-card and visible-result endpoints; a server
terminal is not evidence of visible delivery. Automatic approval-driver work is
identified separately and ends when the normal approval channel returns its
persisted decision. Turns without a real manual wait emit no human-wait interval.
Observer failure marks evidence incomplete and cannot change write authority.

Integrations may declare `RegisteredOperations.observed_mutation_operations` only
for reviewed writes whose actual one-shot transport uses `mutation_transport`.
`BusinessWriteService` binds its receipt to the proposal, operation and consumed
approval. Handler entry alone remains not-dispatched. Application acknowledgements
mark applied or rejected; uncertain transport or invalid acknowledgements retain
unknown. A subsequent failed readback preserves an applied transport receipt and
an indeterminate business outcome. Components have separate receipts, and duplicate
component invocations remain visible. Receipts prove execution facts, not complete
user-request satisfaction. Undeclared or unbound mutation paths prevent complete
dispatch coverage; an empty receipt list alone proves no such coverage.

The Yudao browser-session host publishes `passive_turn_events`,
`turn_event_delivery_complete`, `dispatch_receipts` and `dispatch_coverage_complete`
on normal turn records. Its supported session configuration permits native writes
only through the nine reviewed API operations; form submission and generic clicks
stay disabled. Each operation makes one transport call. Customer creation includes
owner permission; follow-up creation includes the main-record update in that same
application request. Those effects still require separate application readback.
Contacts proves the same transport contract over a REST creation endpoint. Odoo
declares no observed mutation boundary. Request/body capture is absent, so these
metadata receipts cannot attribute incorrect values to model, card or body layers.

The normal Yudao host also supports explicit `--native-writes` (goal executor only)
and uses a persistent SQLite mutation journal. The default remains read-only and
generic browser submission stays disabled. The original nine operations have scripted
real-browser approval and native readback coverage through the disposable demo;
broader natural-language reliability and combined guarded-page workflows remain
unqualified. The standalone demo and the normal deployment host are separate entry
points; coverage of one does not establish coverage of the other.

The Yudao observer polls the normal outcome with a 250 ms wait and reads cards with
the existing browser locator across the panel's shadow root. It scrolls to and waits
600 ms for the complete-content approval gate before reading all card text and
calling the approval policy. A card observation is bounded by 15 seconds.
Final-result observation waits at most 15 seconds
for the bound settled result and completed text reveal. Browser/transport delays
are included; these endpoints are observation times, not exact first-paint times.
