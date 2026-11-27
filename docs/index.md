---
title: PageHelm Documentation
description: Embedded server-governed AI operations framework (web_operation_agent) with same-actor reads, same-origin UI actions, and visible write approvals.
template: splash
hero:
  title: PageHelm
  tagline: Embedded server-governed AI operations framework for first-party web applications.
---

## How It Works

- **Same-actor data reads:** List counts, conditioned aggregates, and multi-table joins run on the server under the signed-in user's identity rather than scraping paginated UI screens or holding user cookies on the server.
- **Same-origin UI actions:** Navigation, filtering, and undoable form prefill run inside the user's live browser tab; password and file upload controls are always left for human input.
- **Guarded state mutations:** Writes are restricted to operations declared by your adapter (with an optional `WOA_READ_ONLY=true` kill-switch). Every write requires human confirmation on a visible approval card, single-use dispatch, and same-actor readback; timed-out or disconnected writes stay `INDETERMINATE` and are never retried automatically.

## Reading Paths

| Goal | Start here |
| --- | --- |
| Run the bundled local demo to inspect queries, prefill, and write approvals | [Local Demo Quickstart](integration/quickstart.md) |
| Integrate PageHelm into your own web application | [Declarative Application Onboarding](integration/onboarding.md) · [Skills & Typed Writes](integration/skills-and-writes.md) |
| Review trust boundaries, execution engine, and write safety | [Architecture Overview](architecture/overview.md) · [Write Safety & Privacy](security/verified-writes.md) |
| Deploy, reconcile writes, or look up config and protocol fields | [Deployment & Operations](security/operations-and-observability.md) · [Configuration & Protocol](reference/protocol-and-configuration.md) |

## Repository Layout

| Path | Responsibility |
| --- | --- |
| `src/web_operation_agent/` | Application-neutral server core: turn loop, write pipeline, gateway, schemas, and SQLite stores |
| `frontend/` | Browser Runtime (`assistant.js`), Shadow DOM panel, `PageController`, and shared `element-plus/` helpers |
| `examples/yudao/` | Full-stack reference adapter with local Compose scripts (`./local`) and release CLI (`./deploy`) |
| `testapps/contacts/` | Independent synthetic Contacts application verifying core contracts and browser integration |
| `docs/`, `frontend/docs/` | Bilingual Markdown documentation corpus and Astro Starlight docs site |

> **Alpha notice:** PageHelm is pre-production software. Evaluate only against isolated, resettable synthetic environments—do not connect production databases, live accounts, or payment flows.
