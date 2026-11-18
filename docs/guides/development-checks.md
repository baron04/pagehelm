# 开发检查

使用Python 3.13，按实际改动选检查。测试使用隔离合成数据，不调用付费模型、不连接现用实例或修改真实费用账本。

```bash
.venv/bin/python scripts/check.py --plan
.venv/bin/python scripts/check.py
# 合入前与兼容性检查
.venv/bin/python scripts/check.py --profile ci --base main
.venv/bin/python scripts/check.py --profile compat --base main
```

默认范围是HEAD、暂存/未暂存及未忽略的新文件；无变更不执行。资源构建先按序执行，其余非浏览器检查并发，浏览器lane随后逐个独占执行；Python套件按模块（浏览器测试按测试或共享夹具的类）分进程并行，`--jobs`设进程数，`--serial`逐项执行并实时输出。失败后不再启动后续批次，`--keep-going`全部执行，`--only <检查名>`只重跑计划中的指定项。dev下芋道包内Python改动只选直接或间接导入它的测试模块，其他芋道改动及ci跑全部。`--path`可重复指定范围，`--plan`仅输出JSON。`--base`包含本地改动，显式`--head`只比较提交；CI比较共同祖先到head，包含删除与重命名两端。无效基线或未知代码路径保守选择产品检查，不能空选成功。任一失败返回非零，未执行不算通过。

## 修复循环

修缺陷时先把失败复现为最小测试，迭代中只跑该模块（`run_all.py --module <模块或测试ID>`）或`check.py --path <改动文件>`/`--base <上次通过的提交>`；合入前检查和实机验证在交接时各跑一次。

## 准备

安装与构建命令见[README](../../README.zh-CN.md#开发)。`.env.example`不含凭据，忽略的`.env`只填当前checkout所需配置；脚本测试无需模型。`AGENT_OPENAI_MODEL`变更后，源码安装重启，代码打包进wheel/镜像的安装重建并部署；本地双实例按[本地指南](local-yudao.md)更新。

从旧Python版本迁移时，使用Python 3.13重新创建`.venv`并安装依赖。

`build_runtime.py --install`先执行`npm ci`，`build_panel.py`构建面板。脚本和回放页是生成物，不提交；缺少构建时助手不可用、相关测试失败或跳过。前端变化及每个独立checkout都需构建。

Contacts 测试应用与芋道演示均包含在仓库内，不再维护单独的旧应用可编辑安装依赖。

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

依赖清单（含`THIRD_PARTY.md`、SBOM）触发锁/许可核对，不按普通文档跳过。dev提供局部反馈，跨模块语义需查看计划并选ci。CI、compat和release均使用Python 3.13，release执行完整资格。compat不重复浏览器或完整前端，但可构建必要Runtime资源、运行Node VM合同。CI按需安装Node、Playwright Chromium、PageController和nginx；PageController依赖不要求构建整个PageAgent实验。

## 完整资格与复用

仅发布候选或所有者明确要求全量时运行`scripts/qualify.sh --require-browser`，也可手动选择release。它覆盖核心、Contacts、芋道、面板、Runtime、生命周期与打包。打包/依赖改动必须运行`bash scripts/check_wheels.sh`，无需先跑全量。在线实时环境按部署文档单独配置，`--require-live`要求真实门禁。`BLOCKED`/`SKIPPED`不算通过。

测试按ID去重；收集时导入失败、选中测试集为空，或必需浏览器lane缺依赖导致skip均失败。保留审批、actor隔离、写入仅一次、不确定写入不重试、账本与篡改回归。浏览器改动在集成/合入时跑对应lane。

芋道绑定规范 `frontend/e2e/yudao-*-binding.spec.ts` 还要 `WOA_YUDAO_UI_MODULES` 指向含 vue 与 element-plus 的合格应用依赖目录（本地芋道前端 `node_modules`）以及 `WOA_PAGE_CONTROLLER_MODULE` 指向固定 PageController 产物；未提供时这些用例跳过，不算通过证据。

仅相关代码、输入和环境未变时复用通过结果；修复后只重跑受影响检查。勿重复核心与其子集、两个lane的同一浏览器测试，或为无关改动构建Storybook/安装wheel。交接准确报告执行、失败、跳过和未覆盖范围。

视觉检查保留确认、过期/拒绝、CSP、宿主交互、降低动态效果、GPU失效回退、主题对比度与声明变量；不以固定GPU像素/动画相位或源码关键词、import/CSS拼写代替行为验证。协议、依赖和边界静态不变量仍检查。
