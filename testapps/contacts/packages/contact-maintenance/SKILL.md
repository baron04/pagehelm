---
name: contact-maintenance
description: >-
  Explain address-book structure, find contacts and their email, create a contact,
  and open its exact profile. Names may repeat; all reads use the current workspace.
---

# Maintain an address-book contact

A contact has its own name and email in the current actor's workspace. The application
is a small REST service, not an ORM: use current readable records, never source access
or another workspace to infer an existing person.

For queries preserve the requested conditions and use the complete list. For a page
request open only the exact record address the adapter returns. A temporary form is a
handoff to the person and is not creation.

Creating needs a name and email provided by the person. Missing information requires a
question; a duplicate name requires clarification. Explain requests do not create.
The service prepares exact values, checks duplicates, displays the change and targets,
waits for a new one-use approval, checks again and dispatches once. Same-actor readback
must agree before claiming success. A lost acknowledgement is indeterminate and is
never retried. Declining approval stops the workflow.

After verification a mixed task may read the updated list or open the returned profile.
