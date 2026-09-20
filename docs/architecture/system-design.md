# Web Operation Agent 系统设计

Web Operation Agent 是一个面向复杂业务 Web 应用的嵌入式 AI 助手。用户在应用页面内用自然语言描述目标；服务端 Agent 负责理解、规划、调用知识与业务工具，页面内 Runtime 负责观察当前页面、执行受限的通用动作并返回结果。

系统采用“两层运行结构、一个逻辑 Agent”。推理只发生在服务端。浏览器 Runtime 通过版本化协议与服务端隔离，不持有计划、业务知识或模型能力。当前发布物只有一个规范执行端：页面内嵌 Runtime；新增浏览器宿主必须实现同一 `ActionCommand`/`ActionReceipt` 合同并通过同一行为套件，不能在产品包内复制另一套动作语义。

## 1. 目标与非目标

### 1.1 目标

- 在不把模型密钥和内部知识下发到浏览器的前提下，完成企业应用中的查询、导航和受控写入。
- 每次动作之后读取新的页面或工具结果，让 Agent 基于运行时事实重新规划。
- 通过 Application Adapter 接入不同应用，使 Agent 核心不包含具体页面路由、API 字段和认证细节。
- 同时支持页面操作、按需加载的 Business Skill、同用户身份的业务 API，以及可选的源码知识。
- 对写操作默认拒绝，展示准确的影响和目标，取得用户确认后最多执行一次。
- 将“Agent 已结束”和“业务目标已验证”分开，避免把模型自述、HTTP 成功或页面提示误当成最终成功。
- 保留经过脱敏的执行记录，使错误、拒绝和不确定结果可以审计。

### 1.2 非目标

- 不把浏览器动作描述成数据库事务，也不承诺任意页面写入具有原子性。
- 不让页面 Runtime 自主规划、读取源码、持有业务凭据或调用模型。
- 不依赖多 Agent、工作流编排平台、知识图谱或通用静态分析平台。
- 不要求源码检索才能使用产品；源码只是可关闭的知识来源。
- 当前实现不等同于可直接部署的多租户云服务，也不宣称支持任意第三方网站。
- 用户确认只表示授权执行某个精确提案，不替代参数校验、执行回执和业务后置验证。

## 2. 系统边界

```mermaid
flowchart TB
    U[用户] --> P[业务应用页面]

    subgraph Browser[浏览器：第一方嵌入 Runtime]
        Panel[助手面板]
        Observe[DOM 观察与脱敏]
        Execute[固定通用动作执行器]
        WAL[页面会话内的动作恢复记录]
        Panel --> Observe
        Execute --> Observe
        Execute <--> WAL
    end

    P <--> Browser

    subgraph Server[服务端：一个逻辑 Agent]
        Gateway[HTTP / SSE Gateway]
        Channel[会话通道与事件序列]
        Host[Conversation Host]
        Turn[Turn Executor]
        Loop[ReAct Agent Session]
        Skills[Skill 与知识路由]
        Tools[受限业务工具]
        Control[策略、确认与控制]
        Ledger[脱敏审计 Ledger]
        Gateway <--> Channel
        Channel <--> Host
        Host --> Turn
        Turn --> Loop
        Loop <--> Skills
        Loop <--> Tools
        Loop <--> Control
        Turn --> Ledger
        Loop --> Ledger
    end

    Browser <--> Gateway
    Tools <--> API[应用 API]
    Execute <--> P
    Skills -.可选.-> Source[版本绑定的源码 Provider]

    Verifier[应用验证器] -.执行结束后.-> P
    Verifier -.同用户回读.-> API
    Evaluator[隐藏 Evaluator] -.执行结束后只读.-> Truth[隔离的业务真值]
```

浏览器到服务端的页面观察、动作结果、用户消息和确认决定通过认证的 POST 返回；服务端到浏览器的动作与进度通过 SSE 事件发送。`LiveProductServer` 可以让多个隔离 session 共享一个有容量上限的 Gateway，并独立撤销 capability 和释放 channel 状态。当前 Gateway 只监听本机回环地址，状态仍在单进程内存中，因此它还不是生产网络入口。

## 3. 组件职责

### 3.1 Embedded Web Runtime

Runtime 嵌入业务页面，承担以下职责：

- 显示助手面板，收集自然语言目标和确认决定；
- 通过 PageController 读取结构化 DOM，并生成带页面 epoch 的元素引用；
- 执行固定的通用动作：观察、点击、填写、选择、提交、同源跳转、返回和有限等待；
- 在副作用动作 claim 前比较当前 URL、目标控件和关联表单与上次观察时的本地指纹；不一致时先重新观察并返回 `stale_observation`。判定只用 URL 与元素指纹，不订阅文档变化事件；
- 按应用组合选择普通文档跳转或显式启用的 SPA 路由跳转；
- 为动作返回结构化回执和新的页面观察；
- 在整页导航前写入恢复记录，并缓存已完成结果，连接恢复后重发结果而不重复动作；
- 隐藏 PageController 的调试编号，并从观察、错误和参数回显中删除凭据形态的数据。

Runtime 不包含任务历史、模型客户端、源码、Skill 目录、ControlPlan、业务 evaluator 或任意 JavaScript 执行工具。它只执行服务端发送且通过当前观察、页面 epoch、截止时间和能力校验的命令。

