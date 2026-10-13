# yudao (ruoyi-vue-pro) local test application

A local, resettable deployment of yudao used as the second test application. Nothing here is imported by the Python package.

## Pinned versions

| Part | Source | Tag | Commit | License |
|---|---|---|---|---|
| Server | gitee.com/zhijiantianya/ruoyi-vue-pro | v2026.09 release commit on `master-jdk17` (not tagged) | `d3d00639c0` | MIT |
| Admin UI | gitee.com/yudaocode/yudao-ui-admin-vue3 | v2026.09 release commit on `master` (not tagged) | `9323ffa2ca` | MIT |

The first three evaluation sets were written against `v2026.08` (`6aa235e493` and `e272d6639b`); their source citations name that release. Each release needs its own checkout — the release that a run's data and rules are read from is the one its task set was authored against, and reading a page against another release's source gives a smaller, wrong index.

Spring Boot 3.5.15 on Java 21 (`eclipse-temurin:21-jre`), MySQL 8.0, Redis, nginx. Vue 3.5 with Element Plus 2.13.

## Schema source

Upstream's MIT schema (`sql/mysql/ruoyi-vue-pro.sql`) creates only the system and infra tables. It already carries every module's menus, buttons and most dictionaries. The business-module schemas on the upstream documentation site are published under separate member-only terms and are not used here.

`generate_ddl.py` derives the missing 420 tables from the MIT entity classes (`@TableName` data objects): column names, Java types, Javadoc comments, a `tenant_id` column unless the class has `@TenantIgnore`, and auto-increment Long keys. Entity classes carry no lengths or defaults, so the generator applies two rules:

