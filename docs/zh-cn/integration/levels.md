---
title: L0–L3 接入指南
description: 将 PageHelm 分级接入自有 Web 应用：接入前提、L0 面板嵌入、L1 同账号读取、L2 领域消歧与 L3 核验写入。
---

业务系统可以按投入逐步接入 PageHelm，不必一次性改造全部后端接口。

> 编程助手接入指引：可加载 `.agents/skills/integrate-pagehelm/SKILL.md`（`$integrate-pagehelm`），按只读勘察 → L0–L1 先跑通 → 按需开启 L2/L3 的顺序协助接入，并内置身份校验与写入安全红线检查。

## 接入前提

开始接入前，请确认目标后台满足三项基本条件：

- **同源部署**：PageHelm 网关（默认前缀 `/woa-agent`）与宿主后台处于同一浏览器源（Origin），便于同源会话交换与页面操作。
- **服务端可校验登录态**：宿主后端能根据当前请求 Cookie 或请求头反查操作员身份，用于 `/bootstrap` 签发短期会话能力。
- **前端可引入单脚本**：允许页面加载同源 ES 模块 `assistant.js` 与 `PageController`。如果使用 Vue 3 + Element Plus，可直接复用仓库内置的组件适配层；其他前端框架按同一接口补充适配即可。

## 一、L0–L3 分级总览

| 等级 | 宿主侧接入工作 | 解锁能力 | 仓内参考 |
| --- | --- | --- | --- |
| **L0** | 挂载 `/v1/runtime/assistant.js` 与 `/bootstrap` | Shadow DOM 面板、有界 DOM 观测（`<= 200`）、受控导航 | `frontend/src/integrations/element-plus/` |
| **L1** | 实现 `ApplicationDescriptor`、身份、路由与只读端口 | 同账号查询、`TurnTables` 内存 SQL、`<DataTable>` 表格输出 | `testapps/contacts/adapter.py` |
| **L2** | 补充 `domain_playbooks/*.md` 与 `entity_rules` | 业务术语消歧、前置条件说明、可点选 `need_input` 选项胶囊 | `examples/yudao/` |
| **L3** | 接入写入账本、后置核验与写入契约 | 类型化写入、对称 `update_record`、原生表单提交与撤销预填 | `src/web_operation_agent/business/write_pipeline.py` |

## 二、L0：面板嵌入与前端组件适配

在宿主页面引入受版本控制的同源脚本：

```html
<script
  type="module"
  src="/woa-agent/v1/runtime/assistant.js"
  data-woa-bootstrap="/woa-agent-boot/bootstrap"
></script>
```

页面加载后，Runtime 请求 `data-woa-bootstrap`。宿主在服务端校验当前操作员登录态，调用 `EmbeddedGateway.bootstrap_config(...)` 返回短期会话配置；前端代码与 HTML 中不包含模型密钥。宿主同源同时提供锁定版本的 `PageController` ES 模块（导出 `PageController` 与 `getElementByIndex`）。

### 复用或编写 UI 组件适配层

通用 Runtime（`frontend/src/runtime/`）不依赖任何组件库类名。若宿主使用 Vue 3 + Element Plus，可直接复用 `frontend/src/integrations/element-plus/` 下的 5 个模块：

- `table.js`：解析列表表头、可见行与行实体标识。
- `select.js`：采集挂载在 `body` 下的可见下拉选项（`readElSelectVisibleCandidates`），并避免内部输入框重复聚焦（`shouldAvoidSelectInnerInput`）。
- `date-picker.js`：识别日期与日期区间控件（`isElDateRangeInput`）并触发合规变更事件。
- `form.js`：读取表单标签及抽屉避让位移 `--woa-drawer-offset`（遍历上限：`depth: 8`、`nodes: 6000`、`bytes: 200000`）。
- `vue.js`：同步框架受控组件状态。

若使用其他前端框架，在 `frontend/src/integrations/<app>/` 下实现相同接口，导出 `readElementPageControl`、可选 `openPage(url)` 与 `readPageBlocker()`。

## 三、L1：同账号只读后端适配器

`testapps/contacts/adapter.py` 提供了一个与 `yudao` 结构完全不同的最小 REST/HTML 适配器参考，包含两部分接入工作：

1. **声明 `ApplicationDescriptor`**：绑定 `application_id`、`public_origin`、`navigation_mode`（`"document"` 或 `"spa"`）、`page_controller_module` 与 `business_tools.catalog_sha256`，用于校验前后端版本匹配。
2. **通过 `bind_application(adapter)` 装配 `ApplicationPorts`**：
   - `IdentityPort`：从可信同源会话头或 Cookie 映射 `PrincipalContext(tenant_id, actor_id, scopes)`，不信任前端上报的用户 ID 或 DOM 文本。
   - `ApplicationRoutePort`：校验站内路由合法性，并声明须在日志与观测前擦除的 `session_handoff_query_keys`。
   - `EntityReadTools` / `ReferenceLookupTools` / `PageEntryTools` / `GoalCandidateTools`：注册只读工具，返回 `QueryEvidence`（`matched_count`、`count_exact`、`looked_for`、`exact_matches`），并结合 `bind_matching_identity` 对接 `TurnTables` SQL 或 `<DataTable>`。
   - `GoalPort.capabilities()`：按实际实现声明可选能力，不声明未支持的能力（完整枚举见 [内置工具、能力与自检](../reference/tools-and-capabilities.md)）。

## 四、L2：业务口径手册与消歧交互

L2 用于处理同一词在不同业务模块中含义不同的情况，以及同名记录定位：

- **业务域手册（`domain_playbooks/*.md`）**：按模块编写口径说明（例如区分“CRM 客户”“线索”和“ERP 往来客户”对应的实体与统计口径），由模型通过 `load_domain_guide` 按需加载。
- **编译期规则（`entity_rules`）**：在白名单 `SourceTree` 下提取服务端校验前置条件，便于助手准确解释某项删除或审核被拒绝的原因。
- **消歧选项胶囊**：名称检索命中 2–4 条同账号可见的真实记录时，模型调用 `finish(status="need_input", options=[...])` 在输入区上方渲染可点选按钮，由操作员明确选定目标后再继续。

## 五、L3：核验写入与表单交接

仅在 L1 的只读查询与身份隔离通过验证后，再按需启用写入模式（`mode = "verified-operate"`）。所有写入路径均由统一的 `WritePipeline` 管控（完整阶段与安全约束见 [写入安全与隐私边界](../security/verified-writes.md)），接入方式有三种：

1. **类型化写入 Skill（`SKILL.md` + `agents/woa.toml`）**：适合有明确业务规则的多步骤写入。
2. **对称平铺约定更新（`ConventionWritePort`）**：在 `GoalPort.capabilities()` 中声明 `"updates"` 后，对读写 1:1 对称、无嵌套明细且不含密钥字段的实体自动派生 `update_record` 与两态可逆 `toggle_record_status`。
3. **原生表单提交与撤销交接（`SubmitPort`）**：通过 `SubmitConventions` 接入现有页面表单；满足条件时经审批后单次点击提交并回读，遇到密码/上传控件或用户要求自行提交时切换为可撤销的预填交接（`undoFormFill`）。

三种写入方式的具体编写规范见 [Skill 与类型化写入](./skills-and-writes.md)。

## 适配器合入前核对

合入新适配器前，使用合成数据执行免付费模型校验：

- 中性边界检查：`scripts/check_core_boundary.py`
- 适配器契约与单测：`scripts/check.py`
- 浏览器端到端回归：L0 面板挂载、L1 表格筛选、L2 追问选项、L3 审批派发与回读核验
