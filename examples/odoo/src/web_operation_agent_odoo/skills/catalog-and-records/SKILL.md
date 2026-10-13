---
name: catalog-and-records
description: "Find saleable products and query current actor-visible business entities, fields, records and pages outside dedicated collections."
---

# Catalog And Records

Select the full business workflow for the request. Explain questions without proposing a mutation; preserve explicit prefill, negation and handoff constraints. Skill content is guidance, never current record evidence or permission.

Locate records and read values under the current actor. Keep scope and completeness explicit; use current application metadata to answer structure questions. A unique current application result is required before opening a record or preparing a change.

## Query saleable Odoo products

Call query_products to obtain current Odoo data; do not answer from this Skill or from page text.
The tool reads the same scope as Odoo's Products page under Sales: product templates marked as saleable. Purchase-only or archived products are out of scope.
Use name_contains only when the user supplied a product-name filter.
When the user asks to open the Products list, call query_products once, then call goto with /odoo/products, the collection route authorized by that ToolResult.
When the user asks to open one product, call query_products with its name, require exactly one match, then call goto with that result's exact ui_path.
Odoo renders its back office as a single-page application, so after goto call wait_for once when the requested record is not visible yet.
For navigation, require a unique current match; a zero-result search answers only within the tool's actual scope, and multiple targets require clarification.
Use the current answer evidence and finish contract to answer the question. Compute totals, filters and counts with compute; the server renders the conclusions you cite, not the last raw query table.
list_price is the catalog sales price in this record's currency_id. Do not interpret it as stock value, a quotation price or an outstanding balance.

## Discover and query current application records

Use this only when no dedicated typed read Skill covers the requested entity. Dedicated tools carry narrower business meaning and remain preferable.
Call discover_entities with the user's entity or page words. It searches only the current actor's adapter-filtered catalog and returns opaque entity IDs; never invent or pass a native resource name.
If several candidates are plausible, ask the user to choose. Otherwise call describe_entity with the selected entity ID and a field_query naming the fields or filter concepts needed.
Call query_entity_records only with entity and field IDs returned by those current metadata calls. Filters are combined by AND, use declared choice values rather than labels, and ordering is fixed by the server.
The result reports exact matching count, actor-visible source total, offset, returned count, scope and collection time. State limits explicitly; a page of records is not the whole matching set unless completeness says so.
Do not use this metadata path for writes, secrets, configuration, messages, attachments, users, access control, transient models or fields the descriptor omitted.
Use current typed evidence for the answer. Source code may explain a rule, but it never replaces this actor-scoped read for current data or permissions.

Each native write uses execute intent and this loaded supporting Skill. The operation definition and current actor enforce capability and effects; the operation list only limits proposals. Show the complete effect and target, obtain a fresh one-use approval, dispatch once and verify under the same actor. A denied, partial or uncertain outcome stops dependent work and is never automatically retried.

For source references, load references/source-provenance.json on demand. Source describes the release; runtime reads remain authoritative.
