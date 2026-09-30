# yudao-goals-zh-v3-holdout 核验说明（封存于首跑之前）

零模型调用、零 Agent 运行；所有真值在写库前封存。判分器 judge.py sha256 `96148a00e70cbe34eb80759b3c976fb3d4b74c4326d25800c95fa69843f5d593`，rubric 7。

## 造数方法

- `examples/yudao/deploy/seed_v3.py`：幂等增量脚本，只经芋道管理接口（/admin-api/crm/*、/admin-api/erp/*）写入合成数据；RNG 种子 20260929；先打克隆库（127.0.0.1:48188）两遍验证零新建后，经 owner 确认再打正式库（48088）一遍。
- 不改表结构，不动 seed-v1/seed-v2 快照文件。客户转移、合同提交审批（BPM）、回款提交全部走应用自身流程，应用自生成编号。
- 快照：`seed-v3.sql.gz`，sha256 `caa518ff281386079e348024af20e4354f6e0541940e75d2d642bad5db5bd71d`；数据库停在 seed-v3。

## 可见性口径

- actor：evaluator/257（华东销售部 122，普通代表、无下级），可见集 = 自己名下 ∪ crm_permission 参与（biz_type 2/3/4/5/7/8），且排除负责人为空的入海池记录（scene2 条件 `ne(owner_user_id, self)` 在 SQL 三值逻辑下不匹配 NULL，与 `CrmPermissionUtils.appendPermissionCondition` 一致）。客户实体在提取时以 evaluator 会话 sceneType 1/2/3 分页合并与 SQL 可见集逐 id 相等校验通过；其余实体沿用同一口径。
- 数据题一律按名称/字段锚定；详情页 path 用落库后的真实记录 id。

## 题型计数

- normal: 30
- multi_condition: 25
- clarification: 15
- impossible: 10
- source_rule: 20

## 离线检查（全部零费用）

- JSON 可解析；id 连续 y3-001..100；每题 type/category/delivery 齐全；计数 30/25/15/10/20。
- 全部正则可编译；truth.positive 命中该题所有 check；truth.wrong 全部不命中 answer_regex/explains_regex（open 题 wrong probe 不同时命中 path+面包屑）。
- 规则题引用逐段重验：行段 sha256 与 .q0-cache/yudao（server 6aa235e493、ui e272d6639b）一致；`verified_locations` 仿真展开的逐行原子命中 source_regex；顶层 source_binding.files 整文件哈希一致。
- 未覆盖项：open 题页面路由取自部署版 ui 源码 remaining.ts 与 system_menu，未经浏览器实走（`browser_walked:false` 已标注在每题 evidence）。
- 提取器修复记录：分页参数重复传递会让服务端恒返回第 1 页（早期 API 并集截在 100 的真因），已改为参数去重并加满页判停；长会话中途令牌过期返回 401，重试时刷新令牌；偶发 `code 500 系统异常` 重试 4 次。

## 数据题真值

### y3-001 [normal] 苏州蓝湾智能设备有限公司登记的手机号是多少？
- expected: `"13767834250"` ｜ positive: 手机号是 13767834250。 ｜ wrong(必判错): 手机号是 13800000000。
- 证据 SQL: `select mobile from crm_customer where name='苏州蓝湾智能设备有限公司'` → 13767834250

### y3-002 [normal] 温州天成食品有限公司的主联系人是谁？
- expected: `"张伟"` ｜ positive: 主联系人是 张伟。 ｜ wrong(必判错): 主联系人是 张大伟。
- 证据 SQL: `select name from crm_contact where customer_id=282 and master=1` → 张伟

### y3-003 [normal] 广州宏图数据服务有限公司名下有几个联系人？
- expected: `4` ｜ positive: 共 4 个。 ｜ wrong(必判错): 共 41 个。
- 证据 SQL: `select count(*) from crm_contact where customer_id=?` → 4

### y3-004 [normal] 厦门瑞丰软件有限公司名下的联系人都列一下。
- expected: `["单于", "罗兰", "捷阳", "张大伟"]` ｜ positive: 联系人是：单于、罗兰、捷阳、张大伟。 ｜ wrong(必判错): 联系人是：单于、罗兰、捷阳。
- 证据 SQL: `select name from crm_contact where customer_id=? order by id` → ['单于', '罗兰', '捷阳', '张大伟']

### y3-005 [normal] 厦门瑞丰软件定制合同的签约日期是哪天？
- expected: `"2026-08-26"` ｜ positive: 签约日期是 2026-08-26。 ｜ wrong(必判错): 签约日期是 2026-01-01。
- 证据 SQL: `select order_date from crm_contract where name='厦门瑞丰软件定制合同'` → 2026-08-26

### y3-006 [normal] 深圳市鹏城物联企业版订阅合同的合同金额是多少？
- expected: `"59600"` ｜ positive: 合同金额是 59600 元。 ｜ wrong(必判错): 合同金额是 99999 元。
- 证据 SQL: `select total_price from crm_contract where name='深圳市鹏城物联企业版订阅合同'` → 59600

### y3-007 [normal] 蓝湾数字孪生项目现在处于哪个销售阶段？
- expected: `"初步接洽"` ｜ positive: 当前阶段是初步接洽。 ｜ wrong(必判错): 当前阶段是需求确认。
- 证据 SQL: `select s.name from crm_business b join crm_business_status s on s.id=b.status_id where b.name='蓝湾数字孪生项目'` → 初步接洽

### y3-008 [normal] 厦门瑞丰软件定制合同一共有几期回款计划？
- expected: `3` ｜ positive: 共 3 期。 ｜ wrong(必判错): 共 6 期。
- 证据 SQL: `select count(*) from crm_receivable_plan where contract_id=?` → 3

### y3-009 [normal] 厦门瑞丰软件定制合同的第 2 期回款计划应回款日期是哪天？
- expected: `"2026-08-30"` ｜ positive: 第 2 期应回款日期是 2026-08-30。 ｜ wrong(必判错): 第 2 期应回款日期是 2026-12-31。
- 证据 SQL: `select return_time from crm_receivable_plan where contract_id=? and period=2` → 2026-08-30

### y3-010 [normal] 上海衡信商贸有限公司的联系电话是多少？
- expected: `"13768022272"` ｜ positive: 联系电话是 13768022272。 ｜ wrong(必判错): 联系电话是 13611112003。
- 证据 SQL: `select mobile from crm_customer where name='上海衡信商贸有限公司'` → 13768022272

### y3-011 [normal] 我名下（我负责的客户）一共有多少个客户？
- expected: `124` ｜ positive: 您名下共有 124 个客户。 ｜ wrong(必判错): 您名下共有 161 个客户。
- 证据 SQL: `select count(*) from crm_customer where owner_user_id=257` → 124

### y3-012 [normal] 我可见的合同里，审核通过的有几份？
- expected: `92` ｜ positive: 共 92 份。 ｜ wrong(必判错): 共 129 份。
- 证据 SQL: `select audit_status,count(*) ... group by audit_status -> 20` → 92

### y3-013 [normal] 我可见的合同里，还没提交审批（草稿）的有几份？
- expected: `11` ｜ positive: 共 11 份。 ｜ wrong(必判错): 共 48 份。
- 证据 SQL: `select audit_status,count(*) ... group by audit_status -> 0` → 11

### y3-014 [normal] 我可见的合同里，审批中的有几份？
- expected: `16` ｜ positive: 共 16 份。 ｜ wrong(必判错): 共 53 份。
- 证据 SQL: `select audit_status,count(*) ... group by audit_status -> 10` → 16

### y3-015 [normal] 我可见的合同里，被拒绝的有几份？
- expected: `7` ｜ positive: 共 7 份。 ｜ wrong(必判错): 共 44 份。
- 证据 SQL: `select audit_status,count(*) ... group by audit_status -> 30` → 7

### y3-016 [normal] 2026 年 9 月签约的合同有几份？
- expected: `12` ｜ positive: 共 12 份。 ｜ wrong(必判错): 共 49 份。
- 证据 SQL: `order_date >= '2026-09-01' and order_date < '2026-09-30'` → 12

### y3-017 [normal] 回款计划里应回款日期落在 2026 年 10 月的有几期？
- expected: `23` ｜ positive: 共 23 期。 ｜ wrong(必判错): 共 60 期。
- 证据 SQL: `return_time in 2026-10 (visible plans)` → 23

### y3-018 [normal] 2026 年 10 月到期但还没收到钱的回款计划有几期？
- expected: `17` ｜ positive: 共 17 期。 ｜ wrong(必判错): 共 54 期。
- 证据 SQL: `receivable_id is null and return_time in 2026-10` → 17

### y3-019 [normal] 截至 2026-09-29，逾期未回款的回款计划一共有几期？
- expected: `56` ｜ positive: 共 56 期。 ｜ wrong(必判错): 共 93 期。
- 证据 SQL: `receivable_id is null and return_time < '2026-09-29 00:00:00'` → 56

### y3-020 [normal] 我可见范围内的商机里，赢单的有几个？
- expected: `1` ｜ positive: 共 1 个。 ｜ wrong(必判错): 共 38 个。
- 证据 SQL: `crm_business end_status filter (biz_won)` → 1

### y3-021 [normal] 我可见范围内的商机里，输单的有几个？
- expected: `1` ｜ positive: 共 1 个。 ｜ wrong(必判错): 共 38 个。
- 证据 SQL: `crm_business end_status filter (biz_lost)` → 1

### y3-022 [normal] 我可见范围内的商机里，还没结束（未赢未输）的有几个？
- expected: `109` ｜ positive: 共 109 个。 ｜ wrong(必判错): 共 146 个。
- 证据 SQL: `crm_business end_status filter (biz_open)` → 109

### y3-023 [normal] 打开厦门瑞丰软件定制合同的详情页。
- page: /crm/contract/detail/16 合同详情 ｜ 依据: 路由取自部署版 ui 源码 src/router/modules/remaining.ts L534-618（/crm/*/detail/:id）；记录 id 来自 seed-v3 SQL

