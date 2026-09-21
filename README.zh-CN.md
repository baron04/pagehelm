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
- 应用拥有的 Skill 与类型化工具提供当前业务事实；源码检索是可选能力，不是普通浏览器
  或 API 操作的前置条件。
- 浏览器动作会按当前观察和应用路由策略校验；换页需要用户可见确认。
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

Adapter 提供五个有界读取和四个可选写入，覆盖客户、产品、销售订单、报价单和客户发票。
它把 Odoo 的 HttpOnly 浏览器 session 与单独委托的逐用户 API key 绑定；只有两者指向同一
数据库、公司和 actor 才建立会话。读取报告完整性，写入执行应用拥有的重复检查与回读。

Adapter 包含版本化 Skill，以及面向 Odoo 菜单、action、view 与 model 的可选源码编译器。
当源码或文档与运行时冲突时，以当前运行时事实为准。

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
- 浏览器与无障碍覆盖以 Chromium 最强，其它引擎与读屏器尚未全面资格验证。

## 许可证

参见 [LICENSE](LICENSE) 与 [THIRD_PARTY.md](THIRD_PARTY.md)。
