# 开发检查

普通开发按当前改动选择检查，不要求每次提交跑整仓库、浏览器和评测装置。使用Python 3.12；这些检查使用合成数据，不调用付费模型，不连接现用实例。

```bash
.venv/bin/python scripts/check.py --plan
.venv/bin/python scripts/check.py
```

默认比较HEAD与暂存、未暂存及未忽略的新文件；`--plan`只输出JSON计划。`--path`可重复，用于明确检查范围。没有变更时不执行检查。`THIRD_PARTY.md`、SBOM等依赖清单会触发锁/许可核对，不按普通说明文档跳过。失败立即返回非零，不把未执行套件记成通过。

## 分流规则

| 改动 | 默认开发检查 | 合入前CI |
| --- | --- | --- |
| Markdown/文档 | 语法、相对链接 | 同左，不安装浏览器或产品依赖 |
| 核心Python | 所属模块及直接依赖边界的测试组；测试文件可单独运行 | 一次完整核心回归，排除另有独立lane的真实Chromium测试 |
| 前端组件/样式 | lint、单测、类型及Runtime构建 | 增加面板Chromium；不运行服务端生命周期或历史评测 |
| 浏览器Runtime、会话/持久状态边界 | 对应核心/前端检查 | 增加真实Runtime及生命周期故障检查 |
| Contacts或芋道适配器 | 对应合同套件 | 对应集成检查；不运行无关适配器 |
| Storybook | 仅展示工程构建 | 同左，不触发产品浏览器；展示效果人工复核 |
| 打包/依赖 | 锁/SBOM及wheel内容、检出外安装 | 同左，另保守覆盖产品边界 |
| 评测/对照工具 | 只跑改动工具的装置 | 同左，不作为普通产品改动的依赖 |

```bash
# 指定合入前基线；CI传入事件的base/head提交
.venv/bin/python scripts/check.py --profile ci --base main
# 不重复浏览器或JS单测的Python兼容性检查
.venv/bin/python scripts/check.py --profile compat --base main
```

本地指定`--base`但不指定`--head`时，也包含尚未提交的改动；显式`--head`只比较提交。CI使用提交共同祖先与head比较，包含删除和重命名前后的路径。基线不存在或不能解析时保守选择产品检查；未知代码路径回退核心检查，不能静默当作文档。开发组是快速反馈，不是跨所有模块的完整证明；修改跨模块语义时先查看计划，必要时显式使用CI profile。

Python 3.12承担浏览器与主要集成检查；3.13只跑Python兼容性。CI按计划安装Node、两个Playwright版本的Chromium、PageController和nginx。PageController作为Runtime依赖保留，但无需因此构建或验收整个PageAgent比较实验。

## 显式完整资格

```bash
scripts/qualify.sh --require-browser
# 仅打包检查，无需先跑全量产品/评测
bash scripts/check_wheels.sh
# 确实修改或需要核对旧参考时
scripts/qualify.sh --require-browser --reference-odoo
```

完整资格用于发布候选或所有者明确要求的全量核对，CI也可通过手动`release`选择。它保留核心、Contacts、芋道、面板、Runtime、生命周期及打包检查。未维护的Odoo适配器/评测默认不执行，显式选用才运行；真实Odoo环境需另行配置，不自动连接。

核心与Chromium集合用显式标记分开，收集时按测试ID去重。测试文件导入失败或选中集合为空会拒绝执行；必需浏览器lane遇到缺依赖导致的skip也失败。审批、actor隔离、写入仅一次、不确定写入不重试、费用账本保护及相关篡改回归保留。

没有改动的相关输入/依赖/环境可以复用已通过检查。不为状态汇报重复全量，不先跑完整核心再重复其中的产品子集，不为无关改动构建Storybook或安装三个wheel。重新验证时说明实际运行、失败、跳过与没有覆盖的范围。

视觉装饰不以固定GPU像素或动画相位作为产品门槛。保留确认点击、过期/拒绝、CSP、宿主交互、降低动态效果及GPU失效回退等实际行为验证；主题对比度和已声明设计变量仍检查。不以源码关键字黑名单、固定import拼写或CSS写法代替行为测试；协议、依赖和边界的静态不变量仍检查。