动作指纹只保存在当前浏览器 Runtime 内，不跨网络发送。它覆盖目标控件的语义、值、可操作状态、选项和关联表单的 action/method/target，因此用户在模型思考期间修改字段、控件被替换或提交目标变化都会使旧动作失效。仅修改无关 DOM 不会自动拒绝动作。JavaScript handler 依赖的其他隐藏页面状态仍无法由该指纹完整证明，应用控制层仍需对写入 fail closed。

Runtime 运行在业务页面的同源脚本环境中。因此当前安全模型要求页面及其第一方脚本可信；Origin 和 capability token 可以阻止其他来源调用会话，却不能隔离同一页面内已经执行的恶意脚本。需要处理不受信任页面时，应使用更强的隔离宿主，而不是把现有嵌入模式直接暴露给任意网站。

### 3.2 Embedded Gateway 与 Remote Session

Gateway 为每个会话签发随机 capability，并绑定允许的页面 Origin。Registry 只保存 token 哈希，产品宿主在会话存续期间保留用于生成 bootstrap 的原始 grant。Runtime 建立连接后还会取得活动 lease；整页导航产生的新 Runtime 会替换旧 lease，旧页面不能继续提交观察或结果。共享 server 对活动 session 数量执行准入上限；关闭一个 session 会原子移除其 token 哈希与 channel，使旧 capability 立即失效而不影响其他 session。每个 session 默认最多存活一小时，并对事件、已接受消息、待处理消息、观察标识、动作、审批和 Runtime 实例分别设置硬上限。消息队列达到瞬时上限时返回可重试的 429；生命周期状态达到硬上限时保留最后一个 terminal 事件并关闭 session。无限等待消息的 Host 也会在 TTL 到达时被唤醒，因此空闲会话不会永久占用线程和准入名额。

Gateway 在 loopback 管理面提供两个无凭据探针：`/healthz` 只证明 HTTP 进程仍在响应，`/readyz` 表示组合仍可处理工作。就绪响应经过固定 schema 投影，只允许协议版本、是否就绪、是否接受新 session、当前 session 数量、容量和 provider guard 的数值/布尔状态；不包含 session id、capability、Origin、用户输入、页面内容、应用数据或回调错误正文。Provider 电路打开时返回 503，但容量已满只会把 `accepting_sessions` 设为 false，现有 session 仍可由同一进程服务。回调异常、字段畸形或不一致会返回不含异常文本的 503。该管理面目前只绑定 loopback，尚未替代部署平台的认证监控、指标采集和告警。

应用宿主还可以在创建 session 前提供短期不可变 `PrincipalContext`。它包含 application、tenant、workspace、actor、delegation 和 scopes，但原始标识只存在于可信服务端内存中；bootstrap、Runtime hello 和跨浏览器协议只携带 application id 与主体摘要。启用 `require_identity` 后，缺少主体、主体过期或应用不匹配会在建立 turn 前拒绝。公开 Twenty demo 已从可信 Playwright 宿主读取即将用于浏览器的 HttpOnly session cookie，由 Twenty `/metadata` 解析页面 actor/workspace，再与 delegated OAuth 身份严格匹配；cookie 不进入 Runtime、模型或日志。浏览器运行期间宿主轮询 cookie 单向摘要，删除或替换会关闭 Agent session。

Remote Session 维护严格 schema 的消息、观察、动作、结果、确认和事件序列。每个页面动作必须：

1. 基于当前 observation id 和 page epoch；
2. 在截止时间前由活动 Runtime 原子 claim；
3. 由 claim 的同一浏览器客户端提交结果；
4. 对重复 id 返回同一结果，对内容冲突的重放拒绝。

这些约束解决页面刷新和 SSE 重连时的重复执行问题。它们不能证明业务写入是否落库；无法证明时必须进入 `INDETERMINATE`。

服务端 `BrowserExecutor` 只是 `observe`、`execute` 和 `console` 的结构合同，不实现浏览器行为。唯一的产品实现 `RemoteBrowserClient` 会把动作封装为 `ActionCommand`，经 Remote Session 发送给内嵌 Runtime，并校验返回的 `ActionReceipt` 与原命令、观察和 claim 一致。早期的 `ThinBrowserClient/browser_worker` 子进程执行端只被未发布实验 CLI 使用，且其选择、导航和观察语义已与内嵌 Runtime 分叉，现已从产品包删除。

### 3.3 Conversation Host 与 Turn Executor

`LiveConversationRunner` 顺序消费同一面板中的多轮消息，并复用同一个 Turn Executor。单轮异常会形成独立 outcome，后续消息仍可继续；关闭页面或会话是正常生命周期，不被伪装成任务失败。

每个 Turn Executor 拥有一个有界 `ConversationState`，默认只在当前进程内保留最近 4 轮且模型投影不超过 32 KiB。已完成轮次可以保留经过 `BusinessToolResult` 边界校验的业务事实，用于理解“它”“第二个”等后续指代；失败、取消或不确定轮次不保留业务事实。历史上下文在 prompt 中显式标记为可能过期的不可信应用数据，只参与引用解析；只有当前目标含指代词时，历史文本才可以补充只读 Skill 候选，写 Skill 仍必须由当前目标直接命中。新 turn 在陈述事实或执行页面动作前必须重新读取页面或类型化 API；历史 `ui_path` 不会进入本轮 `_business_routes`，所以不能直接授权跳转。审计只写保留轮数和上下文摘要，不复制上下文正文。

