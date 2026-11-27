---
title: 应用声明式接入指南
description: 基于核心层 ApplicationPorts、TurnTables 内存 SQL、CompiledWrites/WritePipeline 与参考接入模块（testapps/contacts/declarative/）将 PageHelm 接入第一方 Web 应用。
---

核心包 `src/web_operation_agent/` 提供应用无关的组合端口（`ApplicationPorts`、`bind_application`）、内置每轮内存 SQLite 引擎 `TurnTables`（`src/web_operation_agent/execution/turn_tables.py`）与核验写入管道（`WritePipeline`、`CompiledWrites`）。面向 REST/HTML 应用的单端声明式适配层参考实现位于 `testapps/contacts/declarative/`（遵循核心双应用边界规则，暂不并入核心发布包），宿主可对照该模式以少量声明与回调完成接入，而无需手写十余个重复 CRUD Port 类。

> 编程助手接入指引：可加载 `.agents/skills/integrate-pagehelm/SKILL.md`（`$integrate-pagehelm`），按“只读勘察 → 组装 `DeclarativeAppSpec` 与 `DeclarativeSessionHost` → 运行免付费契约回归 → 按需挂载 `WritePipeline` 写入”的顺序完成接入，并守住身份与写入安全红线。

## 接入前提

开始接入前，请确认目标后台满足三项基本条件：

- **同源部署**：PageHelm 网关（默认前缀 `/woa-agent`）与引导接口（`/woa-agent-boot/bootstrap`）挂载在业务 UI 同一浏览器源（Origin）下，确保同源会话、SSE 事件流与 DOM 观测安全隔离。
- **服务端身份反查**：宿主后端能在服务端根据可信 Cookie 或请求头通过 `IdentityResolver` 返回 `ResolvedActor`，绝不信任浏览器传入的用户 ID、查询参数或 DOM 文本。
- **同源运行时与控制器**：业务页面加载带版本号的 ES 模块 `assistant.js` 与版本锁定的 `PageController`（可直接复用内置组件 Preset，如 `element-plus` 或 `plain-html`）。

## 一、三项核心输入与可选写操作

宿主应用只需准备以下输入，即可由 SDK 自动派生完整后端工具与 Goal/Executor 端口：

| 接入要素 | SDK 核心接口 | 宿主提供内容 | 仓内源码参考 |
| --- | --- | --- | --- |
| **1. 服务端同源身份反查** | `IdentityResolver` / `HttpWhoamiIdentityPort` | 将可信登录态（Cookie / Bearer 头）解析为 `ResolvedActor(actor_id, permissions, tenant_id, workspace_id)`（`permissions` 默认为空集 `frozenset()` 默认拒绝；传 `api_token` 时必须配 `delegated_resolver`，或显式启用 `same_origin_shared_session=True`） | `testapps/contacts/declarative/declarative_identity.py` |
| **2. OpenAPI 或声明式清单** | `entities_from_openapi(...)` 或 `EntitySpec` / `PageSpec` / `FormSpec` | OpenAPI 3.x 文档或实体/页面/表单清单、允许的路由正则及 `session_handoff_query_keys` 脱敏参数 | `testapps/contacts/declarative/openapi.py` |
| **3. UI Preset 与会话宿主** | `FRONTEND_PRESETS` + `DeclarativeSessionHost` | 页面挂载 `assistant.js`，选择 `"element-plus"` 或 `"plain-html"`，托管会话与限流/账本库 | `testapps/contacts/declarative/declarative_host.py` |
| **4. 可选 `WritePipeline` 写入** | `TypedWriteSpec`、`convention_updates`、`StatusToggleSpec`、`SubmitSpec` | 按需挂载受单次可见审批、派发前复读与同账号回读保护的写入能力（`SubmitSpec` 仅用于列表行内动作，页面表单一律只做预填交接） | `testapps/contacts/declarative/declarative_writes.py` |

## 二、前端挂载与 UI 组件库 Preset

在需要助手的业务页面引入同源模块脚本：

```html
<script
  type="module"
  src="/woa-agent/v1/runtime/assistant.js"
  data-woa-bootstrap="/woa-agent-boot/bootstrap"
></script>
```

页面加载时，Runtime 向 `data-woa-bootstrap` 发起请求；宿主在服务端核验操作员登录态，通过 `DeclarativeSessionHost.open_or_reuse_session(...)` 创建或复用会话并返回 `_browser_bootstrap(session)`（内部调用 `EmbeddedGateway.bootstrap_config(...)`）。前端产物不包含模型 API Key 与规划逻辑。

- **Vue 3 + Element Plus（`FRONTEND_PRESETS["element-plus"]`）**：直接复用 `frontend/src/integrations/element-plus/` 下的 5 个通用辅助模块（`table.js`、`select.js`、`date-picker.js`、`form.js`、`vue.js`）。
- **语义化 HTML / 其他组件库（`FRONTEND_PRESETS["plain-html"]`）**：在同源下提供 `PageController` ES 模块（导出 `PageController` 与 `getElementByIndex`）；若需对接自定义组件库，在 `frontend/src/integrations/<app>/` 中导出 `readElementPageControl`、可选 `openPage(url)` 与 `readPageBlocker()`。

## 三、声明式适配器与 TurnTables SQL 装配

