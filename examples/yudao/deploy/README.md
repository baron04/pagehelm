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

`src/web_operation_agent_yudao` is the adapter a goal-directed turn resolves against. It supplies `GoalData` only; the core resolves.

- The schema is compiled under the acting account from its menus and button permissions, the API documentation (`/v3/api-docs`, reachable only from the server side in the public deployment) and the dictionaries. Every module the account's menus reach is considered. A page becomes an entity only when its source or menu identifies a documented list of records; a dashboard or settings page without one is not claimed as a record list. A page whose own address names a documented list is that list even when no permission names it: the resource may be several words deep (`fms/voucher/statistics`) and the endpoint a `/list` rather than a `/page`, and the menu is the evidence the account may open the page. The response must describe its items, so a page whose endpoint the documentation leaves shapeless is not claimed.
- Lists filter only by the parameters each page defines, so a read pushes down the conditions a parameter narrows without losing a match, reads every page up to 4,000 records, and filters the rest in memory with the core's `matches`. Beyond the bound the read is refused rather than answered from part of the records. A list whose endpoint requires a parent the question did not name is read under that parent when the account can read exactly one such record (`mp/user`'s official account, `fms/voucher`'s account set, the payroll record of a month) — the parent the application itself shows. Several candidates stay the person's choice, and the read refuses with the parameter it needs.
- Explicit API or line-column money units are kept on the adapter: cents normalize to yuan in facts, comparisons and totals; cents-based query parameters are not passed yuan and are filtered in memory under the ordinary read bound. Undeclared currencies are not guessed. An unreadable, missing or malformed owner detail refuses line counts and totals instead of silently omitting that owner.
- CRM lists are read under each scene (owned, involved, subordinates') and merged, because the server applies record permissions only for the scene it is given.
- `UiSource` optionally reads the admin UI's source (`utils/dict.ts`, `router/modules/remaining.ts`, `views/**/index.vue` and the `api/**` modules they import): the endpoint each list page reads (including paged, unpaged and tree lists), literal query arguments the page supplies, list columns and their order, the dictionary each coded column renders through, and record pages. Without it the list endpoint is guessed from the page's path, every documented field is shown, dictionaries come from the modules' naming, and a record opens as its list page. An unpaged tree is flattened under the same 4,000-record read bound. Some otherwise mapped lists require a selected parent, account set or accounting period and refuse a read until those parameters are supplied; mapping a page does not by itself prove its data can be read.
- A record is read as the account's list shows it when the detail endpoint refuses it or leaves a number's name unfilled. The adapter calls a detail endpoint only when its documentation accepts the record's `id`; some `/get` endpoints, such as wallet lookup by user ID, create a record when asked and must not be treated as detail reads. A number whose record the account does not list (a business status, a product unit) is shown by its name field; a link whose row carries only the number is named from the linked entity's list.
- People and departments are listed through the endpoints any logged-in account may call, so a condition on an owner or a department resolves.
- `conventions/reference.json` holds what the reference organisation means by some words, as the owner reviewed them (客户 is the CRM customer, 产品 the ERP product, 成交 an approved contract, 订单 every ERP sales order, 回款 money received, an approval process one of the person's own, 我能看的 every record the account may read). `YudaoApplication` loads it unless given another file or none; `--no-conventions` runs without it.
- `YudaoSource` optionally compiles the deployment's rules from the bound release's server and admin UI (`--source-root` holding `server/` and `ui/`, `--source-release` naming the release): when a button shows (its `v-if` and `v-hasPermi`), when a form field cannot be changed (its input's `:disabled` condition on the record or on how the form was opened; conditions on the page's own state are left out), and what the operation a button calls refuses (the service method's `throw exception(...)` under its `if`, with the module's error message), both tied to the operation's URL and cited as `path:lines@release sha256:...`. The rules are shown to the model only for a question about how the application behaves; a question about data is read with the same index as without them.

`YudaoApplication` composes it read-only for a live session: no business tools, the goal data's reads under the scope `yudao.read`, and an identity port that asks yudao who the browser's token and the server's account are and refuses unless both are the same user in the same tenant. The deployment serves the pinned PageController under `/woa/` (copied from a verified tarball and checked with `require_served_directory`). The demo signs a browser in as the evaluation account, injects the assistant, submits tasks through the panel and meters model calls into the project's spend ledger:

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
