# Web Operation Agent

[English](README.md)

Web Operation Agent 是一个 alpha 阶段的框架，用于在第一方 Web 应用中嵌入 AI 助手。
规划、模型访问、凭据、策略、审批和审计状态都留在服务端；小型浏览器 Runtime 只观察
当前页面、执行固定通用动作、展示进度与确认 UI，并返回回执。

仓库只保留一个公开参考实现：[Odoo 18](examples/odoo/README.md)。它不是托管服务、
通用浏览器扩展，也不是已经适合生产的自治 Agent。

## 当前能力

- 服务端 ReAct 循环在每次决策前接收工具结果与新的页面观察，并受步骤、时间、token
  与模型费用预算限制。
- 应用拥有的 Skill 与类型化工具提供当前业务事实；显式启用后，绑定部署版本的结构化事实
  与语言无关的本地代码块索引可回答源码问题；准确的文件、行号、release 与字节摘要
  留在服务端核验和审计，不作为用户答案展示。对源码结论的转述，服务端还核对模型提交的
  简短支持片段确实出现在引证证据中；这不能证明模型对片段的解释一定正确。
  可选的本地 Git Provider 把所有权、blame、commit 与版本差异元数据单独绑定到部署 commit。
  源码检索仍是可选能力，不是普通浏览器或 API 操作的前置条件。为操作检索源码不会
  自动把该操作变成源码问答：操作结果只依据浏览器实际观察的回执；源码问答仍需校验支持证据。
- 应用无关的实体读取合同可以发现当前 actor 可读的业务实体，不必为每个实体写一份 Skill
  就能执行有界读取。Agent 只拿到与当前目录绑定的不透明实体/字段 ID；Adapter 把自己的
  ORM、REST、OpenAPI 或其它原生元数据翻译到合同之后。核心校验仅 AND 的过滤 AST 与分页
  上限；合同要求 Adapter 提供稳定排序和准确计数。Odoo 与结构不同的合成 Contacts REST
  Adapter 运行同一组合同测试。
- 可选的应用无关当前表单合同为无 Skill 场景提供安全的标量字段预填兜底。
  `describe_form` 只取最新视口控件与同 actor Adapter 元数据的交集，并返回短期不透明 ID；
  `prepare_form` 重新核对两侧事实，把准确的原生绑定只保留在服务端；`prefill_form` 按草稿
  填写后立即结束，绝不点击或提交。Odoo 与 Contacts 把不同元数据来源翻译到同一合同。
  可选的源码检索可以为已有字段的取值提供依据；由源码推导的值在内部记录本轮引用和原样支持
  片段。这只能核对来源，不能证明模型解释正确。源码不能凭空增加字段、selector、选项、权限
  或写入授权。
  Odoo 参考翻译仍是实验能力，必须由 Adapter 显式开启。三个标准模块案例的真实浏览器门已
  在零提交、零新增记录下通过，但这不能证明任意部署自定义的 `onchange` 没有副作用。
- Turn 按来源职责组装上下文：当前页面是实时 grounding 基线；只有指代性后续或待澄清回复
  才带入有界对话历史；Skill 按当前任务选择；部署源码保持惰性的、带引证的查询，而不是
  整库注入。
- 浏览器动作会按当前观察和应用路由策略校验。页面观察只覆盖当前视口且最多暴露 200 个
  实时控件；有界的只读滚动会取得下一份新观察，而不是扫描无界的整页 DOM。换页需要用户可见确认。
  确认只被它所针对的那次换页消耗：回答确认时页面上的菜单被关掉，重新打开菜单再点同一项不会再问一次。
  当前页面内的成功点击只有在新观察显示页面变化时才可作为交互回执报告；该回执
  本身不证明业务目标已经完成。已观察到动作后，答案格式修复不能再次派发页面动作；
  源码辅助的页面循环若反复回到相同事实和状态，会停止而不宣称完成。
- 当请求指向一个页面状态（打开某个界面、切换某个 tab、到达某条记录）时，Agent 先用
  `set_target` 声明该状态：一个精确的同应用路径，和/或必须显示为已选中的控件的可访问名称。
  之后每次成功且无写入的页面动作，服务端都会把新观察与声明比对；一致时服务端报告已到达，
  此后只接受 `finish` 与 `set_target`（其他调用被拒绝且不派发；工具清单本身保持不变，以免模型服务的前缀缓存失效），模型要么报告该动作，要么声明下一个状态，
  而不会继续调查已经到达的页面。用户看到的句子由观察到的页面组成，不来自模型文字。声明目标
  不授予任何权限：点击与导航仍按原有方式路由、确认与结算；同一窗口内出现写入时改由写入路径
  结算。服务端从不因目标达成而自行结束本轮：它能核验页面状态，却不能核验该状态就是整个请求。
  问"某个界面、列表或设置在哪里"时，助手直接把它打开作为回答；引用的源码或 Skill 给出菜单位置时一并说明。