既可通过 `entities_from_openapi(...)` 从 OpenAPI 3.x 文档自动生成实体清单（自动排除匹配 `DEFAULT_SECRET_FIELD_PATTERN` 的敏感字段并配对 `GET`/`PUT` 详情端点），也可以直接组合 `EntitySpec`、`PageSpec` 与 `FormSpec`：

```python
from testapps.contacts.declarative import (
    DeclarativeAppSpec,
    DeclarativeApplicationAdapter,
    DeclarativeModelGoalPort,
    FRONTEND_PRESETS,
    PageSpec,
    ResolvedActor,
    build_declarative_profile,
    entities_from_openapi,
)
from testapps.contacts.declarative.declarative_host import (
    DeclarativeHostConfig,
    DeclarativeSessionHost,
)

entities = entities_from_openapi(
    OPENAPI_SPEC,
    entity_overrides={
        "contacts": {
            "list_tool_name": "list_contacts",
            "list_filter_param": "name_contains",
            "list_filter_field": "name",
        }
    },
    default_read_scope="contacts:read",
    default_write_scope="contacts:write",
)

spec = DeclarativeAppSpec(
    application_id="my.app",
    display_name="My Application",
    entities=entities,
    pages=(PageSpec(key="contacts", label="Contacts", route="/contacts", entity="contacts"),),
    page_resolver=lambda cookie: ResolvedActor("actor-1", frozenset({"contacts:read"})),
    delegated_resolver=lambda: ResolvedActor("actor-1", frozenset({"contacts:read"})),
    # 若同源前端与后端共用同一会话 Cookie 且无独立 api_token，可省略 api_token 与 delegated_resolver，改为显式声明：
    # same_origin_shared_session=True,
    navigation_mode=FRONTEND_PRESETS["element-plus"].navigation_mode,
    tool_route_patterns=(r"/contacts", r"/contacts/[a-z0-9-]+"),
)

adapter = DeclarativeApplicationAdapter(spec, "https://app.example.test", api_token=delegated_token)
goal_port = DeclarativeModelGoalPort(adapter)
profile = build_declarative_profile(adapter, goal_port, enable_sql=True, time_zone="Asia/Shanghai")
```

开启 `enable_sql=True` 后，`build_declarative_profile(...)` 会自动将单轮拉取到的列表行写入核心内置内存 SQLite 引擎 `TurnTables`（`src/web_operation_agent/execution/turn_tables.py`），自动注册 `sql`、`tables` 与 `date_range` 工具，并支持模型通过 `finish(table_ref="q1")` 直出脱敏 `<DataTable>` 交互表格。仓内精简重构后的对照实现见 `testapps/contacts/adapter.py` 与 `testapps/contacts/server.py`。

## 四、领域口径手册与多候选消歧（可选）

如果后台存在跨模块同名术语或检索返回多条同账号同名记录：

- 在 `domain_playbooks/*.md` 中编写简明口径定义，供模型通过 `load_domain_guide` 按需读取。
- 仅在白名单目录上挂载 `SourceTree` / `SourceCompiler` 规则索引（严格排除密钥、上传目录、数据库和用户内容）。
- 同名查找命中 2–4 条同账号记录时，让模型调用 `finish(status="need_input", options=[...])` 生成可点选胶囊，由人工选定后再继续。

## 五、按需挂载核验写入（`WritePipeline`）

接入方按需声明写入能力；需要临时关闭所有后台写入时，可在服务端设置 `WOA_READ_ONLY=true`。所有写操作均由 `WritePipeline` 统一保护（完整安全门禁见 [写入安全与隐私边界](../security/verified-writes.md) 与 [Skill 与类型化写入](./skills-and-writes.md)）：

1. **对称平铺约定更新（`convention_updates=True` + `StatusToggleSpec`）**：针对声明 `convention_update=True` 的实体，自动派生 `update_record` 与两态可逆 `toggle_record_status`，自动过滤敏感字段。
2. **类型化写操作（`TypedWriteSpec` + `SKILL.md` / `agents/woa.toml`）**：声明 `normalizer`、`precondition_check`、`dispatch_fn` 与同账号 `readback_fn`；派发超时或回读不一致时立即抛出 `BusinessMutationIndeterminate`，绝不自动重试。
3. **页面表单只预填不提交与行内动作提交（`FormSpec` + `SubmitSpec`）**：页面表单一律只做不写库的字段预填并标亮提交按钮（`DeclarativeMutationPrefill`、`fill_fields(then_highlight=...)` + `undoFormFill` 撤销），交给用户手动核对点击，不自动提交也不弹表单提交审批卡（若对页面表单调用 `request_submit` 会自动降级为预填交接）；仅列表行内状态动作（带 `row_id`）通过 `SubmitSpec` + `SubmitConventions` 走单次 `request_submit` 审批。

## 合并前自检清单

使用隔离、可重置的合成数据执行免付费回归验证：

- 核心中立性检查：`.venv/bin/python scripts/check_core_boundary.py`
- 差异感知与契约测试：`.venv/bin/python scripts/check.py --plan && .venv/bin/python scripts/check.py`
- 安全红线核对：未登录或篡改 Cookie 的引导请求必须被拒绝；跨租户与跨账号完全隔离；敏感字段在入表前及出站前均已脱敏；未获批准不产生后端写请求；`claimed` 之后故障进入 `INDETERMINATE` 且绝不重试。
