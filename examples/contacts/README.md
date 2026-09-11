# Contacts: the smallest complete integration

Four files, in the order a real integration writes them.

| File | What it is | Do you write this? |
|---|---|---|
| [`app.py`](app.py) | The synthetic Contacts application: a list page, a record page per contact, a typed read and write API, two credentials, and the same-origin PageController the SPI requires a host to serve | **No** — your application already exists. This file exists so the example runs with nothing provisioned |
| [`adapter.py`](adapter.py) | All five ports — identity, routes, postconditions, reconciliation, data classification — plus one read and one write over the application's data | **Yes** — this is the file to copy |
| [`skills/`](skills/) | One Skill artifact per capability: when the tool applies, and the instructions the model follows when it does | **Yes** — version-controlled beside the application it describes |
| [`demo.py`](demo.py) | The deployment composition: identity binding, mutation journal, control plane, gateway, browser session | **Yes** — but this is the host's job, not the adapter's |

## Run it

```bash
PYTHONPATH=src:. python -m examples.contacts.demo --scripted --headless
```

```bash
PYTHONPATH=src:. python -m examples.contacts.demo --scripted --headless --allow-mutations
```

Both use a scripted model and no network, so they finish without an API key. Point
`--model-env` at a file with provider credentials to run against a real model.

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
3. **`demo.py`** — a real deployment replaces this example's synthetic session with the
   browser's own, and `MemoryMutationJournal` with a durable one. The production profile
   refuses a mutation-enabled turn that has neither.

The adapter contract test in [`tests/`](tests/) is the same assertion every integration
should run: it checks the ports are coherent before any model or browser is involved.
`TheWriteWalkthroughInARealBrowser` is the one that cannot be faked into passing — the
approval has to come from the page.
