# Contacts: the smallest complete integration

Four files, in the order a real integration writes them.

| File | What it is | Do you write this? |
|---|---|---|
| [`app.py`](app.py) | The synthetic Contacts application: a list page, a record page per contact, and the same-origin PageController the SPI requires a host to serve | **No** — your application already exists. This file exists so the example runs with nothing provisioned |
| [`adapter.py`](adapter.py) | The ports: identity, route validation, result data classification, plus one read-only typed tool over the application's data | **Yes** — this is the file to copy |
| [`skills/contacts-query.json`](skills/contacts-query.json) | The Skill artifact: when this tool applies, and the instructions the model follows when it does | **Yes** — one file per Skill, version-controlled beside the application it describes |
| [`demo.py`](demo.py) | The deployment composition: identity, mutation journal, control plane, gateway, browser session | **Yes** — but this is the host's job, not the adapter's |

## Run it

```bash
PYTHONPATH=src:. python -m examples.contacts.demo --help
```

The default path uses a scripted model and no network, so it finishes without an API key.
Point `--model-env` at a file with provider credentials to run it against a real model.

## What to change first

1. **`adapter.py`** — replace `_ContactsToolExecutor` with calls to your application's API,
   using the browser user's own credentials. The framework will refuse a tool result whose
   routes the adapter did not declare, so `DeclaredRoutePort` has to describe your real URL
   shapes.
2. **`skills/`** — one artifact per capability. `BusinessSkill.from_bytes` validates that the
   tool names a Skill references actually exist, so a Skill cannot drift from the tool surface.
3. **`demo.py`** — a real deployment replaces `AnonymousIdentityPort` with an identity port
   that binds the logged-in user, and `MemoryMutationJournal` with a durable one. The
   composition refuses to start if a mutation-enabled turn has neither.

The adapter contract test in [`tests/`](tests/) is the same assertion every integration
should run: it checks the ports are coherent before any model or browser is involved.