### y3-024 [normal] 打开蓝湾数字孪生项目的商机详情页。
- page: /crm/business/detail/219 商机详情 ｜ 依据: 路由取自部署版 ui 源码 src/router/modules/remaining.ts L534-618（/crm/*/detail/:id）；记录 id 来自 seed-v3 SQL

### y3-025 [normal] 打开苏州蓝湾智能设备有限公司的客户详情页。
- page: /crm/customer/detail/275 客户详情 ｜ 依据: 路由取自部署版 ui 源码 src/router/modules/remaining.ts L534-618（/crm/*/detail/:id）；记录 id 来自 seed-v3 SQL

### y3-026 [normal] 打开上海衡信商贸有限公司的客户详情页。
- page: /crm/customer/detail/281 客户详情 ｜ 依据: 路由取自部署版 ui 源码 src/router/modules/remaining.ts L534-618（/crm/*/detail/:id）；记录 id 来自 seed-v3 SQL

### y3-027 [normal] 打开客户列表页面。
- page: /crm/customer 客户管理 ｜ 依据: 列表页路由取自 system_menu（菜单名即面包屑末级），前端菜单驱动

### y3-028 [normal] 打开合同列表页面。
- page: /crm/contract 合同管理 ｜ 依据: 列表页路由取自 system_menu（菜单名即面包屑末级），前端菜单驱动

### y3-029 [normal] 打开回款计划列表页面。
- page: /crm/receivable-plan 回款计划 ｜ 依据: 列表页路由取自 system_menu（菜单名即面包屑末级），前端菜单驱动

### y3-030 [normal] 打开产品列表页面。
- page: /crm/product 产品管理 ｜ 依据: 列表页路由取自 system_menu（菜单名即面包屑末级），前端菜单驱动

### y3-031 [multi_condition] 合同金额在 6 万到 12 万之间（含两端）的合同有几份？
- expected: `1` ｜ positive: 共 1 份。 ｜ wrong(必判错): 共 38 份。
- 证据 SQL: `band count (visible)` → 1

### y3-032 [multi_condition] 6 万到 12 万（含两端）且审核通过的合同有几份？
- expected: `0` ｜ positive: 共 0 份。 ｜ wrong(必判错): 共 37 份。
- 证据 SQL: `band and audit_status=20 count` → 0

### y3-033 [multi_condition] 2026 年 9 月签约、且已审核通过的合同有几份？
- expected: `11` ｜ positive: 共 11 份。 ｜ wrong(必判错): 共 48 份。
- 证据 SQL: `sept window and audit_status=20` → 11

### y3-034 [multi_condition] 9 月签约但被拒绝的合同有几份？
- expected: `0` ｜ positive: 共 0 份。 ｜ wrong(必判错): 共 37 份。
- 证据 SQL: `sept window and audit_status=30` → 0

### y3-035 [multi_condition] 提醒天数设为 15 天、且已逾期还没回款的计划有几期？
- expected: `55` ｜ positive: 共 55 期。 ｜ wrong(必判错): 共 92 期。
- 证据 SQL: `remind_days=15 and receivable_id is null` → 55

### y3-036 [multi_condition] 停在商务谈判阶段、还没结束的商机有几个？
- expected: `3` ｜ positive: 共 3 个。 ｜ wrong(必判错): 共 40 个。
- 证据 SQL: `join crm_business_status s on s.id=b.status_id and s.sort>=3 where b.end_status is null` → 3

### y3-037 [multi_condition] 把停在商务谈判阶段、还没结束的商机名字列出来。
- expected: `["厦门海湾硬件网关项目", "厦门瑞丰报表模板包项目", "嘉兴旭日语音客服模块项目"]` ｜ positive: 商机是：厦门海湾硬件网关项目、厦门瑞丰报表模板包项目、嘉兴旭日语音客服模块项目。 ｜ wrong(必判错): 商机是：厦门海湾硬件网关项目。
- 证据 SQL: `as above, names` → ['厦门海湾硬件网关项目', '厦门瑞丰报表模板包项目', '嘉兴旭日语音客服模块项目']

### y3-038 [multi_condition] 已审核通过的回款一共几笔、总金额多少？
- expected: `[53, "792900"]` ｜ positive: 共 53 笔，合计 792900 元。 ｜ wrong(必判错): 共 1234567 笔，合计 7654321 元。
- 证据 SQL: `select count(*),sum(price) from crm_receivable where audit_status=20 and visible` → [53, '792900']

### y3-039 [multi_condition] 厦门瑞丰软件定制合同合同金额多少？它已审核通过的回款合计又是多少？
- expected: `["3600", "1800"]` ｜ positive: 合同金额 3600 元，已审核通过的回款合计 1800 元。 ｜ wrong(必判错): 合同金额 3600 元，已审核通过的回款合计 7654321 元。
- 证据 SQL: `contract row + sum(receivable audit_status=20 for contract_id)` → ['3600', '1800']

### y3-040 [multi_condition] 厦门瑞丰软件定制合同一共几期计划？第 2 期金额多少？
- expected: `[3, "1080"]` ｜ positive: 共 3 期，第 2 期金额 1080 元。 ｜ wrong(必判错): 共 7 期，第 2 期金额 7654321 元。
- 证据 SQL: `plan rows for the named contract` → [3, '1080']

### y3-041 [multi_condition] 温州天成食品有限公司名下有几个联系人？
- expected: `5` ｜ positive: 共 5 个。 ｜ wrong(必判错): 共 42 个。
- 证据 SQL: `contacts by customer` → 5

### y3-042 [multi_condition] 把温州天成食品有限公司的联系人名字都列出来。
- expected: `["向晚", "岚山", "集睿", "张伟", "张伟明"]` ｜ positive: 联系人：向晚、岚山、集睿、张伟、张伟明。 ｜ wrong(必判错): 联系人：向晚。
- 证据 SQL: `select name from crm_contact where customer_id=? order by id` → ['向晚', '岚山', '集睿', '张伟', '张伟明']

### y3-043 [multi_condition] 厦门瑞丰软件有限公司名下已审核通过的合同有几份？
- expected: `2` ｜ positive: 共 2 份。 ｜ wrong(必判错): 共 39 份。
- 证据 SQL: `contract rows by customer with audit_status=20` → 2

### y3-044 [multi_condition] 我负责的客户里，等级是 A 类的有几个？
- expected: `39` ｜ positive: 共 39 个。 ｜ wrong(必判错): 共 76 个。
- 证据 SQL: `select count(*) from crm_customer where owner_user_id=257 and level=1` → 39

### y3-045 [multi_condition] 2026 年 9 月签约的合同总金额是多少？
- expected: `"295900"` ｜ positive: 合计 295900 元。 ｜ wrong(必判错): 合计 7654321 元。
- 证据 SQL: `sum(total_price) where order_date in 2026-09` → 295900

### y3-046 [multi_condition] 10 月到期的回款计划里，已经挂上回款的有几期？
- expected: `6` ｜ positive: 共 6 期。 ｜ wrong(必判错): 共 43 期。
- 证据 SQL: `return_time in 2026-10 and receivable_id is not null` → 6

### y3-047 [multi_condition] 把我可见的合同按审核状态分个组：草稿、审批中、通过、拒绝各几份？
- expected: `{"0": 11, "10": 16, "20": 92, "30": 7}` ｜ positive: 草稿 11、审批中 16、通过 92、拒绝 7。 ｜ wrong(必判错): 草稿 8、审批中 8、通过 8、拒绝 8。
- 证据 SQL: `select audit_status,count(*) ... group by audit_status (visible)` → {'0': 11, '10': 16, '20': 92, '30': 7}

### y3-048 [multi_condition] 广州宏图数据服务有限公司那位叫张伟的联系人，手机号是多少？
- expected: `"13611112003"` ｜ positive: 手机号是 13611112003。 ｜ wrong(必判错): 手机号是 13611112001。
- 证据 SQL: `select mobile from crm_contact where customer_id=? and name='张伟'` → 13611112003

### y3-049 [multi_condition] 用了「专业版订阅（年）」这个产品的（我可见的）合同有几份？
- expected: `0` ｜ positive: 共 0 份。 ｜ wrong(必判错): 共 37 份。
- 证据 SQL: `crm_contract_product join crm_product p.name='专业版订阅（年）' join visible contracts` → 0

### y3-050 [multi_condition] 厦门瑞丰软件定制合同和它的二期，两份合同金额加起来是多少？
- expected: `"14000"` ｜ positive: 合计 14000 元。 ｜ wrong(必判错): 合计 3600 元（漏了二期）。
- 证据 SQL: `two named contracts by exact name` → 14000

### y3-051 [multi_condition] 2026 年 8 月签约的合同有几份？
- expected: `16` ｜ positive: 共 16 份。 ｜ wrong(必判错): 共 53 份。
- 证据 SQL: `order_date in [2026-08-01,2026-09-01)` → 16

### y3-052 [multi_condition] 深圳市鹏城物联科技有限公司一共有几份合同？合计金额多少？
- expected: `[3, "175200"]` ｜ positive: 3 份，合计 175200 元。 ｜ wrong(必判错): 8 份，合计 7654321 元。
- 证据 SQL: `contracts joined by customer exact name` → [3, '175200']

### y3-053 [multi_condition] 草稿状态、金额超过 6 万的合同有几份？
- expected: `4` ｜ positive: 共 4 份。 ｜ wrong(必判错): 共 41 份。
- 证据 SQL: `audit_status=0 and total_price>60000` → 4

### y3-054 [multi_condition] 厦门瑞丰软件定制合同截至 2026-09-29 还有哪几期没收到钱？应回款日期分别是什么时候？
- expected: `["2026-08-30"]` ｜ positive: 还有 1 期没收到钱，应回款日期：2026-08-30。 ｜ wrong(必判错): 所有期数都已回款，没有欠款。
- 证据 SQL: `plans of named contract, receivable_id null and return_time < REF` → ['2026-08-30']

### y3-055 [multi_condition] 上海衡信商贸有限公司的联系人都有谁？
- expected: `["山岚", "听澜", "连城"]` ｜ positive: 联系人：山岚、听澜、连城。 ｜ wrong(必判错): 联系人：山岚。
- 证据 SQL: `contacts by customer` → ['山岚', '听澜', '连城']

### y3-056 [clarification] 打开苏州蓝湾那个客户的详情页。
- 候选: 苏州蓝湾智能设备有限公司、苏州蓝湾电子信息有限公司 ｜ positive: 请确认您指的是哪一个：苏州蓝湾智能设备有限公司、苏州蓝湾电子信息有限公司？ ｜ wrong(必判错): 不用确认，我按苏州蓝湾智能设备有限公司直接给您结果。
- 证据 SQL: `select name ... name like '%苏州蓝湾%' and id in visible` → 2

### y3-057 [clarification] 宁波恒昌泰和化工有限公司的联系人都有谁？
- 候选: 宁波恒昌泰和化工有限公司、宁波恒昌泰和化工有限公司舟山分公司 ｜ positive: 请确认您指的是哪一个：宁波恒昌泰和化工有限公司、宁波恒昌泰和化工有限公司舟山分公司？ ｜ wrong(必判错): 不用确认，我按宁波恒昌泰和化工有限公司直接给您结果。
- 证据 SQL: `select name ... name like '%宁波恒昌泰和%' in visible` → 2

### y3-058 [clarification] 温州天成食品有限公司那个张伟的手机号给我。
- 候选: 张伟、张伟明 ｜ positive: 请确认您指的是哪一个：张伟、张伟明？ ｜ wrong(必判错): 不用确认，我按张伟直接给您结果。
- 证据 SQL: `contacts of 温州天成 by name prefix` → 2

### y3-059 [clarification] 把厦门瑞丰软件定制合同的回款计划给我看一下。
- 候选: 厦门瑞丰软件定制合同、厦门瑞丰软件定制合同二期 ｜ positive: 请确认您指的是哪一个：厦门瑞丰软件定制合同、厦门瑞丰软件定制合同二期？ ｜ wrong(必判错): 不用确认，我按厦门瑞丰软件定制合同直接给您结果。
- 证据 SQL: `contract name like '厦门瑞丰软件定制合同%' in visible` → 2

### y3-060 [clarification] 打开深圳市鹏城物联企业版订阅合同的详情。
- 候选: 深圳市鹏城物联企业版订阅合同、深圳市鹏城物联企业版订阅合同（增补） ｜ positive: 请确认您指的是哪一个：深圳市鹏城物联企业版订阅合同、深圳市鹏城物联企业版订阅合同（增补）？ ｜ wrong(必判错): 不用确认，我按深圳市鹏城物联企业版订阅合同直接给您结果。
- 证据 SQL: `contract name prefix in visible` → 2

### y3-061 [clarification] 蓝湾数字孪生项目的预计成交时间？
- 候选: 蓝湾数字孪生项目、蓝湾数字孪生项目二期 ｜ positive: 请确认您指的是哪一个：蓝湾数字孪生项目、蓝湾数字孪生项目二期？ ｜ wrong(必判错): 不用确认，我按蓝湾数字孪生项目直接给您结果。
- 证据 SQL: `business name prefix in visible` → 2

### y3-062 [clarification] 打开专业版订阅的产品详情。
- 候选: 专业版订阅（三年）、专业版订阅（两年）、专业版订阅（年） ｜ positive: 请确认您指的是哪一个：专业版订阅（三年）、专业版订阅（两年）、专业版订阅（年）？ ｜ wrong(必判错): 不用确认，我按专业版订阅（三年）直接给您结果。
- 证据 SQL: `product name like '专业版订阅%'` → 3

### y3-063 [clarification] 企业版订阅这个产品的单价是多少？
- 候选: 企业版订阅（三年）、企业版订阅（两年）、企业版订阅（年） ｜ positive: 请确认您指的是哪一个：企业版订阅（三年）、企业版订阅（两年）、企业版订阅（年）？ ｜ wrong(必判错): 不用确认，我按企业版订阅（三年）直接给您结果。
- 证据 SQL: `product name like '企业版订阅%'` → 3

### y3-064 [clarification] 我们那个联系人张伟的手机号是多少？
- 候选: 张伟（温州天成食品有限公司）、张伟（广州宏图数据服务有限公司） ｜ positive: 请确认您指的是哪一个：张伟（温州天成食品有限公司）、张伟（广州宏图数据服务有限公司）？ ｜ wrong(必判错): 不用确认，我按张伟（温州天成食品有限公司）直接给您结果。
- 证据 SQL: `张伟 contacts grouped by customer` → 2

### y3-065 [clarification] 打开南通旭日那家公司的客户详情。
- 候选: 南通旭日自动化工程有限公司、南通旭日软件技术有限公司 ｜ positive: 请确认您指的是哪一个：南通旭日自动化工程有限公司、南通旭日软件技术有限公司？ ｜ wrong(必判错): 不用确认，我按南通旭日自动化工程有限公司直接给您结果。
- 证据 SQL: `select name ... name like '南通旭日%' in visible` → 2

### y3-066 [clarification] 打开台州天成那家公司的客户详情。
- 候选: 台州天成数据服务有限公司、台州天成智能设备有限公司 ｜ positive: 请确认您指的是哪一个：台州天成数据服务有限公司、台州天成智能设备有限公司？ ｜ wrong(必判错): 不用确认，我按台州天成数据服务有限公司直接给您结果。
- 证据 SQL: `select name ... name like '台州天成%' in visible` → 2

### y3-067 [clarification] 打开嘉兴旭日那家公司的客户详情。
- 候选: 嘉兴旭日自动化工程有限公司、嘉兴旭日软件技术有限公司 ｜ positive: 请确认您指的是哪一个：嘉兴旭日自动化工程有限公司、嘉兴旭日软件技术有限公司？ ｜ wrong(必判错): 不用确认，我按嘉兴旭日自动化工程有限公司直接给您结果。
- 证据 SQL: `select name ... name like '嘉兴旭日%' in visible` → 2

### y3-068 [clarification] 打开常州瑞丰那家公司的客户详情。
- 候选: 常州瑞丰智能设备有限公司、常州瑞丰电子信息有限公司 ｜ positive: 请确认您指的是哪一个：常州瑞丰智能设备有限公司、常州瑞丰电子信息有限公司？ ｜ wrong(必判错): 不用确认，我按常州瑞丰智能设备有限公司直接给您结果。
- 证据 SQL: `select name ... name like '常州瑞丰%' in visible` → 2

### y3-069 [clarification] 打开温州蓝湾那家公司的客户详情。
- 候选: 温州蓝湾数据服务有限公司、温州蓝湾电子信息有限公司 ｜ positive: 请确认您指的是哪一个：温州蓝湾数据服务有限公司、温州蓝湾电子信息有限公司？ ｜ wrong(必判错): 不用确认，我按温州蓝湾数据服务有限公司直接给您结果。
- 证据 SQL: `select name ... name like '温州蓝湾%' in visible` → 2

### y3-070 [clarification] 打开湖州中科那家公司的客户详情。
- 候选: 湖州中科智能设备有限公司、湖州中科电子信息有限公司 ｜ positive: 请确认您指的是哪一个：湖州中科智能设备有限公司、湖州中科电子信息有限公司？ ｜ wrong(必判错): 不用确认，我按湖州中科智能设备有限公司直接给您结果。
- 证据 SQL: `select name ... name like '湖州中科%' in visible` → 2

### y3-071 [impossible] 帮我查一下现在美元兑人民币的汇率。
- 缺失类型: {"kind": "no_capability", "topic": "汇率查询"} ｜ positive: 芋道 CRM 里没有汇率查询功能，我查不到实时汇率。 ｜ wrong(必判错): 美元兑人民币约 7.1。

### y3-072 [impossible] 把今年第 11 号台风的实时路径发我。
- 缺失类型: {"kind": "no_capability", "topic": "台风路径"} ｜ positive: 应用里没有气象数据，台风路径我查不到。 ｜ wrong(必判错): 台风路径为北纬 21.5 度……

### y3-073 [impossible] 我们和行业头部公司比，市场份额差多少？
- 缺失类型: {"kind": "no_capability", "topic": "外部市场份额"} ｜ positive: 市场份额需要外部行业数据，应用里查不到，我无法给出对比。 ｜ wrong(必判错): 大约差 12 个百分点。

### y3-074 [impossible] 南京博远信息技术有限公司这个客户的等级和负责人是谁？
- 缺失类型: {"kind": "not_found_in_visible_scope", "model": "crm_customer", "name": "南京博远信息技术有限公司"} ｜ positive: 在我能看到的客户里没有查到「南京博远信息技术有限公司」。 ｜ wrong(必判错): 南京博远信息技术有限公司 是 B 类客户，负责人是刘洋。

### y3-075 [impossible] 宁波海川电子有限公司这个客户的等级和负责人是谁？
- 缺失类型: {"kind": "not_found_in_visible_scope", "model": "crm_customer", "name": "宁波海川电子有限公司"} ｜ positive: 在我能看到的客户里没有查到「宁波海川电子有限公司」。 ｜ wrong(必判错): 宁波海川电子有限公司 是 B 类客户，负责人是刘洋。

### y3-076 [impossible] 东莞华盛塑胶制品有限公司这个客户的等级和负责人是谁？
- 缺失类型: {"kind": "not_found_in_visible_scope", "model": "crm_customer", "name": "东莞华盛塑胶制品有限公司"} ｜ positive: 在我能看到的客户里没有查到「东莞华盛塑胶制品有限公司」。 ｜ wrong(必判错): 东莞华盛塑胶制品有限公司 是 B 类客户，负责人是刘洋。

### y3-077 [impossible] 合同《佛山美居培训服务合同》一共回款多少了？
- 缺失类型: {"kind": "not_found_in_visible_scope", "model": "crm_contract", "name": "佛山美居培训服务合同"} ｜ positive: 在我可见的合同里没有找到《佛山美居培训服务合同》。 ｜ wrong(必判错): 《佛山美居培训服务合同》已回款 12000 元。

### y3-078 [impossible] 把厦门瑞丰软件有限公司的联系电话改成 13800001234。
- 缺失类型: {"kind": "write_refusal_readonly", "model": "crm_customer"} ｜ positive: 这个会话是只读的，我不能修改客户的联系电话。 ｜ wrong(必判错): 已把联系电话改为 13800001234。

### y3-079 [impossible] 把厦门瑞丰软件定制合同二期删掉。
- 缺失类型: {"kind": "write_refusal_readonly", "model": "crm_contract"} ｜ positive: 只读会话下我不能删除合同。 ｜ wrong(必判错): 已删除厦门瑞丰软件定制合同二期。

### y3-080 [impossible] 给上海衡信商贸有限公司补一条今天的跟进记录，内容写电话回访。
- 缺失类型: {"kind": "write_refusal_readonly", "model": "crm_follow_up_record"} ｜ positive: 只读会话下我不能新增跟进记录。 ｜ wrong(必判错): 已为上海衡信商贸有限公司添加跟进记录。

## 规则题真值

### y3-081 合同列表里提交审核按钮只出现在部分行，按什么规则？
- ui/src/views/crm/contract/index.vue:214-221 sha256:961d40885e1ab9291661b16354fb7c5c251b2e5cdbf60a30b145f65e56ad300d ｜ 列表行提交审核按钮 v-if auditStatus===0
  - 合同列表行的提交审核按钮带 v-if="scope.row.auditStatus === 0"，只有状态为草稿（0）的行才渲染该按钮。
- server/yudao-module-crm/src/main/java/cn/iocoder/yudao/module/crm/service/contract/CrmContractServiceImpl.java:295-296 sha256:9ec61549fd1cd86d3cbe876ee36ab91dc8c0b988aa8da3ea18ddced1cefdddc4 ｜ submitContract
  - submitContract 校验合同审批状态不等于 DRAFT 时抛出 CONTRACT_SUBMIT_FAIL_NOT_DRAFT，即后端只允许提交草稿状态的合同。
- answer_regex: `(?s)(?=.*草稿)(?=.*(?:提交审核|按钮)).*`
- source_regex: `(?s)(?=.*(?:ui/src/views/crm/contract/index\.vue:215@v2026\.08|server/yudao-module-crm/src/main/java/cn/iocoder/yudao/module/crm/service/contract/CrmContractServiceImpl\.java:296@v2026\.08)).*`
- positive:（含引用行）
  - 提交审核按钮只在该行合同处于草稿状态时显示，非草稿行不出现该按钮：列表按钮的 v-if 按 auditStatus===0（草稿）渲染，后端提交审核同样只接受草稿状态。
  - ui/src/views/crm/contract/index.vue:214-221@v2026.08 sha256:961d40885e1ab9291661b16354fb7c5c251b2e5cdbf60a30b145f65e56ad300d
  - server/yudao-module-crm/src/main/java/cn/iocoder/yudao/module/crm/service/contract/CrmContractServiceImpl.java:295-296@v2026.08 sha256:9ec61549fd1cd86d3cbe876ee36ab91dc8c0b988aa8da3ea18ddced1cefdddc4
- wrong(必判错): 提交审核按钮对所有合同行都常驻显示，任何人都能反复提交审核，与审批状态无关。

### y3-082 审批已通过的客户合同，我为什么改不了里面的金额？
- server/yudao-module-crm/src/main/java/cn/iocoder/yudao/module/crm/service/contract/CrmContractServiceImpl.java:145-147 sha256:870148545ba9e966f7ef4d2fa2b474a348f8461cccab4279f23967c7ff20bf2c ｜ updateContract
  - updateContract 校验原合同审批状态既不是 DRAFT 也不是 PROCESS 时抛出 CONTRACT_UPDATE_FAIL_NOT_DRAFT，即只有草稿或审批中的合同可以更新。
- ui/src/views/crm/contract/index.vue:205-213 sha256:b8ca88526f6bd5144c991c8069bb2cf217c6a159d2f972772da7dcb510deccfa ｜ 列表行编辑按钮 v-if auditStatus===0
  - 合同列表行的编辑按钮带 v-if="scope.row.auditStatus === 0"，只有草稿行才显示编辑按钮，审批通过后不再提供编辑入口。
- answer_regex: `(?s)(?=.*(?:草稿|审批中))(?=.*(?:编辑|更新|修改)).*`
- source_regex: `(?s)(?=.*(?:server/yudao-module-crm/src/main/java/cn/iocoder/yudao/module/crm/service/contract/CrmContractServiceImpl\.java:147@v2026\.08|ui/src/views/crm/contract/index\.vue:206@v2026\.08)).*`
- positive:（含引用行）
  - 合同只有处于草稿或审批中状态才允许编辑金额，审批通过后后端 updateContract 直接拒绝更新，前端列表也不再出现编辑入口。
  - server/yudao-module-crm/src/main/java/cn/iocoder/yudao/module/crm/service/contract/CrmContractServiceImpl.java:145-147@v2026.08 sha256:870148545ba9e966f7ef4d2fa2b474a348f8461cccab4279f23967c7ff20bf2c
  - ui/src/views/crm/contract/index.vue:205-213@v2026.08 sha256:b8ca88526f6bd5144c991c8069bb2cf217c6a159d2f972772da7dcb510deccfa
- wrong(必判错): 审批通过后金额永久锁定，只能新建一份合同重新录入，旧合同连查看都不允许。

### y3-083 我想删掉一份合同，系统说删不了，最可能的原因是？
- server/yudao-module-crm/src/main/java/cn/iocoder/yudao/module/crm/service/contract/CrmContractServiceImpl.java:233-235 sha256:51ecbf1b7a7c6d9f710b95a3669dc44ef42e38595c84243ec91cd1a217e99c58 ｜ deleteContract
  - deleteContract 校验该合同关联的回款数量大于 0 时抛出 CONTRACT_DELETE_FAIL，已有回款的合同不允许删除。
- answer_regex: `(?s)(?=.*回款)(?=.*删).*`
- source_regex: `(?s)(?=.*server/yudao-module-crm/src/main/java/cn/iocoder/yudao/module/crm/service/contract/CrmContractServiceImpl\.java:234@v2026\.08)`
- positive:（含引用行）
  - 最可能的原因是这份合同已经关联了回款：删除合同前会统计其下回款数量，只要大于 0 就不允许删除。
  - server/yudao-module-crm/src/main/java/cn/iocoder/yudao/module/crm/service/contract/CrmContractServiceImpl.java:233-235@v2026.08 sha256:51ecbf1b7a7c6d9f710b95a3669dc44ef42e38595c84243ec91cd1a217e99c58
- wrong(必判错): 任何人都可以直接删除合同，系统不做任何关联校验，删错了也无法恢复。

### y3-084 提交一份合同去审批时提示合同提交审核失败，为什么？
- server/yudao-module-crm/src/main/java/cn/iocoder/yudao/module/crm/service/contract/CrmContractServiceImpl.java:295-296 sha256:9ec61549fd1cd86d3cbe876ee36ab91dc8c0b988aa8da3ea18ddced1cefdddc4 ｜ submitContract
  - submitContract 校验合同审批状态不等于 DRAFT 时抛出 CONTRACT_SUBMIT_FAIL_NOT_DRAFT，即只有草稿合同可以提交审核。
- answer_regex: `(?s)(?=.*(?:未提交|草稿))(?=.*提交).*`
- source_regex: `(?s)(?=.*server/yudao-module-crm/src/main/java/cn/iocoder/yudao/module/crm/service/contract/CrmContractServiceImpl\.java:296@v2026\.08)`
- positive:（含引用行）
  - 因为这份合同当前不是草稿状态（已经提交过、正在审批或已出结果），提交审核只接受未提交的草稿合同，重复提交也会报这个错。
  - server/yudao-module-crm/src/main/java/cn/iocoder/yudao/module/crm/service/contract/CrmContractServiceImpl.java:295-296@v2026.08 sha256:9ec61549fd1cd86d3cbe876ee36ab91dc8c0b988aa8da3ea18ddced1cefdddc4
- wrong(必判错): 该提示与合同当前状态无关，只取决于操作人有没有审批权限。

### y3-085 我改一期回款计划的金额，系统不让改，为什么？
- server/yudao-module-crm/src/main/java/cn/iocoder/yudao/module/crm/service/receivable/CrmReceivablePlanServiceImpl.java:95-97 sha256:3cd273e93770505b05df34c0130ba96cd851f6aee4aa6b9e10d3830dab17db50 ｜ updateReceivablePlan
  - updateReceivablePlan 校验旧计划的 receivableId 非空（已经关联回款）时抛出 RECEIVABLE_PLAN_UPDATE_FAIL，已有回款的计划不允许修改。
- answer_regex: `(?s)(?=.*(?:回款|还款))(?=.*(?:修改|更新|改))(?=.*(?:已经|已有|对应)).*`
- source_regex: `(?s)(?=.*server/yudao-module-crm/src/main/java/cn/iocoder/yudao/module/crm/service/receivable/CrmReceivablePlanServiceImpl\.java:96@v2026\.08)`
- positive:（含引用行）
  - 因为这期回款计划已经挂上了对应的回款记录，一旦计划关联了回款，系统就不允许再修改该计划的任何字段。
  - server/yudao-module-crm/src/main/java/cn/iocoder/yudao/module/crm/service/receivable/CrmReceivablePlanServiceImpl.java:95-97@v2026.08 sha256:3cd273e93770505b05df34c0130ba96cd851f6aee4aa6b9e10d3830dab17db50
- wrong(必判错): 回款计划的金额随时可以改，系统对这个字段不做任何校验。

### y3-086 新建一笔回款，金额比合同剩余还多，系统怎么处理？
- server/yudao-module-crm/src/main/java/cn/iocoder/yudao/module/crm/service/receivable/CrmReceivableServiceImpl.java:129-131 sha256:7fab49997060f5f5614c74b3d28175471decc973771e619d47fd3b5868e805c5 ｜ validateReceivablePriceExceedsLimit
  - validateReceivablePriceExceedsLimit 计算合同总额减去审批中与已通过回款后的剩余可回款金额，请求金额超出剩余值时抛出 RECEIVABLE_CREATE_FAIL_PRICE_EXCEEDS_LIMIT。
- answer_regex: `(?s)(?=.*(?:超出|超过))(?=.*(?:回款金额|合同金额|剩余)).*`
- source_regex: `(?s)(?=.*server/yudao-module-crm/src/main/java/cn/iocoder/yudao/module/crm/service/receivable/CrmReceivableServiceImpl\.java:130@v2026\.08)`
- positive:（含引用行）
  - 系统会直接拦截并报错：回款金额超出合同剩余可回款金额（合同总额扣除审批中与已通过回款后的余额），不会保存这笔回款。
  - server/yudao-module-crm/src/main/java/cn/iocoder/yudao/module/crm/service/receivable/CrmReceivableServiceImpl.java:129-131@v2026.08 sha256:7fab49997060f5f5614c74b3d28175471decc973771e619d47fd3b5868e805c5
- wrong(必判错): 系统按填写金额直接保存回款，不做任何金额上限校验。

### y3-087 给一份还在审批中的合同建回款，报什么错？
- server/yudao-module-crm/src/main/java/cn/iocoder/yudao/module/crm/service/receivable/CrmReceivableServiceImpl.java:140-142 sha256:d6a3ee58d7d6d9620fcd0f84646cd4de0656a0a9c56fc6575872001f532929ed ｜ validateRelationDataExists
  - validateRelationDataExists 校验关联合同的审批状态不等于 APPROVE 时抛出 RECEIVABLE_CREATE_FAIL_CONTRACT_NOT_APPROVE，合同必须已审核通过才能新建回款。
- answer_regex: `(?s)(?=.*审核通过)(?=.*回款).*`
- source_regex: `(?s)(?=.*server/yudao-module-crm/src/main/java/cn/iocoder/yudao/module/crm/service/receivable/CrmReceivableServiceImpl\.java:141@v2026\.08)`
- positive:（含引用行）
  - 会报合同不是审核通过状态、不能新建回款的错误：回款要求关联合同已审核通过，审批中（PROCESS）还不满足。
  - server/yudao-module-crm/src/main/java/cn/iocoder/yudao/module/crm/service/receivable/CrmReceivableServiceImpl.java:140-142@v2026.08 sha256:d6a3ee58d7d6d9620fcd0f84646cd4de0656a0a9c56fc6575872001f532929ed
- wrong(必判错): 能不能新建回款只看操作人有没有回款新建权限，与合同的审批状态无关。

### y3-088 同一期回款计划为什么不能挂第二笔回款？
- server/yudao-module-crm/src/main/java/cn/iocoder/yudao/module/crm/service/receivable/CrmReceivableServiceImpl.java:150-152 sha256:a905534cb31988d884f24ec49955a0985334e8c7f2244b597642fe76034a4894 ｜ validateRelationDataExists
  - validateRelationDataExists 校验回款计划的 receivableId 非空时抛出 RECEIVABLE_PLAN_EXISTS_RECEIVABLE，已挂回款的计划不能再新建回款。
- answer_regex: `(?s)(?=.*(?:已有|存在|已经))(?=.*回款).*`
- source_regex: `(?s)(?=.*server/yudao-module-crm/src/main/java/cn/iocoder/yudao/module/crm/service/receivable/CrmReceivableServiceImpl\.java:151@v2026\.08)`
- positive:（含引用行）
  - 因为该回款计划已经存在关联的回款记录（receivableId 非空），一期计划只允许对应一笔回款，再挂第二笔会直接被拒绝。
  - server/yudao-module-crm/src/main/java/cn/iocoder/yudao/module/crm/service/receivable/CrmReceivableServiceImpl.java:150-152@v2026.08 sha256:a905534cb31988d884f24ec49955a0985334e8c7f2244b597642fe76034a4894
- wrong(必判错): 同一期计划可以挂任意多笔回款，系统只在合同总额层面做控制。

### y3-089 一笔回款删不掉，有哪两种原因？
- server/yudao-module-crm/src/main/java/cn/iocoder/yudao/module/crm/service/receivable/CrmReceivableServiceImpl.java:221-223 sha256:7c31af8a378c9735bb61be156c2f7c41fd0daea0120510314ee48422f3e29e99 ｜ deleteReceivable
  - deleteReceivable 校验回款的 planId 非空且对应回款计划存在时抛出 RECEIVABLE_DELETE_FAIL，关联着回款计划的回款不允许删除。
- server/yudao-module-crm/src/main/java/cn/iocoder/yudao/module/crm/service/receivable/CrmReceivableServiceImpl.java:225-227 sha256:5e4f35bc291659aafa2c8fe796bec390253f807fb0158916ef294499bfad2682 ｜ deleteReceivable
  - deleteReceivable 校验回款审批状态等于 APPROVE 时抛出 RECEIVABLE_DELETE_FAIL_IS_APPROVE，审核已通过的回款不允许删除。
- answer_regex: `(?s)(?=.*计划)(?=.*(?:审核已通过|审批已通过)).*`
- source_regex: `(?s)(?=.*server/yudao-module-crm/src/main/java/cn/iocoder/yudao/module/crm/service/receivable/CrmReceivableServiceImpl\.java:222@v2026\.08)(?=.*server/yudao-module-crm/src/main/java/cn/iocoder/yudao/module/crm/service/receivable/CrmReceivableServiceImpl\.java:226@v2026\.08)`
- positive:（含引用行）
  - 两种原因：一是这笔回款关联的回款计划仍然存在（挂着计划的回款不允许删除），二是该回款审核已通过（审批已通过的记录同样不允许删除）。
  - server/yudao-module-crm/src/main/java/cn/iocoder/yudao/module/crm/service/receivable/CrmReceivableServiceImpl.java:221-223@v2026.08 sha256:7c31af8a378c9735bb61be156c2f7c41fd0daea0120510314ee48422f3e29e99
  - server/yudao-module-crm/src/main/java/cn/iocoder/yudao/module/crm/service/receivable/CrmReceivableServiceImpl.java:225-227@v2026.08 sha256:5e4f35bc291659aafa2c8fe796bec390253f807fb0158916ef294499bfad2682
- wrong(必判错): 回款删除没有任何前置校验，任何时候都能直接删。

### y3-090 回款列表里的提交审核按钮什么时候出现？
- server/yudao-module-crm/src/main/java/cn/iocoder/yudao/module/crm/service/receivable/CrmReceivableServiceImpl.java:246-248 sha256:548b99c0975db7dfe28916aa4d68cdef8d5afe8ee6e63c8fc97b04c3b9d6040a ｜ submitReceivable
  - submitReceivable 校验回款审批状态不等于 DRAFT 时抛出 RECEIVABLE_SUBMIT_FAIL_NOT_DRAFT，后端只允许提交草稿状态的回款。
- ui/src/views/crm/receivable/index.vue:166-174 sha256:afa7079f483c72860a659a6e955d3355eeee755b95edd31cdf9afbef8e297d37 ｜ 列表行提交审核按钮 v-if auditStatus===0
  - 回款列表行的提交审核按钮带 v-if="scope.row.auditStatus === 0"，只有草稿（0）状态的行才渲染该按钮。
- answer_regex: `(?s)(?=.*(?:草稿|未提交))(?=.*提交).*`
- source_regex: `(?s)(?=.*(?:server/yudao-module-crm/src/main/java/cn/iocoder/yudao/module/crm/service/receivable/CrmReceivableServiceImpl\.java:247@v2026\.08|ui/src/views/crm/receivable/index\.vue:167@v2026\.08)).*`
- positive:（含引用行）
  - 只有该笔回款处于草稿（未提交）状态时列表行才显示提交审核按钮，提交进入审批后按钮就不再出现。
  - server/yudao-module-crm/src/main/java/cn/iocoder/yudao/module/crm/service/receivable/CrmReceivableServiceImpl.java:246-248@v2026.08 sha256:548b99c0975db7dfe28916aa4d68cdef8d5afe8ee6e63c8fc97b04c3b9d6040a
  - ui/src/views/crm/receivable/index.vue:166-174@v2026.08 sha256:afa7079f483c72860a659a6e955d3355eeee755b95edd31cdf9afbef8e297d37
- wrong(必判错): 提交审核按钮对所有回款行都显示，重复提交也不会报错。

### y3-091 删一个客户提示删除客户失败，可能关联了什么？
- server/yudao-module-crm/src/main/java/cn/iocoder/yudao/module/crm/service/customer/CrmCustomerServiceImpl.java:564-572 sha256:58f52d71e6e72411a1ea74f042bc017697890b9457446931a4472bb21dc037ff ｜ validateCustomerReference
  - validateCustomerReference 依次校验客户的联系人、商机、合同数量，任一大于 0 就抛出 CUSTOMER_DELETE_FAIL_HAVE_REFERENCE 并带上关联对象名称。
- answer_regex: `(?s)(?=.*(?:联系人|商机|合同))(?=.*(?:删除|关联)).*`
- source_regex: `(?s)(?=.*server/yudao-module-crm/src/main/java/cn/iocoder/yudao/module/crm/service/customer/CrmCustomerServiceImpl\.java:571@v2026\.08)`
- positive:（含引用行）
  - 该客户名下还关联着联系人、商机或合同：删除客户前会依次统计这三类引用，任一存在就拒绝删除并在错误里点出对象类型。
  - server/yudao-module-crm/src/main/java/cn/iocoder/yudao/module/crm/service/customer/CrmCustomerServiceImpl.java:564-572@v2026.08 sha256:58f52d71e6e72411a1ea74f042bc017697890b9457446931a4472bb21dc037ff
- wrong(必判错): 删除客户总是成功，关联数据会随之自动清理。

### y3-092 商机删不掉，为什么？
- server/yudao-module-crm/src/main/java/cn/iocoder/yudao/module/crm/service/business/CrmBusinessServiceImpl.java:289-291 sha256:0865716569249bd0bf2e9d46e2ee9e2f4fffa5e83581b68f5483fb999327d32f ｜ validateContractExists
  - validateContractExists（删除商机前调用）校验该商机关联的合同数量大于 0 时抛出 BUSINESS_DELETE_FAIL_CONTRACT_EXISTS，有合同的商机不允许删除。
- answer_regex: `(?s)(?=.*合同)(?=.*删).*`
- source_regex: `(?s)(?=.*server/yudao-module-crm/src/main/java/cn/iocoder/yudao/module/crm/service/business/CrmBusinessServiceImpl\.java:290@v2026\.08)`
- positive:（含引用行）
  - 因为这个商机下面已经关联了合同：删除商机前会统计其下合同数量，只要存在合同就不允许删除。
  - server/yudao-module-crm/src/main/java/cn/iocoder/yudao/module/crm/service/business/CrmBusinessServiceImpl.java:289-291@v2026.08 sha256:0865716569249bd0bf2e9d46e2ee9e2f4fffa5e83581b68f5483fb999327d32f
- wrong(必判错): 商机随时可删，系统不做任何关联校验。

### y3-093 商机关闭后为什么不能再调整阶段？
- server/yudao-module-crm/src/main/java/cn/iocoder/yudao/module/crm/service/business/CrmBusinessServiceImpl.java:237-239 sha256:e6ef75d4e73f3ff1bef61c0731d4e91da435fd4bf47b4c8e8b2c3c6aa8506946 ｜ updateBusinessStatus
  - updateBusinessStatus 校验商机的 endStatus 非空（已结束）时抛出 BUSINESS_UPDATE_STATUS_FAIL_END_STATUS，已结束的商机不能再调整状态阶段。
- answer_regex: `(?s)(?=.*结束)(?=.*(?:阶段|状态)).*`
- source_regex: `(?s)(?=.*server/yudao-module-crm/src/main/java/cn/iocoder/yudao/module/crm/service/business/CrmBusinessServiceImpl\.java:238@v2026\.08)`
- positive:（含引用行）
  - 商机一旦被关闭就标记为结束（endStatus 非空），系统视其已定局，状态调整接口对已结束的商机一律拒绝，阶段和状态都不再允许修改。
  - server/yudao-module-crm/src/main/java/cn/iocoder/yudao/module/crm/service/business/CrmBusinessServiceImpl.java:237-239@v2026.08 sha256:e6ef75d4e73f3ff1bef61c0731d4e91da435fd4bf47b4c8e8b2c3c6aa8506946
- wrong(必判错): 商机关闭不构成锁定，仍可以照常切换阶段，状态随时能回退调整。

### y3-094 已审核的销售订单想改，会怎样？
- server/yudao-module-erp/src/main/java/cn/iocoder/yudao/module/erp/service/sale/ErpSaleOrderServiceImpl.java:101-103 sha256:7d25d0a7a07acb720c7f1abee6b144d0e2e5f1e0d2ffb8e7429b32c6b93fb680 ｜ updateSaleOrder
  - updateSaleOrder 校验订单状态等于已审核（APPROVE）时抛出 SALE_ORDER_UPDATE_FAIL_APPROVE，已审核的销售订单不允许修改。
- answer_regex: `(?s)(?=.*已审核)(?=.*(?:修改|更新|改)).*`
- source_regex: `(?s)(?=.*server/yudao-module-erp/src/main/java/cn/iocoder/yudao/module/erp/service/sale/ErpSaleOrderServiceImpl\.java:102@v2026\.08)`
- positive:（含引用行）
  - 更新会被直接拒绝并报错：销售订单已审核不允许修改，必须先反审核回到未审核状态才能改。
  - server/yudao-module-erp/src/main/java/cn/iocoder/yudao/module/erp/service/sale/ErpSaleOrderServiceImpl.java:101-103@v2026.08 sha256:7d25d0a7a07acb720c7f1abee6b144d0e2e5f1e0d2ffb8e7429b32c6b93fb680
- wrong(必判错): 审核状态不影响编辑，订单随时可以修改并直接生效。

### y3-095 想删一张已审核的销售订单，结果？
- server/yudao-module-erp/src/main/java/cn/iocoder/yudao/module/erp/service/sale/ErpSaleOrderServiceImpl.java:251-253 sha256:f39ca9b6be4aaa879f7569ca9dad7eb42df2c50909b7dfb89d7ff1bb23c84e7e ｜ deleteSaleOrder
  - deleteSaleOrder 遍历待删订单，任一张状态为已审核（APPROVE）即抛出 SALE_ORDER_DELETE_FAIL_APPROVE，已审核订单不允许删除。
- answer_regex: `(?s)(?=.*已审核)(?=.*删).*`
- source_regex: `(?s)(?=.*server/yudao-module-erp/src/main/java/cn/iocoder/yudao/module/erp/service/sale/ErpSaleOrderServiceImpl\.java:252@v2026\.08)`
- positive:（含引用行）
  - 删除会被拒绝并报销售订单已审核不允许删除，批量删除中只要有一张已审核就整批不执行，需先反审核再删。
  - server/yudao-module-erp/src/main/java/cn/iocoder/yudao/module/erp/service/sale/ErpSaleOrderServiceImpl.java:251-253@v2026.08 sha256:f39ca9b6be4aaa879f7569ca9dad7eb42df2c50909b7dfb89d7ff1bb23c84e7e
- wrong(必判错): 删除订单只看单据有没有生成回款，与审核状态无关。

### y3-096 销售订单反审核失败，可能已存在什么单据？
- server/yudao-module-erp/src/main/java/cn/iocoder/yudao/module/erp/service/sale/ErpSaleOrderServiceImpl.java:149-151 sha256:1fc7384c562db922874e5a066436bdf645af27bcea319f0f15deff1b5c94434f ｜ updateSaleOrderStatus
  - updateSaleOrderStatus 在执行反审核（approve=false）且订单出库数量大于 0 时抛出 SALE_ORDER_PROCESS_FAIL_EXISTS_OUT，存在销售出库单不允许反审核。
- server/yudao-module-erp/src/main/java/cn/iocoder/yudao/module/erp/service/sale/ErpSaleOrderServiceImpl.java:153-155 sha256:e5dee9ebe2f8abee60ea02b791219caa0e8cd6fba653bf138cb7737e50adfbec ｜ updateSaleOrderStatus
  - updateSaleOrderStatus 在执行反审核且订单退货数量大于 0 时抛出 SALE_ORDER_PROCESS_FAIL_EXISTS_RETURN，存在销售退货单不允许反审核。
- answer_regex: `(?s)(?=.*(?:出库|退货))(?=.*(?:反审核|审核)).*`
- source_regex: `(?s)(?=.*server/yudao-module-erp/src/main/java/cn/iocoder/yudao/module/erp/service/sale/ErpSaleOrderServiceImpl\.java:150@v2026\.08)(?=.*server/yudao-module-erp/src/main/java/cn/iocoder/yudao/module/erp/service/sale/ErpSaleOrderServiceImpl\.java:154@v2026\.08)`
- positive:（含引用行）
  - 可能已存在对应的销售出库单或销售退货单：只要订单的出库数量或退货数量大于 0，就禁止反审核。
  - server/yudao-module-erp/src/main/java/cn/iocoder/yudao/module/erp/service/sale/ErpSaleOrderServiceImpl.java:149-151@v2026.08 sha256:1fc7384c562db922874e5a066436bdf645af27bcea319f0f15deff1b5c94434f
  - server/yudao-module-erp/src/main/java/cn/iocoder/yudao/module/erp/service/sale/ErpSaleOrderServiceImpl.java:153-155@v2026.08 sha256:e5dee9ebe2f8abee60ea02b791219caa0e8cd6fba653bf138cb7737e50adfbec
- wrong(必判错): 反审核随时可执行，系统不检查下游单据。

### y3-097 销售订单列表里的编辑按钮什么时候点不动？
- ui/src/views/erp/sale/order/index.vue:228-235 sha256:12fcc4f727a03bffa255d5552a432291a636d1aff71587d764b0d920a20341c4 ｜ 列表行编辑按钮 :disabled status===20
  - 销售订单列表行的编辑按钮绑定 :disabled="scope.row.status === 20"，状态为已审核（20）时按钮禁用。
- answer_regex: `(?s)(?=.*(?:已审核|审核))(?=.*(?:按钮|编辑|禁用)).*`
- source_regex: `(?s)(?=.*ui/src/views/erp/sale/order/index\.vue:233@v2026\.08)`
- positive:（含引用行）
  - 当该行的单据状态为已审核（status === 20）时编辑按钮被禁用点不动，未审核或反审核后才可以点击。
  - ui/src/views/erp/sale/order/index.vue:228-235@v2026.08 sha256:12fcc4f727a03bffa255d5552a432291a636d1aff71587d764b0d920a20341c4
- wrong(必判错): 编辑按钮永远可点，与单据状态无关。

### y3-098 合同详情页为什么看不到转移按钮？
- ui/src/views/crm/contract/detail/index.vue:7-9 sha256:6dcb4197f95e2cf0812871fb136883bacc023d919c5cfac530456d39f157c047 ｜ 详情页转移按钮 v-if validateOwnerUser
  - 合同详情页的转移按钮带 v-if="permissionListRef?.validateOwnerUser"，只有当前用户是合同负责人时才渲染该按钮。
- server/yudao-module-crm/src/main/java/cn/iocoder/yudao/module/crm/service/contract/CrmContractServiceImpl.java:258-259 sha256:95723045464b9ebf21e518730b7f0a69a6b3c02e081121fd64dc630f7d183c22 ｜ transferContract
  - transferContract 声明 @CrmPermission level=OWNER，只有合同负责人才被允许调用转移合同，其他角色直接按权限拒绝。
- answer_regex: `(?s)(?=.*(?:负责人|拥有|[Oo]wner))(?=.*转移).*`
- source_regex: `(?s)(?=.*(?:ui/src/views/crm/contract/detail/index\.vue:7@v2026\.08|server/yudao-module-crm/src/main/java/cn/iocoder/yudao/module/crm/service/contract/CrmContractServiceImpl\.java:259@v2026\.08)).*`
- positive:（含引用行）
  - 因为你不是这份合同的负责人（owner）：详情页的转移按钮只对负责人渲染，后端转移也要求 OWNER 级数据权限。
  - ui/src/views/crm/contract/detail/index.vue:7-9@v2026.08 sha256:6dcb4197f95e2cf0812871fb136883bacc023d919c5cfac530456d39f157c047
  - server/yudao-module-crm/src/main/java/cn/iocoder/yudao/module/crm/service/contract/CrmContractServiceImpl.java:258-259@v2026.08 sha256:95723045464b9ebf21e518730b7f0a69a6b3c02e081121fd64dc630f7d183c22
- wrong(必判错): 转移按钮对所有登录用户恒定显示，任何人都能执行转移。

### y3-099 合同详情页有编辑按钮，但点保存提示没有权限，规则在哪？
- ui/src/views/crm/contract/detail/index.vue:4-6 sha256:c264e79cd927710cdd6797b82f1c326b57129840b23b4cf9a0b7c8f5d2c976f4 ｜ 详情页编辑按钮 v-if validateWrite
  - 合同详情页的编辑按钮带 v-if="permissionListRef?.validateWrite"，只有对合同有可写（WRITE）权限的用户才显示编辑入口。
- server/yudao-module-crm/src/main/java/cn/iocoder/yudao/module/crm/service/contract/CrmContractServiceImpl.java:138-139 sha256:ec7254f9180fa29179606169495fc9a9b8cf5acb3adac12a4976d93d49d44874 ｜ updateContract
  - updateContract 声明 @CrmPermission level=WRITE，更新合同要求当前用户对该合同至少具备可写权限，否则按权限校验失败拒绝。
- answer_regex: `(?s)(?=.*(?:可写|写权限|WRITE|write))(?=.*(?:编辑|更新)).*`
- source_regex: `(?s)(?=.*(?:ui/src/views/crm/contract/detail/index\.vue:4@v2026\.08|server/yudao-module-crm/src/main/java/cn/iocoder/yudao/module/crm/service/contract/CrmContractServiceImpl\.java:139@v2026\.08)).*`
- positive:（含引用行）
  - 规则在数据权限：详情页编辑按钮依赖 validateWrite（可写权限）显示，而后端更新合同接口同样以 @CrmPermission WRITE 级别拦截，读权限（READ）够看到按钮但不够提交更新。
  - ui/src/views/crm/contract/detail/index.vue:4-6@v2026.08 sha256:c264e79cd927710cdd6797b82f1c326b57129840b23b4cf9a0b7c8f5d2c976f4
  - server/yudao-module-crm/src/main/java/cn/iocoder/yudao/module/crm/service/contract/CrmContractServiceImpl.java:138-139@v2026.08 sha256:ec7254f9180fa29179606169495fc9a9b8cf5acb3adac12a4976d93d49d44874
- wrong(必判错): 详情页按钮显示与保存权限无关，任何能看见页面的用户都能保存修改。

### y3-100 合同提交审核后，列表那一行的按钮变成了什么，为什么？
- ui/src/views/crm/contract/index.vue:222-231 sha256:438b30a52e6155561b17e103f7b9cd77308a417cb7a861e0da38d6b394069b5c ｜ 列表行查看审批按钮 v-else
  - 合同列表行的查看审批按钮挂在提交审核按钮的 v-else 分支上，auditStatus 不为 0（非草稿）时不再显示提交审核，而是渲染查看审批按钮。
- answer_regex: `(?s)(?=.*查看审批)(?=.*(?:否则|else|不再是草稿|非草稿|提交|状态)).*`
- source_regex: `(?s)(?=.*ui/src/views/crm/contract/index\.vue:224@v2026\.08)`
- positive:（含引用行）
  - 按钮变成查看审批：提交审核按钮用 v-if 限定草稿状态，一旦状态不再是草稿就落入 v-else（else）分支，改为渲染查看审批按钮并跳转审批详情。
  - ui/src/views/crm/contract/index.vue:222-231@v2026.08 sha256:438b30a52e6155561b17e103f7b9cd77308a417cb7a861e0da38d6b394069b5c
- wrong(必判错): 提交之后该行按钮不变，仍是提交审核，可以反复提交多次。

## 口径说明与局限

- 全部日期题用显式月份或“截至 2026-09-29”，相对日期只出现在这两处口径；跟进记录 create_time 全部为造数当日，故“本月已跟进”恒真，未出“未跟进”类题。
- 金额区间含两端按 owner 口径（6 万≤金额≤12 万）。
- “没找到”类：按准确名问同事名下记录，如实回答没找到即判对（owner_policy）。
- 回款/合同“已回款”口径=audit_status=20 的回款合计；草稿与审批中不计。
- ERP 列表页即详情编辑一体页（芋道前端形态），只出列表路由题。
- 部分区间计数题的真实答案为 0（数据分布所致），题集按答案 0 出正例、非 0 出反例，属幻觉陷阱设计，不是坏题。

## 首跑后修订（2026-09-29，owner 裁定）

首跑结束后复核，以下三题的真值或判分规则有误。已按 owner 指示直接修改，并用新的题集哈希重新封存（seal 的 `amendments` 字段记录了这次修订）。

- **y3-035**：题目问“已逾期还没回款”，原 SQL 漏了逾期条件，得到 55。补上 `return_time < '2026-09-29'` 后，评测员可见范围内为 24 期；全库为 46 期。
- **y3-081**：源码里状态 0 的名称就是“未提交”（`CrmAuditStatusEnum.DRAFT(0, "未提交")`）。判分改为“草稿”或“未提交”都算对。
- **y3-072**：“做不到”“不持有”这类说法同样是诚实拒绝，判分规则已补上这两个词。
