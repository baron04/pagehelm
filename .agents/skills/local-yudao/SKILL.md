---
name: local-yudao
description: 启动、更新和诊断芋道本地双实例合成数据验收环境；线上发布使用 deploy-yudao。
---

# 芋道本地验收

使用源码目录的`./local`，按脚本所在目录挂载源码，不依赖分支/worktree，不从测评运行器启动。首次准备、排错或使用合成数据工具时读[本地指南](../../../docs/guides/local-yudao.md)。

- 默认环境1：`./local`，`http://localhost:40001`；环境2：`./local --env 2`，`http://localhost:40002`。任务指定环境2后，所有命令均带`--env 2`，不自动换环境。
- `status`/`check`诊断，`logs`看助手、`logs app`看业务日志；模型`.env`变化用`restart`。`stop`仅停止所选助手，保留业务服务/数据。
- 助手在Compose内；普通代码更新只重启助手，前端变化才构建，依赖变化才重建镜像。环境被另一源码目录使用时，先从原目录停止，不接管。
- 已有卷而数据库配置丢失时恢复原配置，不重置密码/数据。旧`woa-yudao-local-deprecated`及`48163`助手单独保留；启动或重建旧项目用`.q0-cache/yudao-local/deprecated-compose/compose.json`（私有，不输出），否则会按更名后的卷名建出空库。
- 登录账号在`.private/yudao-accounts.json`，工具用`--account 名字`选择条目；不输出文件内容。
- 日常启动不恢复数据库、重置账号、seed或运行测评。不打印私有配置，不改固定端口或杀未知进程；失败查日志后修正，重启安排在验收题之间。

报告环境、地址、检查与实际验收。就绪检查不调用模型，也不证明自然语言题目成功率/耗时；模型题测另定范围并记录费用。
