# 应用接入指南

Application Adapter 把一个第一方 Web 应用接入通用服务端 Agent。Adapter 负责应用事实和权限，不负责 ReAct 循环。一个新 Adapter 需要提供版本化描述、类型化 Business Skill 与工具、可执行端口和健康检查。`bind_application()` 会在用户任务到达浏览器或模型前校验整个组合。

## 必需组件

`ApplicationDescriptor` 声明应用 ID、准确 Web Origin、导航模式、同源 PageController 模块 URL，以及当前 Skill/tool catalog 的 SHA-256。先构造 `BusinessToolRuntime`，再用 `business_tools.catalog_sha256` 构造 Descriptor。这样 Skill 或 schema 发生变化时，陈旧部署会在组合阶段失败。

`ApplicationPorts` 包含身份、路由、可选当前表单、数据分类、mutation 后置条件和核对端口。数据端口必须覆盖每个 typed tool，后置条件与核对端口必须覆盖每个写工具，身份 scope 必须等于工具运行时所有非空 scope 与表单端口 scope 的并集。运行时与 `ApplicationPorts` 必须使用相同的路由、数据和后置条件对象。

`BusinessToolRuntime` 通过固定 JSON schema 暴露当前应用数据。凭据和任意 HTTP 构造应保留在应用 executor 内。Skill 说明何时及如何调用工具，但不能证明当前记录存在。返回的每条路由必须原样出现在结果内容中，并在 Agent 使用前通过 Adapter 路由端口校验。

需要广覆盖读取时，实现可选、应用无关的 `EntityReadPort`，再与 `EntityReadTools` 组合。Agent-facing 工具面固定为 `discover_entities`、`describe_entity` 和`query_entity_records`。Adapter 把自己的 ORM model、REST collection、OpenAPI resource、GraphQL type 或其它元数据翻译成 `EntityCatalog`、`EntityDescriptor`、`EntityField` 与`EntityQueryPage`。原生资源名只作为 Adapter 内部引用；核心签发绑定 actor/catalog 的不透明实体和字段 ID。目录或字段描述发生漂移时旧 ID 自动失效。通用过滤只允许有界 AND、固定操作符、服务端固定排序、页大小和 offset，不能退化为任意 method、URL、SQL、domain 或请求体。

Adapter 启用该工具面时必须运行 `assert_entity_read_contract`，并补充自己的鉴权和翻译测试。仓库目前对 Odoo ORM/RPC 翻译与结构不同的合成 Contacts REST/list 翻译运行同一黑盒断言；只有一个 Adapter 通过不能证明 Agent 能力通用。

读工具的 `QueryEvidence` 决定回答能对返回的行说什么。`matched_count` 填范围内符合条件的记录数；只有应用确实数全了，才设 `count_exact=True`，这样一个只返回 20 行、共 72 条的列表会写"共 72 条，下面列出其中 20 条"。按名称搜索时，`looked_for` 填搜索的文字，`exact_matches` 填名称完全一致的条数，应用无法判断时留 `None`。只有完全一致的匹配才能让回答说"已存在该名称的记录"。

证据用工具自己的字段名和范围说明，核验器也按这些原文核对。要在读者的语言里显示它们，给 `BusinessToolRuntime` 传 `answer_labels={"zh": {...}}`，键是证据里字段名和范围说明的原文；订单状态这类编码取值用 `字段=取值` 作键。标签只改变回答怎么显示，不改变回答断言的内容；审计记录保留原文。

运行时使用的所有应用 URL 形状都归路由端口所有。普通 Adapter 只声明 typed tool 路径，并为空源码合同返回空路由身份。需要源码能力的集成可以额外声明带 `scope`、`route` 命名组的合同路由模式，以及明确的应用入口模式。通用 Agent 只调用端口识别路由、发现入口页 scope 和校验观察到的 handoff 链接，不解析具体框架的 URL 约定。应用特有的 session handoff 查询参数名应写入 `session_handoff_query_keys`，并传给 `LiveProductSession(sensitive_query_keys=...)`，使内嵌 Runtime 在 DOM 内容离开浏览器前删除这些值。

## 目标数据

目标式回合的解析由核心完成，对所有应用都一样：条件是什么意思、一个名字指哪条记录、何时放开页面自带的默认筛选、明细行和所属单据怎么对应、一段描述符合哪条记录。Adapter 只提供 `GoalData`（`web_operation_agent.goal.resolve.service`），应用以 `DataGoalPort(goal_data)` 作为自己的目标端口：

