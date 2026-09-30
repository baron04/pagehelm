# Yudao v5 evaluation set

This set has 48 new Chinese prompts for the synthetic Yudao v2026.09 deployment: 16 navigation, 16 count and configuration, 8 record, 4 composite, and 4 impossible requests across system, infra, pay, BPM, member, mall, CRM, and ERP. Its actor is `opsreader4`, in tenant 1. The same agent that implemented the product authored v5, so this is a frozen **developer-authored qualification set**, not an independently authored holdout. No v5 model or assistant run occurred before sealing. None of its prompt strings is identical to a v4 prompt.

The sealed database is an isolated copy of the synthetic v4 seed, incrementally migrated to v2026.09, then dumped with `mysqldump --single-transaction`. The private archive `yudao-v5-2609-snapshot.sql.gz` is deliberately outside Git; its SHA-256 is in the seal. The live evaluation database is `woa_v5_2609` in the isolated clone MySQL container. The task file, judge, actor, browser origin and snapshot are bound in the seal. A migrated or different database fails the runner's preflight.

Before the first model call, authoring checks read the same account's application API and confirmed every navigation route, count, filtered count, and record value. A separate Chromium check opened all 16 destination routes and found the expected path, breadcrumb and active tab. The rubric-7 judge accepted an ideal answer for every task (48/48); this checks reachability of the rubric, not model quality. The runner's `--preflight-only` passed against the sealed snapshot and verified that the signed-in admin page loaded its permission data from its own origin.

The set preserves four read-only or unavailable-operation requests as impossible tasks. A successful answer must explain the limitation and leave the database unchanged. The runner records the actor-visible answer, final browser page, visited addresses, write fingerprint, provider call count, tokens and actual cost for each attempt.

## First-run harness fault

The first run omitted `--ui-source`. Its actual actor index contained 45 pages, while the authoring check used 64 pages with the UI source. Seven feasible task entities and their pages were absent from the run index. The first run's raw 28/48 is retained in ignored diagnostic artifacts and is **not a valid score**. It made 133 model calls, used 387,339 input and 39,380 output tokens, cost ¥0.222484, and made no database changes. The task set and judge remain sealed and unchanged. A new preflight now compares every feasible target with the actual run index; it rejects this missing-source configuration before model calls. A corrected run with `--ui-source` is a repeat exposure of these prompts and must be labelled as such.

## Corrected run (repeat exposure)

The corrected run used the sealed actor, origin and database snapshot with the v2026.09 UI source. The preflight saw 68 entities and 64 pages, including all feasible targets. It completed exactly one attempt for each of the 48 tasks. The frozen judge's raw result is **43/48 (89.6%)**: 39 correct answers, four truthful refusals on the impossible tasks, and five wrong answers. Navigation was 16/16. The run made 144 provider calls with 456,684 input and 28,716 output tokens, cost **¥0.249527** on `qwen3.8-flash` through token-plan. No harness errors or browser/API writes were recorded; the live database still matched the sealed snapshot after the run. Raw attempt files are in the ignored `.artifacts/yudao-v5-2609-corrected` directory.

Review of the five misses found four strict-label false negatives, without changing the sealed task file or judge:

| Task | Actor-visible answer | Why the frozen regex rejected it |
| --- | --- | --- |
| y5-017 | 用户管理：24 条 | Required the exact words “后台用户” beside 24. |
| y5-031 | 销售订单：145 条 | Required the “ERP” prefix beside 销售订单 and 145. |
| y5-041 | 用户管理：24 条；会员管理：6 条 | Required “后台用户” instead of 用户管理 beside 24. |
| y5-043 | 商品列表：8 条；应用信息：1 条 | Required “商城商品” and “支付应用” instead of their corresponding page labels. |

Each of those answers gives the correct value for the requested entity, in the requested order. The transparent, manually adjudicated result is therefore **47/48 (97.9%)**, while the sealed raw score remains 43/48. This is a developer-authored qualification set, and the corrected run reused prompts exposed by the faulty first run. Neither number is an independent holdout estimate.

The genuine miss was `y5-033`: the assistant found the actor-visible department “华东销售部”, but the Yudao adapter exposed only its name as a field. Its declaration of the department's “编号” could not resolve and it refused. After scoring, the adapter was changed to expose the ID returned by the actor-scoped simple list as a read-only “编号” field for users and departments. A synthetic regression test resolves that department's number through the same goal port. This post-run fix is **not** reflected in the v5 score and was not used to rerun the sealed tasks.
