# Web Operation Agent

[English](README.md)

Web Operation Agent 是一个 alpha 阶段的框架，用于在第一方 Web 应用中嵌入 AI 助手。规划、模型访问、凭据、策略、审批和审计状态都留在服务端；小型浏览器 Runtime 只观察当前页面、执行固定通用动作、展示进度与确认 UI，并返回回执。

[`examples/`](examples/) 下有两个参考接入：[Odoo 18](examples/odoo/README.md)（保留作参考，不再维护）与[芋道](examples/yudao/deploy/README.md)（默认演示与测试应用，带可重置的本地部署）。它不是托管服务、通用浏览器扩展，也不是已经适合生产的自治 Agent。

## 当前能力

- 目标式回合：模型对照应用索引声明请求，服务端确定性地解析（记录、字段值、计数求和、页面、页签、筛选），答复由解析出的值生成。歧义记录会追问；所有声明项落实后回合才算完成。
- 表单预填与交接：填入用户给的值、核对结果、高亮提交按钮，但从不提交。按应用显式开启。
- 可选的源码规则与引用式回答，以及组织审核过的用语约定。
- 逐步 ReAct 执行器，以及保留同一协议、由模型从一小组固定工具里逐步选择的实验性 `--executor slim` 回合。
- 应用拥有的 Skill（Markdown 知识与流程）加独立注册的类型化工具；知识与 Skill 不授予任何权限。
- 写入默认关闭。开启后每项写入绑定完整可见审批、最多派发一次、经应用核验；不确定的写入绝不自动重试。
- React 助手面板只是服务端状态的视图，支持多页面会话，可用可选 SQLite 存储持久化会话上下文与 mutation journal。

以上说的是已实现的路径，不是总体成功率。详细行为、执行器差异与各集成 specifics 见[运行时行为详情](docs/reference/runtime-behavior.md)。

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

文档见 [docs/](docs/README.md)：concepts 讲为什么，guides 讲怎么做，reference 记当前行为。

- [系统概览](docs/concepts/overview.md) · [执行器](docs/concepts/executors.md) · [写入模型](docs/concepts/writes.md) · [Skill 与类型化工具](docs/concepts/skills-and-tools.md)
- [接入应用](docs/guides/integrate-an-application.md) · [持久化运维](docs/guides/operate-durably.md)
- [运行时行为详情](docs/reference/runtime-behavior.md)
- [安全边界](SECURITY.md)

## 仓库结构

```text
src/web_operation_agent/       应用无关的服务端与浏览器 Runtime
examples/odoo/                 参考接入（Odoo 18），不再维护
  src/web_operation_agent_odoo/  可安装 Adapter
  deploy/reference/              可丢弃的本地 Odoo 装置
  deploy/single-node/            单节点部署示例
  deploy/addons/                 两种部署共用的 Odoo Runtime addon
examples/yudao/                默认演示与测试应用，带可重置部署
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
python3 -m pip install --upgrade pip
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

Adapter 包含标准 Markdown 业务流程包（仅静态适配，未重新验证 Odoo），以及面向 Odoo 菜单、action、view、字段、方法说明与用户可见拒绝条件的可选源码编译器。编译出的事实和通用原始代码索引都收窄到当前部署实际安装的模块。当源码或文档与运行时冲突时，以当前运行时事实为准。已安装 addon 还提供有界、按当前 actor 裁剪的 model/action 目录，Adapter 把它翻译到核心的实体读取合同；原生模型名和 RPC 方法不会成为 Agent 参数。

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
- 芋道原生日期范围筛选对输入可见性与日历有具体约束，当前日历未显示或被禁用的日期会被拒绝（详见[运行时行为详情](docs/reference/runtime-behavior.md)）。
- “已到达”只按 Agent 声明的内容核对（路径、选中的控件、已生效的筛选或分组）。这不能证明所有异步加载都已完成；声明里漏掉的要求可能仍未满足。
- 模型选择的查询未必覆盖用户的意图；读取返回空结果时，结果会交回 Agent，不会直接以“未找到”结束回合。
- 列表的自由文本搜索由限定范围的读取回答，不会把文字输入页面的搜索框。新建交接只高亮入口控件，不预填也不提交。
- 目前的评测结果来自同一个参考部署上的小样本，其中若干由实现者自己出题；它们不能证明总体准确率。见 [Odoo 评测说明](evals/odoo/README.md)和[芋道对照结果](evals/yudao/tool_loop/RESULTS.zh-CN.md)。

## 许可证

见 [LICENSE](LICENSE) 与 [THIRD_PARTY.md](THIRD_PARTY.md)。