每条活动消息具有唯一 `message_id`。页面内停止操作通过当前 Runtime lease 提交严格 schema 的 `CancellationRequest`，服务端只接受同一 session 中已登记的消息。页面动作的 `turn_id` 与该 `message_id` 相同，因此取消请求、模型轮次、审批和动作可以在同一消息边界内关联。Conversation Host 在成功、普通异常、会话关闭和非预期退出路径中统一完成消息生命周期，完成后的消息不能重新进入取消状态。

主体绑定会话中的每次业务工具调用还会创建一个不可变 `OperationContext`，把 principal、session、turn、operation 和有效期固定在一起。Business Tool 逐次校验 application 与 required scope；mutation proposal 的摘要包含 operation context 摘要，审批请求与决定必须匹配同一 principal、turn 和 operation。原始身份字段不会进入浏览器或 Ledger，审计只记录稳定摘要。

取消和动作认领共享同一把会话锁。如果取消先发生，尚未认领的动作从 pending 状态移除，等待结果的 Agent 被立即唤醒并以 `user_cancelled` 结束；如果浏览器先认领动作，取消不会伪造“未执行”，该动作必须返回确定回执，或在结果无法判定时进入 `INDETERMINATE`。等待中的业务写入确认会自动形成拒绝决定并唤醒 Agent；即使批准与取消同时到达，业务 mutation 在实际 dispatch 前仍会再次检查取消状态。

`AgentTurnExecutor` 是应用无关的单轮入口。它负责：

- 严格校验 `message_id`、目标文本和页面 URL；
- 把消息绑定到最新页面观察；
- 启动浏览器客户端后再次核对 observation id、epoch 和内容摘要，拒绝绑定期间发生的页面变化；
- 注入预算、动作策略、业务工具、确认通道、控制端和答案 renderer；
- 创建审计 Ledger 并运行一个 `ServerAgentSession`；
- 返回中性的 `AgentTurnExecution`，不判断应用目标是否最终成功。

公开序列化结果只保留运行标识、页面观察摘要、终态、步数、面向用户的答案和业务结果摘要，不直接输出原始用户目标、页面 URL、原始 BusinessToolResult 正文或 Ledger 的绝对路径。

### 3.4 Server Agent Session

`ServerAgentSession` 是系统中唯一进行动态决策的 Agent。它持有当前任务、模型上下文、页面观察、预算、已加载 Skill、业务工具、可选知识先验、控制状态和审计记录。

每个 turn 有独立的步数与墙钟预算。每次 provider 请求的 socket timeout 不得超过该 turn 当时剩余的墙钟预算；provider 在截止后才返回时，结果被丢弃并以 `wall_clock_budget` 结束，不能继续触发工具。取消在 provider 排队、建连、已派发 socket、模型返回后、审批前后、业务工具前和页面动作前重复检查，迟到的模型输出不能触发工具。原生 Agent completion 使用可关闭的连接；建连在独立线程中完成，取消后该线程只能关闭连接，不能继续发送 HTTP 请求。合同规划和框架原生 `request_completion` 仍没有同一消息级取消钩子。模型只能通过结构化工具调用行动，不能直接访问浏览器对象、任意网络、数据库或 shell。上下文仅保留有限数量的完整页面观察，较早结果会被压缩，避免上下文随步骤数失控增长。

## 4. ReAct 执行循环

核心循环遵循“推理一次、调用一个工具、读取结果、重新规划”：

```text
最新页面观察
    ↓
模型选择一个工具
    ↓
服务端进行 schema、Skill、策略、路由和写权限校验
    ↓
执行页面动作或业务工具
    ↓
返回 ToolResult / ActionReceipt，并取得新的页面观察
    ↓
用运行时事实修正计划，再进入下一轮
```

具体规则如下：

- 模型一次返回多个工具调用时，全部拒绝且不执行，下一轮收到明确的协议反馈。
- 模型没有调用工具时，系统要求它选择一个工具或显式结束；成功的只读业务查询可以按配置直接结束，若 ToolResult 提供结构化答案投影，记录、来源和完整性由服务端生成。
- 页面元素引用只在产生它的 observation epoch 内有效。
- 已接受的 Skill 加载、业务工具和页面动作都会把结果加入上下文；被预执行策略拒绝的动作会明确标记为未 dispatch。
- 页面动作、成功的服务端工具以及大多数拒绝路径之后都会重新观察页面，再允许模型继续决策。
- 直接跳转只接受当前页面的精确链接、成功业务 ToolResult 返回的精确 `ui_path`，或服务端已验证为只读的知识路由。
- 步数、墙钟、模型预算、策略拒绝上限或控制错误都可以终止循环。

ReAct 的价值在于让计划始终可撤回。源码或 Skill 提供的路线一旦与页面冲突，系统会记录冲突、暂停或撤回计划，后续动作改用最新事实。

## 5. 事实与知识的优先级

知识来源回答的问题不同，不能把它们混成一个“可信度分数”。当前决策顺序是：

