---
title: L0–L3 分级接入指南
description: 从 L0 浏览器面板与仓内 Element Plus 集成层起步，渐进接入 L1 只读适配器（testapps/contacts）、L2 领域指南与 L3 核验写入及表单交接。
---

> **概要：** 应用按投入产出比四步渐进接入：**L0** 在宿主页面挂载 `assistant.js` 并复用仓内 `element-plus/` 集成层；**L1** 实现最小端口提供同账号读取与 `<DataTable>`（参考 `testapps/contacts`）；**L2** 补充领域口径手册、编译期规则与追问胶囊；**L3** 启用经过评审的写入 Skill、对称 `ConventionWritePort` 更新与原生表单提交/撤销交接。

[English](../../integration/levels.md)

## 四级接入路径总览

| 等级 | 所需宿主改造 | 解锁能力 | 仓内代码参照 |
| --- | --- | --- | --- |
| **L0** | 仅需 `/bootstrap` 会话鉴权交换 | Shadow DOM 面板、页面观察、导航与控件定位辅助 | `frontend/src/integrations/element-plus/` |
| **L1** | 身份、路由与只读数据端口 | 同账号分页读取、内存 `TurnTables` SQL 汇总与 `<DataTable>` | `testapps/contacts/adapter.py` |
| **L2** | 业务黑话手册与实体规则 | 模块级消歧、编译期删除/状态拒绝条件、可点击追问胶囊 | `examples/yudao/src/.../domain_playbooks/` |
| **L3** | 写入账本、后置核验与 Skill | Skill 写入、`update_record`/`toggle_record_status`、原生提交与可撤销预填 | `WriteSafetyPipeline`、`SubmitPort`、`YudaoHost` |

## L0：浏览器 Runtime 与 UI 组件集成层

宿主只需通过同源网关 `/v1/runtime/assistant.js` 引入单个受版本控制的静态脚本：

```html
<script
  type="module"
  src="/woa-agent/v1/runtime/assistant.js"
  data-woa-bootstrap="/woa-agent-boot/bootstrap"
></script>
```

宿主在 `/bootstrap` 端点验证当前登录用户身份，并调用服务端 `EmbeddedGateway.bootstrap_config(...)` 返回短期会话令牌，全程不暴露任何模型密钥。宿主同源还需托管已锁定的 `PageController` ES 模块（导出 `PageController` 与 `getElementByIndex`）。

### 复用仓内 `element-plus/` 公共集成层

若后台采用 Vue 3 + Element Plus，可直接复用 `frontend/src/integrations/element-plus/` 中已从芋道解耦出的公共组件识别与操作能力（`frontend/src/runtime/` 保持零 `.el-*` 选择器）：

- `table.js`、`select.js`、`date-picker.js`、`form.js`、`vue.js` 覆盖挂载于 `body` 末尾的弹层选项、表格行身份绑定、日期范围回调及并排抽屉避让变量 `--woa-drawer-offset`；
- `frontend/scripts/build-yudao.mjs` 负责把该公共层与特定应用的路由事实一并编译至 `frontend/dist/integrations/yudao/`。

采用其他 UI 组件库的应用可在 `frontend/src/integrations/<app>/` 新建同级目录，导出 `readElementPageControl`、可选 `openPage(url)` 与 `readPageBlocker()`。

## L1：最小后端适配器（`testapps/contacts` 蓝图）

`testapps/contacts/adapter.py` 提供了一个与芋道结构完全不同的轻量 REST/HTML 接入样本，核心步骤如下：

1. **声明版本化描述符（`ApplicationDescriptor`）：** 绑定 `application_id`、准确 `public_origin`、`navigation_mode`（`"document"` 或 `"spa"`）、同源 `page_controller_module` 以及 `business_tools.catalog_sha256`，防止陈旧脚本与后端端口版本失配。
2. **通过 `bind_application(adapter)` 装配 `ApplicationPorts`：**
   - `identity`（`ApplicationIdentityPort`）：由同源可信登录态反查 `PrincipalContext(tenant_id, actor_id, scopes)`，严禁轻信前端自报用户 ID 或 DOM 文本；
   - `routes`（`DeclaredRoutePort`）：校验站内路由并登记须在观测前脱敏的 `session_handoff_query_keys`；
   - `reads` / `data_classifier`：在 `BusinessToolRuntime` 注册只读查询并输出准确 `QueryEvidence`（`matched_count`、`count_exact`、`looked_for`、`exact_matches`），可将行数据装入 `TurnTables` 供模型运行只读 SQL 或直接在 `finish` 中下发 `<DataTable>`。
3. **契约先行验证：** 在接入付费大模型前，先跑通核心黑盒契约及 `Contacts` Chromium 集成测试。

## L2：领域口径指南、规则编译与消歧追问

在不写死逐页脚本的前提下消除企业业务黑话混淆：

- **业务域手册（`domain_playbooks/*.md`）：** 按业务域编写简明 Markdown 说明（如在商城与 CRM 模块下“客户”的对应实体差异、时间口径定义），由 `load_domain_guide` 按需懒加载。
- **编译期规则说明（`entity_rules`）：** 在构建或发布阶段从服务端源码白名单提取删除/状态前置条件，供模型准确解释操作被拒原因。
- **可点击追问胶囊（`UX-02`）：** 当 `find_record` 或只读检索返回 2–4 条同名同账号真实记录时，`finish(status="needs_input", input_options=[...])` 在面板显示可点选胶囊，点击即带明确对象进入下一轮，避免猜测误操作。

## L3：核验写入、对称约定更新与表单交接

写入功能默认关闭（`read-only`）。启用后，所有三条写入路径统一经由核心 `WriteSafetyPipeline`（`BusinessWriteService`）保护：

1. **经过评审的类型化写入 Skill（`SKILL.md` + `agents/woa.toml`）：** 声明明确作用实体、相邻易混目标、参数约束与 `ToolDefinition.effect_contract`，并实现同账号后置核验器 `ResultPostconditionVerifier` 与对账器 `MutationReconciliation`。
2. **扁平对称约定更新（`ConventionWritePort`）：** 声明 `"updates"` 能力，自动为满足“同前缀 update/get 扁平可写字段 1:1 对称、排除密码/明细数组”的实体提供 `update_record`，为严格两态可逆字典提供 `toggle_record_status`，已在 `testapps/contacts` 与 `yudao` 双适配器跑通共享契约测试。
3. **原生表单提交与可撤销交接（`SubmitPort`、`UX-03`、`UX-05`）：** 实现 `SubmitConventions` 对常规表单/行内操作提供“批准后由程序点击一次并同账号回读”；对于未闭环表单、多行复杂单据或包含密码框/上传控件的场景，回退预填交接，自动滚动聚焦待人工填写的受限控件，并在卡片上提供“撤销本次自动填写”（`undoFormFill`）按修改前原值一键恢复。
