# 文档

README 给概览，这里给细节。共三部分：

- **concepts/** —— 为什么这样设计。讲取舍与不变量，变化慢。
- **guides/** —— 怎么做。跟接口和命令走。
- **reference/** —— 当前实现行为的确切记录。行为一变就更新这一部分。

## 按你要做的事进入

### 接入一个应用
1. [接入应用](guides/integrate-an-application.md) —— Adapter 需要提供什么：描述符、端口、身份、路由、读取、写入、表单、资格测试。
2. [Skill 与类型化工具](concepts/skills-and-tools.md) —— 知识与能力怎么分工，写入怎么被核对。
3. [运行时行为详情](reference/runtime-behavior.md) —— 具体某条行为的确切说法。

### 部署与运维
1. [持久化运维](guides/operate-durably.md) —— SQLite 共享、备份与恢复、不确定写入的人工裁决。
2. [写入模型](concepts/writes.md) —— 一次批准、单次派发、同 actor 回读，以及三种结果的处置。

### 理解它为什么这样设计
1. [系统概览](concepts/overview.md) —— 系统边界、两层结构、信任与隐私边界、状态语义。
2. [执行器](concepts/executors.md) —— 两类执行器、事实与知识的优先级、上下文怎么组装。
3. [写入模型](concepts/writes.md) 与 [Skill 与类型化工具](concepts/skills-and-tools.md)。

### 查某条行为的确切说法
- [运行时行为详情](reference/runtime-behavior.md)