| 优先级 | 来源 | 可以证明 | 不能证明 |
| --- | --- | --- | --- |
| 1 | 最新页面观察与动作回执 | 当前 URL、DOM、可见控件、动作是否得到页面 Runtime 回执 | 隐藏业务记录、数据库提交或未显示的权限 |
| 2 | 同一委托用户的业务 API ToolResult / 写后回读 | 当前业务数据，以及适配器实际回读到的写入结果 | 浏览器当前视觉状态 |
| 3 | 经审核的 Business Skill、App Skill 和产品文档 | 工具用途、槽位、只读路由、操作约束和来源 | 当前记录、当前权限或本次动作结果 |
| 4 | 与部署版本绑定的源码证据 | 可能存在的路由、字段、处理器和服务关系 | 运行时数据、租户配置、功能开关和用户权限 |

当同一类事实冲突时，更新且更接近执行点的运行时事实优先。页面负责 UI 真值，业务 API 负责动态业务数据；二者不一致时，Agent 应停止做成功声明并通过新的观察或同用户回读消除冲突。

## 6. Application Adapter 与知识工具

### 6.1 Application Adapter

Application Adapter 是应用专有事实进入通用 Agent 的组合边界。它可以提供：

- 应用 Origin 与同源 PageController 模块；
- 健康检查；
- 委托用户的凭据读取和刷新；
- Business Skill 目录；
- 带固定 schema 的业务 API executor；
- 身份绑定、工具路由、结果数据分类、mutation 后置条件和不确定写入核对端口。

每个 Adapter 必须先通过版本化 `ApplicationDescriptor` 和 `ApplicationPorts` 绑定。Descriptor 固定应用 ID、Origin、导航模式、同源 PageController URL，以及当前 Skill/tool catalog 的 SHA-256。Ports 是会被产品路径实际调用的合同：Identity Port 校验 principal，Route Port 限定 typed tool 可授权的页面路径，Data Port 为结果附加应用声明的数据类别，Postcondition Port 在确认写入后决定能否记录为 completed，Reconciliation Port 为历史不确定写入提供只读核对边界。会话组合在接收用户任务前重新计算 catalog 摘要，并检查端口覆盖所有工具、scope 和 mutation；Descriptor、catalog、端口或运行时接线不一致时拒绝启动。自然语言目标不经过应用专有的正则分类器或有限意图枚举。目标先由通用匹配器按 Skill 自己声明的触发词选出候选，这一步会以 `business_skill_candidates` 记账并标出是否用了兜底；匹配为空时退回一个有界的、按能力挑选的候选集，因此一种没被触发词覆盖的说法不会静默地让某项能力从工具面消失。其余交给通用 Agent，由候选 Skill、typed tool 和页面观察逐步收敛。

Adapter 不拥有 ReAct 循环，也不应把具体应用路由写入 Agent 核心。当前仓库中的 `TwentyApplicationAdapter` 是一条窄纵切：它接入公司查询与创建，验证 Origin，把 OAuth 凭据保留在服务端，并构造 `BusinessToolRuntime` 和五类端口。Minimal Contacts 与测试中的发票 Adapter 使用相同绑定暴露不同 Skill、工具和路由，Agent 核心没有应用名或任务类型分支。当前 Reconciliation Port 对不确定写入仍保守返回需要人工处理；持久 reconciliation key、扫描和恢复调度属于后续可靠性工作。

### 6.2 Business Skill

Business Skill 是不可变的服务端 playbook，包含：

- 唯一 Skill id、标题、意图和触发词；
- 使用步骤；
- 允许出现的 typed tool 名称；
- provenance；
- 是否只读。

Skill 不保存当前业务数据或凭据。Agent 只能先从当前任务匹配到候选 Skill，再显式调用 `load_skill`；对应工具 schema 在加载前不可见。Skill 声明的工具集合必须与 executor schema 完全一致，只读 Skill 不能引用写工具。

这一机制把“先加载能力，再查询或操作”的流程变成服务端可校验的状态转换，而不只是 prompt 中的一句建议。

### 6.3 业务 API 工具

业务 API 工具适合处理页面无法完整暴露的动态数据、分页查询和确定性写后回读。工具只能访问 Adapter 声明的固定操作，不能退化为任意 URL、HTTP 方法或请求体。

`BusinessToolResult` 必须是有限 JSON，并同时提供：

- 给模型使用的业务事实；
- 更小的审计投影；
- 可选的、已出现在结果正文中的安全相对页面路径。
- 对集合查询可选的不可变 `StructuredQueryAnswer`，其中记录、来源、范围、采集时间和完整性均由 Adapter 在 ToolResult 边界生成。

结果拒绝凭据形态的字段，正文大小受限，页面路径必须是无跨源、无路径穿越、无 fragment 的相对路径。模型只能打开本次 ToolResult 实际返回的路径。

如果一次 turn 在成功读取 typed tool 后直接返回答案，且该结果带有 `StructuredQueryAnswer`，产品层会忽略模型对业务事实的转述，并用结构化投影生成用户可见答案。完整性说明排在记录之前；答案长度不足以容纳全部记录时只在记录边界截断，并明确写出展示数量。结构化投影也进入公开 `AgentTurnExecution` 摘要，Session Ledger 记录投影摘要、返回数、展示数和是否展示截断，不记录另一份原始结果。用于定位记录后继续导航的查询不触发该替换，最终打开页面的说明仍由 Agent 给出。

