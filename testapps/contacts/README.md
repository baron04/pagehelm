# Synthetic Contacts test application

This checkout-only application exercises the generic adapter and browser protocols. It is
test apparatus, not a public integration template; use `examples/odoo/` when integrating a
real application.

Its files mirror the responsibilities a real integration implements:

| File | What it is | Do you write this? |
|---|---|---|
| [`app.py`](app.py) | The synthetic Contacts application: a list page, a record page per contact, a typed read and write API, two credentials, users with different permissions, two workspaces that repeat contact names, and the same-origin PageController the SPI requires a host to serve | **No** — your application already exists. This file exists so the example runs with nothing provisioned |
| [`adapter.py`](adapter.py) | All five ports — identity, routes, postconditions, reconciliation, data classification — plus one dedicated read, one write, and a REST/list implementation of the application-neutral entity-read contract | See the Odoo adapter for the supported reference |
| [`skills/`](skills/) | One Skill artifact per capability: when the tool applies, and the instructions the model follows when it does | Test fixtures only |
| [`demo.py`](demo.py) | A Playwright-driven qualification composition for repeatable browser tests | **No** — it proves the chain but is not the product host |
| [`server.py`](server.py) | The ordinary-browser composition: application login, server-side identity binding, durable session/journal state, shared gateway, and ReAct executor | Test host |

## Run it

```bash
PYTHONPATH=src:. python -m testapps.contacts.demo --scripted --headless
```

```bash
PYTHONPATH=src:. python -m testapps.contacts.demo --scripted --headless --allow-mutations
```

Both use a scripted model and no network, so they finish without an API key. Point
`--model-env` at a file with provider credentials to run against a real model.

The Contacts adapter is also the second, structurally different qualification target for
`EntityReadPort`. It exposes the same `discover_entities`, `describe_entity`, and
`query_entity_records` schemas as Odoo, but obtains actor-visible rows from the synthetic
REST endpoint and applies the bounded AND filter, stable sort, count, and pagination inside
the adapter instead of translating to an ORM query. Both adapters call the same reusable
`assert_entity_read_contract`; this remains internal qualification, not a second public
reference integration.

To use the product as an ordinary signed-in user, without Playwright or browser injection:

```bash
PYTHONPATH=src:. python -m testapps.contacts.server --scripted --port 8765
```

The ordinary-browser host defaults to 10 sessions, 3 simultaneous model requests, and 2
requests per authenticated actor. Use `--max-sessions`, `--max-active-requests`, and
`--max-active-requests-per-actor` to set a smaller qualified deployment envelope. Capacity
is refused before issuing another capability; provider failures do not invalidate an
otherwise healthy browser session.

Open the printed writer or reader login URL in any browser. The application sets its own
HttpOnly session cookie, derives the actor on the server, and returns one ephemeral Runtime
capability to that browser tab. Reloading reuses the tab's server session and replaces only
the document lease. A different tab gets a different capability. `POST /woa/logout` revokes
the exact principal issuance, closes its Agent sessions, and clears the application cookie.
The scripted mode always runs the fixed Grace Hopper read walkthrough; omit `--scripted` and
provide `--model-env` for free-form goals. The example binds only to loopback and contains
synthetic data.

## The write is the part worth reading

An example with only a read leaves every author to invent the hard half. The chain is five
steps, and each exists because of a specific way writes go wrong:

1. **`prepare_mutation` finds exactly one target** over a scan it knows finished, and
   refuses a duplicate. A write aimed at the wrong record is not something a later check
   undoes.
2. **It states an exact effect and exact targets.** That text is what the user approves. A
   proposal reading "create a contact" would be approval of anything.
3. **The framework shows it and takes one approval**, from the page session holding the
   lease — there is no other way to approve, by design.
4. **`execute_mutation` re-checks immediately before dispatch**, closing the ordinary
   time-of-check gap without ever retrying the write itself.
5. **It reads the record back under the same actor** and compares. Anything else — no id,
   a readback that fails or disagrees — raises `BusinessMutationIndeterminate`, which the
   framework records and never retries.

