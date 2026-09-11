# Integrating your application

This directory holds every worked integration. None of them is the product: the product is
the core package and the ports it asks an application to implement. These show what
implementing them looks like.

**Start with [`contacts/`](contacts/).** It is a deliberately small, complete integration —
a synthetic application, an adapter, one Skill artifact, one typed tool, and the deployment
composition that serves a turn. Copy that directory, point it at your application, and you
have the shape of a real integration. It has no database, no OAuth and no external service,
so it runs from a checkout with nothing provisioned:

```bash
PYTHONPATH=src:. python -m examples.contacts.demo --help
```

`odoo/`, `twenty/`, `ofbiz/` and any application added later are **reference integrations
against real applications**. They are packaged as installable distributions because the evaluation
harness imports them and because the core wheel must be able to assert that it contains
none of them. They are larger and carry real concerns — delegated OAuth, pagination
completeness, session-parameter redaction — so read them second, when the small example
has stopped answering your questions. Of the three, **`odoo/` is the one to read**: it is the
only integration that exercises every port including the offline `SourceCompiler`, and its
identity story — an HttpOnly session the page cannot hand over, plus a per-user API key — is
closer to what a real application will make you deal with than a bearer token in a file.

| Directory | What it is | Installable |
|---|---|---|
| [`contacts/`](contacts/) | The template: synthetic app + adapter + Skill + composition | No — run from a checkout |
| [`odoo/`](odoo/) | Odoo 18 reference integration — the main application: two-sided identity, a source compiler, typed reads with completeness, one guarded write | `web-operation-agent-odoo` |
| [`twenty/`](twenty/) | Twenty CRM reference integration, kept as the second adapter and as the comparison harness's application | `web-operation-agent-twenty` |
| [`ofbiz/`](ofbiz/) | Apache OFBiz reference integration, kept for MPA/iframe regression | `web-operation-agent-ofbiz` |

The contract these implement is documented in
[`docs/integrations/application-adapter.md`](../docs/integrations/application-adapter.md).
Read that for what each port must guarantee; read `contacts/` for what the code looks like.
