# Web Operation Agent

[English](README.md)

Web Operation Agent 是一个 alpha 阶段的框架，用于在第一方 Web 应用中嵌入 AI 助手。规划、模型访问、凭据、策略、审批和审计状态都留在服务端；小型浏览器 Runtime 只观察当前页面、执行固定通用动作、展示进度与确认 UI，并返回回执。

仓库只保留一个公开参考实现：[Odoo 18](examples/odoo/README.md)。它不是托管服务、通用浏览器扩展，也不是已经适合生产的自治 Agent。

## 当前能力

- 目标式回合让模型对照应用索引声明请求。索引由 Adapter 以当前用户身份编译，包含实体、字段、页面、视图、筛选、分组、页签和控件。服务端随后确定性地解析每一项声明：记录用应用自己的名称搜索，字段值用类型化读取，计数、求和与数值极值用限定范围的查询，页面用已审核路由，页签按可见名称，筛选和分组按页面自身状态，要做的修改则把对应控件高亮出来交给用户。答复由解析出的值生成，不采用模型的文字，所以回合报告的事实都是它读到的。做不到的部分如实说明；所有声明项都落实后，回合才算完成。多条记录都符合又无法区分时，回合会追问是哪一条。指名的记录不存在时，不会改成新建。条件支持数字、日期和文本上的比较；日期可以是某天、某月、某年、区间或命名时段，按用户时区计算。声明是模型输出，可能选错实体、字段或范围；这条路径会增加模型调用和耗时，不是一次调用完成的回合。
- 目标式回合可以预填表单并交接：填入用户给的值，核对各字段随后显示的内容，高亮提交按钮，但从不提交；无法确定填对的值留给用户并说明。表单预填默认关闭，需要应用安装已审核的表单端口并设置 `allow_form_prefill=True`。通过页面自身查询控件做筛选同样由应用显式开启，并且要等应用报告查询已生效才算完成。
- 挂接编译后的源码图谱时，索引可以带上部署源码里的规则（控件何时显示或禁用、字段何时必填或只读、方法在什么条件下拒绝），答复会引用准确的源码行。Adapter 还可以带上本组织经过审核的用语约定，说明某些词在这里指什么。
- 服务端 ReAct 循环在每次决策前接收工具结果与新的页面观察，并受步骤、时间、token 和模型花费上限约束。集成方可以显式开启：绑定当前 actor 的数据读取（`AgentTurnConfig.data_reads`）、由应用签发的动作效果（`action_policy`）、导航的渲染路由证明（`require_page_route`）、事先的任务声明（`require_task_contract`）。它们默认都关闭，都不授予写权限。
- 应用拥有的 Skill 与类型化工具提供当前业务事实。显式启用后，绑定部署版本的源码检索可以回答源码问题，文件、行号、版本和摘要等出处保留在服务端。源码检索是可选的，不是普通浏览器或 API 操作的前提；源码不授予导航、API 或写入权限。
- 逐步 Agent 可以在当前页面观察的有界、已脱敏证据中搜索或翻页。这不会搜索应用数据，也不能证明某项内容不存在。
- 应用无关的实体读取合同可以发现当前 actor 可读的业务实体并做有界读取，不必为每个实体写一份 Skill。另有记录引用读取，按名称或编号找候选；有歧义时返回全部候选，不替用户选。Odoo 与结构不同的合成 Contacts REST Adapter 运行同一套合同测试。
- 另有只读的 `find_page_entries`，按当前 actor 的页面和动作元数据给候选排序，只为 Adapter 审核过的独立目的地返回路由。这不代表应用的每个页面都可操作。
- 只读的 `resolve_goal_candidates` 可在一次调用中组合最多三个问答、页面或交接目标的候选检索。它不替用户选候选，也不代表目标已完成。
- 可选的应用无关当前表单合同为无 Skill 场景提供安全的标量字段预填兜底（`describe_form`、`prepare_form`、`prefill_form`）。它输入草稿、核对字段的实时值，从不点击提交控件。Odoo 的标准新建表单在离开时会自动保存，所以通用的手动提交预填在那里会被拒绝；它的类型化客户新建和编辑交接需要开启写入确认通道并取得新的审批。
- Turn 按来源职责组装上下文：当前页面是实时基线；后续追问会带上有界的对话历史；Skill 按当前任务选取；部署源码按需检索并引用，不整体注入。
- 浏览器动作会按当前观察和应用路由策略校验。页面观察只覆盖当前视口，最多暴露 200 个可操作控件。操作页面需要用户同意：每个回合在第一次页面操作（打开页面、点击、填写、选择、提交、返回、应用筛选）时用一张可见卡片征得一次，本回合其余操作据此进行，下一条消息重新征求。读取、滚动、等待和高亮不需要征求。这份同意不是写入审批：每次业务修改仍在各自的卡片上单独确认。
- 助手无法代做的页面操作，可以用 `highlight` 标出一个当前可见、具名的控件，并在不点击、不提交的情况下结束回合。这是引导式交接，不代表业务修改已经发生。
- 收在折叠菜单里的控件（比如记录“操作”菜单里的“删除”）在任何应用里用同一个办法找到：目标式回合最多展开三个页面自己声明为折叠的菜单，标出控件，并在答复里说明它在哪个菜单里。不会选中菜单里的任何一项。
- 当请求指向一个页面状态（打开某个界面、切换页签、应用筛选或分组、到达某条记录）时，Agent 先用 `set_target` 声明这个状态，服务端把每次新的观察与声明比对后才报告到达。声明目标不授予任何权限。用户读到的那句话由观察到的页面生成，不采用模型的文字。
- 通用点击作为动作可以保持禁用，同时由应用通过可选的点击策略放开其中一部分。Odoo 参考集成放开页签、主页按钮、指向已审核页面和记录的链接，以及这些页面上具名的视图切换、翻页、搜索和菜单控件。工作流按钮、新建、操作、行选择、行内编辑和未审核页面仍被拒绝。
- 声明了页面只读守卫的集成，可以在当前观察中提供最多 32 个由应用列出的筛选与分组选项；可选的 `apply_facet` 工具应用其中一个，并以新观察到的生效状态为准。
- 应用可以声明 `page_write_guard` 路径，在 Agent 操作页面期间把已登录会话变成只读。Odoo 的 `woa_runtime` 1.5 实现了它：窗口开启期间，该会话的每个请求都在只读事务中执行。请先部署 1.5 版 addon，再部署这个 Adapter。
- 已注册的类型化工具不要求匹配或装载 Skill；Skill 提供可选的业务知识，不负责安装工具或授予权限。工具 schema 按部署的写入开关和当前 actor 的权限范围过滤，派发时再次校验。
- 只有用户的请求本身能要求写入。请求把某个写动作当作要做的事说出来时，才会提供对应的写工具；记录、页面或模型输出里的文字都不算。英文否定句（“don't create”）目前还识别不了，这种情况仍有审批卡片兜底。
- 写入默认关闭。核心 turn 配置默认关闭所有可能触发应用写入的浏览器动作（包括导航），Adapter 必须显式开启自己审核过的子集。开启的写入把准确的效果和对象绑定到一次性的新审批上，最多派发一次，并通过应用核验结果。超时或结果不确定的写入记为 `indeterminate`，不会自动重试。
- 短期 actor/tenant/workspace 主体可绑定浏览器 capability、工具 scope、审批、mutation、审计记录和撤销状态。
- React 助手面板只是服务端状态的视图。启动按钮从脚本运行起就在页面上。用户发送消息或重新打开对话时建立会话，空闲五分钟后释放；失效的会话在下一条消息时恢复，启动失败会在面板里说明并提供重试。打开面板时会恢复当前用户在服务端的历史。
- 一个会话可以同时在多个页面上打开。所有页面看到同一段对话；回合在发起它的页面上运行，只有那个页面会收到页面指令。
- 面板有两种形态：贴着启动按钮所在角落弹出的浮层，和位于页面右侧、占满窗口高度的抽屉，标题栏上的按钮可以切换。视口宽度不小于 900px 时，抽屉会把宿主页面收窄；应用按视口定位的元素（对话框、通知）不会被收窄，可能有一部分落在抽屉下面。所选形态、位置和宽度保存在浏览器的本地存储里。
- 可选 SQLite 存储提供 session 所有权、进度 outbox、有界会话上下文、mutation journal、备份和明确的人工结算。

