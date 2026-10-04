# Web Operation Agent

[简体中文](README.zh-CN.md)

Web Operation Agent is an alpha framework for embedding an AI assistant in a first-party
web application. Planning, model access, credentials, policy, approvals, and audit state stay
on the server. A small browser Runtime observes the current page, executes fixed generic
actions, displays progress and confirmation UI, and returns receipts.

The repository has one public reference implementation: [Odoo 18](examples/odoo/README.md).
It is not a hosted service, a general browser extension, or a production-ready autonomous
agent.

## Current capabilities

- A goal-directed turn asks the model to declare the request against the application's index (entities,
  fields, pages, views, filters, groups, sections and controls, compiled by the adapter
  under the acting user). The server resolves each declaration deterministically: records by the application's own name search,
  field values by typed reads, counts, sums and numeric extremes by scoped queries, pages by reviewed action
  routes, tabs by their visible name, filters and groups by the page's own facet state, and
  a requested change by highlighting its control for the person. Working the page still takes
  the turn's one leave, every page command runs under the application's page write control, and no
  control that could write is pressed. The reply is rendered from resolved values, never
  from model prose, so a fact the turn reports is one it read. A part that could not be done
  is told as such, with the page to check and the records that fit; the resolver's own
  reason, written in the index's terms for a repair, stays in the audit. The Odoo demo and the goal
  evaluation runner take this path for read-only sessions; mutation sessions and the
  scripted apparatus keep the step-by-step loop below. The declaration is one model output
  and can name the wrong entity, field, scope or search words; the resolver reports those
  as not done rather than guessing, apart from three deterministic fallbacks (a reference
  found on an entity that links to the named one, a label containing a link field's label,
  the page's own "My …" facet when the person asks for everyone's records), and a zero
  count inside a page's scope is reported together with the entity-wide count. Conditions
  take comparison operators on numbers, dates and text, including a prefix ("starts with"); a date condition accepts a day,
  month, year, range or a named period (today, this month, last quarter, last 30 days,
  overdue), and a page filter that names "today" is evaluated for the turn in the user's
  time zone. The lines a record's form embeds (order lines, invoice lines) are entities of
  their own, so they can be listed, counted and summed by their link to the record, and
  the owner may be given as a condition the link itself cannot resolve (an invoice number
  on order lines, an amount or a date that describes the order). A record the person
  describes rather than names ("the quotation sent to Joel Willis", "last month's
  customer invoice", "the 31,750 credit note") is picked out by its conditions within the
  declared page's scope, or beyond the page's defaults when they hide it; only exactly one
  match is acted on, and none or several is said so, with the candidates. A name condition
  that matches several records can bind the sole candidate in the actor's complete,
  fully specified scope. Incomplete reads or unusable conditions cannot prove uniqueness.
  A named record
  that does not exist is never turned into a creation; a creation opens the page declared
  for the entity, else its widest page. A list that finds exactly one record binds a
  later deliverable of the same turn that names none. The declaration is made knowing the
  application's current date, so a month or day said without a year lands in the right
  year. When the resolver finds something a different declaration would fix (a field the
  entity does not have, a description that matched several records, a change asked for
  with nothing to point at), it says so in the index's terms and the model declares once
  more, for at most two repair rounds. A repair is accepted only when the unresolved
  work decreases, or at equal unresolved work when it enables a useful clarification;
  the kind of record the person named is preserved. A handoff that carries the value the person gave for a field enters
  it first (typed text, a date at day precision, a choice the field offers, or a linked
  record typed by name and chosen among the field's own suggestions) and checks what the
  field then shows; saving stays with the person, the page write guard holds throughout,
  and the reply says to discard the change if it is not wanted, since the form saves
  itself on leaving. A value that cannot be entered with certainty is left to the person
  and said so. Field labels match by whole words
  or by the field's own name, never by substring. A view the page lacks is stated in the
  reply while the page opens in its own view. A condition on an unstored computed field
  is refused rather than silently ignored, unless its value names one of the page's own
  filters. The resolver's reach is measured by a zero-model matrix over the reference
  fixture (`evals/odoo/goals/resolver_matrix.py`: 184 declarations across entities,
  deliverable kinds and condition types, all read-only RPC). Odoo implements the full
  reference index and resolver; the structurally different Contacts REST test application
  implements the same core port for actor-scoped facts, counts, lists and basic pages, and
  explicitly refuses unsupported page controls. The port contract has two
  levels: the required index, records, facts, counts, lists and page addresses, and
  optional capabilities a port declares by name (page filters and groups, tabs, embedded
  lines, controls to point at, value entry, page search, source rules); a deliverable
  that needs an undeclared one is answered with what the application does not offer,
  without asking the port. A question about the assistant itself ("what can you do?") is
  declared as such and answered by the server from the same list: the required reads,
  plus guiding a change, entering a value or explaining a rule only when the port
  declares them. The Contacts application declares tabs and handoffs and takes
  goal turns in its demo. A turn is marked complete only when every declared deliverable
  settles. An actor-index page may also offer navigation without a record entity;
  it cannot declare data reads, filters or form operations. Explicit page selections
  survive entity narrowing. Such navigation requires a fresh application-owned
  `page_route` observation proving the rendered destination; the URL, title or global
  navigation controls alone cannot establish success. Missing or mismatched proof
  leaves the navigation unverified. Large navigation directories expose a bounded ranked
  sample, with space reserved ahead of entity summaries; omitted entries are not proof
  of absence. Goal declarations receive a redacted visible-page reference projection
  of at most 4 KiB, without selectors, input attributes or action references. The
  visible control-name projection prioritizes working controls over navigation containers and
  removes duplicate names within the same budget. These hints do not establish applied
  query parameters or authorize actions. Changing
  visible context invalidates the declaration cache; an observation epoch alone does
  not. Runtime checks still establish permissions and action targets.
  The native Yudao integration and Contacts goal demo can prioritize a current-record
  candidate through the existing `values` capability. The rendered page and record
  routes must agree, an actor-bound application read supplies its name, and the live
  document observation must remain unchanged across that read. The candidate carries
  both name and identifier; resolution rechecks them against application data so a
  duplicate name cannot select another record. A URL alone supplies no such candidate.
  This grounding is a hint to declaration, not proof that the model chooses it correctly.
  Yudao can extract literal record-tab labels from deployed Vue components
  when native sections are enabled; dynamic labels remain unresolved. Source labels
  do not replace live visibility or selected-tab verification. Its native integration
  also requires rendered-route proof for data lists, record details and form handoffs.
  Same-origin application GETs that are pending or fail block arrival, including
  business errors returned with HTTP 200 and failures whose toast has disappeared.
  Simple parameter routes require the exact live component; CRM details additionally
  require a successful exact-record read and matching visible title. Read admission
  still depends on the deployment's reviewed API policy.
  Yudao's identifier-free flat unpaged tables retain every row, including identical rows,
  using query-local read references instead of business record IDs. Such tables are
  read-only and have no record form. Server-bound query scopes use the existing native
  condition contract, validate declared parameters and separate query caches; the
  Contacts REST-derived report tests exercise the same scoped core reads without source.
  The native integration can bind the general ledger's successful applied query
  from a fresh observation of the active actor/session/turn document and rechecks the
  account set through the same actor's API. Draft months, pending or failed reads,
  mismatched pages and another document's query cannot establish that scope.
  The initial application projection covers account set, start/end month and level
  bounds; additional query criteria remain unbound. Opening this report under an
  active guard still requires its FMS read chain to be reviewed in the deployment policy.
  The Agent retains the refreshed
  observation before resolution. Ordinary API reads do not require this page refresh.
  Read-only entities retain every declared typed query field in the index even when
  a dynamic UI column cannot be extracted; this does not expose form entry or writes.
  The declaration is staged by default: the request's hard references (document
  numbers, codes, Latin-script names) are cut out by shape and located by the adapter, the
  model first picks entities and deliverable kinds from ranked entity summaries, and
  then declares the details against those entities' neighbourhood of the index (their
  links, lines and owners, a few KB rather than the whole index), with the request at the
  start and at the end of that message; the details are read twice and reconciled like a
  single declaration. If both readings resolve to different answers, two additional
  independent readings vote among those resolved answers; an unresolved tie falls back
  to the reconciliation path. A tie between an answer and a clarification that offers
  that answer may keep the answer. These extra readings and the bounded repairs add
  model calls, latency and spend; this is not a one-call turn.
  Adjacent field reads with exactly the same entity, record, link and query context
  agree whether grouped into one fact or split across facts. Field order and duplicate
  labels do not create a vote; the original declaration and display order are retained.
  Conditions, rule requests and intervening operations are not collapsed.
  A valid zero count or empty filtered list is an answer, not evidence that a competing
  positive interpretation is correct. Date exclusion selects known dates outside the
  half-open period and excludes missing dates; adapters apply the user's time zone.
  Date exclusion headings retain the exclusion operator in both languages.
  Query results may carry page links, but only a declared page or handoff goal navigates.
  Numeric minimum/maximum aggregates and lists return every tied record in a complete scope of at
  most 4,000 records. Requested display fields stay attached to every tied record;
  these fields are read over the same complete actor scope before choosing the ties.
  Missing values are excluded; zero and negative values remain.
  Incomplete reads, invalid numbers and differing or unknown currencies are refused;
  native field units are retained when the application declares no currency. No currency
  conversion is inferred. Counts checked before and after the read detect count changes,
  but do not provide a database snapshot across those reads.
  A candidate's displayed date does not replace a declared date condition. An empty
  scoped lookup states that no matching record was found under the current account.
  Explicit local timestamps compare instants on datetime fields, with storage-zone
  conversion owned by the adapter; date-only fields do not accept time precision.
  Clock components are excluded from amount and record-number grounding.
  A repair must retain every declared deliverable; dropping an unresolved part cannot
  make a turn complete. It must also retain record targets, requested sections, views,
  facets and query restrictions; correcting a name cannot downgrade a record detail
  to an unqualified collection. Ambiguous linked names expose exact candidates for a bounded
  repair that preserves the user's explicit owner or exclusion conditions; they do not
  authorize choosing a candidate. When one part needs a record choice, the reply keeps the other
  parts' resolved answers and limitations alongside the clarification. Entity summaries
  describe types and incomplete samples; names absent from them remain unresolved until
  application reads, including when repository answers are enabled.
  A link declaration whose requested fields belong only to the original entity is
  refused before record reads and gets direction-specific repair feedback. An owner
  restriction is a condition, not permission to follow that link or replace fields.
  An adapter-classified permission denial names the unreadable record type while
  retaining other answers; it is not inferred from exception text or a failed service.
  Refused reads retain the requested operation and field labels in the reply. An
  adapter-classified service failure cannot be repaired by dropping query conditions
  or hidden by a cached-list fallback. Neither outcome establishes an empty result.
  Explicit count-grouping phrases naming an indexed field are checked before reads;
  an unresolved grouping is reported rather than replaced by an overall total. This
  narrow check does not establish coverage of implicit or translated categories.
  When an accepted repair removes unavailable requested fields, its explanation
  remains beside available answers and the turn does not report complete success.
  A request containing only unavailable fields returns that scoped limitation without
  offering unrelated record candidates. Mixed repository/current-data questions resolve
  actor-bound reads first, then supply only those requested facts and known choice codes
  to the source answer; rule application is an inference, never proof of execution.
  Goal stages share one deadline and model-request limit (each
  request or retry uses one step; cache hits use none). Fixed browser and read substeps
  check the same deadline and cancellation. Browser commands use the smaller of the
  configured action timeout and remaining turn time; late receipts remain in the audit.
  A form fill is complete only after the exact requested or application-normalized value
  is observed on the same ready page and record. Failed selection never tries an alternate
  value; only a pre-dispatch stale-observation refusal can retry the same value.
  An adapter may resolve a manual-submit handoff to a server-only `FormPlan`, requiring
  `fill` and `handoffs` capabilities and `allow_form_prefill=True`. The current form port
  must confirm its exact entry reference and every requested field before input. Goals
  for the same entry combine at most twenty fields into one consumed draft and one
  verified handoff; conflicting values or different entries are refused. Other page work
  runs first, and the form handoff runs last. The server replies with the fields entered
  and the highlighted manual-submit button; it never submits. This path uses the existing
  fixed draft executor and shared deadline, adds no new tool or per-field model calls,
  and only admits transient forms. A handoff can declare up to twenty explicit field/value
  pairs in `field_values`, keeping the four-deliverable limit; read-only staged requests
  omit that batch property from their tool schema. Batch entry actions remain subject to
  application review: an existing form may omit the action or name its reviewed editor;
  a new form uses its reviewed creation entry. A batch never authorizes submission.
  Missing form values request clarification before navigation, approval or input. Empty
  model values require explicit clearing instructions naming indexed fields; unrecognized
  clearing phrasing is left for clarification. Missing values are never silently cleared.
  If the operation stops before input dispatch, the reply states that no values were entered; uncertain input keeps the partial-entry warning.
  Before any value is prepared, an unobserved submit control on an application-verified
  transient form permits at most three fixed read-only scrolls. Each fresh descriptor
  must bind the same entry; ambiguous or disabled buttons remain refusals.
  A reviewed opener may use `open_entry=True` and an
  application-owned entry port: the server clicks the exact observed control under the
  ordinary page approval, then binds its successful receipt to the fresh form before
  preparing values. DOM instance tokens invalidate replaced or reopened forms; they grant
  no authority. The Contacts goal demo opts in. Yudao keeps forms disabled by default;
  its explicit reviewed-policy prototype supports customer update scalars with live actor,
  record and permission checks. An isolated native browser has qualified all eight customer
  scalars in one declaration, with a visible approval, exact live values, a highlighted
  manual-submit button and unchanged business tables. Creation needs its own reviewed
  opener/component policy and create permission; project and complex control qualification
  remains separate. Defaults stay closed. Final handoff can recheck this Runtime's own
  entered controls outside the viewport in the same live form instance; it never fills an
  unobserved or hidden control.
  The `goal_run_receipt` audit event records
  stage times, request counts and serialized input bytes without prompt or result content;
  Business timing includes index preparation, name lookup and candidate reads;
  overlapping intervals are counted once within a stage and for total model occupancy.
  Token and cost totals come from the spend meter; unavailable usage remains unknown.
  Tab selection refuses duplicate exact names and ambiguous partial names. When a tab
  declares an ARIA panel relationship, arrival also requires a unique visible panel
  linked back to that tab, without a declared busy state or progress bar. This does not
  detect loading that the application leaves unmarked; tabs without that relationship
  retain the selected-control check.
  In the tool loop, a previously rejected deterministic request is warned and then stopped
  even if other rejected requests intervene. Changed page state or a successful business
  read invalidates that refusal; authentication, permission and transient failures are
  not remembered this way.
  Parsed read-only declarations have an executor-local, 128-entry cache with a
  120-second lifetime. Keys bind the principal, model/options, source digest and exact
  prompt context (including history, date, scope and candidate values). The two initial
  readings use separate entries; repairs, extra votes and write declarations are never
  reused. A new conversation clears the cache. Hits are audited as `cache_hit` with zero
  provider attempts; business data is resolved again, and page/write approvals remain fresh.
  `declaration_cache=False` disables this optimization for comparisons. Declaration
  instructions avoid repeating tool-field explanations, and scope/repair JSON is compact;
  the staged request still appears at both ends of its message.
  Every declaration field, description, kind, enum and constraint remains available,
  including mixed goals and repairs. The scope step never removes
  schema branches based on its predicted kinds, apart from omitting the optional form
  batch property for staged read-only requests. Scope summaries retain entity keys,
  aliases, controls, owner links and distinct page breadcrumbs; page keys appear there
  only when explicitly written in the request; detailed-index page keys are unchanged.
  Yudao's actor-local complete-list/detail cache stays at 15 seconds, starts its lifetime
  after a successful read, and holds at most 128 entries. A cached complete list can serve
  narrower local filters under the same required parent scope; credential renewal
  invalidates cached reads and linked-record names. Failed or incomplete reads are not cached.
  Yudao condition and name searches preserve duplicate-name choices with actor-readable
  owner labels, using exact record IDs when those labels also coincide. Selecting a
  qualified name resolves that record again under the same actor.
  Yudao also opts into four concurrent actor-bound reference lookups, preserving index order
  and checking every entity; other adapters keep sequential reads unless explicitly enabled
  with `DataGoalPort(reference_workers=...)`. Each reader owns its resolver.
  A 2026-10-01 real-model comparison using `qwen3.8-flash` on the previously seen, sealed
  Yudao v6 set scored 59/63 before and 61/63 after, with the original judge unchanged.
  Two score changes reflect answer-label matching rather than corrected data; the new
  scope failure passed two fresh-session rechecks in each version. This does not establish
  general accuracy equivalence. Cold median attempt time stayed essentially unchanged
  (21.99 vs 22.13 seconds), with 202 vs 208 model calls. Two fixed questions repeated five
  times each passed 10/10 in both versions; calls fell from 33 to 20. Once context stabilized,
  optimized repetitions used zero model calls and took 20–25 ms of server-turn time on the
  unchanged fixture; this is not UI end-to-end latency. The full comparison and rechecks
  used 478 calls (1,517,136 input and 142,386 output tokens), metered at CNY 0.862724 from
  provider-reported usage and registered rates, rather than a provider invoice.
  A subsequent scope-summary comparison on the same seen set scored 60/63 before and
  61/63 after; one new raw failure counted the correct 104 lines but lacked the judge's
  expected label. Median scope input fell from 11,457 to 10,331 serialized bytes (9.8%).
  Observed cold median time was 19.30 vs 17.26 seconds, but calls rose from 199 to 209
  and total input tokens from 604,532 to 616,745; this is not a general latency or token
  reduction claim. A variant rewriting tool descriptions scored 59/63 and repeatedly
  refused a previously answered condition; that variant was rejected and the original
  descriptions retained. Mixed count/fact/page requests and explicit page keys were
  checked in both versions; supplemental amount checks required adjudicating a thousands
  separator. The sealed judge was unchanged. These small fixture runs do not prove
  general accuracy equivalence.
  For an index larger than 60 entities, actor-visible labels, pages
  and fields retrieve up to 60 detailed candidates; the other entities keep only their
  key and label in a compact directory, with a roughly 12,000-character first-step budget.
  The model may select from either. An even larger index may leave some trailing labels
  out, so retrieval alone does not prove the requested entity is absent.
  `staged=False` on the executor (`--single` on the goal runner)
  declares once against the whole index, for comparison; the Contacts demo's scripted
  walkthrough does so. A port that declares candidate values (`values`) is asked, after
  the entities are chosen, for real values near what the request names: a named record
  and its lines, a named customer's recent documents on those entities (and their lines
  when lines are asked about), documents dated on a day the request names by shape
  (2026-08-29, 8 月 29 日, 8/29, Aug 29). They are shown numbered; the model writes an id
  where one is meant and the core binds it back to the exact name before comparing
  readings or resolving, so a description in another language is matched by choice
  rather than guessed. Nothing is searched across the whole database by a guessed name.
  The neighbourhood shows every field a chosen entity's list and form show (fields a form
  always hides are left out); the full index still shows twenty per entity. Fields the
  account may read that no page it sees shows (a product's weight while the feature that
  shows it is off) are held on the entity and shown to no ordinary reading: when both
  readings say the application does not hold what the request asks and the request names
  such a field, it is read once more with those fields. `values=False`
  (`--no-values` on the goal runner) measures what the candidates change.
  Before resolving, a staged declaration is checked by shape for what it leaves out: values
  the first step said were asked for while only a page or handoff is declared, or a code,
  amount or day the request wrote that appears nowhere in the declaration or the values it
  chose; such gaps go back to the model once. When several records fit and nothing tells
  them apart, the turn ends needing input: it asks which one, listing each with what
  distinguishes it, and a reply naming one by number or name continues the earlier
  request with that record. The same question is asked when a declaration names a record the
  request never names while the request's own conditions fit several, when it picks one of
  several records whose names hold the words the request writes (a pick that passes over the
  record those words name exactly, for a longer name, stands: the request said what tells
  them apart), and when one amount is asked of a few records and the request asks for no
  total; a record the request's own amounts, days or references pick out stands. A name the
  person chooses in reply is resolved as the record of exactly that name. On the development set of 94 Chinese requests the staged path
  answered 89 and the single declaration 87 (one full pass each). A zero-model replay of recorded runs
  (`evals/odoo/goals/reference_recall.py`) reports how many declared records the reference
  cut recovers. With a compiled source map attached to the Odoo port (the demo attaches
  the same compile the tool loop uses), the index carries rules of the deployment's
  source per entity: when a form control is shown or disabled, for which group, and what
  it opens or calls (including the window a method's returned action opens, cited at the
  lines that say so), when a field is read-only, required or hidden, what a model method
  refuses and under which condition (a refusal whose text another method returns says
  so), and what a field means by its help text. A window a control opens brings its own
  refusals and field meanings along, on the entity whose page has the control. Rules
  render only in a focused index (the staged path's neighbourhood), one catalogue entry
  per control or field and kind, ranked by the request's words and capped, never in the
  full index; an entity the request names by label, alias or the exact label of one of
  its controls joins the neighbourhood with a few rules. The model names rules by id;
  the server answers from the whole index with the rules of the same kind about the same
  thing and of the same operation, plus the same refusal where other entities raise it (the
  first sentence of its message, placeholders aside), and appends each rule's citation verbatim
  (`path:start-end@release sha256:<hex>` over the exact lines), marked as a rule of the
  deployed version's source, not current data. When it names none, the refusal repeats
  the person's question as the thing no rule was found about.
  An adapter may carry its organisation's conventions: what some words mean there ("订单"
  is every sales order, quotations included), written by the deployment in the index's
  terms and reviewed by its owner, never inferred from the application. Both declaration
  steps show only the conventions whose words the request says. A convention may also
  restate its meaning in structure, as an entity and conditions; the server then passes
  the ones the request names to the resolver, which widens the declared page's own scope
  by those records rather than letting the page leave them out (quotations beside the
  confirmed-orders page when the request says 报价), along the fields the entity's
  conventions are about, so the page's other conditions stay. A count or list declared
  through a page that narrows its entity on its own (customers among contacts) loses that
  page when the request names the entity and not the page ("联系人里…"). A term inside a longer term belongs
  to the longer one, and a convention meaning every record yields to a narrower one on
  the same entity. Their entities are always in the neighbourhood, as is the entity of a
  record the request names by its number.
  Two readings are settled before resolving: when one says what the application does not
  hold and the other answers from the same entities, the turn refuses and names what is
  missing; when exactly one resolves to something there, it is taken without a third
  reading; a sum counts as a total over every matching record only when both say so.
  A port may report an operation the account may not perform (`denied`; a new record is
  denied as a creation, not a change), which is said, naming the records as the person
  does (the entity's Chinese name in a Chinese reply), instead of asking which record. A
  control's rules declared for a record the request names by its reference ("S00005 的
  Confirm 按钮在哪") become a handoff to that control; when the page does not show the
  control, the reply gives the deployed source's rule of when it shows, with its citation,
  instead of guessing at permissions. When a request asks one thing and it is not done, the reply quotes the request; a reason the
  model wrote in a language other than the request's is not shown, the request is quoted
  instead.
  If the model declares a write intent but only resolves read-only pages or facts, opening
  those pages is reported as partial guidance, never as the requested change having occurred.
  For a new record, an adapter may expose an actor-scoped `create` operation on a collection
  page; the goal turn can open that page and highlight the live entry control, without
  searching for the proposed name as an existing record or pressing the control. A completed
  handoff is never recorded as a claim that the requested write happened.
- A server-side ReAct loop receives every tool result and a fresh page observation before
  its next decision, with step, time, token, and provider-spend limits.
  Integrations can set `require_page_route=True` in `AgentTurnConfig` for this loop:
  navigation and target completion then require a fresh application-owned `page_route`
  matching the current URL, with no pending render or blocking application error. A
  missing proof returns unfinished-state feedback to the same Agent; losing a target's
  proof invalidates its earlier checkpoint. This option defaults to false and does not
  change the selected executor or grant page actions.
- Application-owned Skills and typed tools provide current business facts. When explicitly
  enabled, deployment-bound structured facts plus a language-neutral local code-block index
  can answer source questions while retaining exact file, line, release, and byte-digest
  provenance on the server rather than showing source excerpts in the user answer. For a
  source paraphrase, the server also checks that a short supporting span occurs in the cited
  evidence; that check does not prove the model's interpretation of the span.
  A direct repository lookup can be configured with
  `SourceCapability(runtime_source=True, lookup=repository_lookup)` without a SourceMap or
  source compiler. It retrieves bounded code blocks, including Vue components and Java
  services, under the deployment's source allowlist. Raw blocks authorize no navigation or
  API call; page actions still need current runtime evidence and the existing policy.
  An optional local Git provider separately cites ownership, blame, commit, and version-diff
  metadata bound to the deployed commit. Source retrieval remains optional and is never a
  prerequisite for ordinary browser or API operation. A lookup that informs an operation
  does not itself turn the operation into a source answer: the action report comes from
  an observed runtime receipt. Source answers still require checked supporting evidence.
- An application-neutral entity-read contract can discover a current actor's readable
  business entities and perform bounded reads without adding one Skill per entity. The Agent
  receives only catalog-bound opaque entity and field IDs; an adapter translates its ORM,
  REST, OpenAPI, or other native metadata behind the contract. The core validates an
  AND-only filter AST and pagination bounds; the contract requires stable adapter-owned
  ordering and exact counts. Odoo and the structurally different synthetic Contacts REST
  adapter run the same contract tests. When one answer combines different read scopes,
  numeric headlines identify their own scope; this does not prove that the Agent selected
  the scope the user intended. The Odoo reference also opts into a narrow count/sum
  answer check: before its first business read the Agent declares the requested read
  collections and measures; the server binds computed numbers to the executed query and
  refuses a complete answer that substitutes another collection. This does not yet verify
  that the declared collection, subject, or filters match the user's intent.
  A separate `resolve_record_reference` read can locate bounded name/number candidates in
  actor-readable entities without first calling discovery, field description, and a record
  query. It returns ambiguous matches rather than selecting a record, and an empty result
  is not a global nonexistence claim. Odoo uses the actor's native name search without the
  narrower `query_customers` collection filter; Contacts implements the same contract over
  its REST data. This is a record-location primitive, not a completed-answer or page-action
  guarantee.
  Once a type is known, this same tool accepts its current opaque `entity_id` instead
  of `entity_query`, and revalidates the actor/catalog binding before searching only
  that type. The two arguments cannot be combined. A bound type's permission refusal
  remains a typed refusal rather than widening to other types. Results identify the
  types actually searched and their incomplete/refused reads; recovery hints ask the
  Agent to check the original name and type independently, without adding type words
  to a name or substituting a collection for a requested record. These hints neither
  choose a record nor establish that the model understood the request.
- A separate `find_page_entries` read ranks current-actor page/action metadata, including
  entries with no menu breadcrumb. It returns a route only for an adapter-reviewed,
  standalone destination; an unreviewed or current-record-dependent action is information
  only. Ordinary page policy and fresh page-state verification still govern navigation.
  Odoo translates its window-action catalog and Contacts its REST application's page map
  through the same core contract. An optional detail query also ranks form-field and tab
  hints for at most the first two openable candidates from actor-visible adapter metadata,
  without adding another Agent tool. Odoo uses the actor's default form view and field labels;
  Contacts uses its page map. These are
  not selectors or evidence that the requested part is present on the live, possibly
  action-specific view. This does not make every application page operable.
- A read-only `resolve_goal_candidates` call can combine up to three answer, page or
  handoff candidate lookups using those existing actor-scoped entity, record, field and
  page catalogs. It neither chooses a candidate nor freezes a complete user goal; the
  Agent must still bind the intended scope, read or observe the result, and verify the
  final state. This composition has passed zero-model Odoo and Contacts checks, but has
  not been shown to improve model accuracy or end-to-end latency.
- An optional application-neutral current-form contract supports a no-Skill fallback for
  safe scalar form filling. `describe_form` intersects the latest viewport controls with
  actor-scoped adapter metadata and returns only short-lived opaque IDs; `prepare_form`
  rechecks both sides and retains the exact native bindings on the server; `prefill_form`
  types that draft, checks the live field values and, where an application declares a
  read-only window, requires its acknowledgement of release before ending the turn.
  It never clicks or submits the form.
  Fixed prefill steps inherit elapsed turn time, recheck cancellation after approval,
  and stop after a late receipt without replaying entered fields or issuing the next step.
  Drafts prepared through this contract retain a server-only check of the actor, turn and
  full application form descriptor. It runs after approval and after fresh ready receipts;
  a changed entry stops the remaining fields and handoff. Loading permits only bounded
  waits before revalidation. Source or entry checks do not add model requests.
  A named field wrapper may identify its input; a containing HTML form's name cannot.
  The Runtime rechecks that binding at handoff and refuses an action if the wrapper changes.
  Live credential identities also redact the control's text and value projections.
  An adapter may instead issue an exact associated-label binding inside one DOM-owned form
  or dialog scope. A reviewed submit label in that scope is highlighted at handoff;
  fields, button, and guard release are rechecked before the server asks the person to submit.
  Duplicate labels or buttons and explicit conflicting field names are refused.
  A failed handoff does not claim the form is ready; some fields may already contain the
  draft. Odoo and Contacts translate different metadata sources to the same contract.
  An optional source lookup can inform a value for an existing field; a source-derived
  value carries a turn-local reference
  and exact supporting span for internal audit. This verifies provenance, not the model's
  interpretation. Source cannot create a field, selector, choice, permission, or write
  authority.
  The Odoo reference translation is available by default only for the exact scalar fields
  covered by its three standard-module browser cases (customer name/email, product
  name/price/invoicing policy, quotation expiration). Other create pages or fields are not
  promoted to safe drafts from `fields_get` metadata alone: arbitrary deployment-specific
  `onchange` code may have external effects that a read-only database transaction cannot stop.
  The separate existing-customer edit path requires approval before filling. Its Odoo
  controller adapter delivers the form's `change` event so a value visible in the DOM also
  enters Odoo's pending record; the reference pilot verified an email remained unchanged
  until the person left the page, then saved. This does not qualify arbitrary edit forms,
  in-flight requests started before the guard, or external effects.
- Turn context is assembled by source role: the current page is the live grounding baseline;
  bounded conversation history is included for referential or clarification follow-ups;
  Skills are selected from the current task; and deployment source remains a lazy cited
  lookup instead of being injected wholesale.
  The step Agent retains recent distinct page observations and replaces older server-marked
  snapshots with an omission notice. Business results, source citations, target state and
  policy feedback outside those snapshots remain intact; the current observation is not
  clipped by this compaction. This reduces stale page history, not every kind of model input.
- Browser actions are checked against the current observation and application route policy.
  Page observations are viewport-bound and expose at most 200 live controls; a bounded
  read-only scroll obtains the next fresh observation instead of scanning an unbounded DOM.
  Working the page needs the person's leave, given once per turn on a visible card at the first page operation (opening a page, clicking, filling, choosing, submitting, going back, applying a filter); the rest of that turn goes ahead on it, and the next message asks again. Reading, scrolling, waiting and highlighting ask nothing, so a turn that only reads and answers shows no card. The leave is not a write approval: each business change is still confirmed on its own card.
  When answering the card closes the menu the first operation's link was in, the server opens that link by its address instead of making the model reopen the menu.
  A successful same-page click can be
  reported as an interaction only when a fresh observation shows a changed page; this
  receipt is not proof that a business goal was completed. After an observed action,
  answer-format repair cannot dispatch another page action. A supplied answer that
  fails evidence verification is repaired even after page arrival, never silently
  dropped to report only the action. The same Agent can observe, use registered
  actor-scoped reads and compute during repair; the action receipt is retained and
  writes remain unavailable. Source-assisted page loops
  that revisit the same facts and states stop without claiming completion.
- For a page operation the assistant cannot perform, `highlight` can mark one named,
  currently visible control and end the turn without clicking or submitting. The server
  checks the latest ready observation, any declared page target, and the fresh action
  receipt before saying where to continue. A matched typed mutation remains the path for
  an actual write; a marker cannot substitute for it. If the application declares a
  page write guard, the Runtime requires its explicit release acknowledgement before
  reporting the handoff ready. A real page gesture or closing the panel removes the marker;
  once an active turn has worked the page, a gesture fences later Agent actions and
  requests cancellation; while the turn only reads and thinks, the page stays the person's.
  This is a guided handoff, not a claim that the business change happened.
- A control that sits in a collapsed menu (a record's "Delete" under its "Actions" menu) is found the same way in every application. When the named control is not on the page, the goal turn opens up to three menus the page itself declares collapsed, in page order, and looks for the control among what each shows; it then marks it and names the menu in the reply, since the page may close the menu when the person clicks elsewhere. Opening uses the internal `reveal` page action, which the model never chooses: the Runtime clicks only a button that declares `aria-expanded="false"` or an unopened `aria-haspopup`, and refuses form inputs, links, buttons that would submit a form and anything inside page navigation. Nothing in a menu is chosen, and it runs inside the application's read-only window like any page action. Whether a control is offered is the application's to show: an account the page shows no Delete to is told the control was not found. Links are never marked; a tag's "Delete" link is not a record's Delete. Verified on Odoo (the customer record's Actions menu, fixture unchanged) and on the Contacts test application.
- For a request that names a page state -- open a screen, switch a tab, apply a filter
  or grouping, arrive at a record --
  the Agent declares that state up front with `set_target`: an exact same-application path
  and/or the accessible names of controls that must show as selected, or application-reported
  active filters and groups, or a filter previously observed active on that page that must
  become absent. Explicit requests to filter, group, or remove a filter cannot be settled
  with a path-only target. After every successful
  write-free page action the server compares the fresh observation with the declaration. If
  a single-page application has changed its URL but still exposes the prior page projection,
  the embedded Runtime marks rendering pending and the server does not report arrival; a
  later observation must show the replacement view. An application adapter may also report
  a blocking page error; that state cannot settle a page target. Application-reported loading
  also keeps rendering pending when the URL and projection have not changed. When the target matches a
  ready observation, the server reports the reached state. An explicit request for a conventional
  List, Kanban, Calendar, Pivot, or Graph view also requires that view to be observed,
  even if the Agent declared only a path. An application may report its actually rendered
  view without a visible view-switch control; a URL or candidate alone is not evidence.
  A Chinese destination name such as “销售订单列表” does not add a selected List control;
  explicit wording such as “切到列表视图” still requires verified view state.
  A fresh observation may be needed after the switch.
  For a pure navigation request it accepts
  only `finish` and `set_target` (other calls are refused without dispatch; the tool list stays
  the same for prefix caching). A request that also needs a fact may continue reading or
  observing. The sentence the user reads is composed from the observed
  page, never from model prose. A declared target grants no permission: clicks and navigation
  are routed, confirmed, and settled exactly as before, and a write in the same window is
  settled by the write path instead. The server never ends the turn on a target by itself,
  because it can verify a page state but not that the state is the whole request. A question
  about where something is kept -- which screen, list or setting -- is answered by opening it,
  with where it sits in the menus when a cited source or Skill says so.
- Generic click can stay disabled as an action while an application re-allows part of it: an optional click policy sees the current observation and the element named and either allows that click or refuses it before dispatch. The Odoo reference integration allows tabs, the Home button, same-application links to reviewed pages and records, and named view-switch, pager, search and menu controls on those pages; reviewed lists can also open their rows or cards. On a record it normally allows only the pager, top menus, and a link to a related record. One reviewed exception on customer invoices opens the Pay dialog: the record route, visible button name, and live button method must all match the adapter review. It does not click Create Payment or grant write approval. Other header workflow buttons, New, Actions, row selection, inline editing, module activation, unnamed buttons, and unreviewed pages remain refused. The review lives in the adapter and names actions by XML ID, resolved (with their URL paths) against the deployed database. A policy may also name the pages it reviewed as destinations, which the assistant may then open by address rather than by finding the menu; Odoo allows a reviewed page or a record opened from one, with at most a `view_type` query. A tab that marks itself current only by class, as Bootstrap's `.active` does in Odoo 18 notebooks, is reported by the Runtime as `selected=true`, so a declared tab can be verified.
- An integration with a page write guard may also expose up to 32 application-owned filter and group choices in the current observation. The optional `apply_facet` tool accepts only a current choice ref and a changed active state; the server checks the page policy, and the Runtime checks that the choice still has the observed meaning before dispatch. Odoo obtains choices from its current search model on reviewed collection pages; an unreviewed page exposes no usable facet action. A fresh observation of applied state, not the candidate list or the action receipt, settles the target. This does not install business tools or require a Skill. Restart Odoo after deploying the new backend asset; the reference pilot loaded it on restart without a database module upgrade.
- An application can also make the signed-in session read-only while the Agent acts on the page. It declares a `page_write_guard` path on its descriptor; the Runtime posts to `<path>/begin` before every page action and refuses the action unless the application confirms the window, and posts to `<path>/end` when the turn ends. Odoo's `woa_runtime` 1.5 implements it: while the window is open every request of that session runs in a PostgreSQL read-only transaction and a write is refused with nothing saved, so a click the review missed -- or an autosave the web client makes on its own -- cannot change data. A page action that loses its result while the window holds, such as a menu button the model took for a link, is therefore settled as no write rather than as an uncertain one, and the turn continues; the receipt says the window held, and the server honours that only for an application that declares a guard. Deploy the 1.5 addon code before this adapter: against older addon code the Runtime cannot open the window, so every page action is refused.
- The Runtime also reports the sole active button in a visible multi-button navigation group as selected, independently of the bounded action-ref list; it does not treat a standalone active action button as a selected view. These bounded names are observation facts, not clickable refs or permissions.
- When no Skill trigger matches a request's wording, the catalog's read-only fallback stays
  loadable but is presented as an unmatched index rather than as task candidates. A typed read
  whose result repeats one already returned in the same turn is answered with a short notice
  instead of the full result, and a third such repeat ends the turn as `no_progress` without
  claiming completion; a completed write resets this.
- Registered typed tools do not require a matching or loaded Skill; Skills provide optional
  business guidance, not tool installation or authorization. Tool schemas are filtered by
  deployment write enablement and current actor scope, then checked again at dispatch.
- Only the person's request can ask for a write. A write tool is offered, and may be proposed, only when the request words its action as something to do: by the tool's name or its leading action ("create" in `create_contact`), or by a trigger of a Skill that declares it, read so that a description ("contacts created today", "已创建的") does not count. Text in records, pages or model output never counts. Any other call to a write tool is refused before a proposal, so no approval card appears, and the turn continues. English negation ("don't create") is not recognised yet; the approval card still stands behind that case. It follows that a change request wording no installed write is one this turn cannot make, and the capability check says so plainly ("this integration does not support that operation; nothing was changed"), even when an installed write under another name might have served; the person can reword the request. Only when the request words an installed write that the capability search still misses is no gap claimed.
- Writes are disabled by default. Core turn configuration initially disables every browser
  action that can initiate an application write, including navigation; an adapter must
  explicitly opt into its reviewed subset. An enabled write binds its exact effect and
  targets to a fresh one-use approval, dispatches at most once, and is verified through the application.
  A timed-out or otherwise uncertain write is recorded as `indeterminate` and never retried
  automatically.
- A short-lived actor/tenant/workspace principal can be bound to the browser capability,
  typed-tool scopes, approvals, mutations, audit records, and revocation state.
- The React assistant panel is a view over server state. The copy the server sends, its text
  limits, and its state names come from server-owned contracts; its palette, scale, window
  geometry, and its words for a start that failed are its own. The panel's launcher is the
  one entry to the assistant: it is on the page from the moment the script runs. A session
  is made when the person sends something, opens a conversation the tab already has, or a
  turn is under way (as on a page the agent opened); an idle one is let go after five
  minutes. A session whose sign-in lapsed, that the page left idle, or that a host restart
  ended comes back unannounced with the next message; why a session could not be started
  is said in the panel, with Retry.
  The Runtime remembers the last session ID in local storage: with one, opening the panel
  restores the current actor's server history before showing the conversation; without one,
  it shows the default greeting immediately. A failed history fetch offers Retry rather than
  presenting an empty conversation. The ID grants no authority; bootstrap still authenticates
  the application's current sign-in.
- One session can be open on several pages at once, as when a host keys it to the person
  rather than the tab. Every page follows the same conversation and may ask; a turn runs on
  the page that asked it, and only that page is handed its page commands and its card
  asking to work the page. A message from another page while a turn waits on that card
  stops the waiting turn and runs next; one that arrives while a turn is working waits for
  it.
  Full document navigation transfers the turn to the same tab’s new document and retires
  the old one. An action already claimed is completed by receipt and is never dispatched again.
- The panel opens as a floating window on the launcher's corner, or as a full-height drawer at the right edge; a title-bar button switches between them. On a viewport at least 900px wide the drawer narrows the host page by setting the root element's width, and gives it back when closed; elements the application positions against the viewport (`position: fixed`, such as dialogs and notifications) are not narrowed and can sit partly under the drawer. On a narrower viewport the drawer covers the page instead. The chosen mode, the floating window's placement, and the drawer's width are kept in the browser's local storage; switching modes resets placement and width to their defaults.
- Optional SQLite stores provide session ownership, progress outbox, bounded conversation
  context, mutation journals, backups, and explicit operator settlement.

These are implemented-path claims, not a general success-rate claim. The repository has no
public benchmark large enough to establish superiority over another browser agent.

## Architecture

```text
First-party page
  assistant.js + panel + fixed browser tools
                    │ authenticated observations/actions/decisions
                    ▼
Server composition
  session policy · planner/model · approval · audit · durable state
                    │ typed application ports
                    ▼
Application adapter
  identity · routes · reads · writes · verification · optional source map
```

The core package is application-neutral. Application behavior enters only through adapters,
Skills, typed tools, and application-owned verification. A goal-directed turn is resolved by
the core over the adapter's goal data (its index, schema and reads); the adapter never
resolves a declaration itself. Odoo ships as a separate
`web-operation-agent-odoo` distribution. The synthetic Contacts application under
[`testapps/`](testapps/contacts/README.md) is internal browser/protocol test apparatus, not a
second public reference. The experimental [Yudao adapter and resettable deployment](examples/yudao/deploy/README.md)
provide another goal-data implementation; they are not a production readiness claim.
The Yudao native host uses staged goal declarations for actor-bound business reads and
page requests, with bounded raw repository answers beside them. Its optional native-tab
prototype requires a release-matched reviewed
read policy, deployed UI routes and verified API ingress through its page-guard proxy.
The proxy fences earlier requests, blocks unreviewed requests during page operations,
and preserves uncertain mutation admission and acknowledged windows across restart. It is an HTTP admission
boundary, not a database sandbox; it pauses other browser sessions of the same actor.
Its app-owned controller binds CRM detail response IDs to visible record headers and
keeps pending or failed panel reads blocked. A same-route detail read for another record,
or a missing or repeated ID parameter, invalidates that record binding until the latest
exact read succeeds and concurrent reads finish. It needs an early facts module; late
installation or opaque response envelopes fail closed. Form entry and submission remain
disabled. Full native-page qualification is pending.
The native host also has an explicit `--executor page-agent` read-only page-planning pilot
(the default is `goal`). It reuses the existing step Agent and the actor's session,
conversation, approval, audit and spend allowance. It requires a reviewed native read
policy and verified guard, checks rendered arrival, and admits only reviewed native
record-tab clicks from fresh observations. Source Q&A, form entry, submission and page
filters are outside this pilot; it never runs automatically after a goal refusal. Its
scripted isolated browser checks do not establish model accuracy or production readiness.
This pilot enables the application-neutral `require_task_contract` option (off by default
for other step executors). Before application tools, the same Agent quotes the user's
operation and answer clauses in an immutable `declare_task`. A complete finish must map
each declared question to evidence selected by its validated conclusions and carry an
observed receipt for the operation. Partial or unable outcomes preserve observed actions
and identify missing clauses; answer repair cannot repeat page actions. This accounts for
declared obligations, not semantic entailment: clause classification and evidence relevance
still depend on the model, and one receipt cannot prove every operation inside a clause.
The extra declaration uses the existing request/time allowance. Native Contacts tests
exercise the same option without source access; this is not an accuracy improvement claim.
Product step turns bound planning, planner repairs, execution and turn-level provider retries
by one model-request allowance and monotonic deadline. Authenticated approval waits are
excluded from that deadline; a reply arriving after it cannot trigger the next action.
The audit and execution result include a content-free per-turn receipt with request counts,
input bytes, timing and available meter usage. Missing usage or cost stays unknown.
This request count measures ChatPort calls; connection reconnection is transport telemetry.
The adapter also provides an opt-in `YudaoFormPort` that translates on-demand raw-source
scalars through the existing form contract. It requires an application-owned live verifier
for the actor, turn, entry instance, create/update target, dialog scope and reviewed
component bytes. Changed entry or source invalidates the draft; literal input limits use
UTF-16 units as the native controls do. The native host installs no such verifier by default;
this port does not yet qualify the full Goal form-entry flow.
One scope declaration
preserves every business and static-source part of a mixed request; business details use
the existing paired readings and deterministic resolver, rather than a growing generic
tool history. Applications may opt into this path with `repository_answers=True` on a
staged `GoalTurnExecutor`; existing goal callers keep their prior behavior by default.
Optional repository search indexes allowlisted code and documentation once at host
startup, without a SourceMap or rule compiler. Matched windows include bounded adjacent
lines so a guard is not separated from its refusal. Source access checks both the requested
path and its resolved target against the allow/deny policy, including symlinks within the
repository. Source answers first use small
matched summaries to select relevant primary evidence or locate an implementation,
then select server-numbered literal spans from bounded evidence. They verify exact
release bytes. Summary ranking retains the original question's terms; an exact
allowlisted filename can refine retrieval. Documented constraints must be identified
as documented, while claims about enforced behavior need implementation evidence.
Missing implementation evidence does not establish that a rule is absent from the repository.
Follow-ups about what the original evidence proves retain that topic.
Source decisions receive bounded redacted prior dialogue (at most four turns and 8 KiB)
and fresh visible-page reference context (at most 4 KiB). Neither supplies source proof,
current values or permissions. Retrieval refinement retains original caller/template
evidence alongside new helper matches, deduplicated and renumbered per question, within
12 references and 64 KiB of whole excerpts; omitted evidence is explicitly marked.
Source scope declares whether qualified repository evidence or actual implementation
proof is required, and carries that distinction through retrieval and answering without
another model stage. Ordinary repository answers can cite explicit documentation;
implementation claims still require actual code or markup. This distinction is model
declared; byte-level citation verification does not prove its semantic correctness.
Current record-field comparisons declare an explicit live-read dependency. A dependency
without a separate fact scope is rejected for bounded declaration repair. Unrepaired
repository-aware scope failures refuse the turn rather than falling back to a business-only
declaration that could omit static work. Completed
comparisons supply an assessment and indices into actor-read facts; the server displays
the limited inference. This validates structure and provenance, not the model's selection
of the needed fields or semantic comparison. The dependency itself remains model declared.
For a declared comparison entity, a successfully read empty field is an observation;
it does not trigger an alternate-field declaration repair. Missing or failed fields still do.
A comparison explicitly limited to one documented rule dimension states that limit;
it does not establish permission for the whole action.
They display selected citations and allow one shared declaration repair across stages.
This verifies attribution, not semantic entailment of the model's
paraphrase. Source never grants API, navigation or write authority, and cannot supply
current data or Git history. This goal composition explicitly refuses history queries,
including when a separate history provider is configured. Each source retrieval returns to the same server-side
turn with a fresh page observation before another model decision. This composition is
also tested with the structurally different Contacts REST application. Ordinary reads
and page navigation still work without repository access.
Generic counts accept `field_ids=[]` and return a ready aggregate bound to the executed
query. Yudao and Odoo skip row reads for this request; filters, actor identity and exact
count validation remain in force. A certified count does not prove that the model chose
the scope the user meant. Raw source queries retain the language of source/documentation
rather than requiring English.
A 2026-10-01 full replay with `qwen3.8-flash` kept the prior 63-case sealed set and
judge unchanged. The goal baseline scored 59/63; the refactored loop scored 23/63, and
count/language fixes scored 22/63. Median attempt time was 18.79 / 32.40 / 27.45 seconds;
the final loop's mean stayed at 36.85 seconds and P95 reached 95.60 seconds. Some failures
come from missing count labels and internal rather than visible source citations, but real
execution and scope failures also remain. This is not a successful qualification.
A separate 40-case, implementation-agent-authored synthetic development set scored 30/40
before and 37/40 after; median server-turn time was 19.05 / 13.58 seconds and calls 214 / 181.
It uses a transport-double browser and is not independent holdout or UI latency evidence.
An automatic-navigation variant was rejected after its live replay worsened the tail;
its activation and core changes were removed. See the [full comparison and accounting](evals/yudao/tool_loop/RESULTS.zh-CN.md).
A 2026-10-02 recovery replay used the unchanged 63-case judge with the final frozen
staged business/raw-source composition: **57/63**, with business, navigation and refusal
cases passing 47/47 and source rules 10/16. Compared with the 22/63 loop, input tokens
fell from 4,266,926 to 695,595 (83.7%) and calls from 322 to 204. Median attempt time
fell from 27.45 to 23.27 seconds and P95 from 95.60 to 35.41; it remains slower than the
old goal baseline. The same corrected development browser apparatus scored 37/40 for
the loop and 40/40 for the final composition; median server-turn time was 12.14 / 10.01
seconds, but P95 worsened from 46.11 to 51.17. This seen synthetic data does not establish
general accuracy or production UI latency. The earlier 60/63 candidate failed development
qualification and is not the final score. See the [recovery results, residual failures and
complete paid accounting](evals/yudao/tool_loop/RECOVERY.zh-CN.md).
The older goal demo/evaluation path remains available for comparison. Native-host writes,
form entry and clicks (including tab selection) remain disabled.
Assistant-requested Yudao page changes use full document navigation, letting the application's
Vue Router initialize its own browser history. Manual menu changes retain the application's
normal SPA navigation; assistant page changes incur a full page load.
Yudao keeps failed detail reads as failures, so incomplete line data cannot become a
zero count or total. Explicit yuan/cents units from its API or line columns are retained:
cents are normalized to yuan for facts, comparisons and totals, while a field with no
declared currency is not assigned one.
Yudao line fields with an unambiguous literal dictionary binding in their deployed table
use labels from dictionaries read under the current actor, preserving the raw keys for
conditions. Missing dictionaries, ambiguous bindings and unknown values retain raw values.

Important design documents:

- [System design](docs/architecture/system-design.md)
- [Application adapter contract](docs/integrations/application-adapter.md)
- [Durable operations](docs/integrations/durable-operations.md)
- [Security boundary](SECURITY.md)

## Repository layout

```text
src/web_operation_agent/       application-neutral server and browser Runtime
examples/odoo/                 the only public reference integration
  src/web_operation_agent_odoo/  installable adapter
  deploy/reference/              disposable local Odoo fixture
  deploy/single-node/            operated single-node example
  deploy/addons/                 shared Odoo Runtime addon
evals/odoo/                    independent Odoo evaluator and sealed task apparatus
  domain/ fixture/ execution/  evaluation specification and runtime
  provider/ judging/ reporting/ mediation, scoring, and evidence
evals/pageagent/               generic PageAgent comparison bridge
testapps/contacts/             synthetic internal qualification application
frontend/                      assistant panel source
```

For a new application, follow the [Odoo reading guide](examples/odoo/README.md#reading-guide-for-integrators): start at adapter composition and tool specifications, then reads and
writes, identity and routes, Skills, turn composition, and finally deployment.

## Development setup

Python 3.12 and 3.13 are supported. Node.js is required for the complete Runtime and panel
suite.

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 python -m pip install --editable .
python -m pip install --no-deps --editable examples/odoo
```

For model-backed local runs, copy the short configuration template and fill in only the
sections you use:

```bash
cp .env.example .env
```

`.env` is ignored by Git and is the local checkout's configuration; `.env.example` contains
no credentials. The Odoo fixture's `provision` command prints the exact Odoo values to use.
Scripted runs need no model settings, and the source and resettable-fixture sections are
optional.

The server's `policies/model_options.json` disables reasoning by default for
`qwen3.8-flash` with `enable_thinking=false`; planner answer-budget growth does not
enable it again. For `deepseek-flash`, it sends `thinking={"type":"disabled"}`
using the [DeepSeek thinking-mode contract](https://api-docs.deepseek.com/guides/thinking_mode/).
Its registered CNY rates use the published peak rates as a conservative fixed estimate;
off-peak charges can be lower, so this is not an invoice or time-dependent billing.
The selected model still comes from `AGENT_OPENAI_MODEL`. This
latency preference does not guarantee unchanged accuracy; see the
[frozen model comparison](evals/yudao/qualification/RESULTS.zh-CN.md).
Policy changes require restarting a source-installed server, or rebuilding and
redeploying a wheel/container installation.

Build the browser assets. The assistant script the gateway serves and the trace replay page
are generated, not committed; a checkout without them has no assistant in the browser, and
the browser-backed tests and the one replay-page test fail or skip until they are built:

```bash
python scripts/build_runtime.py --install
python scripts/build_panel.py
```

`--install` runs `npm ci` once for a fresh checkout. Rebuild after pulling frontend
changes; a separate worktree needs its own build.

Run the core suite:

```bash
python src/web_operation_agent/tests/run_all.py
```

Run all repository gates:

```bash
scripts/qualify.sh
```

`--require-browser` makes Chromium and built frontend prerequisites mandatory.
`--require-live` additionally requires the configured live Odoo qualification.
The offline gates include the Yudao adapter, native host lifecycle, evaluation apparatus
and separate wheel import/assets checks. A `BLOCKED` gate did not run and is not a pass.

The independent Odoo evaluator's offline apparatus can be run directly:

```bash
PYTHONPATH=src:examples/odoo/src:. \
  python scripts/offline_suite.py --start-dir evals/odoo/tests --top-level .
```

## Odoo reference integration

The adapter provides five domain-specific bounded reads, a generic read fallback, and four
opt-in writes. The domain-specific tools cover customers, products, sales orders, quotations,
and customer invoices. It binds Odoo's HttpOnly browser session to a separately delegated
per-user API key and refuses the composition unless both identify the same database, company,
and actor. Reads report completeness; writes perform application-owned duplicate checks and
readback.

The adapter includes versioned Skills and an optional source compiler for Odoo menus, actions,
views, fields, method documentation, and user-visible refusal conditions. A generic raw-code
index covers allowlisted Python, JavaScript, XML and access declarations alongside those
compiled facts. Both are restricted to modules installed in the running deployment, and
the generic tool loop presents source-backed conclusions with citations kept in server-side
audit. The opted-in goal composition displays its selected citation locations. A full matching Git commit can optionally
enable local history; an extracted image tree without `.git` reports history unavailable.
Runtime facts remain authoritative when source or documentation disagrees. The installed
add-on also exposes a bounded actor-scoped model/action catalog. The
Odoo adapter translates it to the core `discover_entities`, `describe_entity`, and
`query_entity_records` contract, then maps validated generic filters back to bounded Odoo
reads under the same delegated actor. Native model names and RPC methods are never Agent
arguments. Generic monetary reads attach the actor-visible currency to each value when
Odoo exposes a valid stored currency binding. The same query can request application-computed, full-scope sums over
matching stored numeric fields; monetary sums are grouped by currency and are independent
of the returned row limit. Count, page and aggregate are separate application reads, not
one transactional snapshot; a disagreement in counts is refused. Without a stored currency
binding an amount remains text and cannot be summed. References to an actor-readable catalog entity retain an opaque related-record
binding for a follow-up read; the related record is not treated as readable until that read
succeeds. This does not authorize writes.
A record found this way carries where it opens (`/odoo/<model>/<id>`, which Odoo 18 opens for any actor who may read the record), and `goto` may use exactly that path. A related record must first be read by its returned opaque entity and exact ID; an unread relation label alone does not authorize opening it. Deterministic navigation reports arrival only after a fresh observation marks the destination rendered and the application reports the matching record identity, not merely when the URL changes.

Provision the disposable local fixture from a checkout:

```bash
PYTHONPATH=src:examples/odoo/src:. \
  python -m web_operation_agent_odoo.deploy provision
```

The private single-node deployment and its short rebuild command live at
[`examples/odoo/deploy/single-node/`](examples/odoo/deploy/single-node/README.md). The rebuild
must update the Agent, run the Odoo addon upgrade, and restart Odoo as one operation:

```bash
./examples/odoo/deploy/single-node/rebuild.sh
```

See the [Odoo integration guide](examples/odoo/README.md) for identity, routes, tool behavior,
source compilation, fixture provisioning, and qualification.

## Known limitations

- Alpha interfaces may still change; compatibility policy is not yet stable.
- Horizontal scaling, active-task migration, and cross-host revocation are not implemented.
- An application must provide its own trusted login assertion, delegated credentials,
  authorization mapping, and application-specific postcondition checks.
- Browser automation cannot provide database transactions or rollback. Uncertain writes need
  application reads and an explicit operator decision.
- The Odoo page guard waits for prior same-session transactions before acknowledging its
  read-only window; a controlled in-flight write test verified this fence. It still cannot
  isolate external effects, other browser sessions, custom mid-request commits, or client
  retries after the window closes. Do not treat it as a general write sandbox.
- Local source search can match Han-language labels and comments, but does not translate a
  Chinese business question into English code identifiers. Cross-language recall still
  depends on the Agent choosing suitable source terms and has not been broadly qualified.
- Source-informed no-Skill form filling has a scripted production-session contract test, but
  no independent model-backed success-rate result yet. It does not establish arbitrary
  page-operation coverage.
- When source lookup is enabled with a hosted model, retrieved allowlisted source is sent in
  model requests. Use that combination only where the deployment's data policy permits it.
- Browser and accessibility coverage is strongest on Chromium; other engines and screen
  readers are not comprehensively qualified.
- A reached page state is checked against the declared path, selected controls and any declared active filters or groups, plus explicit requests for the five conventional views and filter/group changes above. An application may first resolve a reviewed, deployment-bound route alias to its installed path; that does not grant navigation or count as a rendered page. Removing a filter additionally requires that exact filter to have been observed active on the same path earlier in the turn, followed by typed evidence that it is absent. The application must supply typed active-facet state; a same-named menu choice or column never counts. The pending-render signal catches a changed SPA URL with the old page projection; an adapter-reported blocking error and a wrong view also prevent arrival. A changed URL with no rendered page controls or application-reported page state cannot settle as ready. These guards do not prove that all asynchronous loading has finished or verify record identity, other view names, or that the model declared an implicit facet request such as “look at orders by salesperson.” The new facet action has scripted contract tests, not a repeated model-backed success rate. The earlier 37-task Chinese development run must not be presented as the P8.8 holdout result.
- An empty typed read no longer ends the turn automatically: the Agent receives the exact query result and can check whether the entity and filters match the question. This does not yet prove that a model-selected query covers the user's intent. In the earlier 37-goal run, the old automatic answer incorrectly reported two existing records as absent; one of those reads had searched a customer name for an order number.
- Reaching a declared page target no longer blocks further reading or observation for requests classified as more than pure navigation. Apart from the explicit view and facet checks above, a target still covers only the conditions the Agent declared, so a question or other presentation requirement omitted from that declaration can remain unmet.
- The goal-directed turn scored 35/37 on one rerun of the seen Chinese development set before a write-intent settlement fix; one failure was an unrequested page added to a data answer, and one was a misleading "opened" reply to a create request with no write. The two cases were then rerun three times each: the data question succeeded, and the create request was honestly refused, not fulfilled. A separate 17-task qualification set written and frozen before execution passed twice (34/34 attempts), but its author also implemented the system, so this is not an independently authored holdout or a broad 90% accuracy claim. Odoo and the smaller Contacts REST app implement the index/resolver contract, not equivalent model-level coverage. The server requires every declared deliverable to settle and refuses a page without two stable rendered observations; this does not prove all asynchronous content has loaded. Free-text list search is answered from a scoped read rather than typed into the page's search box. A creation handoff was verified three times on an actor-writable Odoo quotation page, but it only highlights New; it does not prefill or submit, and an actor without create access cannot use it.

The second Chinese goal holdout is frozen with 84 normal goals and 10 honest-limitation goals; see [evaluation details](evals/odoo/README.md). It has offline oracle checks, but has not been model-run or fully walked in a browser. Select it with `--version v5` in the goal runner.

The frozen `v6-rules` set adds 40 source-rule questions bound to deployment release `18.0-20260909`. Its evaluator checks cited source bytes and reports answer and citation matches separately; this is evaluation support, not a completed three-arm experiment. See [the citation contract](evals/odoo/README.md#source-rule-holdout).

On two single-turn Chinese holdouts written to the v7 specification by an author blind to the implementation, the goal-directed turn (qwen3.8-flash, the ordinary reader, one pass each, 2026-09-27) answered 87/100 (v7-C, trustworthiness 88.3%, 3 false claims) and 86/100 (v7-D, 89.0%, 3 false claims), with no writes; the earlier v7-A and v7-B first runs score 78 and 80 under the same judge. The remaining failures are mostly rule questions whose cited source line is not the one that states the rule, readings that differ between runs, and model-service timeouts. These are two 100-task samples of one reference deployment, with about ±5 points of run-to-run variation; they do not establish 90% accuracy.

The Yudao rule compiler may reuse bounded pure Java/Vue parse results after rehashing
all current inputs. Actor labels and choices are assembled anew; permissions, live page
state and business reads are outside that cache. Missing, added, changed or linked source
inputs invalidate reuse. Source retrieval remains optional.


Source-backed how-to questions explain evidenced user-facing steps; they do not execute
the operation. Ambiguous module workflows are distinguished rather than treated as one
application path. Native Yudao hosts can compose separately reviewed update and creation
entry policies; undeclared entries remain unavailable and submissions stay manual.

An optional server-side product-knowledge index groups original, release-bound source
excerpts into draft workflow guides and retains raw-source fallback. Derived summaries
reach retrieval and answering as untrusted aids; only original numbered excerpts can
be cited. Source changes invalidate affected guides, and guide text participates in
the declaration-cache digest. Applications supply these guides explicitly; Yudao uses
raw-source lookup and does not generate workflow drafts automatically. Guides do not
prove complete backend rules or current permissions.

## License

See [LICENSE](LICENSE) and [THIRD_PARTY.md](THIRD_PARTY.md).
