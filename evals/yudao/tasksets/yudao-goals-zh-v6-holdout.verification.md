# Yudao v6 evaluation set

This set has 63 new Chinese prompts for the synthetic Yudao v2026.09 deployment: 8 navigation, 13 count and configuration, 12 multi-condition count or total, 6 cross-line, 16 source-rule and 8 impossible requests across ERP, CRM, BPM, member, pay, mall, infra and system. Its actor is `opsreader4` (user 258, tenant 1, East China sales). It was authored in a session of its own, with no assistant run and no model call before sealing, so this is an **independently authored holdout**, not the qualification set the implementer wrote. No v6 prompt string, and no v6 navigation destination, is the same as a v5 one.

## Scope: what this actor can reach, and therefore what this set covers

The brief asked for every module the two releases added: 财务 (FMS), 商城, 公众号 (MP), MES, WMS 库存, OA, HRM, PMS, IM, 会员. Measured against the sealed deployment, `opsreader4` cannot reach most of them. Its two roles (`sales_rep`, `v4_ops_reader`) grant 45 menus covering 72 page menus in eight modules: system, infra, pay, BPM, member, mall, CRM and ERP. The FMS, MP, MES, WMS, OA, HRM, PMS and IM top-level menus exist in the database but are not granted to this role, so the adapter compiles no entity and no page for them and the assistant can never open them. Those modules are also empty of records: WMS, MES, OA (announcements, calendar, reports), PMS and IM tables hold zero rows, and FMS, MP, OA mail and HRM hold only the parent records a list needs to be readable at all (one accounting set, one official account, one mail account, ten employees). The owner ruled that this set be authored now, within what the actor can actually reach, rather than widening the fixture from a second session while the deployment session was still writing the same database.

So the set covers the ground inside that reach that no earlier set had asked about: ERP purchase, sale-out, sale-return, stock, stock-record, product, product-category, product-unit, warehouse, supplier, account and finance; CRM clue, contact, business, contract, receivable, receivable-plan, product-category, business-status, customer-limit, performance-config and product statistics; the BPM manager views, the five approval-centre pages and the OA leave query; mall coupon templates, member tags, pay refunds and infra parameter configuration. Its 63 tasks name 26 distinct entities, none of which v5 exercised except where noted below.

## How the truth was established

Every count, filtered count, total and line value was read through the **same account's own application API** (`YudaoGoalData` under `opsreader4`), never a model and never a second account; the build script asserts each computed value against the number written into the task, so a drifted database fails the build instead of silently sealing a wrong truth. Two values come only from a record's detail: the July sale-order total (¥337,975.09) and the per-line figures of order `XSDD20260929000129` (3 lines, ¥193.31 tax, highest unit price 工作服) — none of them can be produced from a list header. The cross-line counts (289 sale-order lines, 104 sale-out lines, 39 purchase-in lines, 48 purchase-order lines) are read through the line entities the adapter exposes for a document's detail.

All eight navigation destinations were **opened in a real Chromium as this account**, and the route, the breadcrumb's last item and the active tab were read off the page: `/erp/purchase/order` 采购订单, `/erp/product/product` 产品信息, `/erp/stock/stock` 产品库存, `/crm/contract` 合同管理, `/bpm/manager/process-instance/manager` 流程实例, `/member/tag` 会员标签, `/pay/refund` 退款订单, `/mall/promotion/coupon/template` 优惠劵列表. Four further routes (`/erp/sale/return`, `/infra/config`, `/crm/clue`, `/bpm/task/done`) were opened the same way and matched.

The sixteen source-rule questions were not written from a reading of the source by hand. Each cites a rule the deployment's own compiler (`YudaoSource`, the same one a run offers the agent) compiles from the bound v2026.09 checkout, and the task's `source_regex` is generated from that rule's own citation — the file, its first and last line, and the release. The whole-file SHA-256 of each of the 15 cited files is in the seal's `source_binding`, and the runner verifies the answer's citations byte by byte against those files before the judge accepts the part.

Finally, a model-free check built the answer a correct attempt would give for each of the 63 tasks and confirmed the frozen rubric-7 judge accepts every one (63/63), and that all 16 source tasks reject an answer whose citation hash is wrong. This measures reachability of the rubric, not model quality.

## What is covered

