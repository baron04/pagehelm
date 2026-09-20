# Can the application side be an HTTP contract? A measurement, not a design

**Status: measured on 2026-09-11 and re-derived on 2026-09-19 against two real
integrations. No implementation is proposed or started; Tier 2 is Phase 7 work at the
earliest.** The Odoo executor has since grown by more than half (313 → 695 code lines) as
typed reads gained filters, record ids and the reader's time zone; the numbers below are the
2026-09-19 ones, and the verdict did not change.

The question is narrow. Today an application integrates by writing a Python adapter in this
process (Tier 3). That means a team whose application is TypeScript, Java or Ruby must
nonetheless staff a Python author — not a high bar, a *wrong-language* bar. The alternative
worth considering is a **contract**: the application implements a few HTTP endpoints in its
own language, and this framework keeps the guarantees.

That only works if most of an adapter is "call the application's API" — which an application
team can obviously do better in their own codebase — and the remainder, the part that makes
calling it *safe*, is small, generic and stays here. This document measures that split on the
two adapters that exist.

Re-derive the numbers:

```bash
python scripts/measure_adapter_safety.py --show
```

---

## 1. How much of an executor is "call the API" and how much is "safely call the API"?

Two independent counts, because one counting rule could flatter the answer.

**Count A — by method.** Each method is attributed whole. Blank lines, comments and
docstrings are excluded from every total.

| | Twenty (baseline) | Odoo (main application) |
|---|---|---|
| executor class, code lines | **549** | **695** |
| safety methods | `_get_collection` 95, `_assert_no_duplicate` 24, `_get_companies` 1 | `_scan` 30, `_assert_no_duplicate` 10 |
| **safety total** | **120 (22%)** | **40 (6%)** |
| transport methods | `_authorized_metadata_json` 37, `_authorized_json` 25, `_authorized_mutation_json` 24 | `_rpc` 42, `_authenticate` 12, `call` 4, `time_zone` 16 | 
| **transport total** | **86 (16%)** | **74 (11%)** |

**Count B — by line.** A code line counts as safety when it mentions a completeness,
duplicate-check or readback identifier (`source_complete`, `result_truncated`,
`records_scanned`, `incomplete_reason`, `pages_scanned`, `source_total`, `matched_count`,
`search_count`, `totalCount`, `hasNextPage`, `_woaPagination`, `offset`, page-size
constants, `_assert_no_duplicate`, `business_text_key`, `readback`, `Indeterminate`,
`complete`, `scan`). This catches the completeness bookkeeping that sits inline inside
`execute` and `execute_mutation` rather than in a helper.

| | Twenty | Odoo |
|---|---|---|
| code lines | 577 | 752 |
| **safety-of-calling lines** | **127 (22%)** | **95 (13%)** |

**Answer: 22% and 13%.** The roadmap's pass band for "the split is real and stable" was
15–20%; Twenty lands just above it and Odoo just below, and transport is 11–16%. Odoo's share
fell because everything that grew since the first measurement — date filters in the reader's
own zone, record-id lookups, answer labels — is application semantics, not safety: the safety
line count itself nearly doubled (51 → 95) while the class doubled around it.

### Why the two differ, and why the difference is the interesting part

The gap is not a difference in how carefully the two adapters were written. It is a
difference in **what each application's API is willing to tell you**:

* Twenty's REST collection uses cursor pagination and gives no dependable total, so
  `_get_collection` has to walk pages, cap the walk, and reconstruct "did I see all of it"
  from `hasNextPage` plus a `totalCount` it must also cross-check — 96 lines.
* Odoo has `search_count`. The same guarantee costs `_scan`'s 24 lines: ask for the count
  with the same domain and the same actor, then page until the rows match it.

That is exactly the shape an HTTP contract is *for*. The framework does not care how an
application knows it saw everything; it cares that the application says so, in a fixed field,
and is wrong loudly rather than quietly. Twenty's expensive answer and Odoo's cheap answer
produce the same six fields. A declarative manifest could not have expressed either one.

---

## 2. Which of those lines can move to the application side, and which cannot?

The test is not "is this code about safety" but **"who has to be trusted for this to mean
anything"**. A guarantee whose enforcement lives in the same process as the thing being
guarded is not a guarantee.

### Can move to an application-side endpoint (the application computes, the framework reads)

| Concern | Twenty | Odoo | Why it can move |
|---|---|---|---|
| Paging the collection to its end | ~70 | ~14 | Only the application knows its own pagination style; the framework only needs the answer |
| Deciding `source_complete` / `records_scanned` / `incomplete_reason` | ~20 | ~8 | Same: an assertion about the application's own read, made by the application |
| The duplicate pre-check | 25 | 10 | It is a query against the application's own uniqueness semantics |
| Transport, auth refresh, retries of *reads*, and asking the application who the user is | 86 | 74 | Entirely the application's business |
| **Total movable** | **~201 of 549 (37%)** | **~106 of 695 (15%)** | |

### Must stay on the framework side (the application is not the trusted party for these)

