---
title: 内置工具与能力参考
description: 模型可见浏览器操作、同主体读取与 Decimal/SQL 分析、Skill 与原生提交工具全集，以及 GoalPort 能力契约和 ProgramCheckSpec 程序化自检表。
---

> **概要：** PageHelm 中模型每一步能看到哪些工具完全由服务端依据会话事实状态动态投影（`execution/tools.py` `ToolSurface`），不由提示词拍脑袋决定。业务适配器通过 `GoalPort.capabilities()` 声明实际支持的可选能力，每个回合结束前还必须通过严格的代码级 `ProgramCheckSpec` 核验。

[English](../../reference/tools-and-capabilities.md)

## 一、模型可见工具全集

### 浏览器动作与页面状态工具（`TOOL_SCHEMAS` / `ToolSurface`）

| 工具名 | 核心参数 | 用途与安全边界 |
| --- | --- | --- |
| `observe` | `{}` | 采集当前页有界 DOM 快照（最多 `200` 个元素），只读不修改页面 |
| `click` | `ref`，可选 `expect_navigation` | 仅可点击最新 `epoch` 观测到的元素句柄；非 JS 触发跳转勿设 `expect_navigation` |
| `fill` | `ref`、`value` | 向文本控件输入值；密码及受限凭据框将被跳过并移交人工 |
| `select` | `ref`、`value`，可选 `option_label` | 选择原生 `<select>` 或按可见文案匹配单选下拉选项 |
| `submit` | `ref`，可选 `expect_navigation` | 提交所属原生表单（严格受执行模式与审批门禁限制） |
| `goto` | `url` | 仅允许跳转到当前页精确链接或同主体工具返回并获准的 `ui_path` |
| `back` | `{}` | 返回浏览器上一历史页面 |
| `scroll` | `direction`（`up`\|`down`），可选 `pages` | 在 `MAX_SCROLL_PAGES` 上限内滚动视口并带回新观测 |
| `wait_for` | 可选 `ms`（`0..10000`） | 有界等待异步加载并重新观测页面 |
| `highlight` | `ref`（`^e[0-9]+$`） | 在界面上高亮提示某个控件并结束本轮，由操作员亲自完成点击 |
| `set_target` | `path`、`selected`、`filters`、`query`、`groups`、`filters_absent` | 动作前显式声明预期页面终态，供服务端校验是否真正到达 |
| `apply_facet` | `ref`、`active` | 开关页面已声明的筛选标签或分组控件（需开启 `allow_page_facets`） |
| `enter_application` | `ref` | 跟随主应用跳转链接并自动剥离会话接力密钥参数 |
| `prefill_form` | `{}` | 应用服务端签发的瞬时表单草稿；绝不代为提交 |
| `finish` | `done`、`why` 或 `outcome`、`answer`（及 `table_ref`、`status="need_input"`、`options`） | 携带已核验证据、`<DataTable>` 表引用或消歧按钮优雅收口 |

*（内部动作 `handoff_form`、`reveal`、`form_model`、`form_submit` 仅由服务端安全流水线直接下发，不对模型开放自由参数调用。）*

---

### 同主体数据读取、`Decimal` 计算与内存 SQL（`TurnTables`）

- **`inspect_data(query, entity?)`**：检索当前登录主体有权访问的数据实体、字段显示标签与只读页面范围（仅元数据，不能当作业务行事实）。
- **`resolve_data(deliverables[])`**：调用确定性解析器一次执行 1–4 项 `fact`、`list` 或 `aggregate`（支持 `count`、`sum`、`min`、`max` 及严格筛选条件）。
- **`compute(operation, dataset_ref, ...)`**：在当前服务端实证数据集上执行 `Decimal` 精确运算（`filter`、`count`、`sum`、`min`、`max`、`argmin`、`argmax`）。
- **单轮临时 `TurnTables`（`turn_tables.py`）**：支持表分析的适配器将分页接口数据导入进程内只读 SQLite 表（`t1`, `t2`… 及查询缓存 `q1`, `q2`…），仅放行单条只读 `SELECT` / `WITH`（工具名 `sql`）与 `date_range` 辅助计算，随后通过 `finish(table_ref=...)` 零转抄输出交互式 `<DataTable>`。

---

### 追问、能力发现、Skill 与原生表单提交工具

