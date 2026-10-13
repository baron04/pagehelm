---
name: order-billing
description: "Read customer invoices and create a regular draft invoice from an invoiceable confirmed order; never post or send it."
---

# Order Billing

Select the full business workflow for the request. Explain questions without proposing a mutation; preserve explicit prefill, negation and handoff constraints. Skill content is guidance, never current record evidence or permission.

Locate records and read values under the current actor. Keep scope and completeness explicit; use current application metadata to answer structure questions. A unique current application result is required before opening a record or preparing a change.

## Query Odoo customer invoices

Call query_customer_invoices to obtain current Odoo data; do not answer from this Skill or from page text.
The tool reads customer invoices only (account.move with move_type out_invoice). Credit notes are excluded even though Odoo's own Invoices page lists them; say so if the user asked about refunds.
A draft invoice has no number yet -- Odoo assigns one on posting -- so the tool reports it as '(draft, not yet numbered)'. Never present that as a reference the user can quote.
The sale_order field carries the invoice's origin, which is how an invoice is tied back to the order it came from.
When the user asks to open one invoice, call query_customer_invoices, require exactly one match, then call goto with that result's exact ui_path.
Odoo renders its back office as a single-page application, so after goto call wait_for once when the requested record is not visible yet.
Use the current answer evidence and finish contract to answer the question. Compute totals, filters and counts with compute; the server renders the conclusions you cite, not the last raw query table.
amount_total includes taxes; amount_residual is the amount still due, not the original total. Both use the invoice currency_id. date_from/date_until bound invoice_date inclusively/exclusively, not the posting date. customer_id uses an exact res.partner id from a current read.

## Invoice an Odoo sales order

Call query_sale_orders first and read the order's invoice_status; only an order with something still to invoice can be invoiced. When it cannot, still call invoice_sale_order: the server re-checks invoice_status and refuses before proposing anything, so nothing is written and the user is never asked to approve it, and the refusal it returns is the one to report. Do not answer with what the order query returned instead.
Call invoice_sale_order with the exact order reference. The server re-checks invoice_status immediately before writing, shows the exact effect and targets for confirmation, and reads the created invoice back under the same user.
Odoo's own form offers two buttons both labelled Create Invoice, separated only by the condition under which each appears, and one of them proposes a percentage down payment. This tool is always the ordinary invoice: it pins the regular-invoice method. Never describe the result as a down payment.
The created invoice is a draft. Odoo assigns its number on posting, so report it as a draft without a number rather than quoting one.
Use query_customer_invoices when the user wants to see the invoice alongside the others.
After a successful result, open only the returned ui_path and observe the page before finishing.
If confirmation is denied or a write outcome is uncertain, stop; never retry the mutation.

Each native write uses execute intent and this loaded supporting Skill. The operation definition and current actor enforce capability and effects; the operation list only limits proposals. Show the complete effect and target, obtain a fresh one-use approval, dispatch once and verify under the same actor. A denied, partial or uncertain outcome stops dependent work and is never automatically retried.

For source references, load references/source-provenance.json on demand. Source describes the release; runtime reads remain authoritative.
