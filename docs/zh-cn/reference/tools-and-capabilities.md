---
title: 内置工具、能力与自检
description: 模型可见工具目录、GoalPort 能力声明契约与 ProgramCheckSpec 回合核验规格。
---

模型每一步可见的工具集合由服务端根据当前会话状态和权限动态投影（`execution/tools.py` `ToolSurface`）。业务适配器通过 `GoalPort.capabilities()` 声明实际支持的可选能力，每个回合结束前须通过代码级 `ProgramCheckSpec` 检查。

## 一、模型可见工具目录

### 浏览器动作与页面工具（`TOOL_SCHEMAS` / `ToolSurface`）

| 工具名 | 核心参数 | 用途与边界 |
| --- | --- | --- |
| `observe` | `{}` | 采集当前页有界 DOM 快照（最多 `200` 个元素），只读 |
| `click` | `ref`，可选 `expect_navigation` | 仅可点击最新 `epoch` 观测到的元素句柄；非 JS 导航不设 `expect_navigation` |
| `fill` | `ref`、`value` | 向文本控件输入值；密码及受限凭据框跳过并交回人工 |
| `select` | `ref`、`value`，可选 `option_label` | 选择原生 `<select>` 或按可见文案匹配单选下拉项 |
| `submit` | `ref`，可选 `expect_navigation` | 提交原生表单（受安全模式与审批门禁限制） |
| `goto` | `url` | 仅允许跳转到当前页精确链接或同账号工具返回的 `ui_path` |
| `back` | `{}` | 返回浏览器上一历史页面 |
| `scroll` | `direction`（`up`\|`down`），可选 `pages` | 在 `MAX_SCROLL_PAGES` 上限内滚动视口并返回新观测 |
| `wait_for` | 可选 `ms`（`0..10000`） | 有界等待异步加载并重新观测 |
| `highlight` | `ref`（`^e[0-9]+$`） | 高亮提示某个控件并结束本轮，由操作员自行点击 |
| `set_target` | `path`、`selected`、`filters`、`query`、`groups`、`filters_absent` | 动作前声明预期页面终态，供服务端校验是否到达 |
| `apply_facet` | `ref`、`active` | 开关页面声明的筛选标签或分组控件（需开启 `allow_page_facets`） |
| `enter_application` | `ref` | 跟随主应用跳转链接并剥离会话接力参数 |
| `prefill_form` | `{}` | 应用服务端签发的瞬时表单草稿，不代为提交 |
| `finish` | `done`、`why` 或 `outcome`、`answer`（及 `table_ref`、`status="need_input"`、`options`） | 携带证据、`<DataTable>` 表引用或消歧选项结束本轮 |

内部动作 `handoff_form`、`reveal`、`form_model`、`form_submit` 仅由服务端安全流水线直接下发，不对模型开放自由调用。

### 同账号数据读取与计算工具

- **`inspect_data(query, entity?)`**：检索当前登录人可访问的数据实体、字段标签与只读页面范围（仅元数据，不作为业务行事实）。
- **`resolve_data(deliverables[])`**：通过确定性解析器一次执行 1–4 项 `fact`、`list` 或 `aggregate`（`count`、`sum`、`min`、`max` 及筛选条件）。
- **`compute(operation, dataset_ref, ...)`**：在服务端已读取数据集上执行 `Decimal` 运算（`filter`、`count`、`sum`、`min`、`max`、`argmin`、`argmax`）。
- **`sql` 与 `date_range`（`TurnTables`）**：在绑定内存 SQLite 表的适配器上执行单条只读 `SELECT` / `WITH` 查询，随后通过 `finish(table_ref=...)` 渲染 `<DataTable>`。

### 追问、能力发现、Skill 与表单提交工具