| 工具名 | 参数 | 说明 |
| --- | --- | --- |
| `ask_user` | `question`、`response_kind`、`choice_ids` | 因缺少必填条件或目标歧义而暂停追问；回复仅作任务输入，不等同写审批 |
| `withdraw_request` | `{}` | 操作员回复“不用了/算了”时干净撤回待定追问 |
| `check_capability` | `{}` | 当请求涉及持久化修改但当前无写工具时，全面检索主体已装写入能力后再报告能力缺口 |
| `load_skill` | `skill_id` | 按需加载已打包的 `SKILL.md` 领域指南；本身不授予任何工具或权限 |
| `enter_skill_app` / `open_skill_route` | `skill_id`、`bindings?` | 按服务端核验后的 Skill 只读路由进入应用模块或详情页 |
| `request_submit` | `index` | 触发 `WritePipeline` 的 `run_submit_pipeline`：读取整表数据、校验值来源、展示审批卡、批准后点击一次并回读核验 |

---

### 按需启用的 `SourceTree` 源码检索工具

仅当宿主启动时显式注入白名单 `SourceTree`（`runtime_source=True`）时开放：

- `lookup_source(query)`：在白名单文件内词法检索（返回受限 `source#N` 摘要）。
- `lookup_source_history(kind, ...)`：读取受限本地 Git 元数据（`owners` / `blame` / `commits` / `changes`），生成 `history#N`。
- `read_source(path, start_line, end_line)`：按行范围只读检索白名单源码文件。
- `open_source_route(url)` / `open_source_candidate(candidate_id, bindings)`：仅允许打开经白名单源码证实为只读查看的页面路由。

---

## 二、`GoalPort` 能力声明契约

`src/web_operation_agent/goal/ports.py` 将数据端口划分为**核心必填能力**与通过 `capabilities()` 显式声明的**可选扩展能力**（适配器未声明时核心自动解释限制，绝不伪造支持）：

### 必填基线能力（`REQUIRED`）

`index` · `records` · `facts` · `aggregates` · `pages`

### 可选能力矩阵（`OPTIONAL`）

| 能力键 | 声明后解锁的系统行为 |
| --- | --- |
| `facets` | 页面筛选项与分组视图切换（`apply_facet`、`set_target` 筛选态校验） |
| `paging` | 确定性列表翻页控制与全集覆盖率标记 |
| `sections` | 复杂详情页/多标签列表页的子分区切换 |
| `lines` | 主从单据内嵌明细行表读取 |
| `handoffs` | 应用内接力 URL 敏感查询参数自动擦除 |
| `fill` | 交接前结构化表单预填与一键撤销（`undoFormFill`） |
| `search` | 绑定页面原生搜索栏并核验已应用搜索条件摘要 |
| `source` | 白名单源码规则检索与只读路由候选 |
| `values` | 发起修改提案前按当前操作员身份检索合法候选值 |
| `updates` | 对称平铺 `ConventionWritePort`（`update_record` 与可逆 `toggle_record_status`） |

---

## 三、程序化回合核验规格（`ProgramCheckSpec`）

系统在向操作员渲染最终结果或落实副作用前，依据 `TurnTrace` 与证据链自动执行代码级检查，拒绝幻觉数字或越界声明：

### 核心级检查（`CORE_CHECK_SPECS`，共 7 项）

| 检查项 | 拦截条件与约束目标 |
| --- | --- |
| `untraced_numbers` | 禁止回复正文出现未由同轮读取、计算或 `<DataTable>` 证据支撑的孤立数值 |
| `model_delivery_rejected` | 拦截格式残缺或违反答案 Schema 的 `finish` 负载并触发有界修复 |
| `completion_review` | 核验复合诉求的所有子目标均已完成或明确标注未完成部分 |
| `slim_verify` | 对即将下发给用户的最终正文与证据绑定执行一致性终审 |
| `failed_read_repeated` | 防止相同失败参数在同一轮内无意义重复试错 |
| `same_step_stalled` | 识别页面指纹零变化的原地停滞循环 |
| `model_answer_not_delivered` | 确保正常结束的回合一定产出可展示给操作员的交付内容 |

### 适配器扩展策略检查（以参考适配器 15 项 `ProgramCheckSpec` 为例）

- **读写边界与草稿纪律：** `read_only_question`、`draft_only`、`dangerous_unconfirmed`、`unrequested_draft_field`、`draft_handoff_required`
- **交付完整性与界面一致性：** `delivery_sensitive`、`delivery_missing_receipt`、`delivery_foreign_receipt`、`delivery_page_handoff`、`delivery_page_home`、`delivery_dialog_missing`、`delivery_page_masked`、`delivery_write_unverified`、`delivery_page_obligation`、`delivery_incomplete`