- 通用点击作为动作可以保持禁用，同时由应用放开其中一部分：可选的点击策略根据当前观察和被点的元素，逐次决定允许或在派发前拒绝。Odoo 参考实现允许：标签页、Home 按钮、指向审查过的只读页面（及从中打开的记录）的同应用链接、这些页面上具名的视图切换、翻页、搜索和菜单控件，以及审查清单标明可打开行的页面上的列表行和看板卡片（点击即打开该记录）；在记录页上只允许记录翻页、顶部菜单，以及指向关联记录的链接（订单的客户、发票的付款条款，形如 `/odoo/<model>/<id>`）；关联记录页本身未经审查，所以只能走这一步。其余控件（新建、操作菜单、表头工作流按钮、勾选行、行内编辑、模块激活、无名按钮、未审查页面上的一切）一律拒绝。审查清单放在 Adapter 中，动作以 XML ID 登记，并对已部署数据库解析出动作号和 URL 路径。点击策略还可以列出它审查过的目的地，助手可以直接按地址打开这些页面，而不必先找到菜单；Odoo 允许审查过的页面及从中打开的记录，查询参数最多带 `view_type`。只靠 class 标明当前页签的页面（Odoo 18 的 notebook 用 Bootstrap 的 `.active`），Runtime 会报告为 `selected=true`，这样声明的页签才能被核验。
- 应用还可以在 Agent 操作页面期间把已登录会话变成只读：在描述符上声明 `page_write_guard` 路径，Runtime 在每个页面动作前向 `<path>/begin` 请求开窗，应用未确认就拒绝该动作，本轮结束时向 `<path>/end` 关窗。Odoo 的 `woa_runtime` 1.5 实现了它：窗口打开期间该会话的每个请求都在 PostgreSQL 只读事务中执行，写入被拒绝且不保存任何内容，因此审查清单遗漏的点击、或 Web 客户端自发的自动保存都改不了数据。窗口有效期间丢失结果的页面动作（比如模型把菜单按钮当成了链接）因此按"没有写入"结算，而不是"写入不确定"，本轮继续；回执会说明窗口是否全程有效，服务端只对声明了守卫的应用采信这一点。部署本 Adapter 前先部署 1.5 版插件代码：插件代码较旧时 Runtime 无法开窗，所有页面动作都会被拒绝。
- 请求措辞没有命中任何 Skill 触发词时，目录的只读兜底仍可装载，但以"未匹配本请求"的索引呈现，
  而不是作为本任务的候选。同一轮内结果与此前某次 typed 读取完全相同时，只返回简短提示而不重发
  整份结果；第三次重复以 `no_progress` 结束且不宣称完成；完成一次写入后重新计数。
- 写入默认关闭。核心 turn 配置默认关闭所有可能触发应用写入的浏览器动作（包括导航），
  Adapter 必须显式开放经过审查的子集。启用后的写入把准确影响与目标绑定到一次性的新鲜确认，
  最多派发一次，并由应用核验后置条件。超时或结果不确定的写入记录为 `indeterminate`，
  绝不自动重试。
- 短期 actor/tenant/workspace 主体可绑定浏览器 capability、工具 scope、审批、mutation、
  审计与撤销状态。
- React 助手面板只是服务端状态的视图；文案、边界、状态名与配色由服务端合同定义。
- 可选 SQLite 存储提供 session 所有权、进度 outbox、有界会话上下文、mutation journal、
  备份与显式人工裁决。

这些只是已实现路径的说明，不是通用成功率结论；当前没有足够大的公开 benchmark 可以
证明其优于其它浏览器 Agent。

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

核心包保持应用无关。应用行为只通过 Adapter、Skill、类型化工具和应用自己的核验进入。
Odoo 作为独立的 `web-operation-agent-odoo` distribution 发布。
[`testapps/`](testapps/contacts/README.md) 下的合成 Contacts 应用只用于内部浏览器与协议测试，
不是第二套公开参考实现。

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