- every non-key string is `text`;
- numbers, amounts and flags default to zero, because services rely on it for initial states (a new CRM contract is a draft through its column's 0), except properties the code tests for null (a CRM opportunity is open while `end_status` is null).

There are no secondary indexes. Every admin list endpoint (`GET */page`, 278 endpoints) was called on the empty database, and `seed.py` writes through the CRM, ERP, workflow and mall services; neither hits a schema error. A column whose NULL the code tests only indirectly may still carry the wrong default.

`subject_chart.py` supplies the one reference *table* the fixture needs and upstream does not publish: the chart of accounts an accounting set is initialized from. Initializing builds the set's subjects from `fms_subject_template` and then its closing templates and schemes, each naming the subjects it posts to, so with an empty table the first template fails and the set can never be initialized. The chart the script writes is **synthetic** — the small-business standard's coding, carrying every subject those presets name — and is not the published chart.

```bash
python3 subject_chart.py > $YUDAO_CACHE/subject-chart.sql
docker exec -i woa-yudao-mysql-1 sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" \
    --default-character-set=utf8mb4 ruoyi-vue-pro' < $YUDAO_CACHE/subject-chart.sql
```

The client's character set is not optional: without it the subjects are stored double-encoded and every page renders them mojibake. The file is applied rather than mounted into the database's init directory on purpose — a mount of a file a reader has not generated is a directory, and the database then refuses to start.

## Modules

`enable_modules.py` enables every module except:

- `iot`: the server exits at startup without a TDengine instance.
- `report`: it wraps the closed-source JimuReport starter, which downgrades swagger-annotations and breaks `/v3/api-docs`.

The CMS and EAM top-level menus have no module in the open-source repositories. `harden.sql` disables these four areas' top-level menus (IoT, report, CMS, EAM), so a person is not shown pages nothing serves.

Loaded with every other module, AI included, the server holds about 1.9 GB; `compose.cloud.yaml` gives it a 1.28 GB heap and a 512 MB metaspace.

## Upgrading a release

A seeded database is upgraded in place, not rebuilt. `upgrade.py` compares the old and new checkouts and business schemas and writes the SQL: new tables, added columns, and the upstream's reference rows (menus, dictionaries, settings, jobs, built-in role grants) brought to the new release. `cloud/upgrade-db.sh` dumps the public host's database to `/opt/yudao/backups/` before applying it, and re-applies `harden.sql`'s menu statements. Rehearse on a copy of the database and call every admin list endpoint before upgrading a host.

## Build and run

```bash
export YUDAO_CACHE=/path/to/ignored/cache/yudao   # holds server/, ui/, business-schema.sql
git clone https://gitee.com/zhijiantianya/ruoyi-vue-pro.git $YUDAO_CACHE/server && git -C $YUDAO_CACHE/server checkout d3d00639c0
git clone https://gitee.com/yudaocode/yudao-ui-admin-vue3.git $YUDAO_CACHE/ui && git -C $YUDAO_CACHE/ui checkout 9323ffa2ca
python3 enable_modules.py $YUDAO_CACHE/server iot report
git -C $YUDAO_CACHE/ui apply "$PWD"/patches/ui/*.patch   # the upstream's promotional links: login page, user menu, home page
python3 generate_ddl.py $YUDAO_CACHE/server > $YUDAO_CACHE/business-schema.sql
docker run --rm -v $YUDAO_CACHE/server:/src -v yudao-m2:/root/.m2 -w /src maven:3.9-eclipse-temurin-21 mvn -B -q -T 1C clean package -DskipTests -Dmaven.test.skip=true
(cd $YUDAO_CACHE/ui && pnpm install --frozen-lockfile --registry https://registry.npmmirror.com/ && VITE_BASE_URL= VITE_APP_CAPTCHA_ENABLE=false pnpm run build:prod)
docker compose up -d
```
Before building, copy `patches/server/` over the server checkout (`cp -R patches/server/. $YUDAO_CACHE/server/`). It adds four classes under `yudao-server` and changes none of upstream's: a read-only window per signed-in user (`/admin-api/woa/page-guard/begin`, `/end`, `/status`). While a user's window is open, every insert, update and delete their requests reach is refused at the one place all mapped statements pass, whatever handler or HTTP method they came through; work handed to other threads (access logs) is not affected, and the window closes by itself when its time runs out. It covers database writes only: a handler that sends a message or calls another service is outside it, which is why the assistant's host still refuses every request that is not a read. The assistant's host opens this window with its own and does not work the page without it.


The UI lockfile resolves from `registry.npmmirror.com`, so install against that registry; a current pnpm rejects the lockfile against npmjs metadata. The UI build needs about 8 GB of heap, so build it on the host rather than in a default Docker VM. Keep `VITE_BASE_URL` empty for the local build: nginx proxies `/admin-api` on the page's own origin, including an isolated clone on another port. A bundle baked for `localhost:48088` leaves a clone on another port stuck at its loading screen.

The app is served at `http://localhost:48088`; MySQL listens on `127.0.0.1:43306`. The database password defaults to a local-only value; set `YUDAO_DB_PASSWORD` to change it.

`reset.sh` recreates the database from the schema scripts, discarding every record. `menu_stats.sql` counts pages and buttons per top-level menu.

## Seed data

`seed.py` writes synthetic records through the admin API (and, for the mall, the member API), so numbering, totals, stock and approval state are the application's own. It looks every record up before creating it, so a second run creates nothing.

| Area | Records |
|---|---|
| Organisation | 6 departments, 5 posts, 4 roles with menu grants and data scopes, 9 users |
| Workflow | contract, receipt and leave approvals (single approver each); approvals in progress: 4 contracts, 2 receipts, 2 leave requests |
| CRM | 30 leads, 24 customers, 32 contacts, 18 opportunities, 15 contracts (8 approved, 4 in approval, 3 drafts), 16 receivable plans, 6 receipts |
| ERP | 14 products in 4 categories, 3 warehouses, 8 suppliers, 10 customers, 12 purchase orders, 15 sales orders, with receipts into stock and shipments from it |
| Mall | 8 products, 6 members with addresses, 15 orders paid through the mock channel: 4 unpaid, 4 awaiting shipment, 5 shipped, 2 received |

`seed.py --only parents` adds the record every other record of a module is read under: one accounting set (initialized from the chart above: 86 subjects, 21 closing templates and 4 closing schemes), one official account, one mail provider with its account, an employee record for every seeded person (the HR portal reads the signed-in person's own record), and a payroll configuration with its first payroll record. Without them those modules cannot be used at all — their pages show an empty selector and, where the page asks the server for the parent's records, answer nothing.

The non-admin evaluation account is `evaluator` (role `sales_rep`, East China sales department). Seeded users share the password in `YUDAO_SEED_PASSWORD` (a local-only default). Members log in with the deployment's fixed SMS code; codes are single-use and limited to one per mobile per minute.

```bash
python3 seed.py
./snapshot.sh seed-v1              # export to $YUDAO_CACHE/snapshots/seed-v1.sql.gz
./snapshot.sh restore seed-v1      # restore; restarts the server, which invalidates login tokens
```

## Known application behaviour

A page the assistant opens is opened by the application's own router, in the document the
person is on, so the assistant's window stays open and the application does not start
again. The application's page module (`page-facts.js`, `openPage`) asks the mounted Vue
application's router; the Runtime calls it for a `goto` and still proves the navigation by
the address, as for any other. Nothing writes to the History API: the generic SPA tool's
synthetic state omits Vue Router's `current` field, after which an ordinary menu change
could try to navigate to the origin with `undefined` appended (reproduced on the public
site), which is why the adapter's `navigation_mode` stays `document`.

What a loaded document gave is kept. The layout keeps every visited page alive with the
filters and tab it was left with, and a kept page may read its own list again whenever
the route changes (the CRM customer list does); read on another page's address, that
list would be taken for that page's. So `openPage` first drops the kept pages through the
tab store's `delAllViews` -- the tab strip is not stored, so a document load drops them
too -- and enters the page through the application's `/redirect` route, the one its own
tab refresh uses: the page is built fresh, and a read the page being left starts on the
way out belongs to the address in between.

The address changes before the page is there, since a route's component is fetched when
it is first entered; `openPage` waits up to two and a half seconds for the page's own
component to be mounted before it answers, so that the first observation is of the page
and not of the empty layout.

The page is loaded as a document, as before, when the application cannot be asked (no
mounted router, no tab store, no `/redirect` route), when the address is the one already
shown or carries a fragment, when a dialog, drawer or message box is open over the page
(some belong to the layout and outlive the page that raised them -- the finance pages'
account guide does -- and only a document load clears those), when the router refuses or
takes the navigation elsewhere, or when the application does not answer within six
seconds.

- CRM list endpoints apply record permissions only for the `sceneType` they are given (1 owned, 2 involved, 3 subordinates'). Without it a non-admin account reads every record: `evaluator` sees 15 contracts without `sceneType` and 2 with `sceneType=1`. The UI always sends it.
- springdoc names schemas by simple class name, so same-named nested classes overwrite each other: the published line-item schema of ERP orders is a MES class. Line-item shapes must come from the source.
- ERP purchase receipts and sale shipments fail with a server error unless `otherPrice` is sent; the UI always sends 0.

## Public deployment

`compose.cloud.yaml` adapts the stack to a public host behind its own nginx (MySQL and the server bound to the host only, pinned image digests, `YUDAO_JAVA_OPTS` for the host's memory). `cloud/provision.sh <ssh-target> <domain>` prepares an Ubuntu 24.04 host with Docker once, and again whenever its configuration changes: nginx and certbot (the site's certificate, renewed by certbot's timer), the firewall (22, 80, 443), SSH by key only, `/opt/yudao` and a database password kept on the host. The assistant on the public site: `cloud/yudao-loader.js`, injected by nginx into every page, starts the runtime once someone is signed in and adds their yudao access token (kept by yudao in localStorage, not a cookie) to the runtime's one bootstrap request. `web_operation_agent_yudao.native_server` asks yudao whose token it is and opens or renews that person's session, reading yudao with the same token, so the assistant sees what the person may see; the token stays in memory and is neither written nor logged, and no writes are offered. A person has one session whichever pages they have open: one conversation any of those pages may ask in, each turn running on the page that asked it. Its history and the conversation the model reads are kept in the host's state volume, so a session that lapsed (its sign-in ran out while unused, or the host restarted) comes back as it was on the next question. When yudao refuses the bootstrap because the access token has expired, the loader renews it with yudao's own refresh-token call, writes the new token back where yudao keeps it and asks once more; only a refresh yudao refuses means signing in again. nginx publishes the core's gateway at `/woa-agent/` and the bootstrap at `/woa-agent-boot/bootstrap` on the site's origin; a same-origin GET has no Origin header, so nginx gives such requests the site's own. The host runs as the compose service `agent`, built on the server from the wheels `deploy.sh` brings, once the owner has put the model provider's settings (`AGENT_OPENAI_BASE_URL`, `AGENT_OPENAI_API_KEY`, `AGENT_OPENAI_MODEL`, `AGENT_PROVIDER`) in `/opt/yudao/agent.env` on the host; model calls are metered into the host's own spend ledger in the `agent-state` volume. Each signed-in person may spend ¥5 a day on the model (Beijing time, all their tabs together; `--daily-allowance-cny`); past it the assistant says so and makes no further calls until the next day, and a call already in flight can take them past the line by what it costs. The tally is rebuilt from the ledger when the host restarts. The host holds at most 300 sessions, one per person (`--max-sessions`); an idle session is about a megabyte.

The public site's UI is built with `--mode cloud` from an `.env.cloud` that sets `VITE_BASE_URL` to the site, turns off the captcha, the default login and the documentation banners (`VITE_APP_DOC_ENABLE=false`, `VITE_APP_DOCALERT_ENABLE=false`), and writes `dist-cloud`. `cloud/deploy.sh <ssh-target>` releases the locally built server jar, the admin UI built for the public site (`ui/dist-cloud`) and the pinned PageController into a new directory under `/opt/yudao/releases`, switches `current` to it, and returns to the previous release when the server does not answer within eight minutes; the last three releases are kept and the database volume is left alone. With `--restore <snapshot> --credentials <file>` it also replaces the database with the snapshot and gives every account a new password at once, written only to that local file.

- images pinned by digest, since the host pulls through third-party mirrors;
- a 512 MB heap (the process settles near 0.9 GB), a 96 MB InnoDB buffer pool, and no performance schema; the host needs swap;
- no mock login, a random SMS code range, no Spring Boot Admin client;
- the server listens on `127.0.0.1:48080` only, and the host's nginx serves the UI and the PageController and proxies `admin-api`, `app-api` and the websocket (`cloud/nginx.site.conf`), holding the site's certificate. API docs, Swagger, actuator and Druid stay unreachable from outside.

Build the UI with the public origin and without the upstream login defaults (a `--mode` file whose `VITE_BASE_URL` is the public URL and whose `VITE_APP_DEFAULT_LOGIN_*` values are empty). Upstream still hard-codes `admin123` in the social-login page; it stops mattering once `admin` has a new password.

After restoring a snapshot, and before publishing the site, run `harden.py` through an SSH tunnel and apply `harden.sql`. They give `admin` and the seeded users new random passwords, disable every other admin account (the upstream demo accounts have published passwords), disable the demo tenants, and revoke existing logins. The passwords go only to the file named by `--out`. A host whose database is created or restored the same way also needs `subject-chart.sql` applied once (the command above, over the tunnel), or its accounting module cannot initialize an account set — `deploy.sh` leaves the database volume alone, so this is a step of its own and not part of a release switch.

The server takes about four minutes to start on two vCPUs.

## Goal data adapter

`src/web_operation_agent_yudao` supplies actor-bound metadata and reads. The native host
uses staged `GoalTurnExecutor` declarations and deterministic resolution for business
records, conditions, aggregates and page requests. Raw repository answers share the
same scope and turn, preserving mixed requests without a growing general tool history.
The demo and sealed runner expose `--executor goal|agent|hybrid`: `goal` is the prior
compiled-rule baseline, `agent` retains the generic loop for comparisons, and `hybrid`
uses the native host's current composition. All use the same actor-bound reader.

Native-host `--source-root` holds `ui/` and `server/`; `--source-release` identifies the
deployed snapshot. `repository.py` indexes the code and documentation allowlist in
`policies/repository.json` once at startup and returns bounded exact-byte citations.
It does not compile `YudaoSource` or require a SourceMap. Repository access is optional:
without these flags the ordinary read tools still work. An explicitly missing root or
release fails startup. Static ownership requires a declaration in code or documentation;
business record owners are not engineering owners. Snapshot-only deployments explicitly
refuse Git-history questions rather than substitute application operation logs.
`SourceFormReader` is an adapter-owned, on-demand raw Vue template reader for scalar
field labels, bindings and literal length limits. It reads only an already located,
allowlisted component, keeps exact-byte citations and rechecks source bytes before reusing
its bounded parse cache. Conditional fields and declared handlers remain explicit facts;
the reader does not execute scripts, establish side-effect freedom, authorize typing,
identify the current create/update target, or enable native form entry in the host.
`YudaoFormPort` translates those facts through the existing `ApplicationFormPort` contract,
using a server-owned `NativeFormVerifier` that must revalidate the actor, session/turn,
entry instance, exact create/update record, dialog scope and reviewed transient component
bytes. It refuses missing or changed attestations and source, excludes handler-bearing
fields and conditions other than exact create/update mode gates attested by the verifier,
and checks literal length limits in UTF-16 units before issuing
a draft. `YudaoApplication(forms=..., page_write_guard=...)` permits explicit composition;
the native host does not install a verifier or enable form actions by default. A verifier
must come from application-owned live entry verification, never model output or a browser
claim. The optional customer-update prototype below has a complete scripted browser
qualification; that does not establish real-model accuracy or general form coverage.
Single-choice radio groups reuse the same current-form contract and exact option-label
verification. The source reader accepts literal Boolean/integer values and bounded direct
imports of literal constants from allowlisted view-local TypeScript files; it executes no
scripts and binds dependency bytes to the draft. Multi-select and datetime inputs remain
excluded. Selecting public project visibility may remove the unentered members control
during the approved radio action; editor identity, entered fields and the application
descriptor must remain valid before manual handoff. Added or replaced controls still stop
the draft. Reviewed project creation admits only the source-declared `tabs=participate`,
`tabs=owner` and `tabs=all` collection queries, bound unchanged through handoff. This does
not enable automatic navigation: the project-group GET can initialize persisted defaults
and cannot be admitted as read-only merely because it is a GET.
Source questions use one small retrieval declaration and one answer declaration, with
at most one shared format repair. Answers include selected release-bound citations. Literal support checks establish attribution, not semantic correctness.
Source-only and mixed source/data turns are covered by Yudao and Contacts synthetic tests.
See the [recovery qualification](../../../evals/yudao/tool_loop/RECOVERY.zh-CN.md) for
real-model results and remaining limits. Writes, form entry and clicks, including
tab selection, are disabled in the native host by default.

The optional **native-tab prototype** starts with `--native-read-policy <private-policy.json>`
and `--native-guard-port 48093`, together with `--ui-source` and `--source-release`.
The policy is a reviewed deployment configuration, never model context: it has exactly
`schema_version: 1`, `release` matching `--source-release`, and `read_paths`, an array
of exact `/admin-api/...` GET paths whose handlers have been checked for persistence
and asynchronous effects. This repository does not supply a production read allowlist.
Do not derive that authority from a GET verb or an OpenAPI schema alone.

The optional customer-editor prototype additionally accepts `--native-form-policy
<private-review.json>` and requires the guard configuration, `--source-root`, `--ui-source`
and matching `--source-release`. The review has exactly `schema_version: 1`, `release`,
and `hashes`, mapping these relative source paths to their reviewed full-file SHA-256:

- `ui/src/views/crm/customer/detail/index.vue`
- `ui/src/views/crm/customer/CustomerForm.vue`
- `ui/src/components/Dialog/src/Dialog.vue`
- `ui/src/views/crm/permission/components/PermissionList.vue`

Those bytes must have been reviewed for the exact update opener and transient editor
behavior; generating hashes alone establishes neither. No production policy is supplied.
The host can additionally load repeatable `--native-create-policy <private-review.json>`
files. Each uses the existing reviewed creation contract (`schema_version`, `release`,
`reference`, `hashes`); only installed, byte-matching entry reviews are offered. An
update review and creation reviews compose in the same actor session; creation alone
does not enable updates. Duplicate references or changed source abort startup. Both
require the verified native guard.

The explicit `--executor slim` pilot (`slim_loop.py`) is a measured prototype hosted by
this adapter. It subclasses the goal executor and replaces only what decides the next
step: one conversation in which the model searches the actor's menu, searches and calls
the application's read-only API as the actor, works the page with the Runtime's fixed
actions and marks a submit control for the person. Startup refuses it without the method
fence (`--native-read-policy` in `method-fence` mode) and the application's own window.
It uses no form or filter review and declares no goal-port capability of its own.
Without `--native-writes` it has no write path; with it, see the end of this section.
What the model is shown leaves out the application's navigation menu and
names the regions that scroll; only an element listed there may be acted on. A dropdown,
a radio group and a date box are marked as what they are, and the two boxes of a date
range as its start and its end: the first is committed with the second, since the
component clears a first box committed alone. The deployment's reviewed conventions file
is told to the model in full, so that a name found in two modules is read the way the
organisation means it. An existing record is edited from its detail page where it has
one; a detail page without an edit entry is reported as no permission to change it,
rather than entering through the list. A page step repeated from an unchanged page is
not run again, and a turn that keeps repeating one ends. `find_record` asks the goal
port which entities hold a record of a name, so that a bare name is not placed by the
words inside it. The application's page module (`page-facts.js`, `lineControls`) names
each control of a form's line table by its row and column and indexes the searchable
selects the generic controller leaves out inside a scrolling table; nothing on the page
is changed for it, and it reads only what is on the screen. A value typed at a dropdown
is chosen by its text rather than typed and entered. Where a list's filter does not take
effect, the records under a record are shown from that record's detail page and its tab.

An API read adds a bounded amount to the conversation and says how many rows it left
out; earlier reads leave the conversation once they outgrow their share, because every
request resends it and the run's input-token ceiling otherwise ends a turn that reads a
long list. A read that names the columns it wants is followed to the last page and comes
back with each numeric column's sum, highest and lowest and each date column's row count
by month, so that counting and adding are not left to the model; instants are shown as
the application's clock has them. A list the application divides by whose records they
are (`sceneType`), read without naming a scope, is read as the adapter's data port reads
it: the scopes together, once per record. When that reading finds nothing under a
condition, one read without a scope tells whether such records exist outside the actor's
scopes: that is no permission, not absence. Every instant column of a list read is
counted by month and named, whichever column was asked for, because a list usually holds
more than one (made, ordered, received) and the person's verb says which is meant.

What an API read returns is blanked of credentials before anything is counted or shown
(`api_content.py`): the fields the adapter's data port never reads (`goal._SECRET` --
passwords, secrets, keys, tokens, a channel's configuration) when they hold text or a
structure, the value of a named setting whose key or name says credential, and credential
or session-parameter assignments inside any string. A number under such a name (a token
limit, a validity period) and the business fields the actor may read in the application
(phone, bank account, address) pass unchanged. A lookup by name and a refusal's own words
are blanked the same way. Without this a single `token=` inside a record would also stop
the request at the provider egress check. The rows, values and names an API read or
`find_record` tells the model sit between `BEGIN_UNTRUSTED_APPLICATION_DATA` and
`END_UNTRUSTED_APPLICATION_DATA`, with marker text inside the data defused; what the
program itself says about the read (totals, field descriptions, computed sums, what was
left out) stays outside. The model is told that fenced content and the text of a page
observation are data anyone with edit rights may have written, never instructions, and
the closing check is told the same. This is a statement to the model, not an enforcement:
writes remain behind the approval card and reads behind the method fence whatever a
record says.

What the turn is doing between its page actions is published on the session's existing
progress channel as `step` events, facts only: a search (`search_menu`, `search_api`,
`find_record`) with what is looked for and the word, a read (`call_api`) and then how many
rows of which list it returned, a Skill being loaded, a change being prepared (its values
are on the approval card, never on the timeline), and the closing check. The sentences are
written by the core's panel view (`describe_step`), which has a neutral `search` and
`check` stage for this; the panel renders them as it renders any step. Page actions are not
reported a second time: the channel already publishes each dispatched action. A page the
assistant opens loads with the panel closed, as every page load does; the launcher shows
that a turn is under way, and the timeline is there when the panel is opened. A panel that
cannot be told does not fail the turn.

Round trips to the model are most of a turn's time, so fewer are asked for. The first
request already carries the menu pages and paged lists whose own names the person's words
contain, found by the program; `find_record` returns each entity's page and list with
it. `fill_fields` fills or chooses several controls of the shown form, filter or line
table by their labels in one step and may end by clicking a named button. After a page
change, the steps of the same reply that name a path or a field still run; those that
name an element by its number do not, since the numbers are the old page's. A scroll that
names a region moves that region and says so when nothing moved.

The first turn of a session that looks a record up by name, or prepares a write, needs the
actor's application index, and building it reads one record's detail for every entity
whose page labels line columns: a few hundred small reads. They go out four at a time, as
a lookup by name's own reads do. The host's `--warm-index` builds the index in the
background when the actor's session opens instead, so that the turn finds it built (or
waits for the rest of the same build); the application then answers those reads once per
session whether or not a turn ever needs the index, which is why it is off by default.

A closing model request checks the person's words against the page and may send the turn
back once. It is told which controls the turn itself filled or chose; every other value a
form holds was there when it opened and is not a change. Before its last two steps the
turn is told to report what it has. Set `WOA_SLIM_DEBUG_DIR` only in development: it
writes what the model was shown.

The model's conversation lasts one turn. A turn starts from the host's bounded
conversation state (`conversation-state.sqlite3`, keyed by the session's stable
identifier): the person's words and the answer of each of the last four turns, within
32 KiB, and under an answer the writes that turn verified. Nothing an earlier turn asked
of its tools or saw on the page is carried; the page is observed and the data read again.
The same state is what a session finds when it is opened again after its sign-in lapsed
or the host restarted, so the conversation goes on from there. A new conversation clears
it. A value for a write must still be in the person's own words of those turns: one that
only an earlier answer showed is refused before any card.

With `--native-writes` the same turn may also carry out a change an installed Skill
covers. The model is told the installed Skills that offer the current account an
operation, loads one with `load_skill`, and reads its instructions and shared
references in the result; the Skill's operations then appear as tools with their
installed input schemas and the account's current dictionary choices. Calling one is a
proposal: the host checks the inputs against the schema and the person's own words,
prepares the change read-only as the account, compares what the application prepared
with what was proposed, shows the complete approval card, dispatches once after
approval and reads the result back as the same account. A proposal refused before the
card is shown returns to the conversation with the reason, at most three times a turn;
a `finish` written in the same reply as such a proposal does not end the turn.
A declined card, a refusal after approval and an uncertain result each end the turn;
nothing is sent twice, and an identical proposal that already completed in the turn is
not repeated. A request a Skill covers goes through the Skill rather than through a
prefilled form; the page's own submit controls stay unpressed. The next turn starts
with the Skills the last one loaded or proposed through, told again with their text; one it
does not use is dropped after it. The closing check is
given the turn's verified writes. An account that is offered no operation is told
nothing about writes, and its turn is the read-only one.

The explicit `--executor page-agent` pilot uses the same configured optional source
lookup and reviewed forms as the goal composition. Its same server Agent discovers
current reviewed entries with `find_form_entries`, opens one opaque candidate with
`open_form_entry` under the ordinary visible page approval, and feeds the actual
receipt and fresh observation back before describing or preparing fields. A pending
render retains that acknowledged receipt; it does not repeat the opener. Prefill checks
the values and highlights submission for the person. Declared accompanying questions
may continue using only read tools after handoff, preserving the form receipt in the
answer. Arbitrary buttons, unreviewed fields, form submission and page filters remain
unavailable. Source lookup supplies evidence, never action authority. This is a measured
prototype, not a claim of general native-form coverage or model accuracy.

The pilot also offers the neutral `inspect_data` / `resolve_data` tools over the existing
deterministic data resolver. Metadata is bounded and reports omissions; facts retain
exact record and field identities, native values, units and query completeness. The
same Agent receives results without a nested planning model. Each operation checks
the delegated actor, tenant and workspace against the application's identity endpoint;
every API subrequest checks the shared cancellation/deadline and fixed credential
generation. Renewal during a read rejects that operation. Data results grant no page,
form or write authority. These engineering contracts do not establish model accuracy
or widen reviewed native actions; the default executor remains `goal`.

Host startup and authenticated session creation load API documents only when a business
tool needs the schema. An unavailable document endpoint remains a typed read failure,
not an empty catalog. The page-agent pilot also acquires browser context on demand:
static repository answers can finish without page transport, while page operations
require a fresh binding to the submitting page. Login verification and native-guard
ingress qualification remain prerequisites; an unavailable identity service cannot
create an authenticated session.

Git history is optional and requires actual local repositories matching the deployed
source bytes. Repeat `--source-commit ui=<full-commit>` and
`--source-commit server=<full-commit>` for separate repositories below `--source-root`,
or use `--source-commit root=<full-commit>` for a single repository. Each history hit
retains its own repository and deployment commit; two repositories do not acquire a
fictional shared commit. Missing Git objects, allowlisted source drift, or a repository
outside the source boundary refuse this explicit configuration. History queries retain
the source allowlist, and a diff target must belong to the selected repository.

The server checks the acting API identity, `crm:customer:update` (or the application's
wildcard), the exact customer's read and its actor-scoped owner/write membership. It
issues only the unique enabled Edit button on that verified detail route, consumes its
successful guarded receipt and binds the fresh dialog instance. Permission, source,
record or instance changes invalidate the draft. Customer scalar fields use the existing
form port; unsupported fields, creation and complex controls are refused. The Goal stage
never presses Confirm or invokes update APIs. Its isolated scripted browser qualification
opened the editor through the assistant, consumed a visible page approval, filled two
fields and highlighted Confirm, with zero business write requests and unchanged CRM
tables. This prototype is not enabled by default and is not a real-model accuracy result.

Route **all browser admin API traffic** and `/woa/page-guard/` through that proxy before
starting the host with the option. The optional nginx locations are in
[`cloud/nginx.native-guard.locations.conf`](cloud/nginx.native-guard.locations.conf);
include them inside the HTTPS server and publish the container's proxy port on loopback.
Also include `cloud/nginx.native-facts.filter.conf` inside the existing `location /`,
alongside its loader filter. That location overrides server-level substitution filters.

Normal detail/editor loading is reported as pending, including on an unchanged URL. Failed reads, conflicting record identities and visible error messages remain blocking; loading does not grant a verified record route. The Runtime waits for readiness before checking the editor instance and entering values.

The deployment copies the app-owned `page-facts.js` and `yudao-page-controller.js`
from `frontend/src/integrations/yudao/` into `/woa/`; the early facts module must execute
before the application entry, otherwise record arrival fails closed.
They supersede the ordinary API regex and disable alternative app API and websocket
ingress on this origin. A nonce probe checks both public routes before the host offers
native tabs; a failed probe aborts startup. The ordinary deployment is unchanged unless
the option and ingress are installed together.

`--native-navigation` separately enables default-menu clicks under this same fence.
It requires `--native-read-policy`. The app-owned controller extracts mounted menu
targets, while the server checks branches and destinations against the acting user's
page index. It does not require source retrieval or per-page menu allowlists. Overrides
such as custom `menuSelect`, root-only menus and supplied menu trees fail closed.
Visible approval and fresh element refs are still required. This option does not
enable arbitrary buttons, search fields, record-row clicks or submission, and does not
switch the default `goal` executor to the `page-agent` pilot.

During a window, only reviewed GETs reach the upstream. Begin waits for earlier admitted
requests; end acknowledges only when no actor window remains. Lost mutation responses
persist as uncertain admission in `native-guard.sqlite3`; acknowledged windows are also
recorded before the browser operates. Restart treats remaining windows as uncertain
because their monotonic deadlines cannot be safely reconstructed; restarting does not clear them,
and an operator must reconcile the upstream state rather than replay or delete the journal
to recover. Identity cache entries contain credential digests and expire within 60 seconds;
the upstream still authorizes each forwarded request. Window cookies are HttpOnly and
never forwarded upstream. Other browser sessions of that actor also pause writes.
Direct upstream access, background jobs and external services are outside the boundary.
The proxy has isolated HTTP/SQLite tests; complete native application qualification is
pending. Tabs use live ARIA controls, not a site-wide prompt catalog. The optional controller
observes same-origin CRM GET completion without retaining headers, credentials, queries
or result bodies. A detail read must bind its response ID to the route and its bounded
title to one visible header. Current-panel pending and failed reads block completion;
a failed lazy read stays failed after its toast disappears until a new successful read.
Native clicks require that verified record route. Local customer details are qualified
for these facts; other CRM detail structures still need their own qualification, and
opaque/encrypted response envelopes are refused. Cross-tenant visit context is not
qualified: a different `visit-tenant-id` is refused rather than silently reading the
primary tenant. New/Edit buttons,
field entry and submit remain disabled until their separate form qualification.

- The schema is compiled under the acting account from its menus and button permissions, the API documentation (`/v3/api-docs`, reachable only from the server side in the public deployment) and the dictionaries. Every module the account's menus reach is considered. A page becomes an entity only when its source or menu identifies a documented list of records; a dashboard or settings page without one is not claimed as a record list. A page whose own address names a documented list is that list even when no permission names it: the resource may be several words deep (`fms/voucher/statistics`) and the endpoint a `/list` rather than a `/page`, and the menu is the evidence the account may open the page. The response must describe its items, so a page whose endpoint the documentation leaves shapeless is not claimed.
- Lists filter only by the parameters each page defines, so a read pushes down the conditions a parameter narrows without losing a match, reads every page up to 4,000 records, and filters the rest in memory with the core's `matches`. Beyond the bound the read is refused rather than answered from part of the records. A list whose endpoint requires a parent the question did not name is read under that parent when the account can read exactly one such record (`mp/user`'s official account, `fms/voucher`'s account set, the payroll record of a month) — the parent the application itself shows. Several candidates stay the person's choice, and the read refuses with the parameter it needs.
- Explicit API or line-column money units are kept on the adapter: cents normalize to yuan in facts, comparisons and totals; cents-based query parameters are not passed yuan and are filtered in memory under the ordinary read bound. Undeclared currencies are not guessed. An unreadable, missing or malformed owner detail refuses line counts and totals instead of silently omitting that owner.
- CRM lists are read under each scene (owned, involved, subordinates') and merged, because the server applies record permissions only for the scene it is given.
- `UiSource` optionally reads the admin UI's source (`utils/dict.ts`, `router/modules/remaining.ts`, `views/**/index.vue` and the `api/**` modules they import): the endpoint each list page reads (including paged, unpaged and tree lists), literal query arguments the page supplies, list columns and their order, the dictionary each coded column renders through, and record pages. Without it the list endpoint is guessed from the page's path, every documented field is shown, dictionaries come from the modules' naming, and a record opens as its list page. An unpaged tree is flattened under the same 4,000-record read bound. Some otherwise mapped lists require a selected parent, account set or accounting period and refuse a read until those parameters are supplied; mapping a page does not by itself prove its data can be read.
- A record is read as the account's list shows it when the detail endpoint refuses it or leaves a number's name unfilled. The adapter calls a detail endpoint only when its documentation accepts the record's `id`; some `/get` endpoints, such as wallet lookup by user ID, create a record when asked and must not be treated as detail reads. A number whose record the account does not list (a business status, a product unit) is shown by its name field; a link whose row carries only the number is named from the linked entity's list.
- People and departments are listed through the endpoints any logged-in account may call, so a condition on an owner or a department resolves.
- `conventions/reference.json` holds what the reference organisation means by some words, as the owner reviewed them (客户 is the CRM customer, 产品 the ERP product, 成交 an approved contract, 订单 every ERP sales order, 回款 money received, an approval process one of the person's own, 我能看的 every record the account may read). `YudaoApplication` loads it unless given another file or none; `--no-conventions` runs without it.
- `YudaoSource` optionally compiles the deployment's rules from the bound release's server and admin UI (`--source-root` holding `server/` and `ui/`, `--source-release` naming the release): when a button shows (its `v-if` and `v-hasPermi`), when a form field cannot be changed (its input's `:disabled` condition on the record or on how the form was opened; conditions on the page's own state are left out), and what the operation a button calls refuses (the service method's `throw exception(...)` under its `if`, with the module's error message), both tied to the operation's URL and cited as `path:lines@release sha256:...`. The rules are shown to the model only for a question about how the application behaves; a question about data is read with the same index as without them.

`YudaoApplication` composes generic read tools under `yudao.read`. Mutations are off
by default; the host's explicit `--native-writes` (goal or slim executor) enables thirty reviewed CRM, ERP, system organisation/notice, mall catalog, member tag/group and workflow category
operations with a persistent mutation journal, supporting Skill, fresh visible approval
and same-actor readback. It does not enable browser submission. The identity port refuses
unless the browser token and the server reader
identify the same user and tenant. Credential renewal invalidates data caches and metadata
bindings; expired or denied reads preserve authentication/permission errors. The shared
read contract carries API-declared fixed money units, refuses malformed amounts, and does
not report partial or missing aggregate data as zero. Only qualified record templates
yield record addresses; a collection address is not a record address. The deployment
serves the pinned PageController under `/woa/` (copied from a verified tarball and checked
with `require_served_directory`). The goal demo signs a browser in as the evaluation
account, injects the assistant, submits tasks through the panel and meters model calls:

```bash
python -m web_operation_agent_yudao.demo --account account.json --source-root $YUDAO_CACHE --source-release v2026.09 --model-env ../../.env --task "为什么我看不到合同的提交审核按钮？"
```

`evals/yudao/run_goals.py` runs a sealed goal set against the local deployment: it checks the task file and the judge against their seal, requires the sealed browser origin, actor, tenant and snapshot when declared, checks that the current actor's index contains every feasible task's entity and page, then verifies that the signed-in admin page actually renders and reads its permission data from that origin in Chromium before any model call. Pass `--ui-source $YUDAO_CACHE/ui` when the evaluated coverage relies on the deployment's UI source. It runs each task in a fresh session under a hard deadline (the browser's process group is killed past it), treats any change to the database other than a sign-in's stamps as a write, and scores each attempt with the rubric-7 judge. A task set that cites source carries a `source_binding`; the runner then requires `--source-root` and `--source-release` of that release and hands the judge its own sealed copy of the cited files, so a rule counts only with a citation whose lines hash as bound. `--snapshot` checks that the live database is the snapshot the truth was read from; a migrated database needs a newly authored and sealed task set. `--summarize-only` reports all lanes by category and task type. `evals/yudao/replay.py` replays a run's recorded declarations through the current code at no model cost.

- Line tables are offered (the `lines` capability): a record's detail carries them (an order's `items`, a contract's `products`), and the API documentation gives every nested item one shared schema, so each table is found by reading one record's detail and its columns are named as the page's own tables label them. A question about one record's lines reads its detail; a question across all lines reads the details of every record the account may read, up to 400, kept for fifteen seconds.

Known limits: ERP, MES and WMS records open as their list pages; `crm/customer`'s remark is documented upstream as "负责人的用户编号". An accounting set is initialized from `subject_chart.py`'s synthetic chart; without that table loaded, initialize fails on the first closing template whose subject is missing and the set stays uninitialized, while its vouchers, ledgers and settings are read normally. A list whose endpoint requires a period, a month, a dimension or a folder the page itself supplies (`fms/ledger/*`, `wms/inventory`, `hrm/attendance/statistics/*`, `oa/mail-message`) still refuses a read the question gives no value for. Two WeChat pages (`mp/draft`, `mp/free-publish`) call WeChat's own API and fail against a synthetic official account.

`coverage.py` measures this per account, without a model: which of the account's page menus the adapter maps to a readable list, why each unmapped one is not claimed, and — with `--probe` — what every mapped entity answers when it is read through the port a question resolves against.

```bash
PYTHONPATH=src:examples/yudao/src python -m web_operation_agent_yudao.coverage \
    --account .private/yudao-local-evaluator.json --ui-source $YUDAO_CACHE/ui --probe
```

## Offline qualification

`scripts/qualify.sh` runs this adapter's tests, the native host lifecycle tests and the Yudao evaluation apparatus without an application connection or model call. The wheel gate builds the separate adapter, checks its conventions and modules, and composes it outside the checkout. CI installs this distribution alongside core and Odoo.

A deployment may install a reviewed customer-bound contact creation entry with
`--native-create-policy <contact-review.json>` and reference `contact-create`. Pin
`ui/src/views/crm/contact/components/ContactList.vue`, `ContactForm.vue` in the
same contact directory, the customer detail index, and the shared Dialog source. Review the real opener,
create permissions and transient persistence before generating hashes. This admits
an already-open contact creation dialog on a readable customer detail page, with
its parent customer disabled and owner enabled; it never submits or changes the parent.

`--native-filter-policy <query-review.json>` explicitly enables native Goal page
filtering under the existing verified GET guard. The JSON has `schema_version: 1`,
`release`, `entity`, `route`, `component`, `endpoint` (without `/admin-api/`) and
`hashes` for the reviewed query component and its relevant API/helper dependencies.
All bytes must match the allowlisted release and the endpoint must already be in
`native-read-policy`. Review input handlers and query/persistence semantics; hashes
alone grant no authority. The initial compiler accepts reviewed date ranges and
single-choice dictionary/customer selectors, and verifies the successful applied
query through application-owned observations. Calendar entry selects exact enabled days in its owned native popup, preserving the source-defined default endpoint times; dates outside its displayed months are refused. Unsupported fields or changed
actors, form instances, choices, source bytes or query results prevent completion.
No production review is supplied; browser/API writes and form submission remain closed.