| 工具名 | 参数 | 说明 |
| --- | --- | --- |
| `ask_user` | `question`、`response_kind`、`choice_ids` | 缺少必填条件或存在目标歧义时暂停追问；用户回复只作任务输入，不等同写审批 |
| `withdraw_request` | `{}` | 操作员取消请求时撤回待定追问 |
| `check_capability` | `{}` | 涉及数据修改但当前无可用写工具时，检查主体已装写入能力后再说明限制 |
| `load_skill` | `skill_id` | 按需加载 `SKILL.md` 领域规程；本身不授予工具或权限 |
| `enter_skill_app` / `open_skill_route` | `skill_id`、`bindings?` | 按核验后的 Skill 只读路由进入模块或详情页 |
| `request_submit` | `index` | 触发 `WritePipeline.run_submit_pipeline`：读取整表、校验值来源、展示审批卡，批准后点击一次并回读 |

### 按需启用的 `SourceTree` 源码检索工具

仅在宿主启动时显式注入白名单 `SourceTree`（`runtime_source=True`）时可用：

- `lookup_source(query)`：在白名单文件内词法检索（返回受限 `source#N` 摘要）。
- `lookup_source_history(kind, ...)`：读取受限本地 Git 元数据（`owners` / `blame` / `commits` / `changes`），返回 `history#N`。
- `read_source(path, start_line, end_line)`：按行范围只读读取白名单源码文件。
- `open_source_route(url)` / `open_source_candidate(candidate_id, bindings)`：仅允许打开经白名单源码证实为只读查看的页面路由。

## 二、`GoalPort` 能力声明契约

`src/web_operation_agent/goal/ports.py` 将数据端口划分为必填能力与通过 `capabilities()` 显式声明的可选能力。适配器未声明某项可选能力时，核心直接向用户说明不支持，适配器不应伪造声明。

- **必填能力（`REQUIRED`）**：`index`、`records`、`facts`、`aggregates`、`pages`。
- **可选能力（`OPTIONAL`）**：

| 能力键 | 解锁行为 |
| --- | --- |
| `facets` | 页面筛选项与分组视图切换（`apply_facet`、`set_target` 筛选态校验） |
| `paging` | 确定性列表翻页控制与全集覆盖率标记 |
| `sections` | 详情页或多标签列表页的子分区切换 |
| `lines` | 主从单据内嵌明细行读取 |
| `handoffs` | 应用内接力 URL 敏感查询参数自动擦除 |
| `fill` | 交接前结构化表单预填与一键撤销（`undoFormFill`） |
| `search` | 绑定页面搜索栏并核验已应用的搜索条件摘要 |
| `source` | 白名单源码规则检索与只读路由候选 |
| `values` | 发起修改提案前按当前操作员身份检索合法候选值 |
| `updates` | 对称平铺 `ConventionWritePort`（`update_record` 与可逆 `toggle_record_status`） |

## 三、程序化回合核验（`ProgramCheckSpec`）

向操作员输出最终结果或推进副作用前，系统根据 `TurnTrace` 与证据链执行代码级检查：

### 核心级检查（`CORE_CHECK_SPECS`，7 项）

| 检查项 | 检查目标 |
| --- | --- |
| `untraced_numbers` | 拦截回复中未由同轮读取、计算或 `<DataTable>` 支撑的数值 |
| `model_delivery_rejected` | 拦截格式残缺或违反答案 Schema 的 `finish` 负载并触发有界修复 |
| `completion_review` | 核验复合诉求的各子目标均已完成或明确标出未完成部分 |
| `slim_verify` | 对最终交付正文与证据绑定做一致性终审 |
| `failed_read_repeated` | 阻止相同失败参数在同一轮内重复调用 |
| `same_step_stalled` | 识别页面指纹无变化的停滞循环 |
| `model_answer_not_delivered` | 确保正常结束的回合一定产出可展示的交付内容 |

### 适配器扩展检查（参考适配器 15 项示例）

- **读写边界与草稿纪律**：`read_only_question`、`draft_only`、`dangerous_unconfirmed`、`unrequested_draft_field`、`draft_handoff_required`
- **交付完整性与界面一致性**：`delivery_sensitive`、`delivery_missing_receipt`、`delivery_foreign_receipt`、`delivery_page_handoff`、`delivery_page_home`、`delivery_dialog_missing`、`delivery_page_masked`、`delivery_write_unverified`、`delivery_page_obligation`、`delivery_incomplete`