- `index()`：模型据以声明的 `ApplicationIndex`；`capabilities()`：提供了哪些可选能力；
- `schema()`：核心解析所依据的实体、字段（类型、关联实体、选项及其内部键、能否作为条件、出现在哪里）、筛选、页面和各实体的合计字段，以当前用户身份从应用自己的元数据得出；
- `find`、`read`、`count`、`rows`、`total`、`groups`：读取，接受以字段键表示的条件树（`Compare`、`All`、`Any`、`Not`、`Via`）；以及 `page_address`、`record_address`；
- `time_zone()`；`name_of()` 给出记录显示名里的本名；可选的 `labels_for()` 给出应用自己的翻译里一个词对应的标签。

Adapter 负责把条件翻译成自己的查询语言。应用接口不能按某种比较筛选时，读取当前用户可见的记录，用 `matches` 在内存中过滤，限制条数，读不全时如实说明。页面保存的范围可以是 `Native` 子句，只由产生它的 Adapter 执行。`resolve` 不是 Adapter 的方法。

## PageController 与静态 Runtime

应用 Origin 必须提供一个导出 `PageController` 和 `getElementByIndex` 的 ES module。Minimal 示例为合成页面提供了一个小型兼容 controller。真实集成应固定并部署已经通过资格验证的 PageController 实现，再把准确的同源 URL 写入 `ApplicationDescriptor.page_controller_module`。其它 Origin 上的 CDN URL 会被拒绝。

应用模块可通过 `readElementPageControl(element)` 为行内控件返回 `{kind: 'record', id, table, values}`：`id` 是应用记录标识，`table` 区分当前挂载的数据视图，`values` 是可见列名和值的二元数组。这些事实只描述当前页面，不授予点击或读取权限；应用必须证明控件、渲染行及同账号记录的对应，渲染未提交或无法对应时不返回。Runtime 从实时回调生成 `record_id/record_table/record_values`，清除控制器文本中的同名属性，并把这些事实纳入派发前指纹；行或视图变化后旧引用失效。应用标识可以是非数字字符串，Runtime 不认识 Vue 或业务实体。实现与反例须同时覆盖应用组件和结构不同的合成页面。

这个模块还可以导出 `openPage(url)`：由应用在当前文档里打开自己的页面，例如经它的路由。描述符的导航模式为 `document` 时，Runtime 在执行 `goto` 时先调用它；只有它在六秒内准确返回 `true`，页面才不按整页加载。它只应在应用此刻显示的地址与之前不同的情况下返回 `true`，因为 Runtime 仍然按地址来证明跳转发生了。路由只靠 History API 就能驱动的应用改为声明 `navigation_mode="spa"`，不需要这个导出。

该模块还可导出 `readPageBlocker()`：就绪时严格返回 `false`，正常加载时返回 `"pending"`，阻断错误返回 `true`；其它值及异常均按阻断处理。即使 URL 和 DOM 投影未变，加载也保持待确认；服务端等待新的就绪观察，不重放前一个动作。错误优先于加载。记录身份与权限仍需应用自己的证据，加载标识不能授予这些证据。

宿主页面只需引入一个脚本就能获得助手：`assistant.js`，由 `EmbeddedGateway` 在 `/v1/runtime/assistant.js` 提供。它把启动监督者、Runtime 和助手面板打在同一个文件里，之后不再加载任何东西；由同一个网关提供，因此客户端始终与服务端协议同版本。它是构建产物：未跑过 `scripts/build_runtime.py` 的 checkout，网关会回 `runtime_unavailable`，而不是发一份旧的。不要把它复制到每个应用。生产宿主在已登录页面上声明 `<script type="module" src=".../v1/runtime/assistant.js" data-woa-bootstrap="/your/bootstrap"></script>`。bootstrap 端点位于应用自己的 Origin，把已登录的应用会话换成 agent 会话：它收到 `{"runtime_instance_id": ...}`，用 `EmbeddedGateway.bootstrap_config` 构造并返回 `{"schema_version": 1, "config": ..., "renew_at": ...}`。`runtime_instance_id` 是页面实例名（`[A-Za-z0-9._-]`，最长 80 字符），同时充当会话键分量和守卫窗口名：同实例续期复用会话，不同实例各自建独立会话。身份由应用另行签发时再加 `data-woa-identity`：脚本先把 `{"runtime_instance_id": ...}` 发到那里，再把返回结果交给 bootstrap 端点，Odoo 插件就是这样做的。宿主还可以加 `data-woa-examples`：一个 JSON 数组，每种请求最多一条 `{"kind": ..., "text": ...}`，种类是 `query`（查数据）、`find`（找功能）、`operate`（操作页面），新对话把它们作为入口展示，点击后填入输入框，不会直接发送。这些是应用对自己记录和菜单的说法，所以由页面声明，服务端不声明；不是合法 JSON、种类重复、种类不在上述三种之内、或示例超过输入框长度上限时，整组都不采用，面板只保留"问助手能做什么"这一个入口。面板的启动按钮在脚本运行时就出现在页面上，但会话要到需要时才建立：用户发送问题时；用户打开这个标签页已有的对话时；或者有一轮正在进行（比如 Agent 自己打开了新页面）时，不等用户操作就建立。会话空闲五分钟后断开，下一条消息会重新建立，对话由服务端保存，会随之恢复。此后续期由脚本完成；对于通过端点建立且绑定应用身份（`applicationId` 与 `principalBinding`）的会话，脚本也会在新任务发送前重新调用身份与 bootstrap 端点，并在身份更新完成后才发送观察和消息；定时续期与发送前续期共享同一次在途请求。bootstrap 需支持同一逻辑会话的空闲续期，保持会话 capability 与 generation 稳定。未绑定应用身份的独立 capability 会话不在每次发送前重新 bootstrap。认证失败不会自动重放任务。有一轮正在进行时，断线也由脚本自己重连。会话建立不了时，面板会说明原因并提供重试；除非有一轮正在进行，否则不会在用户不知情时自动重试。标签页的进度保存在它的 `sessionStorage` 里（`woa.assistant.engagement.v1`）。开发宿主可以用 `live.bootstrap_script`：它返回一段临时脚本，把整套会话交给页面并加载同一个 `assistant.js`；该脚本包含短期 session capability，不能写日志、缓存、打入静态 bundle 或暴露给其它 Origin。公开 Demo 用 Playwright `context.add_init_script` 注入它。