以上说的是已实现的路径，不是总体成功率。仓库没有规模足够的公开基准来证明它优于其它浏览器 Agent。

## 架构

```text
第一方页面
  assistant.js + 面板 + 固定浏览器工具
                    │ 已认证的观察/动作/决定
                    ▼
服务端组合
  session 策略 · 规划/模型 · 审批 · 审计 · 持久状态
                    │ 类型化应用端口
                    ▼
应用 Adapter
  身份 · 路由 · 读取 · 写入 · 核验 · 可选源码图谱
```

核心包保持应用无关。应用行为只通过 Adapter、Skill、类型化工具和应用自己的核验进入。目标式回合由核心依据 Adapter 提供的目标数据（索引、结构描述与读取）解析，Adapter 自己不解析声明。Odoo 作为独立的 `web-operation-agent-odoo` distribution 发布。[`testapps/`](testapps/contacts/README.md) 下的合成 Contacts 应用只用于内部浏览器与协议测试，不是第二套公开参考实现。[实验性芋道适配器与可重置部署](examples/yudao/deploy/README.md)提供另一套目标数据实现，不代表已具备生产上线条件。

芋道原生宿主默认关闭写入、表单填写和点击。它的各项页面操作试点（原生导航、页签点击、列表筛选、表单预填、`--executor page-agent` 规划器）都需要显式开启，并且要有部署方审核过的策略；任何一项都不开启自动提交。