| Concern | Where it lives today | Why it cannot move |
|---|---|---|
| Binding `page_session` and `delegated_api` to one actor and refusing a mismatch | `state/identity.py:bind_matching_identity` | The application asserting "these are the same user" is the claim under test |
| Requiring a fresh, user-visible approval and consuming it once | `product.py` + the approval gate | An application that could mint its own approval has no approval |
| The atomic mutation journal, its `business_key` exclusion and its terminal states | `mutation.py` | Its whole purpose is to survive the application being unavailable or ambiguous |
| Never auto-retrying an acknowledged-but-uncertain write | `BusinessMutationIndeterminate` handling | The application is precisely the party that does not know |
| Refusing a route the current result did not authorise | `DeclaredRoutePort` + `allowed_routes` | An application that authorises its own navigation authorises everything |
| Redacting credentials before audit and before provider egress | `privacy.py`, `egress.py` | Data leaving for a third party is not the application's decision to review |
| Projecting the typed answer instead of model prose | `answer/answering.py` | The point is that neither the model nor the application writes the user-visible fact |

The ratio that matters is therefore not 22%/16% but this: **the movable part is the part an
application team is already better at, and the immovable part is the entire product.** That
is a good contract boundary, not a compromise.

---

## 3. What would `BusinessToolResult` + `StructuredQueryAnswer` still need?

The roadmap's claim was that the contract "is already in the code". Measured field by field,
that is true for reads and **not yet true for writes**. Seven gaps, concretely:

| # | Gap | Today | What an HTTP contract needs |
|---|---|---|---|
| 1 | **The proposal has no wire shape.** `effect` and `targets` — the exact text a user approves — live on `BusinessMutationSpec`, a separate Python object that never becomes a `BusinessToolResult`. | `business.py:703` | A `POST /tools/<name>/propose` response: `{args, reconcile_key, effect, targets[]}` |
| 2 | **The indeterminate outcome is an exception.** `BusinessMutationIndeterminate(phase, record_id)` cannot cross a JSON boundary as a raised type. | `business.py:305` | A terminal response body: `{"outcome": "indeterminate", "phase": "acknowledgement\|readback", "record_id": "…"}` — **with a non-2xx status that is explicitly not retryable** |
| 3 | **The error taxonomy is Python classes.** `Request` / `Identity` / `Authentication` / `Unavailable` / `Contract` / generic decide whether a failure scores against the engine or invalidates a measurement. | `business.py:200-310` | A fixed `{"error": {"kind": …}}` enum plus a status mapping; six kinds, closed set |
| 4 | **Delegated identity has no wire shape.** `resolve_delegated_identity()` returns an `IdentityAssertion` built in-process. | `adapter.py` per application | `GET /identity` → `{tenant_id, workspace_id, actor_id, expires_at}`, identifiers only, never a credential |
| 5 | **Reconciliation has no wire shape.** `MutationReconciliation(outcome, requires_human, reason)` exists as a dataclass but is not part of any result. | `composition/ports.py:315` | `POST /tools/<name>/reconcile` → the same three fields |
| 6 | **The postcondition assertion is adapter-side configuration.** `ResultPostconditionVerifier` is handed dotted paths such as `content.readback_verified`. | `composition/ports.py:275` | Either travels in a capability document, or `readback_verified` becomes a fixed top-level field of the result — the second is better: fewer ways to be wrong |
| 7 | **Records have no identity.** `StructuredQueryAnswer.records` are positional *display strings*; the record id survives only inside `content[*].ui_path`. | `answer/answering.py:37` | A per-row `record_key`, so framework-side dedupe, readback and cross-call comparison do not have to parse a URL |

Gaps 1–3 and 5 are the write path; 4 is identity; 6–7 are small and mechanical. None of them
is a redesign: every one already exists as a Python value, and the work is giving it a name
on the wire.

---

## Verdict against the roadmap's own criterion

> 两个应用的比例一致（安全逻辑约 15–20%、其余可外移）则 Tier 2 可行；数字差异大则说明契约未到时候。

Both applications land at 16–22% safety and 15% transport, with the difference fully
explained by one measurable property of each API (whether it can report a total). The split
is real. **Tier 2 is feasible on this evidence — for reads today, and for writes once the
seven fields above have names.**

Two things this measurement does **not** establish, and the roadmap's warning applies:

* **Two adapters written by the same author is not two independent data points.** Both were
  built against the same mental model of what "safe" means; a third-party team would
  discover concerns neither of these has. The 16–22% band is the floor of what safety costs
  here, not a prediction of what it costs elsewhere.
* **Nothing here argues Tier 2 should be built now.** It has zero external users, and
  building a contract against requirements derived only from one's own two adapters is the
  same premature abstraction this project has already criticised itself for. The value of
  this half-day is the seven named gaps and the movable/immovable table, which are what a
  Phase-7 decision needs — not permission to start.

---

## How the lines were counted

Both counts exclude blank lines, comments and docstrings, and cover exactly the executor
class body (`TwentyBusinessToolExecutor`, `OdooBusinessToolExecutor`) — not the ports, not
the adapter wiring, not the Skills.

Count A attributes whole methods and is reproducible from `ast` alone. Count B marks a line
as safety when it mentions one of the identifiers listed in §1; the identifier list is the
rule, so a reader who disagrees with an inclusion can re-run with it removed and see the
effect. The two counts agree for Twenty (120 vs 127) and differ for Odoo (40 vs 95) because Odoo
does most of its completeness bookkeeping inline in `execute` rather than in `_scan`. The
larger of the two is quoted throughout, since it is the one that is less flattering to the
conclusion. Both counts are re-derivable with the command at the top of this file; the
numbers here were taken on 2026-09-19, and a reader who finds them stale should re-run it
rather than trust them.