当前 Twenty executor 使用委托用户 OAuth 身份查询公司。公司枚举通过 `pageInfo.endCursor` 和 `starting_after` 进行有页数、记录数与响应一致性上限的 cursor 扫描；cursor 不前进、记录重复、总数变化、缺失分页信息、响应异常或达到上限都会产生带原因的部分结果，不能对用户宣称为完整清单。OAuth token 刷新使用进程内锁与 POSIX 文件锁串行化，在获得锁后重新读取 token，若其他执行者已经替换被拒绝的 token 就直接复用；新 token 以 `0600` 临时文件、`fsync` 和原子替换落盘。创建公司时，它在提案前和 dispatch 前检查重复项，且只有完整扫描当前集合才继续；随后执行一次创建请求，并用同一用户读取新记录。响应或回读无法确定时返回不确定写入，不自动重试。

Twenty Adapter 通过 `/metadata` 查询 `currentUser` 与 `currentWorkspace`，并将页面 session 身份与 delegated OAuth 身份逐字段匹配。只有 application、tenant/workspace 和 actor 全部一致才会生成 `PrincipalContext`。页面 DOM、自报 user id 或 capability 的持有本身都不能证明 Twenty 登录身份。当前 Playwright demo 只把严格匹配 host、根路径、Secure 语义和有效期的 `twenty-session` 或 `__Host-twenty-session` HttpOnly cookie 直接发送到固定 Twenty origin，不跟随重定向、不发送 analytics cookie 或 localStorage，并限制 storage state 与响应大小；普通第一方嵌入部署应由应用服务端登录边界产生等价断言。

### 6.4 App Skill 与源码 Provider

App Skill 适合描述当前菜单不可见、但已审核的只读页面入口。路由模板和 provenance 留在服务端；Agent 只拿到有限的 Skill 卡片，通过当前页面的真实应用入口进入目标应用，再由服务端验证参数并组装同源只读路径。

源码 Provider 是另一种可选知识来源。现有 Session 支持基于版本绑定 SourceMap 的预先 contract、运行时查询和只读候选路由。每条证据携带发布标识、仓库相对路径、行范围和内容摘要。源码内容始终是先验，不能代替页面观察、业务 API 或写后验证。这套机制在 Session 层已经实现，但当前公开组合（`product.py`）从未调用 `observe_entry()`，因此 auto 模式的只读候选路由在产品路径上始终交付空候选集；这条能力目前只能通过直接编排 Session 使用，尚未接入产品组合。

应用无关的 `AgentTurnExecutor` 当前默认关闭 source contract，也没有默认注入运行时源码 Provider。应用不接入源码仍可使用页面和 Business Skill/API 主链路；需要源码能力的产品组合必须显式提供并验证版本绑定。

## 7. 读写控制与用户确认

### 7.1 读取与导航

只读任务可以在 turn 配置中禁用点击、填写、选择和提交，只开放观察及受限导航。页面策略拒绝发生在 dispatch 前；拒绝后通常会取得新观察，让 Agent 选择其他路径。

业务读取必须经已加载 Skill 的 typed tool。它使用 Adapter 管理的委托身份，并把当前数据作为 ToolResult 返回。API 返回的页面路径只是后续导航能力，不自动证明用户目标已经完成。

Business Skill 候选只根据当前用户文本和受限的历史用户文本产生，不读取页面、模型回复或 ToolResult。触发匹配没有得到合格候选时，产品会在固定上限内回退到已安装的只读 Skill；只有当前 turn 已接入真实确认路径时，回退集合才可包含写 Skill。Session ledger 记录 matcher 版本、任务摘要、触发命中、候选 ID、是否兜底和 mutation 开关，不记录原始任务文本。Skill 工件中以 `call tool_name` 形式明确引用的工具必须存在于浏览器工具面或该 Skill 声明的业务工具面，否则应用组合在启动时失败。

### 7.2 服务端业务写入

服务端写工具采用以下固定流程：

1. 校验工具已经由相关 Skill 加载；
2. Adapter 规范化参数并进行可确定的前置检查；
3. 生成包含 effect、targets、规范化参数和随机 nonce 的 mutation proposal；
4. 在页面面板展示 effect 与 targets；
5. 将当前受认证浏览器会话的决定在服务端绑定到 proposal digest；
6. 在持久 mutation journal 中原子地将 proposal 从 `prepared` 认领为 `dispatching`，使并发调用和进程重启都不能再次执行同一 proposal digest；当 proposal 携带 `business_key` 时，认领还会检查同一 `business_key` 下是否已有 `dispatching` 或 `indeterminate` 状态的另一个 proposal，阻止针对同一业务对象的两个并发 proposal 同时通过；
7. 最多发送一次写请求；
8. 将确定结果持久记录为 `completed` 或 `failed`，将无法确定的结果记录为 `indeterminate`；需要业务成功判定的集成还应由 Adapter 进行同用户回读。

