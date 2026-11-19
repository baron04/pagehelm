---
title: L0–L3 渐进式接入指南
description: 从 L0 浏览器面板与 Element Plus 集成层起步，渐进接入 L1 同账号读取（testapps/contacts）、L2 领域指南与消歧，以及 L3 核验写入与表单交接。
---

> **概要：** 业务系统可按投入产出比四步渐进接入 PageHelm：**L0** 挂载 `assistant.js` 并复用仓内 `element-plus/` 集成层；**L1** 实现 `ApplicationDescriptor` 与 `ApplicationPorts` 提供同账号只读查询与 `<DataTable>`（参考 `testapps/contacts/adapter.py`）；**L2** 补充领域口径手册、编译期规则与 `need_input` 消歧胶囊；**L3** 启用经过评审的写入 Skill、对称 `ConventionWritePort` 更新与可撤销原生表单交接。

[English](../../integration/levels.md)

## 一、四级演进阶梯总览

```text
  L0：嵌入面板与 UI 组件辅助层       -> /v1/runtime/assistant.js + /bootstrap + PageController
         |
         v
  L1：同账号身份与类型化只读查询       -> ApplicationDescriptor + ApplicationPorts + QueryEvidence
         |                                （TurnTables 内存 SQL + <DataTable> + read_summary）
         v
  L2：领域口径消歧与规则提示          -> domain_playbooks/*.md + entity_rules + need_input 选项胶囊
         |
         v
  L3：核验写入与可撤销表单交接        -> WritePipeline + SKILL.md 写入包 + ConventionWritePort
                                          + SubmitPort / MutationPrefillOffer + undoFormFill
```

| 等级 | 所需宿主改造 | 解锁能力 | 仓内代码参照 |
| --- | --- | --- | --- |
| **L0** | 仅需 `/bootstrap` 会话鉴权交换 | Shadow DOM 面板、有界 DOM 观测（`<= 200`）、安全导航 | `frontend/src/integrations/element-plus/` |
| **L1** | `ApplicationDescriptor`、身份、路由与只读端口 | 同账号查询、`TurnTables` SQL、`<DataTable>` 直出 | `testapps/contacts/adapter.py` |
| **L2** | `domain_playbooks/*.md`、`entity_rules` | 业务黑话消歧、删除/状态前置条件解释、可点击 `need_input` 胶囊 | `examples/yudao/` 领域手册 |
| **L3** | 写入账本、后置核验与 Skill / `ConventionWritePort` / `SubmitPort` | 类型化写入、对称 `update_record`、原生提交与撤销预填 | `src/web_operation_agent/business/write_pipeline.py` |

---

## 二、L0：浏览器 Runtime 与 UI 组件集成层

宿主页面只需通过同源网关引入单个受版本控制的脚本：

```html
<script
  type="module"
  src="/woa-agent/v1/runtime/assistant.js"
  data-woa-bootstrap="/woa-agent-boot/bootstrap"
></script>
```

页面加载后，Runtime 自动请求 `data-woa-bootstrap`：宿主在服务端校验当前操作员登录态，调用 `EmbeddedGateway.bootstrap_config(...)` 下发短期会话令牌，HTML 与 JS 产物全程零模型密钥。宿主同源还需提供锁定的 `PageController` ES 模块（导出 `PageController` 与 `getElementByIndex`）。

### 复用 `frontend/src/integrations/element-plus/` 公共集成层

通用 Runtime（`frontend/src/runtime/`）严格保持零 `.el-*` 依赖。若目标后台采用 Vue 3 + Element Plus，可直接复用 `frontend/src/integrations/element-plus/` 内的五份公共辅助模块：

- `table.js`：解析列表表头、可见行内容与行实体身份绑定。
- `select.js`：采集挂载于 `body` 末尾的下拉候选（`readElSelectVisibleCandidates`），并规避内部输入框循环聚焦（`shouldAvoidSelectInnerInput`）。
- `date-picker.js`：识别日期与日期区间组件（`isElDateRangeInput`）并触发合规变更事件。
- `form.js`：受控读取表单标签与抽屉避让位移 `--woa-drawer-offset`（边界：`depth: 8`, `nodes: 6000`, `bytes: 200000`）。
- `vue.js`：辅助读取并同步框架受控组件状态。