Runtime 只会在顶层 document 且 `location.origin` 与 session grant 一致时启动。它观察 DOM、执行固定通用动作、展示进度和确认 UI，并返回回执。模型凭据、源码索引、隐藏权限、业务计划和 evaluator 都不能进入 Runtime。

## 身份

认证应用应根据可信服务端登录边界产生的证据实现 `ApplicationIdentityPort`，把页面 actor、tenant 或 workspace、委托 API actor、scope、delegation ID、签发时间和过期时间绑定为 `PrincipalContext`。DOM 文本、浏览器自报用户 ID 或持有 Runtime capability 都不能作为身份证据。

身份端口要求认证时，设置 `AgentTurnConfig.require_identity=True`。`AgentTurnExecutor(application=...)` 会在启动浏览器传输或模型前校验 principal 和准确应用 Origin。随后每个 typed tool 都会收到 `OperationContext`，其中绑定 principal、session、turn、operation、deadline 和所需 scope。未认证的合成示例必须显式声明 `AnonymousIdentityPort`。

应用若保存用户时区，在执行器上实现 `time_zone()` 并通过 `AgentTurnConfig.time_zone` 传给 turn。这样"今天""本月"按用户自己的日子算，报出的时间也与应用界面一致；没有时区时本轮不提时钟，模型会退回 UTC 时间戳。

principal 上只放应用实际授予该 actor 的 scope，而不是 catalog 声明的全部 scope。principal 缺少所需 scope 的 typed tool 会在调用应用之前以 `BusinessToolPermissionError`（`layer="tool_authorization"`）拒绝。应用自己的授权拒绝——HTTP 403、访问规则异常——同样映射为 `BusinessToolPermissionError`；被拒绝或已过期的凭据仍然是 `BusinessToolAuthenticationError`。对于写入，只有应用能保证被拒绝的调用没有产生业务变更时才能使用 `BusinessToolPermissionError`；仍可能部分完成或结果未知时必须使用 `BusinessMutationIndeterminate`。前者是授权结果，产品向用户报告为 `permission_denied`，evaluator 计分；后者是装置故障。被拒绝的写入直接结束本轮。被拒绝的读取会交还 Agent：它只能用已经读到的事实作答，并且必须说明哪一部分没能完成；同一次拒绝会在本轮内关闭页面作为事实来源，因此它无法改从屏幕上取走被拒的记录。scope 检查只约束 typed tool；应用自己的页面向 actor 展示什么，由应用自己的权限决定。每次读取也要限定在该 actor 的范围内，并在证据来源中写明：完整查询没有找到，只能证明在这个范围内不存在。

## 写入与验证

Adapter 与 turn policy 默认都关闭写入。启用写入的 Adapter 在生产环境中必须提供持久 mutation journal、覆盖每个写工具的后置条件 verifier 和核对端口。用户确认会绑定一个规范化 proposal digest、影响、目标、principal、turn 和 operation，且只能消费一次。

一次已确认的 dispatch 完成后，由后置条件端口决定 mutation 能否记录为 completed。缺少或未通过后置条件会得到 indeterminate 结果，系统不会自动重试。当前保守 reconciler 会把不确定写入留给人工处理；持久 reconciliation key、后台恢复和跨 session 在途业务键互斥属于后续可靠性工作。

