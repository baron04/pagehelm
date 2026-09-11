# Security policy

Web Operation Agent can observe pages and request browser or application actions. A defect in its identity, transport, redaction, approval, or retry logic may therefore have a larger impact than an ordinary UI bug. The project is still alpha and must be evaluated only with synthetic data in an isolated, resettable environment.

## Supported versions

There is no stable release line yet. Security fixes are applied only to the latest revision of the default branch. Older commits, local experiment snapshots, and development fixtures are not maintained as supported versions.

## Report a vulnerability privately

Do not publish exploit details, credentials, sensitive traces, or a proof of concept in a public issue.

1. If the repository host offers private vulnerability reporting, use the repository's **Security** page and choose **Report a vulnerability**.
2. If that feature is unavailable, use a private contact channel published by the repository owner or hosting profile.
3. If no private channel is listed, open a minimal issue asking the maintainer to establish private contact. Include no technical details beyond the affected project and a request for a private channel.

This repository does not currently publish a dedicated security email address.

A useful report includes:

- the affected commit or version;
- the deployment and browser context;
- the trust boundary involved;
- minimal reproduction steps using synthetic data;
- expected and observed behavior;
- likely impact;
- whether any write was dispatched or its outcome is uncertain; and
- suggested remediation, if known.

Do not include live tokens, cookies, customer records, model credentials, or full browser profiles. Redact trace data before sharing it.

No response or remediation SLA is promised while the project is pre-release. The maintainer should confirm a private channel before requesting additional sensitive evidence.

## High-priority security areas

Reports are especially valuable for flaws that could cause:

- an action to execute without a current, matching page observation;
- a mutation to execute without explicit user approval, more than once, or after approval expiry;
- automatic retry of an uncertain write;
- cross-session, cross-origin, cross-user, or cross-tenant access;
- model credentials, OAuth material, cookies, form values, or user data to cross the browser boundary or enter logs;
- untrusted page, Skill, source, API, or model content to bypass the fixed tool surface;
- arbitrary JavaScript or unrestricted navigation to run in the browser;
- evaluator or hidden authorization data to reach the Agent;
- audit records to omit, reorder, or misrepresent a consequential action; or
- package resources to be replaced or loaded from an unintended location.

## The approval surface and the host page

The assistant panel renders inside the host application's page, isolated for style by a
shadow root but sharing that page's JavaScript realm. Two consequences follow, and neither
is something a future change to this panel can remove:

**A script in the host application page can complete an approval.** Anything running in
that realm -- the application's own code, a cross-site scripting defect in it, a third-party
tag it loads, a browser extension content script -- can read the pending request and produce
the decision that answers it. The approval therefore establishes that *the authenticated
page session decided*, not that *a person decided*. The first-party embedded version of
this product depends on the application page being trustworthy; a deployment that cannot
assume that does not have a usable confirmation step, and should not enable mutations.

**Content Security Policy and Trusted Types belong to the host page.** They are response
headers on a document, so the panel cannot turn them on. What it can do, and what a
regression test pins, is never require them to be relaxed: it constructs and runs a
confirmation under `script-src 'self'`, `style-src 'self'` and
`require-trusted-types-for 'script'` with `trusted-types 'none'`, because it writes no
HTML, injects no inline style or script, and mints no Trusted Types policy. If your
application can set a strict policy, set one; the panel will not be why you cannot.

What the panel does still enforce is that a confirmation cannot be answered by a press the
user could not have made or seen: the decision requires a trusted user gesture, and the card
must have been continuously and substantially visible for a short dwell before it. That
closes visual deception -- something drawn over the card, or the window shrunk until the
card is not really on screen -- and closes nothing else. It is not a defence against a
script that shares the panel's realm, and must not be described as one.

A confirmation that cannot be forged by the page it sits on needs an out-of-band surface: a
browser extension side panel, or browser chrome. That is a different product with a
different threat model, and this repository does not ship it.

## Operational limitations

The current code does not yet provide complete production identity binding, tenant isolation, durable session storage, horizontal scaling, rate limiting, or comprehensive operational monitoring. The Twenty demo binds its configured browser session to the delegated OAuth actor and closes on cookie replacement, but other embedded applications still need a trusted server-side session assertion and no distributed revocation or principal renewal service exists. Browser actions do not provide database transaction or rollback guarantees.

In-process Runtime sessions expire after one hour by default and enforce hard limits for retained events, accepted messages, queued messages, observations, actions, approvals, and Runtime instances. Oversized event payloads and identifier or URL fields fail before entering retained state; queue pressure returns HTTP 429, while a lifetime-capacity breach terminates and revokes the session. These controls limit memory exposure but do not provide restart recovery or distributed quotas.

The legacy external page-control client requires its secret to be a regular, non-symlink file of at most 4 KiB with no group or world access. It sends the secret only in the configured header, rejects redirects and oversized responses, leases a private per-user lock across local processes, resets the global gateway before a run, and clears it before releasing the lease. This protects cooperating local clients from cross-run permit and trace reuse; it does not turn a legacy global gateway into a distributed tenant-scoped service or protect against another client that bypasses this library. Do not enable concurrent page writes through that gateway.

Do not connect this version to production accounts, customer data, payments, email, messaging, or other consequential systems. Keep test applications isolated, use least-privilege delegated test users, leave mutations disabled unless the test requires them, and restore fixture state after each write scenario.

## Handling suspected write incidents

If a write times out, loses its acknowledgement, or produces a partial result, stop the session and treat the outcome as indeterminate. Do not retry automatically. Preserve the redacted audit record, inspect the application through the same actor, and restore state only after the actual outcome is known.