若使用其他前端框架，可在 `frontend/src/integrations/<app>/` 下新建同级目录，导出 `readElementPageControl`、可选 `openPage(url)` 与 `readPageBlocker()`。

---

## 三、L1：最小只读后端适配器（`testapps/contacts/adapter.py`）

`testapps/contacts/adapter.py` 展示了与芋道（`yudao`）结构完全不同的轻量 REST/HTML 接入蓝图：

1. **声明 `ApplicationDescriptor`：** 绑定 `application_id`、`public_origin`、`navigation_mode`（`"document"` 或 `"spa"`）、`page_controller_module` 与 `business_tools.catalog_sha256`，防止陈旧脚本误调新版后端接口。
2. **通过 `bind_application(adapter)` 装配 `ApplicationPorts`：**
   - `IdentityPort`：从可信同源会话头/Cookie 映射 `PrincipalContext(tenant_id, actor_id, scopes)`，严禁信任前端上报的用户 ID 或页面 DOM 文本。
   - `ApplicationRoutePort`：校验站内路由合法性，并声明在日志与观测前必须脱敏的 `session_handoff_query_keys`。
   - `EntityReadTools` / `ReferenceLookupTools` / `PageEntryTools` / `GoalCandidateTools`：在 `BusinessToolRuntime` 注册只读工具，产出精确的 `QueryEvidence`（`matched_count`、`count_exact`、`looked_for`、`exact_matches`），并配合 `bind_matching_identity` 接入 `TurnTables` SQL 或 `<DataTable>` 直出。
   - `GoalPort.capabilities()`：按实际实现情况声明可选能力（`facets`、`paging`、`sections`、`lines`、`handoffs`、`fill`、`search`、`source`、`values`、`updates`），严禁伪造未支持能力。

---

## 四、L2：领域口径手册、编译规则与追问消歧

在不编写脆弱的逐页硬编码脚本前提下，系统化消除业务黑话歧义：

- **业务域手册（`domain_playbooks/*.md`）：** 按业务模块编写简明口径说明（例如区分“CRM 客户”“线索”与“ERP 往来客户”的所属实体和财务区间口径），由 `load_domain_guide` 按需懒加载。
- **编译期规则说明（`entity_rules`）：** 在安全 `SourceTree` 白名单下提取服务端校验前置条件，使助手能够准确向操作员解释为何某条删除或审核操作会受阻。
- **可点击消歧胶囊（`finish(status="need_input", options=[...])`）：** 当姓名或名称检索命中 2–4 条同账号可见真实记录时，在输入框上方展示已核验的 `input_options` 胶囊，点选即带明确主键进入下一轮，杜绝猜值操作。

---

## 五、L3：核验写入、对称约定更新与可撤销交接

在 L1 只读查询与身份隔离全部通过契约校验后，方可按需启用写入（`mode = "verified-operate"`）。三条写入路径共享同一 `WritePipeline` 安全底座：

1. **经评审的类型化写入 Skill（`SKILL.md` + `agents/woa.toml`）：** 包格式、两阶段 `prepare_mutation` / `execute_mutation` 协议与 `ResultPostconditionVerifier` 写法详见 [Skill 规范与类型化写入](./skills-and-writes.md)。
2. **扁平对称约定更新（`ConventionWritePort`）：** 声明 `"updates"` 能力后，对满足“同前缀 update/get 1:1 对称且不含嵌套明细/密钥字段”的实体自动派生 `update_record`，对严格两态可逆字典自动派生 `toggle_record_status`。
3. **原生表单提交与撤销交接（`SubmitPort`）：** 声明 `SubmitConventions` 后，`request_submit` 可将整表差异、目标与按钮投影成单次审批卡，批准后由程序点击一次并回读确认；对于复杂单据或含密码/上传框表单，自动切换为预填交接，并将焦点定位至受限控件，同时在面板保留“撤销本次自动填写”（`undoFormFill`）按钮。

### 适配器上线核对清单

合入新适配器代码前，请使用合成测试数据完成下列免付费模型校验：

- 运行中性边界守护：`scripts/check_core_boundary.py`
- 运行适配器契约套件：通过 `scripts/check.py` 执行对应测试集
- 覆盖浏览器端到端回归：`L0` 面板挂载、`L1` 表格即时过滤、`L2` 追问胶囊、`L3` 审批派发与回读核验
