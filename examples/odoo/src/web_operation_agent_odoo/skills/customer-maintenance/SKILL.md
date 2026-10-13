---
name: customer-maintenance
description: "Query, explain, create, archive and hand over edits to Odoo customers; handle name ambiguity and automatic form saving."
---

# Customer Maintenance

Select the full business workflow for the request. Explain questions without proposing a mutation; preserve explicit prefill, negation and handoff constraints. Skill content is guidance, never current record evidence or permission.

Locate records and read values under the current actor. Keep scope and completeness explicit; use current application metadata to answer structure questions. A unique current application result is required before opening a record or preparing a change.

## Query Odoo customers

Call query_customers to obtain current Odoo data; do not answer from this Skill or from page text.
The tool reads the same scope as Odoo's Customers page: partners with a customer rank above zero. Ordinary contacts and vendors are out of scope.
Use name_contains only when the user supplied a customer-name filter.
When the user asks to open the Customers list, call query_customers once, then call goto with /odoo/customers, the collection route authorized by that ToolResult.
When the user asks to open one customer, call query_customers with their name, require exactly one current match, then call goto with that result's exact ui_path. The goto result already contains a fresh observation; do not call wait_for or observe after a successful navigation.
For navigation, require a unique current match; a zero-result search answers only within the tool's actual scope, and multiple targets require clarification.
Use the current answer evidence and finish contract to answer the question. Compute totals, filters and counts with compute; the server renders the conclusions you cite, not the last raw query table.
The read includes phone, mobile, email and vat, the tax identification number. Cite the requested fields from current evidence; a returned empty field differs from a field that was not read. res.partner ids can filter order and invoice queries through customer_id.
When the question is how many customers there are, call query_customers with no limit, so the read covers the whole scope; then count with compute. A count over a read that was limited or truncated can only answer for the part that was read, and the answer says so.

## Create an Odoo customer

Use query_customers when useful to inspect current delegated-user facts before proposing a write, but never treat that read as the terminal answer to this write request.
Both customer name and a complete email address such as name@example.com are mandatory. If either one is missing or malformed, ask the user to correct that field and end the turn. Never invent a value or describe either field as optional.
Treat query_customers only as a duplicate preflight for this intent. Whether it finds a match or not, call create_customer with the supplied fields: the mutation tool repeats the complete duplicate check at the authoritative pre-dispatch boundary and returns a safe refusal when a duplicate exists. Never answer a creation request with a read-only query result.
Call create_customer only with the exact customer name and email address supplied by the user.
The server will normalize the proposal, check duplicates over a complete scan, show the exact effect for confirmation, execute at most once, and read it back under the same user.
After a successful result, open only the returned ui_path and observe the page before finishing.
If confirmation is denied or a write outcome is uncertain, stop; never retry the mutation.

## Archive one Odoo customer

Call query_customers first and resolve exactly one active customer. Never infer a record id from a name and never archive one of several matches without clarification.
Call archive_customer with only the exact record_id returned by query_customers. The server reads that record again, shows the exact customer in a confirmation card, writes at most once, and verifies that the same record is inactive.
After success, do not try to open the archived record. Say that it was archived and that it no longer appears in the active customer list.
If confirmation is denied, expires, or the outcome is uncertain, stop and never retry the mutation.

## Prepare an Odoo customer form

Both customer name and email address are mandatory. If either is missing, ask the user for the missing value and end the turn. Never invent either value.
Call prepare_customer_form with exactly the name and email the user supplied. It checks duplicates over a complete delegated-user scan and issues a one-turn form draft.
After a successful result, call prefill_form with no arguments only for a confirmed form handoff. The server shows the exact name, email and automatic-save warning before input; without the enabled write-confirmation channel, filling is refused.
The draft can save automatically when the page is left. Never describe it as transient or promise that it remains unsaved after leaving. Stop after prefill_form; never click Save, submit, invoke another mutation, or claim the customer was created.
If preparation or prefill is refused, explain the short reason and do not retry unchanged input.

## Prepare a change to an existing Odoo customer

Only an existing customer is edited this way. Read the customer first, with query_customers, and use the exact record_id it returns; never guess an id from a name.
Call prepare_customer_edit_form with that record_id and only the fields the user asked to change. Never invent a value, and never include a field the user did not name.
After a successful result, call prefill_form with no arguments. The server shows the user a confirmation naming the old and new values, and only then types them into the form.
This path only enters the approved values. Odoo can save the open form when the page is left, so stop after prefill_form: never click, submit, invoke a mutation, or say the change was saved.
If the confirmation is declined or expires, nothing was typed. Say so plainly and do not retry the same values unasked.

Each native write uses execute intent and this loaded supporting Skill. The operation definition and current actor enforce capability and effects; the operation list only limits proposals. Show the complete effect and target, obtain a fresh one-use approval, dispatch once and verify under the same actor. A denied, partial or uncertain outcome stops dependent work and is never automatically retried.

For source references, load references/source-provenance.json on demand. Source describes the release; runtime reads remain authoritative.