| Type | Count | What it asks |
| --- | --- | --- |
| navigation | 8 | Open a named page this actor can reach and no earlier set opened |
| configuration | 13 | Count a module's records; one conditional count inside it |
| condition | 12 | Count or total under a combination: a date range with a status, a business type inside a ledger, a status inside an approval or a job list |
| lines | 6 | Cross-line statistics: line counts, a tax total, the dearest line, line counts across all documents |
| rule | 16 | When a control shows, when a record may not be changed, what an operation refuses — each with a verified citation |
| impossible | 8 | Seven read-only refusals and one request missing a condition the page itself selects |

| Module | Tasks | The new ground inside it |
| --- | --- | --- |
| erp | 29 | purchase orders and receipts, sale-out, sale-return, stock, stock ledger, products, units, accounts, order lines |
| crm | 7 | contracts, clues, customers, the product-sales page, contract and customer rules |
| bpm | 7 | process instances, tasks, the whole approval centre, leave, instance cancellation |
| member | 4 | tags and their uniqueness rule |
| mall | 4 | coupon templates and shipped or unpaid orders |
| infra | 6 | parameter configuration, jobs and two of their rules |
| pay | 3 | refunds and the pay-app deletion rule |
| system | 3 | dictionary types, departments |

### The sixteen source rules

| Task | Module | Rule | Citation |
| --- | --- | --- | --- |
| y6-040 | erp | Sale-order 审批 button shows for 未审核 and needs `erp:sale-order:update-status` | `ui/src/views/erp/sale/order/index.vue:237-245` |
| y6-041 | erp | Sale-order 反审批 button shows for the other status | `ui/src/views/erp/sale/order/index.vue:246-254` |
| y6-042 | erp | An approved sale order cannot be modified | `.../sale/ErpSaleOrderServiceImpl.java:101-102` |
| y6-043 | erp | An approved purchase order cannot be deleted | `.../purchase/ErpPurchaseOrderServiceImpl.java:239-240` |
| y6-044 | infra | A parameter of type 系统内置 cannot be deleted | `.../config/ConfigServiceImpl.java:61-62` |
| y6-045 | infra | Only a running job may be modified | `.../job/JobServiceImpl.java:78-79` |
| y6-046 | system | A dictionary type with data cannot be deleted | `.../dict/DictTypeServiceImpl.java:83-84` |
| y6-047 | system | A department with children cannot be deleted | `.../dept/DeptServiceImpl.java:84-85` |
| y6-048 | crm | A customer with contacts cannot be deleted | `.../customer/CrmCustomerServiceImpl.java:564-565` |
| y6-049 | erp | A product used by a sale order cannot be deleted | `.../product/ErpProductServiceImpl.java:101-102` |
| y6-050 | crm | Contract 提交审核 shows while 未提交 and needs `crm:contract:update` | `ui/src/views/crm/contract/index.vue:214-222` |
| y6-051 | crm | A contract may only be edited while a draft | `.../contract/CrmContractServiceImpl.java:145-147` |
| y6-052 | pay | A pay app with orders cannot be deleted | `.../app/PayAppServiceImpl.java:97-98` |
| y6-053 | crm | A clue may only be converted once | `.../clue/CrmClueServiceImpl.java:187-188` |
| y6-054 | member | A member tag's name must be unique | `.../tag/MemberTagServiceImpl.java:87-88` |
| y6-055 | bpm | Process-instance 取消 shows while 审批中 and needs `bpm:process-instance:query` | `ui/src/views/bpm/processInstance/manager/index.vue:144-152` |

Each of those line ranges was chosen so that the answer it supports is the only rule of that operation that the question can mean. Where an operation has several refusal rules (an ERP product referenced by any of six document types; a customer with contacts, opportunities or contracts), the question names one of them and the citation is pinned to that one, so a correct answer cannot be confounded with a neighbouring rule.

## Where the confidence is lower

