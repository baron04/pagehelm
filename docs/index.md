---
title: PageHelm Documentation
description: Embedded AI operations helm for first-party web applications—server-owned planning, hybrid same-actor reads and DOM control, and fresh visible write approvals.
---

> **TL;DR:** **PageHelm** (`web_operation_agent`) embeds an AI assistant inside first-party web admin applications. The server owns planning, models, credentials, safety modes, and audit; a fixed browser Runtime observes the active page, renders `<DataTable>` results and diff approval cards in a Shadow DOM panel, and never holds model secrets or business plans.

[简体中文](zh-cn/index.md) · [Architecture](architecture/overview.md) · [5-Min Quickstart](integration/quickstart.md) · [L0–L3 Integration](integration/levels.md) · [Verified Writes](security/verified-writes.md) · [Protocol Reference](reference/protocol-and-configuration.md)

## Why PageHelm

Most enterprise back-office assistants break down in one of two ways: API-only chatbots cannot open a screen or prefill a multi-line form in the user's browser, while headless RPA agents hold user cookies on the server and click slowly across paginated tables. **PageHelm** combines same-actor server reads with an embedded same-origin browser Runtime so read queries stay fast and accurate, page navigation happens inside the live app, and every state change stops at an explicit human approval card.

## Documentation map

| Section | Focus | Start here |
| --- | --- | --- |
| **Introduction & Architecture** | Server–Runtime–Adapter split, hybrid API/DOM execution, session isolation | [Architecture Overview](architecture/overview.md) |
| **Quickstart & Integration** | `.env` + `pagehelm.toml`, `./local`, `./deploy`, CI gates, L0–L3 onboarding | [5-Min Quickstart](integration/quickstart.md) · [L0–L3 Integration](integration/levels.md) |
| **Security & Verified Writes** | Modes (`read-only`, `assisted-draft`, `verified-operate`), 8-stage write pipeline, redaction | [Security & Verified Writes](security/verified-writes.md) |
| **Reference** | `pagehelm.toml` schema, gateway HTTP/SSE endpoints, `panel_contract.json` v2 | [Protocol & Configuration](reference/protocol-and-configuration.md) |

## Repository layout

| Path | Role |
| --- | --- |
| `src/web_operation_agent/` | Application-neutral core: turn execution, `WritePipeline`, gateways, schemas, SQLite stores |
| `frontend/` | Browser Runtime (`assistant.js`), Shadow DOM React panel, and shared `element-plus/` helpers |
| `examples/yudao/` | Full-stack reference adapter (`yudao`) with local dual environments (`./local`) and cloud deployment (`./deploy`) |
| `testapps/contacts/` | Structurally distinct synthetic Contacts application verifying core contracts and Chromium integration |
| `docs/` & `frontend/docs/` | Bilingual documentation source (`docs/`) and Astro Starlight static site (`frontend/docs/`) |

> **Alpha status:** PageHelm is pre-production software. Evaluate only against isolated, resettable synthetic fixtures without connecting customer accounts, live payment flows, or production databases.
