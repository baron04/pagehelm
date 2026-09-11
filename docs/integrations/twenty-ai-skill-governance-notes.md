# What Twenty AI does about tool governance, read against ours

**Source: the built server in `twentycrm/twenty:v2.39.0`, read on 2026-09-11.** Paths below
are inside `/app/packages/twenty-server/dist/`. This is a half-day design read of a shipping
competitor's in-product agent, not a benchmark and not a comparison of outcomes — we ran
nothing of theirs and measured nothing. Read it as "what a well-funded team decided", which
is useful precisely because the two designs disagree in places.

## Four things they do

### 1. Tools are generated per object × operation, and named for the user

`engine/core-modules/record-crud/services/` ships one service per operation —
`create-record`, `create-many-records`, `update-record`, `update-many-records`,
`upsert-record`, `upsert-many-records`, `delete-record`, `delete-many-records`,
`find-records`, `group-by-records` — and `tool-provider/utils/get-crud-tool-label.util.js`
turns `(operation, objectLabel)` into a **translated, user-facing** label: "Create company",
"Delete person". The tool surface is therefore the object metadata crossed with a fixed
operation vocabulary, and every tool has a name a person can read in an approval dialog.

### 2. The catalog is deferred: `learn_tools` then `execute_tool`

`tool-provider/utils/build-tool-catalog-section.util.js` renders a system-prompt section
that lists **every** tool by category, marks a small pre-loaded subset with `✓`, and tells
the model:

> Pre-loaded tools (marked with ✓): use directly. Other tools: first call
> `learn_tools({toolNames: […]})` to learn the schema, then call
> `execute_tool({toolName: …, arguments: {…}})`.

So the model always sees the *names* of everything and the *schemas* of almost nothing.
`learn-tools.tool.js` returns schemas plus `notFound` and fuzzy `suggestions`
(`find-similar-tool-names.util.js`) when a name misses.

### 3. Agents are rows, not code

`metadata-modules/ai/ai-agent/entities/agent.entity.js` is a `SyncableEntity` with
`name`, `label`, `icon`, `description`, `prompt`, `modelId`, `responseFormat`,
`modelConfiguration`, `evaluationInputs` and `isCustom`, carried through the same workspace
migration machinery as any other metadata (`workspace-migration/.../builders/agent`). An
agent is configuration with a migration history, editable in the product.

### 4. There is an in-product grader

`metadata-modules/ai/ai-agent-monitor/` holds `agent-turn-evaluation.entity.js`
(`turnId`, `score`, `comment`), `agent-turn-grader.service.js` and
`evaluate-agent-turn.job.js`, fed by the agent row's own `evaluationInputs`. Turn quality is
scored inside the product, asynchronously, per turn.

Adjacent and worth noting: `tool/services/tool-output-spill.service.js` plus
`max-inline-tool-output-bytes.constant.js` and the `search_output` /
`extract_json_paths` tools. When a tool result is too large they **spill it and hand the
model navigation tools over the spilled blob** rather than truncating it.

---

## Where that lands against our Skill governance

| Question | Twenty AI | Us | What we take |
|---|---|---|---|
| How is the tool surface sized? | Names always visible, schemas on demand via `learn_tools` | `load_skill` reveals a Skill's tools; unloaded Skills are invisible | **Their split is finer than ours.** We hide the name *and* the schema; they hide only the schema. A model that cannot see a capability exists cannot ask for it — that is a real cost of our design, and it is the thing to measure in G3, not assume |
| Who names a tool? | Generated `{verb} {object}`, translated | Hand-written per Skill artifact | Their generation is the right answer for a CRUD surface. Ours is hand-written because a tool is not always one object and one verb — but our naming has no rule at all, and it should |
| Where does agent config live? | A migrated database row | A JSON artifact with a content digest, pinned into the catalog digest | **We keep ours.** A row is editable in the product, which is exactly what an audit trail must not allow: our comparison and our journal bind a `catalog_sha256`, and a row that changed between two turns would be indistinguishable from one that did not. Their design optimises for product self-service; ours optimises for "what exactly was this agent told" |
| Is there a grader? | Yes, in-product, per turn, LLM-scored | No — evaluation is out-of-process in `evals/`, with hidden expected state kept out of the agent's context | **We keep ours.** An in-product grader that can see the turn is a grader that can be optimised against. But their asynchronous per-turn score is a product feature we have nothing equivalent to, and a deployment will ask for it |
| Large tool output | Spill + navigation tools | 64 KiB hard cap on `BusinessToolResult`, and a typed projection that is deliberately small | **We keep ours, with a note.** Our cap exists so a result cannot flood the provider request; theirs exists so a result is never lost. The two are not in conflict, and spill-plus-navigate is the better answer *if* a deployment ever needs a large read — which our completeness fields currently answer by saying "truncated" instead |

## The one thing worth copying, and its cost

`learn_tools` returning **`notFound` plus fuzzy suggestions** is a small, concrete idea we
lack. When our model names a tool that is not loaded, it gets a refusal; when it names one
that does not exist, it also gets a refusal, and those are different mistakes. Telling it
"no such tool; did you mean …" costs nothing and is auditable.

The cost to watch: suggestions are a channel from the tool catalog into the model's context
that is *not* a Skill. Ours must be bounded by the same catalog digest that everything else
is bound by, or it becomes an unaudited way to learn what exists.

## What this does not tell us

* **Nothing about their quality.** We ran none of it. Every statement above is read from
  shipped code, and code that exists is not evidence that it works.
* **Nothing about scale.** We do not know how many tools a real Twenty workspace generates,
  so "names always visible" may be cheap for them and expensive for an ERP with hundreds of
  objects — which is exactly the case G3 has to handle.
* **Nothing about their write safety.** We did not look for an approval, a journal or an
  idempotency story, so this document must not be cited as evidence that they lack one.
