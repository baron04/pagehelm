# Reference integration

[`odoo/`](odoo/) is the repository's only public reference integration. It is an
installable `web-operation-agent-odoo` distribution and demonstrates the complete adapter
boundary: trusted identity, typed reads, confirmed writes, postcondition verification,
routes, Skills, optional source compilation, deployment, and live qualification.

Start with [the Odoo reading guide](odoo/README.md#reading-guide-for-integrators). The core
package remains application-neutral and does not import this distribution.

The small Contacts application used by protocol and browser tests lives under
[`testapps/contacts/`](../testapps/contacts/). It is deliberately internal test apparatus,
not a second integration template.
