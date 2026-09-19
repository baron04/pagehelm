"""What each packaged Odoo Skill was compiled from, so it can be compiled again.

A Skill artifact records the facts it was drafted with, not the question they answered. That
is fine until a citation rots, which is the one time you need to ask the source again -- and
recovering "which declared request was this Skill about" from the artifact would mean reading
it out of the citations that are exactly what went wrong. G3 found both failure modes in the
shipped set: four Skills cited *unqualified* Odoo XML ids that four different addons each
declare, and one cited a file outside the compiled tree. In neither case could the artifact
say where to look.

So the drafting input is declared here instead: the relation `subject` the core compiler
keys on, plus any model methods whose declarations the Skill quotes. `subject` is the core's
spelling (`odoo/<route>`), not Odoo's `action_id`, because that is what
`BusinessSkillCompiler.draft` and `.reissue` take -- a core module keyed on `action_id` would
work for exactly one application.

An entry naming a subject this release's map does not contain fails loudly in `reissue`
rather than producing a confident artifact about the wrong request. That is the intended
behaviour when Odoo renames or removes an action: a person decides what the Skill is now
about, which is not a decision a compiler can make.
"""
from __future__ import annotations


#: skill_id -> (subject, model methods quoted by the Skill)
SKILL_SOURCES: dict[str, tuple[str, tuple[str, ...]]] = {
    "odoo.sales.customer-invoices-query": ("odoo/customer-invoices", ()),
    "odoo.sales.customers-create": ("odoo/customers", ("res.partner.create",)),
    "odoo.sales.customers-draft": ("odoo/customers", ()),
    "odoo.sales.customers-edit": ("odoo/customers", ()),
    "odoo.sales.customers-query": ("odoo/customers", ()),
    "odoo.sales.products-query": ("odoo/products", ()),
    "odoo.sales.quotations-query": ("odoo/sales", ()),
    "odoo.sales.sale-orders-confirm": ("odoo/orders", ("sale.order.action_confirm",)),
    "odoo.sales.sale-orders-invoice": (
        "odoo/customer-invoices",
        ("sale.order._create_invoices", "sale.advance.payment.inv.create_invoices")),
    "odoo.sales.sale-orders-query": ("odoo/orders", ()),
}