`allow_business_mutations` 默认是 `false`。启用时，turn 配置必须同时要求经过验证的 principal；组合还必须提供写工具。`deployment_profile` 默认为 `production`，该 profile 只接受明确声明为跨重启持久的 mutation journal。内存 journal 仅可在显式 `test` profile 中使用，且 test profile 不放宽身份或确认要求。Twenty Adapter 自身也拒绝启用写入却省略 journal。没有显式启用或没有确认通道时，写工具在 dispatch 前终止。用户拒绝对应 `CANCELLED`。

任何发生在 dispatch 后、又无法确定结果的异常都必须对应 `INDETERMINATE`。系统不会因为超时、连接丢失、回执格式错误、回读失败或进程在 dispatch 后崩溃而自动重试写请求。公开 Twenty composition 要求独立的私有 SQLite journal；它持久保留 proposal digest、operation context digest、状态、期限、结果摘要，以及一份供 reconciliation 使用的元数据：按 application id、工具名、租户/工作区/执行者摘要和规范化参数算出的 `business_key`，和包含归一化调用参数（`args`）的 reconciliation 记录。journal 不保存原始身份字段，身份只以租户/工作区/执行者摘要形式保留。journal 解决的是重复发送风险，不提供应用数据库事务或自动判断崩溃前的请求是否已经生效。

这项一次性保证以 proposal digest 为单位。每次 proposal 都包含新的服务端 nonce，因此两个会话分别为同一业务对象生成的新 proposal 目前不会互斥。基于应用声明的业务键和 actor 的跨会话在途互斥属于后续持久恢复工作；在此之前，部署方不应把 proposal 幂等描述为业务语义上的全局 exactly-once。

### 7.3 页面写入

页面中的点击、填写、选择、提交、跳转和返回在部分旧式应用中都可能触发服务端写入，不能仅凭工具名称判断安全性。`ServerAgentSession` 能读取注入 control 的许可与拒绝事件，并在许可已消耗但结果未确认时停止后续潜在写入。

仓库保留的旧 `ControlPlaneClient` 面向一个许可和 trace 均为全局状态的外部网关。客户端会在本机线程和进程间独占这个网关，在 run 开始前重置状态，将 `run_id` 附加到声明，并在读取最终 trace 后再次清空状态才释放租约。密钥只允许从不超过 4 KiB、非链接且不对 group/world 开放的普通文件读取；管理请求拒绝重定向和超限响应。结束清理失败会保留租约并使只读 run 失败，已出现写入决策的 run 则进入 `INDETERMINATE`。这是一条防止协作客户端串 run 的兼容边界，不支持并发页面写入，也不能代替网关内部的 tenant/session/turn/operation scope、TTL 和 replay protection。

当前通用 Turn Executor 不根据自然语言自行生成或安装隐藏 ControlPlan。产品组合只有在外部控制层已经建立精确许可和网络侧 fail-closed 约束时，才能开放页面写动作；否则必须通过 `disabled_actions` 在 dispatch 前关闭这些动作。当前公司创建纵切采用受确认的服务端 API 写入，页面执行层只负责受限导航。

## 8. 状态与错误语义

`AgentTurnExecution.status` 描述 Agent 循环如何结束，不等同于业务成功：

| 状态 | 含义 |
| --- | --- |
| `FINISHED` | Agent 声称完成或返回了允许直接结束的只读答案；仍需应用验证 |
| `REFUSED` | Agent 明确表示无法完成 |
| `CANCELLED` | 用户拒绝了精确写入提案 |
| `INDETERMINATE` | 写操作可能已发生，结果无法证明；禁止自动重试 |
| `FAILED` | 预算、provider、控制、策略或其他运行错误导致结束 |

进入 Agent 循环前的边界错误使用 typed code，例如无页面观察、消息页面已过期、Runtime 不可用或消息格式无效。可重试标志只用于尚未产生写入风险的边界错误。Conversation Host 会向面板发布安全的恢复提示，并继续处理下一轮消息。

终态消息发送失败不会抹掉已经产生的 `AgentTurnExecution`，也不会把它变成可重试任务；失败类型只写入 Ledger。对 `INDETERMINATE`，用户答案固定说明结果不确定，不能被模型生成的乐观文本覆盖。

## 9. 信任与隐私边界

| 边界 | 信任假设与约束 |
| --- | --- |
| 用户与模型输入 | 不可信；严格 schema、长度和工具参数校验 |
| 页面 DOM 与页面文本 | 不可信运行时数据；可以改变计划，不能发出服务端指令或扩大工具能力 |
| Embedded Runtime | 只信任固定实现和当前活动 lease；无模型密钥、业务 token、源码或控制计划 |
| Embedded Gateway | 校验 capability、Origin、lease、schema、大小、截止时间和重放一致性 |
| Server Agent | 可以推理，但模型输出仍不可信；所有动作必须经过确定性工具边界 |
| Application Adapter | 受信任的应用集成代码；负责身份、参数规范化、API 范围和错误分类 |
| Control / Approval | 受信任的确定性边界；确认绑定精确提案，未知情况默认拒绝 |
| Evaluator | 独立的最高信任读取端；Agent 和 Runtime 不得访问隐藏真值 |

所有跨浏览器边界的数据都会检查未脱敏的 session handoff 和 credential assignment。填写值不会进入动作描述或参数回显，Ledger 在写盘前再次脱敏。原始 capability 会进入短期 bootstrap 和 Runtime，并由产品宿主在会话存续期间持有；registry 只保存其哈希。业务 OAuth 凭据只留在 Adapter 侧。