重要文档：

- [系统设计](docs/architecture/system-design.md)
- [应用 Adapter 合同](docs/integrations/application-adapter.zh-CN.md)
- [持久运维](docs/integrations/durable-operations.zh-CN.md)
- [安全边界](SECURITY.md)

## 仓库结构

```text
src/web_operation_agent/       应用无关的服务端与浏览器 Runtime
examples/odoo/                 唯一公开参考接入
  src/web_operation_agent_odoo/  可安装 Adapter
  deploy/reference/              可丢弃的本地 Odoo 装置
  deploy/single-node/            单节点部署示例
  deploy/addons/                 两种部署共用的 Odoo Runtime addon
evals/odoo/                    独立 Odoo evaluator 与密封任务装置
  domain/ fixture/ execution/  评测规格、装置与运行流程
  provider/ judging/ reporting/ 模型中介、判定与证据报告
evals/pageagent/               通用 PageAgent 对比桥接
testapps/contacts/             内部合成资格测试应用
frontend/                      助手面板源码
```

接入新应用时，请按 [Odoo 阅读顺序](examples/odoo/README.md#reading-guide-for-integrators)：先看 Adapter 组合和工具规格，再看读写、身份与路由、Skill、turn 组合，最后看部署。

## 开发环境

支持 Python 3.12 与 3.13。完整 Runtime 与面板测试还需要 Node.js。

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 python -m pip install --editable .
python -m pip install --no-deps --editable examples/odoo
```

需要模型的本地运行可复制精简配置模板，并且只填写实际使用的部分：

```bash
cp .env.example .env
```

`.env` 已被 Git 忽略，用于当前 checkout 的本地配置；`.env.example` 不含任何凭据。Odoo 装置的 `provision` 命令会打印准确的 Odoo 配置值。脚本模式无需模型配置，源码和可恢复装置两组配置也都是可选的。

模型由 `AGENT_OPENAI_MODEL` 选择。修改后，源码安装的服务需要重启；wheel 或容器安装需要重新构建并部署才能生效。

构建浏览器资源。网关提供的助手脚本和轨迹回放页都是生成物，不提交进仓库；没有构建时，浏览器里没有助手，依赖浏览器的测试和那项回放页测试会失败或跳过：

```bash
python scripts/build_runtime.py --install
python scripts/build_panel.py
```

`--install` 在新检出上先执行一次 `npm ci`。拉取前端改动后要重新构建；另开的 worktree 也要各自构建。

运行核心测试：

```bash
python src/web_operation_agent/tests/run_all.py
```

运行仓库门禁：

```bash
scripts/qualify.sh
```

`--require-browser` 会把 Chromium 和前端构建前置条件设为必需；`--require-live` 还会要求已配置的真实 Odoo 资格门禁。离线门禁包含芋道适配器、宿主生命周期、评测装置及独立 wheel 的导入与资产检查。`BLOCKED` 表示没有执行，不能算通过。

独立 Odoo evaluator 的离线装置可单独运行：

```bash
PYTHONPATH=src:examples/odoo/src:. \
  python scripts/offline_suite.py --start-dir evals/odoo/tests --top-level .
```

## Odoo 参考实现

Adapter 提供五个领域专用有界读取、一个通用读取兜底和四个可选写入。专用工具覆盖客户、产品、销售订单、报价单和客户发票。它把 Odoo 的 HttpOnly 浏览器 session 与单独委托的逐用户 API key 绑定；只有两者指向同一数据库、公司和 actor 才建立会话。读取报告完整性，写入执行应用拥有的重复检查与回读。

Adapter 包含版本化 Skill，以及面向 Odoo 菜单、action、view、字段、方法说明与用户可见拒绝条件的可选源码编译器。编译出的事实和通用原始代码索引都收窄到当前部署实际安装的模块。当源码或文档与运行时冲突时，以当前运行时事实为准。已安装 addon 还提供有界、按当前 actor 裁剪的 model/action 目录，Adapter 把它翻译到核心的实体读取合同；原生模型名和 RPC 方法不会成为 Agent 参数。

从 checkout 准备可丢弃的本地装置：

```bash
PYTHONPATH=src:examples/odoo/src:. \
  python -m web_operation_agent_odoo.deploy provision
```

私有单节点部署及简短重建命令位于 [`examples/odoo/deploy/single-node/`](examples/odoo/deploy/single-node/README.zh-CN.md)。重建必须一次完成 Agent 更新、Odoo addon 升级和 Odoo 重启：

```bash
./examples/odoo/deploy/single-node/rebuild.sh
```

身份、路由、工具行为、源码编译、装置准备与资格测试详见 [Odoo 接入指南](examples/odoo/README.md)。

## 已知限制

- 项目仍处于 alpha；接口兼容策略尚未稳定。
- 尚未实现跨主机扩缩容、活跃任务迁移和跨主机撤销。
- 应用必须提供自己的可信登录断言、委托凭据、授权映射和应用专有后置条件核验。
- 浏览器自动化不能提供数据库事务或回滚；不确定写入需要应用读取和明确人工裁决。
- Odoo 页面守卫不能隔离外部副作用、其它浏览器会话、自定义请求中途提交，或窗口解除后客户端自行重试；不能把它当作通用写入沙箱。
- 本地源码检索能匹配源码中的汉字标签和注释，但不会把中文业务问题翻译成英文代码标识符。跨语言召回尚未经过充分资格验证。
- 源码辅助的无 Skill 表单预填已有脚本合同测试，但尚无独立的真实模型成功率结果；这不证明任意页面操作都已覆盖。
- 启用源码检索并使用托管模型时，检索到的 allowlist 内源码会进入模型请求；只有部署的数据政策允许时才组合使用。
- 浏览器与无障碍覆盖以 Chromium 最强，其它引擎与读屏器尚未全面资格验证。
- “已到达”只按 Agent 声明的内容核对（路径、选中的控件、已生效的筛选或分组）。这不能证明所有异步加载都已完成；声明里漏掉的要求可能仍未满足。
- 模型选择的查询未必覆盖用户的意图；读取返回空结果时，结果会交回 Agent，不会直接以“未找到”结束回合。
- 列表的自由文本搜索由限定范围的读取回答，不会把文字输入页面的搜索框。新建交接只高亮入口控件，不预填也不提交。
- 目前的评测结果来自同一个参考部署上的小样本，其中若干由实现者自己出题；它们不能证明总体准确率。见 [Odoo 评测说明](evals/odoo/README.md)和[芋道对照结果](evals/yudao/tool_loop/RESULTS.zh-CN.md)。

## 许可证

见 [LICENSE](LICENSE) 与 [THIRD_PARTY.md](THIRD_PARTY.md)。