接入新应用时，请按 [Odoo 阅读顺序](examples/odoo/README.md#reading-guide-for-integrators)：
先看 Adapter 组合和工具规格，再看读写、身份与路由、Skill、turn 组合，最后看部署。

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

`.env` 已被 Git 忽略，用于当前 checkout 的本地配置；`.env.example` 不含任何凭据。
Odoo 装置的 `provision` 命令会打印准确的 Odoo 配置值。脚本模式无需模型配置，源码和
可恢复装置两组配置也都是可选的。

运行核心测试：

```bash
python src/web_operation_agent/tests/run_all.py
```

运行仓库门禁：

```bash
scripts/qualify.sh
```

`--require-browser` 会把 Chromium 和前端构建前置条件设为必需；`--require-live` 还会要求
已配置的真实 Odoo 资格门禁。`BLOCKED` 表示没有执行，不能算通过。

独立 Odoo evaluator 的离线装置可单独运行：

```bash
PYTHONPATH=src:examples/odoo/src:. \
  python scripts/offline_suite.py --start-dir evals/odoo/tests --top-level .
```

## Odoo 参考实现

Adapter 提供五个领域专用有界读取、一个通用读取兜底和四个可选写入。专用工具覆盖客户、
产品、销售订单、报价单和客户发票。它把 Odoo 的 HttpOnly 浏览器 session 与单独委托的
逐用户 API key 绑定；只有两者指向同一数据库、公司和 actor 才建立会话。读取报告完整性，
写入执行应用拥有的重复检查与回读。

Adapter 包含版本化 Skill，以及面向 Odoo 菜单、action、view、字段、方法说明与用户可见
拒绝条件的可选源码编译器；通用原始代码索引同时覆盖 allowlist 内的 Python、JavaScript、
XML 与权限声明。两种索引都收窄到当前部署实际安装的模块；用户答案直接给结论，
源码引证字节与位置只在服务端核验和审计。若另有完全匹配的本地 Git commit，
还可以启用历史查询；只有镜像提取树而没有
`.git` 时会明确报告历史不可用。当源码或文档与运行时冲突时，以当前运行时事实为准。已安装 addon
还提供有界、按当前 actor 裁剪的 model/action 目录。Odoo Adapter 把它翻译到核心
`discover_entities`、`describe_entity`、`query_entity_records` 合同，再把校验后的通用过滤
映射为同一委托 actor 下的有界 Odoo 读取。原生模型名和 RPC 方法不会成为 Agent 参数；
这也不授权写入。
这样找到的记录会带上它的打开地址（`/odoo/<模型>/<记录号>`，Odoo 18 对任何有读权限的用户都能打开），`goto` 只能原样使用这个地址。

从 checkout 准备可丢弃的本地装置：

```bash
PYTHONPATH=src:examples/odoo/src:. \
  python -m web_operation_agent_odoo.deploy provision
```

私有单节点部署及简短重建命令位于
[`examples/odoo/deploy/single-node/`](examples/odoo/deploy/single-node/README.zh-CN.md)。
重建必须一次完成 Agent 更新、Odoo addon 升级和 Odoo 重启：

```bash
./examples/odoo/deploy/single-node/rebuild.sh
```

身份、路由、工具行为、源码编译、装置准备与资格测试详见
[Odoo 接入指南](examples/odoo/README.md)。

## 已知限制

- 项目仍处于 alpha；接口兼容策略尚未稳定。
- 尚未实现跨主机扩缩容、活跃任务迁移和跨主机撤销。
- 应用必须提供自己的可信登录断言、委托凭据、授权映射和应用专有后置条件核验。
- 浏览器自动化不能提供数据库事务或回滚；不确定写入需要应用读取和明确人工裁决。
- 本地源码检索能匹配源码中的汉字标签和注释，但不会把中文业务问题翻译成英文代码标识符。
  跨语言召回仍依赖 Agent 选对源码词项，尚未经过充分资格验证。
- 源码辅助的无 Skill 表单预填已有生产会话的脚本合同测试，但尚无独立的真实模型成功率结果；
  这不证明任意页面操作都已覆盖。
- 启用源码检索并使用托管模型时，检索到的 allowlist 内源码会进入模型请求；只有部署的数据
  政策允许时才组合使用。
- 浏览器与无障碍覆盖以 Chromium 最强，其它引擎与读屏器尚未全面资格验证。

## 许可证

参见 [LICENSE](LICENSE) 与 [THIRD_PARTY.md](THIRD_PARTY.md)。