发送给模型的页面观察、业务工具结果、Skill 与源码证据都标记为不可信应用数据。模型可以使用其事实，但不能据此扩大任务范围、获得授权、请求凭据或重新定义工具；最终约束仍由确定性工具 schema、动作策略、principal scope 和确认通道执行。每次 provider 请求在网络调用前经过结构、数量、总字节、有限 JSON 和已知凭据形态检查，并向 Session Ledger 写入模型输入摘要、字节数、角色计数和数据类别，不写入原始 payload。固定版本的外部框架适配器可以通过同一服务端传输提交其原生 OpenAI-compatible 请求体；传输不改写消息和工具选择，但拒绝模型重定向、流式响应和额外字段中的凭据。传输层再次验证完整请求体并限制为 2 MiB。远程 provider endpoint 必须使用 HTTPS，只有 loopback 开发端点可以使用 HTTP；返回体有 4 MiB 上限，HTTP 错误正文不会进入异常或日志。

同一服务进程中的会话必须共享 provider guard。默认最多同时发送 4 个请求；等待槽位的时间会从该次请求的剩余 deadline 中扣除。Guard 接受五类结果：成功、provider 瞬时故障、provider 永久故障、用户取消和本地 deadline。只有前三类改变连续健康故障状态；取消与 deadline 既不增加也不清空计数。连续 3 次 HTTP 408、425、429、5xx、传输异常、畸形 JSON 或超大响应会打开 30 秒冷却电路，在电路打开期间请求不会触网。非瞬时 HTTP 错误或一次成功响应会重置连续故障计数。系统不自动重试已经发送的请求，因为断线或超时无法证明供应商没有完成并计费。排队、建连或请求发送前的取消不写 spend ledger；已派发请求使用随机相关 ID，有可信 usage 的成功调用把 ID、provider 返回的模型标识、token 和费用写入 spend ledger，无法获得可信 usage 的已发送调用写入 `usage_unavailable` 与未知 token/费用，不能按零成本处理。逐 run 摘要只公开模型标识、计数、token、已知费用及请求 ID/账本行的有序摘要，不公开原始相关 ID 或费用行。Session Ledger 分开记录 provider 健康失败、用户取消和本地 deadline；并发出现取消信号不会覆盖已经得到的 provider 失败。Guard 暴露不含内容的进程内状态，但它不是分布式限流器。

这些通用检查不能决定某个应用的普通业务字段是否允许发送到某个模型服务。部署方仍需提供字段级分类、provider/区域策略、保留期和必要的加密或 DLP 边界；在这些策略完成前，不应把包含真实客户数据的页面接入当前 Alpha。

## 10. 产品执行与 Evaluator 隔离

Agent 的 `finish(done=true)`、页面 toast、HTTP 2xx、动作回执和用户确认都不是最终业务成功。

`AgentTurnExecutor` 只返回中性的执行事实。应用组合可以在 turn 结束后，用当前页面或同用户 API 检查公开后置条件，再决定面向用户的结果。基准测试还可以使用隐藏 evaluator 读取只读数据库真值，但该 evaluator 必须位于 Agent 工具、prompt、文件可见范围和 Runtime 协议之外。

这条边界带来三个约束：

- 产品代码不依赖环境 reset、隐藏答案或 benchmark 的 `product_success`；
- evaluator 不参与 Agent 规划，也不能把正确路径泄露给某一运行；
- 页面或业务验证失败时保留 Agent 的原始终态和轨迹，不能事后改写执行记录。

## 11. 当前实现限制

当前代码是一条可运行的本地产品纵切，距离工业级部署仍有明确差距：

