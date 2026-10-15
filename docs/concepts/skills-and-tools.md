# Skill 与类型化工具

Skill 是应用拥有的**知识与流程指引**；工具是已安装的**能力**，带类型化输入、actor scope、效果和应用处理器。两者独立注册，知识加载不授予任何权限。运行时的应用事实优先于 Skill 正文。

## 一个包长什么样

```text
customer-maintenance/
  SKILL.md                     YAML name/description + Markdown 正文
  agents/woa.toml              审核过的应用身份与写操作边界
  references/business-meaning.md   按需加载的文本资料
```

`SKILL.md` 的 description 用于发现，正文写业务含义、缺输入处理、查询与页面流程、停止条件。更长的材料放 `references/`，Agent 按需加载。宿主在安装时对包做快照并取哈希，拒绝脚本、符号链接、URL、路径穿越和超限包；包内容是不可信输入。

`agents/woa.toml` 声明审核过的应用身份，用于知识选择与写权限边界，不复制工具 schema：

```toml
schema_version = 1
scope_entities = ["customers"]
scope_pages = ["customer-help"]
write_operations = ["create_customer", "update_customer"]
```

`scope_entities`/`scope_pages` 是精确的应用索引身份，不是自然语言关键词；纯知识包可以只声明它们。标准的 `allowed-tools` 字段只是描述性的，不授予权限。

## 工具拥有真正的能力

每个审核过的操作定义一个 `ToolDefinition`：输入 schema、读写效果、scope、实体/操作、处理器、版本、允许的结果引用输入，以及可选的输入到字段绑定。**不要把任意 endpoint、方法或原生请求体暴露为 Agent 参数。**

应用处理器负责：在当前 actor 下解析业务名称、准备准确效果与完整审批细节、做冲突检查、只派发一次、核验承诺的每项效果。

## 效果合同：模型输入 → 应用效果

写入要被核对，就要能把“模型填的输入”翻译成“应用会发生什么”。操作可以安装应用拥有的 `effect_contract`，其 `project_inputs` 以应用的实体/字段词汇返回 `RequestedEffect`：

- 这套词汇独立于 `ToolOperation` 的策略标识；
- 应用从**实际准备的原生请求**解码出效果投影，与锁定的请求核对后才展示审批；
- 投影参与审批摘要，改它即令批准失效。

芋道与合成 Contacts REST 适配器实现了同一合同（结构不同的两个应用，同一套合同测试）。

## 两条写入路径，同一段链路

| 路径 | 谁决定下一步 | Skill 的角色 |
| --- | --- | --- |
| 目标式 | 服务端按声明确定性解析 | 声明前选择知识；写入按其审核过的操作提出 |
| 模型驱动回合 | 模型逐步选择 | `load_skill` 加载后，Skill 的操作成为可调用工具 |

两条路径都走[写入模型](writes.md)里的同一段链路：只读准备 → 效果核对 → 一次批准 → 单次派发 → 同 actor 回读。模型驱动回合里，加载谁的 Skill 由部署开关与该 actor 的 scope 决定，**能跑什么由用户对那条精确提案的批准决定**。

## 完整请求核对

对安装效果合同的集成，写声明默认走**单提案**路径：直接从完整请求、已加载知识、当前 actor 可读候选和已安装 schema 提出原生操作与读取/页面声明。前置 scope 只选知识与工具，一旦推测出类别不成为强制的提案步骤。

可选的**锚定**路径并行做第二次解读：它不声明字段名，只声明用户提供的事实，与服务端投影的草案及独立的读取/页面义务比较；分歧允许准备前的一次整任务重声明。值一致不能证明字段身份一致，也不能替代完整请求评估。

**这些检查建立的是行为，不是模型准确率。** 脚本合同覆盖不建立成功率；卡片的回读只证明按批准的内容写入过，不证明批准的内容等于用户的要求。

## 相关

- [写入模型](writes.md)
- [执行器](executors.md)
- [接入应用](../guides/integrate-an-application.md)：Adapter 怎么提供端口与工具