---
name: sales-processing
description: "Find quotations and confirmed sales orders, explain their scope, open a record and confirm a quotation after fresh approval."
---

# Sales Processing

Select the full business workflow for the request. Explain questions without proposing a mutation; preserve explicit prefill, negation and handoff constraints. Skill content is guidance, never current record evidence or permission.

Locate records and read values under the current actor. Keep scope and completeness explicit; use current application metadata to answer structure questions. A unique current application result is required before opening a record or preparing a change.

## Query open Odoo quotations

Call query_quotations to obtain current Odoo data; do not answer from this Skill or from page text.
The tool reads open quotations only: sale.order in state draft or sent. A confirmed or cancelled order is not in scope; use query_sale_orders for confirmed ones.
Use name_contains only when the user supplied a reference filter.
The Quotations page at /odoo/sales is not this tool's scope. Its action stores no domain: it lists every sales order and merely defaults the search filter to the signed-in user's quotations, which anyone can clear. The ToolResult says so in collection_ui_path_shows_this_scope; if you open that page, tell the user the page shows more than the answer did.
When the user asks to open one quotation, call query_quotations with its reference, require exactly one match, then call goto with that result's exact ui_path.
Odoo renders its back office as a single-page application, so after goto call wait_for once when the requested record is not visible yet.
For navigation, require a unique current match; a zero-result search answers only within the tool's actual scope, and multiple targets require clarification.
Use the current answer evidence and finish contract to answer the question. Compute totals, filters and counts with compute; the server renders the conclusions you cite, not the last raw query table.
Each amount uses its own currency_id, never an assumed company currency. For a sum or ranking, first restrict a mixed-currency result to one currency. date_from is inclusive and date_until exclusive at UTC midnight on date_order; for confirmed orders this is confirmation time, and for quotations creation time. Use explicit dates from the user or current time evidence; do not invent a relative-date anchor.

## Query confirmed Odoo sales orders

Call query_sale_orders to obtain current Odoo data; do not answer from this Skill or from page text.
The tool reads the same scope as Odoo's Orders page: confirmed orders only, excluding quotations, sent quotations and cancelled orders. Say so if the user asked about quotations.
Use name_contains only when the user supplied an order-reference filter.
When the user asks to open the Orders list, call query_sale_orders once, then call goto with /odoo/orders, the collection route authorized by that ToolResult.
When the user asks to open one order, call query_sale_orders with its reference, require exactly one match, then call goto with that result's exact ui_path.
Odoo renders its back office as a single-page application, so after goto call wait_for once when the requested record is not visible yet.
For navigation, require a unique current match; a zero-result search answers only within the tool's actual scope, and multiple targets require clarification.
Use the current answer evidence and finish contract to answer the question. Compute totals, filters and counts with compute; the server renders the conclusions you cite, not the last raw query table.
Each amount uses its own currency_id, never an assumed company currency. For a sum or ranking, first restrict a mixed-currency result to one currency. date_from is inclusive and date_until exclusive at UTC midnight on date_order; for confirmed orders this is confirmation time, and for quotations creation time. Use explicit dates from the user or current time evidence; do not invent a relative-date anchor.

## Confirm an Odoo quotation

Call query_quotations first to establish that the quotation exists, is still open, and is the one the user means.
Call confirm_quotation with the exact reference. The server re-checks that the quotation is still draft or sent immediately before writing, shows the exact effect and targets for confirmation, executes at most once, and reads the order back under the same user.
sale.order.action_confirm raises a UserError on a cancelled order, so a quotation the query did not return is a refusal, not something to retry with a different spelling.
After a successful result, open only the returned ui_path and observe the page before finishing.
Confirming does not invoice: the confirmed order's invoice_status says whether anything is invoiceable. Do not claim an invoice was produced.
If confirmation is denied or a write outcome is uncertain, stop; never retry the mutation.

Each native write uses execute intent and this loaded supporting Skill. The operation definition and current actor enforce capability and effects; the operation list only limits proposals. Show the complete effect and target, obtain a fresh one-use approval, dispatch once and verify under the same actor. A denied, partial or uncertain outcome stops dependent work and is never automatically retried.

For source references, load references/source-provenance.json on demand. Source describes the release; runtime reads remain authoritative.