* **A count check's word requirement costs correct answers (owner ruling, 2026-09-30).** `y6-038` answers `销售出库：104 条` for the 104 sale-out lines the truth names: the value and the entity are right, and the check is failed only because it requires the counted thing's own word (`明细`) beside the number, which the answer writes as `条`. The four v5 misses were the same class (`用户管理` where the check wanted `后台用户`, `销售订单` where it wanted `ERP 销售订单`). The owner ruled that the product is not changed for this — an answer that follows the request's own words is not defective — and that **a later task set must widen the check**: accept the synonyms for a count (`条`/`行`/`笔`/`个`) and any alias of the entity the truth names, or require the entity's alias rather than one fixed word. Adjudicating this set already counts `y6-038` as correct; its sealed raw score stays 58/63.
* The eight impossible tasks depend on the frozen judge finding an explicit limitation phrase in the answer. A correct refusal that says only what is missing, without a word the judge reads as a limitation (无法, 不能, 没有, 做不到 …), is scored wrong. This is the judge's rule, not the task's; the task set does not weaken it.
* `y6-056` (the product-sales page) is the only task of the brief's "the page itself selects the condition" class that this actor can reach. It requires the answer to name 时间范围; the API answers `400 请求参数不正确:请选择时间范围` without it, which was observed directly.
* The conditional counts use a date range on `orderTime`, compared in memory after the list is read, because the endpoint does not filter dates. The July window (2026-07-01 to 2026-07-31) was chosen to divide the 145 sale orders cleanly: 49 in July, 38 of them approved, 11 not.
* Two label-based checks were widened after the first draft: a prompt that says 还没审核 must not demand the word 未审核 back from the answer, and one that says 已经审核通过 must not demand 已审核. The checks now accept the request's own wording, with the number beside it. This is the known failure mode of the v5 count checks, which produced four strict-label false negatives there.
* No task asks for a value that a wrong-but-plausible reading would also produce, and none is scored by a bare literal: every count check needs the counted thing's own word next to the number.

## Binding and the first run

The seal records the task-file digest, the frozen judge's digest (rubric 7) and the database snapshot `seed-2609-accounts.sql.gz` (SHA-256 `5998abb1…babfeeb`), plus the actor, tenant, origin and reference date. The preflight restores that snapshot into a scratch database, fingerprints it against the live one, checks every feasible task's entity and page against the account's actual index, and opens the admin page in a real browser before any model call. Any difference fails before a call is paid for.

Two things in the brief's environment section had changed by the time of authoring, and the seal follows the deployment rather than the brief: the fixture named in the brief (`seed-2609-parents.sql.gz`) was re-cut as `seed-2609-accounts.sql.gz` when the deployment session finished the synthetic chart of accounts, and this set is bound to the newer one. The brief's preflight command also needs `examples/odoo/src` on `PYTHONPATH` and a reachable model env file; neither is available from the worktree, where the `.env` holding the provider settings is deliberately absent from Git. The command that was actually run, and that passed, is:

```bash
PYTHONPATH=.:src:examples/yudao/src:examples/odoo/src .venv/bin/python -m evals.yudao.run_goals \
  --taskset evals/yudao/tasksets/yudao-goals-zh-v6-holdout.json --output .artifacts/yudao-v6 \
  --account .private/yudao-holdout-v4/actor.json --model-env ../.env \
  --origin http://127.0.0.1:48088 --ui-source ../.q0-cache/yudao-2609/ui \
  --source-root ../.q0-cache/yudao-2609 --source-release v2026.09 \
  --snapshot ../.q0-cache/yudao-2609/snapshots/seed-2609-accounts.sql.gz --preflight-only
```

No v6 score exists at the time of writing: the set was sealed before any assistant run, and the first run of these prompts has not happened. Whoever runs it first should report `rule` results separately from the rest, since this is the first yudao set that offers the compiled source rules to the agent and no yudao source question has been scored before.
## First run

The set was run once, on 2026-09-30, with the sealed actor (`opsreader4`), origin, tenant, reference date and snapshot, split across four lane processes, one attempt per task. The harness preflight restored the sealed snapshot into a scratch database, matched its fingerprint against the live one, checked every feasible task's entity and page against the account's index and opened the admin page in Chromium; every attempt fingerprinted the database before and after itself. The run made 215 provider calls with 708,716 input and 64,860 output tokens, cost **¥0.424916** on `qwen3.8-flash` through token-plan, recorded **no database write** and **no harness error**, and the live database still matched the sealed snapshot. Median attempt time was 21 s (max 81 s); the four lanes finished in about fifteen minutes.

The frozen judge's raw result is **58/63 (92.1%)**: 51 correct answers, 7 truthful refusals (all on the impossible tasks, of which one other was scored wrong) and 5 wrong answers.

| Type | Result | | Module | Result |
| --- | --- | --- | --- | --- |
| configuration | 13/13 | | erp | 26/29 |
| rule | 15/16 | | crm | 7/7 |
| condition | 11/12 | | bpm | 7/7 |
| navigation | 7/8 | | infra | 5/6 |
| impossible | 7/8 | | member | 4/4 |
| lines | 5/6 | | mall | 4/4 |
| | | | pay | 3/3 |
| | | | system | 2/3 |

