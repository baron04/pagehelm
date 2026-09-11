# 应用接入指南

Application Adapter 把一个第一方 Web 应用接入通用服务端 Agent。Adapter 负责应用事实和权限，不负责 ReAct 循环。一个新 Adapter 需要提供版本化描述、类型化 Business Skill 与工具、五个可执行端口和健康检查。`bind_application()` 会在用户任务到达浏览器或模型前校验整个组合。

## 必需组件

`ApplicationDescriptor` 声明应用 ID、准确 Web Origin、导航模式、同源 PageController 模块 URL，以及当前 Skill/tool catalog 的 SHA-256。先构造 `BusinessToolRuntime`，再用 `business_tools.catalog_sha256` 构造 Descriptor。这样 Skill 或 schema 发生变化时，陈旧部署会在组合阶段失败。

`ApplicationPorts` 包含身份、路由、数据分类、mutation 后置条件和核对端口。数据端口必须覆盖每个 typed tool，后置条件与核对端口必须覆盖每个写工具，身份 scope 必须等于工具运行时声明的所有非空 scope。运行时与 `ApplicationPorts` 必须使用相同的路由、数据和后置条件对象。

`BusinessToolRuntime` 通过固定 JSON schema 暴露当前应用数据。凭据和任意 HTTP 构造应保留在应用 executor 内。Skill 说明何时及如何调用工具，但不能证明当前记录存在。返回的每条路由必须原样出现在结果内容中，并在 Agent 使用前通过 Adapter 路由端口校验。

运行时使用的所有应用 URL 形状都归路由端口所有。普通 Adapter 只声明 typed tool 路径，并为空源码合同返回空路由身份。需要源码能力的集成可以额外声明带 `scope`、`route` 命名组的合同路由模式，以及明确的应用入口模式。通用 Agent 只调用端口识别路由、发现入口页 scope 和校验观察到的 handoff 链接，不解析具体框架的 URL 约定。应用特有的 session handoff 查询参数名应写入 `session_handoff_query_keys`，并传给 `LiveProductSession(sensitive_query_keys=...)`，使内嵌 Runtime 在 DOM 内容离开浏览器前删除这些值。

## PageController 与静态 Runtime

应用 Origin 必须提供一个导出 `PageController` 和 `getElementByIndex` 的 ES module。Minimal 示例为合成页面提供了一个小型兼容 controller。真实集成应固定并部署已经通过资格验证的 PageController 实现，再把准确的同源 URL 写入 `ApplicationDescriptor.page_controller_module`。其它 Origin 上的 CDN URL 会被拒绝。

Web Operation Agent Runtime 由 `EmbeddedGateway` 在 `/v1/runtime/embedded.js` 提供；它从同一个网关导入 `/v1/runtime/panel_host.js` 与构建好的助手面板 `/v1/runtime/panel/panel.js`，因此客户端始终与服务端协议同版本。未跑过 `scripts/build_panel.py` 的 checkout 没有面板，网关会以 `panel_unavailable` 回绝该请求，而不是发一份旧的。不要把它复制到每个应用，也不要持久保存生成的 bootstrap script。`LiveProductSession.start()` 后，获取 `live.bootstrap_script` 并直接注入已认证的顶层应用页面。该临时脚本包含短期 session capability，不能写日志、缓存、打入静态 bundle 或暴露给其它 Origin。公开 Demo 使用 Playwright `context.add_init_script` 只是开发宿主；生产第一方应用应从可信应用 shell 注入等价的 session bootstrap。

Runtime 只会在顶层 document 且 `location.origin` 与 session grant 一致时启动。它观察 DOM、执行固定通用动作、展示进度和确认 UI，并返回回执。模型凭据、源码索引、隐藏权限、业务计划和 evaluator 都不能进入 Runtime。

## 身份

认证应用应根据可信服务端登录边界产生的证据实现 `ApplicationIdentityPort`，把页面 actor、tenant 或 workspace、委托 API actor、scope、delegation ID、签发时间和过期时间绑定为 `PrincipalContext`。DOM 文本、浏览器自报用户 ID 或持有 Runtime capability 都不能作为身份证据。

身份端口要求认证时，设置 `AgentTurnConfig.require_identity=True`。`AgentTurnExecutor(application=...)` 会在启动浏览器传输或模型前校验 principal 和准确应用 Origin。随后每个 typed tool 都会收到 `OperationContext`，其中绑定 principal、session、turn、operation、deadline 和所需 scope。未认证的合成示例必须显式声明 `AnonymousIdentityPort`。

## 写入与验证

Adapter 与 turn policy 默认都关闭写入。启用写入的 Adapter 在生产环境中必须提供持久 mutation journal、覆盖每个写工具的后置条件 verifier 和核对端口。用户确认会绑定一个规范化 proposal digest、影响、目标、principal、turn 和 operation，且只能消费一次。

一次已确认的 dispatch 完成后，由后置条件端口决定 mutation 能否记录为 completed。缺少或未通过后置条件会得到 indeterminate 结果，系统不会自动重试。当前保守 reconciler 会把不确定写入留给人工处理；持久 reconciliation key、后台恢复和跨 session 在途业务键互斥属于后续可靠性工作。

## 组合与资格验证

先执行 `application = bind_application(adapter)`，再把该对象传给 `AgentTurnExecutor(application=application, ...)`，并使用它的 Descriptor 和路由端口配置 `LiveProductSession`。产品组合不应再单独传递原始 tool runtime。

每个 Adapter 都应运行可复用的 contract assertion，并增加有效路由、身份不匹配、读取完整性、mutation 拒绝、确认、后置条件失败和凭据脱敏测试。`scripts/qualify.sh --require-browser` 会运行产品套件、隔离 wheel 检查、Minimal 内嵌 Runtime 演练和真实 Chromium 中的规范浏览器协议。真实应用门若显示 `BLOCKED`，表示没有执行，不能算通过。

Minimal Contacts 与 Twenty CRM 当前使用同一 Adapter v2 合同，Agent 核心没有应用分支。Minimal Contacts 是接入参考。SPI 尚未稳定，因此 Twenty 暂时保留在主 distribution 中；现在拆包只会增加第二套发布和兼容面，尚无独立版本需求的证据。