- 多个 session 可以共享一个 Gateway，并通过独立 capability 和 channel 隔离；TTL、队列背压和逐类硬配额已经限制单 session 资源占用，但 Remote Session registry、事件序列、消息队列和 lease 仍在单个 Python 进程内存中，进程重启后不能恢复会话。
- Conversation Host 使用进程内线程、队列和有界引用上下文，没有加密的持久会话、外部任务队列、跨实例调度或服务级高可用。进程崩溃会丢失对话引用；当前同步 Python 调用栈也无法从 ReAct 中间步骤安全续跑。
- Session Ledger 是本机 append-only JSONL，逐条 flush 与 fsync；它不是集中式审计存储，也没有跨进程事务。
- capability、Origin、短期 principal、turn、业务工具 scope、审批和 mutation proposal 绑定已经实现；Twenty demo 会匹配页面 session 与 OAuth 身份，并在浏览器 cookie 变化时终止 session；Twenty API 写 proposal 的 dispatch 状态可以持久化。普通嵌入部署的应用服务端身份断言、principal 续期、完整的持久会话、组织级策略和跨进程撤销尚未完成。
- 旧外部页面控制网关仍是全局状态，当前客户端只能通过跨进程独占和双边界 reset 安全地串行运行；网关原生的租户/会话/轮次隔离与崩溃后 TTL 尚未实现。
- 浏览器 sessionStorage 可以处理同一标签页内的刷新恢复，不能替代服务端持久幂等存储。
- 模型调用、页面动作和确认有局部超时；provider 排队和 socket 调用共用剩余 deadline，并受进程内并发槽与瞬时故障短路保护。消息级取消已覆盖审批、业务工具和未认领页面动作；provider 请求的主动中断、跨进程取消、分布式租户配额和完整资源回收仍不完整。
- 目前只有一个窄业务 Adapter 和少量 Skill/API 工具，覆盖面不能代表复杂企业应用的完整操作集。
- 源码能力存在于 Session 层，但默认产品 Turn Executor 尚未将它作为可配置 Provider 接入。
- 页面内助手是一个独立的 React 19 项目（[frontend/](../../frontend/)），以 npm 入口挂进宿主页的 Shadow DOM。它是纯视图：不持会话、不发自己的请求、不存时间线——时间线是服务端把会话事件投影出来的，因此整页导航之后由服务端回放，而不是随旧文档一起丢掉。上限、文案、状态名与调色板全部来自服务端拥有的 `panel_contract.json` 与 `design_tokens.json`，面板自行声明任何一项都会被契约测试判红。

  面板与宿主页共用同一个 JavaScript realm（不用 iframe，2026-09-11 定）。代价写在 `SECURITY.md`：宿主页里的脚本可以完成一次审批；CSP 与 Trusted Types 是宿主页的响应头，面板按"永不需要放宽"来写并有回归测试（不用 `innerHTML`、不注入内联样式或脚本、样式走 `CSSStyleSheet.replaceSync`），但开不开由宿主页决定。确认卡仍然拒绝非真实手势、以及它无法证明当时可见的按下——这堵住的是视觉欺骗，不是伪造。

  Chromium 覆盖契约里的每个状态、确认卡的键盘路径与 320px 布局、Markdown 渲染与链接策略、减弱动画、窗口拖动缩放，以及 strict CSP + Trusted Types 下的构造。发布前仍需完成 VoiceOver/NVDA 与支持浏览器矩阵测试。
- 当前嵌入模式假定第一方页面可信；面向第三方页面的隔离执行形态尚未实现。

## 12. 演进方向

以下内容是后续架构方向，不是当前已实现能力：

1. 把 session、事件 cursor、动作 claim、确认和幂等结果迁移到可恢复的持久存储，同时保持现有 wire schema。
2. 将现有短期 principal/operation 绑定接入可信应用登录会话，补充组织策略、持久撤销和跨进程强制执行。
3. 将 Conversation Host 迁移到有租约、取消、deadline、背压和故障隔离的 worker 执行模型。
4. 把 Application Adapter 抽成稳定 SDK，增加契约测试、能力声明、统一错误语义和同身份验证接口。
5. 将源码、产品文档和其他知识源统一为可开关 Provider，保留运行时事实优先和版本 provenance。
6. 为页面写入补齐通用的提案、网络侧许可、动作关联和写后验证合同，再按证据决定可开放的动作面。
7. 在核心协议稳定后，评估更强隔离的页面执行端；嵌入面板与浏览器扩展可以共享协议，但需要不同的信任模型。
8. 建立开发集、冻结测试集和强基线，分别测量页面执行、Skill/API、源码开关、写入安全、恢复能力、延迟和成本。

## 13. 代码导航

- 单轮产品边界：[product.py](../../src/web_operation_agent/execution/product.py)
- 单轮绑定、会话构造与终态结算：[turn.py](../../src/web_operation_agent/execution/turn.py)
- 多轮会话与本地产品宿主：[live.py](../../src/web_operation_agent/live.py)
- 有界对话引用状态：[conversation.py](../../src/web_operation_agent/state/conversation.py)
- 单 Agent ReAct 循环：[session.py](../../src/web_operation_agent/execution/session.py)
- Business Skill 与业务工具合同：[runtime.py](../../src/web_operation_agent/business/runtime.py)
- 当前 Application Adapter 示例：[twenty.py](../../src/web_operation_agent/twenty.py)
- Embedded Gateway：[gateway.py](../../src/web_operation_agent/transport/gateway.py)
- 浏览器端入口 `assistant.js`（启动监督者 + Runtime，构建源在 [frontend/src/runtime/](../../frontend/src/runtime/)）：[boot.ts](../../frontend/src/runtime/boot.ts)、[runtime.ts](../../frontend/src/runtime/runtime.ts)
- 助手面板（React 19 + Shadow DOM，构建源在 [frontend/](../../frontend/)）：[mount.tsx](../../frontend/src/mount.tsx)
- 面板与 Runtime 的契约、上限与文案（服务端唯一来源）：[panel_contract.json](../../src/web_operation_agent/web_runtime/panel_contract.json)
- Runtime 侧的面板宿主与传输失败分类：[panelHost.ts](../../frontend/src/runtime/panelHost.ts)
- 服务端把会话事件投影成面板时间线：[panel_view.py](../../src/web_operation_agent/panel_view.py)
- 会话协议与幂等通道：[protocol.py](../../src/web_operation_agent/transport/protocol.py)、[channel.py](../../src/web_operation_agent/transport/channel.py)、[registry.py](../../src/web_operation_agent/transport/registry.py)
- 版本化 wire schema：[schemas.py](../../src/web_operation_agent/schemas.py)
- 主体与操作上下文：[identity.py](../../src/web_operation_agent/state/identity.py)