**Source rules, reported on their own** (this is the first yudao run that offers the compiled rules to the agent): **15/16 (93.8%)**. All sixteen attempts carried a source check, and fifteen answers cited the rule the question meant with a citation the runner verified byte by byte against the sealed files — the file, the line range and the release. The one miss is `y6-045`, which answered the modify button's visibility rule (`ui/src/views/infra/job/index.vue:109-116`) instead of the service's state refusal (`JobServiceImpl.java:78-79`): a real citation of a neighbouring rule of the same operation.

The five misses:

| Task | Type | Actor-visible answer | Why it is scored wrong |
| --- | --- | --- | --- |
| y6-003 | navigation | 「这个应用没有提供页面筛选与分组，这一项无法完成。」 | The request also asked for a grouping the port does not declare, and the page it could have opened was never opened. |
| y6-029 | condition | 「订单列表 · 是否已支付为 no：0 条」 | Filtered on the paid flag and answered 0 where 4 orders are unpaid. |
| y6-038 | lines | 「销售出库：104 条」 | The value is the true one (104 sale-out lines); the check needs the counted thing's own word (明细) beside the number and the answer says 条. |
| y6-045 | rule | the modify button's visibility rule and its citation | The question asks under which state a job may be modified; the answer gives the control rule, not the refusal. |
| y6-056 | impossible | 「无法完成这一项。」 | Refuses without naming the condition the page itself supplies (时间范围), which the check requires. |

Counting `y6-038` as correct — its value and entity are the ones the truth names, only its wording differs — the transparent, manually adjudicated result is **59/63 (93.7%)**. The sealed raw score stays 58/63, the task file and judge are unchanged, and no attempt was re-run. Both numbers are an independent holdout estimate: the set was authored in a session of its own, with no assistant run before this one.

## Fixes after the first run

Two of the five misses were behaviours of the core rather than of this set, and both were fixed after the run. The sealed task file, judge and score are unchanged, and no sealed attempt was re-run: each fix was checked by running its own task once, separately, into `.artifacts/yudao-v6-fixcheck`, which is not part of the score above.

- `y6-003`. A page the port can open was refused outright because the request also asked for the list to be grouped and the port declares no page filters and groups. The core now asks the port for the page without them, opens it, and says in the reply what this application does not offer. Run again, the list opens at `/erp/stock/stock` with the tab and breadcrumb the check needs, and the attempt scores correct.
- `y6-056`. A read the application refused reached the person as "无法完成这一项。", with the application's own reason left in the audit. That reason now reaches the reply when it is written in the person's language, and the adapter adds the conditions the read still left out, named as its own documentation names them: the endpoint reports one missing parameter at a time ("部门 id 不能为空") while the page waits for two ("部门 id", "时间范围"). Run again, the attempt scores truthful_refusal.

The two attempts cost ¥0.007416 together and made no database write. A zero-cost replay of all 63 recorded declarations through the fixed code changed no other task: its only differences from the run are the two things a replay cannot corroborate — a page arrived at in a real browser, and the model's own wording of a count.

Two further misses were fixed the same way, and checked the same way — a separate single-task run each (`y6-045` scored correct, ¥0.005851), plus a zero-cost replay of all 63 recorded declarations, which changed `y6-045` and nothing else.

- `y6-045`. The rules of one operation are answered as a set — when the control shows, what the operation refuses — but only when the control's own rule knows the operation it calls. The pattern that reads an API function's URL required `async` and `await`, which this release's modules never write, so it resolved no URL at all: a job's 修改 button and the state it refuses under were answered as two islands, and the question heard only the button's permission. The URL now resolves for 943 of the deployment's 4239 buttons; the answer carries the refusal ("只有开启状态的任务，才可以修改") and its citation.
- `y6-029`. Its value was wrong, not its wording: a yes/no field's `false` was treated as an empty field, so every "equal to" against it matched nothing and the count was 0 where 4 orders are unpaid. A value now compares as a boolean only when it is one of the words for yes or no (`bool("no")` is true, which had turned "还没付款" into the orders that are paid). The wording was a second matter: the answer said which boolean field it had filtered on, where the check asks for the order state's own word. The deployment's reviewed conventions now say what its organisation means by an unpaid mall order — 未付款/没付款/还没付款 mean 订单状态 = 待支付 — so the answer is "订单列表 · 按本组织对“未付款”的口径 · 订单状态为待支付：4 条" and the attempt scores correct (¥0.007819). That the mall tracks payment as the order's state is the deployment's own knowledge, declared and reviewed as the rest of the table is, not something the core could infer.
