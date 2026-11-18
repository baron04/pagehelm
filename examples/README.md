# Reference integration

[`yudao/`](yudao/deploy/README.md) is the repository's reference integration. It is an
installable `web-operation-agent-yudao` distribution and demonstrates the complete adapter
boundary: trusted identity, typed reads, confirmed writes, postcondition verification,
routes, Skills, optional source compilation, and deployment.

Start with [the Yudao deployment and integration guide](yudao/deploy/README.md). The core
package remains application-neutral and does not import this distribution.

The small Contacts application used by protocol and browser tests lives under
[`testapps/contacts/`](../testapps/contacts/). It is internal test apparatus that serves as
the second, structurally different contract-test target, not a public integration template.
