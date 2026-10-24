# 开发检查

使用Python 3.12，按实际改动选检查。测试使用隔离合成数据，不调用付费模型、不连接现用实例或修改真实费用账本。

```bash
.venv/bin/python scripts/check.py --plan
.venv/bin/python scripts/check.py
# 合入前与兼容性检查
.venv/bin/python scripts/check.py --profile ci --base main
.venv/bin/python scripts/check.py --profile compat --base main
```

默认范围是HEAD、暂存/未暂存及未忽略的新文件；无变更不执行。`--path`可重复指定范围，`--plan`仅输出JSON。`--base`包含本地改动，显式`--head`只比较提交；CI比较共同祖先到head，包含删除与重命名两端。无效基线或未知代码路径保守选择产品检查，不能空选成功。失败立即返回非零，未执行不算通过。

## 准备

安装与构建命令见[README](../../README.zh-CN.md#开发)。`.env.example`不含凭据，忽略的`.env`只填当前checkout所需配置；脚本测试无需模型。`AGENT_OPENAI_MODEL`变更后，源码安装重启，代码打包进wheel/镜像的安装重建并部署；本地双实例按[本地指南](local-yudao.md)更新。

`build_runtime.py --install`先执行`npm ci`，`build_panel.py`构建面板。脚本和回放页是生成物，不提交；缺少构建时助手不可用、相关测试失败或跳过。前端变化及每个独立checkout都需构建。

旧Odoo额外安装`python -m pip install --no-deps --editable examples/odoo`，准备与更新见[接入指南](../../examples/odoo/README.md)，独立离线装置见[评测指南](../../evals/odoo/README.md#running-it)。它们不是普通产品检查的依赖。

## 检查分流

| 改动 | 默认dev | 合入前ci |
| --- | --- | --- |
| 文档/Markdown | 语法、相对链接 | 同左 |
| 核心Python | 模块及直接依赖边界 | 一次核心回归，排除独立浏览器lane |
| 前端 | lint、单测、类型、Runtime构建 | 增加面板Chromium |
| Runtime、会话/持久状态 | 对应核心/前端检查 | 增加真实Runtime与生命周期故障检查 |
| Contacts/芋道适配器 | 对应合同 | 对应集成，不跑无关适配器 |
| Storybook | 展示工程构建，效果人工复核 | 同左 |
| 打包/依赖 | 锁/SBOM、wheel内容与检出外安装 | 同左，并覆盖相关产品边界 |
| 评测/对照工具 | 对应工具装置 | 同左 |

依赖清单（含`THIRD_PARTY.md`、SBOM）触发锁/许可核对，不按普通文档跳过。dev提供局部反馈，跨模块语义需查看计划并选ci。日常CI用3.12；手动compat覆盖3.12/3.13，release由3.12跑完整资格、3.13跑兼容性。compat不重复浏览器或完整前端，但可构建必要Runtime资源、运行Node VM合同。CI按需安装Node、Playwright Chromium、PageController和nginx；PageController依赖不要求构建整个PageAgent实验。

## 完整资格与复用

仅发布候选或所有者明确要求全量时运行`scripts/qualify.sh --require-browser`，也可手动选择release。它覆盖核心、Contacts、芋道、面板、Runtime、生命周期与打包。打包/依赖改动必须运行`bash scripts/check_wheels.sh`，无需先跑全量。Odoo仅相关改动或显式`--reference-odoo`时执行；其真实环境另行配置，`--require-live`要求真实门禁。`BLOCKED`/`SKIPPED`不算通过。

测试按ID去重；收集时导入失败、选中测试集为空，或必需浏览器lane缺依赖导致skip均失败。保留审批、actor隔离、写入仅一次、不确定写入不重试、账本与篡改回归。浏览器改动在集成/合入时跑对应lane。

仅相关代码、输入和环境未变时复用通过结果；修复后只重跑受影响检查。勿重复核心与其子集、两个lane的同一浏览器测试，或为无关改动构建Storybook/安装wheel。交接准确报告执行、失败、跳过和未覆盖范围。

视觉检查保留确认、过期/拒绝、CSP、宿主交互、降低动态效果、GPU失效回退、主题对比度与声明变量；不以固定GPU像素/动画相位或源码关键词、import/CSS拼写代替行为验证。协议、依赖和边界静态不变量仍检查。
