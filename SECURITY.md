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

## Operational limitations

The current code does not yet provide complete production identity binding, tenant isolation, durable session storage, horizontal scaling, rate limiting, or comprehensive operational monitoring. Browser actions do not provide database transaction or rollback guarantees.

Do not connect this version to production accounts, customer data, payments, email, messaging, or other consequential systems. Keep test applications isolated, use least-privilege delegated test users, leave mutations disabled unless the test requires them, and restore fixture state after each write scenario.

## Handling suspected write incidents

If a write times out, loses its acknowledgement, or produces a partial result, stop the session and treat the outcome as indeterminate. Do not retry automatically. Preserve the redacted audit record, inspect the application through the same actor, and restore state only after the actual outcome is known.