Two consequences worth noticing in the code:

- **A writeable integration needs a real identity port.** `AgentTurnConfig` refuses to
  enable business mutations without a verified session identity. `_ContactsIdentityPort`
  asks the application who the browser is and who the delegated token is, *separately*, and
  `bind_matching_identity` refuses unless both name the same actor. An application with no
  authenticated actor uses `AnonymousIdentityPort` and cannot write.
- **The application refuses a write carrying only the browser session.** A page that could
  write with its own cookie is one CSRF away from a write nobody asked for, so `POST
  /api/contacts` takes the delegated credential and nothing else.

## Authority belongs to the application

Three synthetic users sign in to the `northstar` workspace: `ada-lovelace` may read and write,
`rosalind-franklin` may only read, and `hedy-lamarr` may do nothing at all. A second workspace, `southwind`, holds contacts with the
same names as some of `northstar`'s.

```bash
PYTHONPATH=src:. python -m testapps.contacts.demo --allow-mutations --actor rosalind-franklin
```

- **The principal carries what the application grants, not what the catalog offers.**
  `_ContactsIdentityPort` asks the application what the actor may do. Rosalind's principal
  has `contacts:read` only, so the create tool is refused before any request is made; the
  panel says the account is not permitted and nothing changed, and the turn ends there —
  the Agent is never handed the refusal to try the page instead.
- **The application refuses the same write on its own** (HTTP 403), and the adapter maps
  that to `BusinessToolPermissionError` as well. A test forges an over-scoped principal to
  show that this second layer holds by itself.
- **A refused read ends the turn the same way.** The application refuses Hedy every read,
  its pages included, and a principal without the read scope is refused before any request
  is made. Neither refusal goes back to the Agent, so it never falls back to the page.
- **Every read carries the delegated credential** and returns only the actor's workspace.
  The duplicate check cannot mistake `southwind`'s Radia Perlman for one in `northstar`, and
  "not found" is stated only for the scope this account could search.

Reconciliation is the other half of "never retried": when an outcome is lost,
`_ContactsReconciler` returns `unknown` with `requires_human` and the counts an operator
needs. It never answers "completed" — a matching record is not proof that this proposal
created it.

## What to change first

1. **`adapter.py`** — replace `_ContactsToolExecutor` with calls to your application's API,
   using the browser user's own credentials. The framework refuses a tool result whose
   routes the adapter did not declare, and one that authorises a route it did not show, so
   `DeclaredRoutePort` has to describe your real URL shapes.
2. **`skills/`** — one artifact per capability. `BusinessSkill.from_bytes` validates that
   the tool names a Skill references actually exist, so a Skill cannot drift from the tool
   surface, and that every `provenance` entry is a citation somebody can go and check.
3. **`server.py`** — connect its bootstrap hook to your authenticated application shell.
   Keep actor resolution on the application server: the browser supplies a per-tab id, not
   an actor, token, tenant, or scope. Put the shared Agent gateway behind an HTTPS reverse
   proxy and pass its browser-visible URL as `public_base_url`; the listener itself remains
   loopback-only. Use the durable session and mutation stores as the starting point, then
   replace the local SQLite backend when the deployment spans hosts.

The adapter and entity-read contract tests in [`tests/`](tests/) are the same assertions
every integration enabling those surfaces should run: they check the ports are coherent and
the discover/describe/query/evidence path is opaque and bounded before any model or browser
is involved.
`TheWriteWalkthroughInARealBrowser` is the one that cannot be faked into passing — the
approval has to come from the page. The same browser suite also exercises no decision,
denial, expiry, an exact HTTP replay, two independently approved sessions racing on one
business key, a lost response after commit, and failed readback after acknowledgement.
`TheApplicationsOwnAuthorityInARealBrowser` signs in as the read-only user and as a user whose
workspace shares names with another, and checks the model context, audit ledger, replay,
page and panel for anything from the other workspace.