原生表单提交由 `SubmitPort` 适配器承担，并由其 `contract()` 声明应用的提交约定 `SubmitConventions`：应用自行维护、回写时忽略的字段集合 `volatile_keys`；请求里命名目标记录的键 `record_key`；明细行的身份键 `line_key`；确认卡上给目标记录命名的字段 `name_key`；毫秒时间值解读时区 `epoch_zone`；以及把一次写响应判定为接受并取回新建编号的 `decode_write`。核心不给这些约定默认值；适配器未声明时，提交以类型化拒绝关闭。接入时要让共享提交合同测试在本适配器和另一个声明结构不同的应用上各跑一遍。

扁平对称约定更新由声明 `"updates"` 能力的 `ConventionWritePort` 承担，与 `convention_tool_definitions` 配合生成应用无关的 `update_record`（仅准入扁平 1:1 对称更新+详情读取且剥离密码与独立状态开关）与 `toggle_record_status`（仅准入两态互逆状态切换），同样经 `BusinessWriteService` 履行批准、派发前全字段重读与派发后全字段回读，并由共享合同测试（`testapps/contacts` 与芋道 `test_convention_writes.py`）覆盖两个结构不同的适配器。

## 临时表单交接

表单预填是可选能力，不是通用的浏览器权限。专用 typed tool 仍可为特定流程返回`FormDraft`。作为无 Skill 兜底时，由 Adapter 实现 `ApplicationFormPort`：`describe_form`返回当前路由下同 actor 的可写字段元数据，`prepare_form` 校验选定值并返回准确草稿。用`AgentTurnConfig.allow_form_prefill=True` 启用。

通用 `describe_form` 只取 Adapter 描述与最新视口观察的交集；只存在于元数据、只存在于DOM、重复出现以及凭据形态的字段都会消失。它返回短期不透明 form/field ID，而不是原生字段名或 selector。`prepare_form` 会重新读取两侧事实，拒绝过期 ID、不符合可移植字段类型或声明choice 的值，并拒绝增加字段、改变路由或持久化合同的草稿。只有服务端保存这份准确草稿时，无参数 `prefill_form` 才会出现。表单 scope 在读取 Adapter 元数据之前检查；缺少 scope 的 actor
拿不到通用表单工具。

服务端只消费一次草稿，从最新的页面观察中唯一地解析每个字段，填写或选择 Adapter 签发的值，在点击或提交之前停下。Adapter 由此断言：编辑这些字段本身不会保存任何东西。会写入的 onchange、边编辑边保存的表单、凭据字段、上传，以及任何编辑后可能持久化或触发外部效果的控件，都不能签发草稿，应走常规的确认写入路径。应用在离开页面时保存的表单（Odoo 18 就是这样）之所以可以用，只是因为助手离开任何页面之前都会先问，预填完成的回答也会告诉用户离开时内容可能被保存或丢弃。要在部署的应用上验证这一断言，包括运行结束后用 API 或数据库确认没有创建记录。

源码检索可以在准备表单前解释字段、校验规则或流程，但不能创建 form ID、field ID、selector、scope、取值或写权限；当前页面与当前应用元数据才是执行依据。接入通用表单时，要在该 Adapter 上运行共享的 `assert_current_form_contract`，还要通过真实浏览器门；只有 Adapter
单元测试不能证明端到端表单准确率。

Odoo 参考翻译不设全局启用开关，但默认仅开放三个标准模块浏览器资格案例覆盖的精确字段。`fields_get` 本身不能证明自定义 `onchange` 没有持久化或外部副作用；未知页面或字段在单独
资格验证前不签发预填草稿。
应用离页保存的编辑草稿须先确认准确效果与目标，再预填。

## 组合与资格验证

先执行 `application = bind_application(adapter)`，再把该对象传给 `AgentTurnExecutor(application=application, ...)`，并使用它的 Descriptor 和路由端口配置 `LiveProductSession`。产品组合不应再单独传递原始 tool runtime。

每个 Adapter 都应运行可复用的 contract assertion，并增加有效路由、身份不匹配、读取完整性、mutation 拒绝、确认、后置条件失败和凭据脱敏测试。`scripts/qualify.sh --require-browser` 会运行产品套件、隔离 wheel 检查、Minimal 内嵌 Runtime 演练和真实 Chromium 中的规范浏览器协议。真实应用门若显示 `BLOCKED`，表示没有执行，不能算通过。

`examples/` 下有两个参考接入：Odoo 保留作参考、不再维护，其独立可安装 distribution 完整实现 Adapter v2 合同；芋道是默认演示与测试应用，带可重置的本地部署。两者都说明核心中没有应用分支。`testapps/` 下的小型 Contacts 应用只用于内部浏览器与协议测试，不是第二套接入模板。
