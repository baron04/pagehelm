---
title: Skill 与类型化写入
description: 编写 SKILL.md 与 agents/woa.toml 业务规程包，实现两阶段 prepare/execute 写入、ConventionWritePort 对称更新与 SubmitPort 表单交接。
---

在 PageHelm 中，Skill 负责描述组织级的业务操作规程与作用域声明，**本身不具备执行或写入权限**。所有实际数据变更都必须绑定到适配器实现的类型化写入接口，并经过 `WritePipeline` 审批与核验（流程细节见 [写入安全与隐私边界](../security/verified-writes.md)）。

## 一、Skill 包结构与加载校验

加载 Skill 目录时，`src/web_operation_agent/skills/artifacts.py` 会将其构建为不可变内容寻址快照（`SkillArtifact`），运行期不允许动态改写文件或沿软链接越界读取。

```text
skills/<kebab-case-name>/
├── SKILL.md          # 必填：YAML 元数据头 + 五段式业务规程
├── agents/
│   └── woa.toml      # 可选：作用域与 write_operations 声明（schema_version = 1）
├── references/       # 可选：UTF-8 辅助参考文档（经 context.read_resource() 按需读取）
└── assets/           # 可选：小型静态资源
```

`SkillArtifact.from_files` 校验规则：

- **体积与文件上限**：单文件 `<= 64 KiB`（`MAX_FILE_BYTES = 65536`）、整包 `<= 512 KiB`（`MAX_PACKAGE_BYTES = 524288`）、最多 `64` 个文件；仅允许安全扩展名（`.md`, `.txt`, `.csv`, `.json`, `.toml`, `.yaml`, `.yml`, `.png`, `.jpg`, `.jpeg`, `.webp`, `.svg`），拒绝软链接、反斜杠与路径穿越。
- **`SKILL.md` YAML 头**：不超过 `8 KiB`，禁止 YAML 锚点、别名与自定义 Tag；仅允许 `name`、`description`、`license`、`compatibility`、`metadata`、`allowed-tools` 六个键。`name` 须符合 `[a-z0-9]+(?:-[a-z0-9]+)*` 且与目录名一致（`allowed-tools` 仅作提示，不授予实际权限）。
- **正文五段约定（参考 `examples/yudao/`）**：
  1. **适用目标**：说明该 Skill 负责的实体创建或维护场景。
  2. **相邻目标**：点明容易混淆但不属于本 Skill 的业务对象。
  3. **所需用户信息**：必填字段、缺失时必须追问的规则，以及需保留原文的字段。
  4. **利用当前事实的流程**：人名、字典、层级路径定位方式与默认字段保留规则。
  5. **完成条件**：批准后仅派发一次、同账号回读核对及跨负责人可见性边界。
- **`agents/woa.toml` 作用域声明**：
  ```toml
  schema_version = 1
  scope_entities = ["crm_clue"]
  scope_pages = ["crm/clue"]
  write_operations = ["create_clue", "update_clue"]
  ```
  写工具发起提案时，`SkillContext.support(skill_id, operation)` 要求对应 Skill 在本轮已加载且将 `operation` 声明在 `write_operations` 中，再签发 `artifact_sha256` 支撑摘要。

## 二、两阶段类型化写入（`prepare_mutation` / `execute_mutation`）

每个类型化写工具都拆分为**准备提案**与**执行变更**两步：

1. **`prepare_mutation(args, context)`**：校验当前登录主体并读取目标实体最新状态；检查新值是否来自用户原话或已提供候选、未误覆盖需追加的内容；返回 `BusinessMutationProposal`（包含结构化差异、目标标识与 `content_digest`）。
2. **审批与并发锁**：由 `WritePipeline` 展示审批卡片、消费单次审批并进入单飞锁（见 [写入安全与隐私边界](../security/verified-writes.md)）。
3. **`execute_mutation(proposal, approval, context, claimed=...)`**：调用外部写接口前先回调 `claimed()` 落盘意图；若标记 `claimed` 后发生超时或连接中断，抛出 `BusinessMutationIndeterminate` 且不重试；写接口返回后通过 `ResultPostconditionVerifier` 按同一操作员身份回读核验。

参考实现见 `testapps/contacts/adapter.py`（`TypedWriteSpec`）、`testapps/contacts/declarative/declarative_writes.py`、`src/web_operation_agent/business/compiled_writes.py` 与 `src/web_operation_agent/business/write_pipeline.py`（提供 `change_detail`、`create_detail`、`preserved_detail`、`side_effect_detail`、`derived_detail`、`target_rule_detail`、`body_detail` 辅助构造函数）。

## 三、扁平对称约定更新（`ConventionWritePort`）

如果系统存在大量结构简单的编辑实体，为每个实体手写写工具成本较高。在 `GoalPort.capabilities()` 声明 `"updates"` 后，可通过 `DeclarativeConventionWritePort`（`testapps/contacts/declarative/declarative_writes.py`）或核心层的 `ConventionWritePort` / `CompiledWrites`（`src/web_operation_agent/business/convention_writes.py`、`src/web_operation_agent/business/compiled_writes.py`）自动派生写入能力：

- **字段规范（`ConventionFieldSpec`）**：仅面向读写 1:1 往返对称的顶层标量字段（`string`、`integer`、`number`、`boolean`、`id_array`），支持定点单位缩放（`unit_scale`，如人民币分 `100`）与时间戳格式（`epoch_ms`、`datetime`、`date`）。
- **敏感字段排除**：字段名或标签匹配 `DEFAULT_SECRET_FIELD_PATTERN`（密码、密钥、Token 等）的字段不会暴露给约定更新。
- **派生操作与安全链路**：自动派生 `update_record` 与两态可逆的 `toggle_record_status`，均按 `verify_actor` -> `resolve_target` -> `read_record` -> 审批卡 -> `recheck_bindings` 派发前复读 -> 单飞派发 -> 同主体全量非易失字段回读的链路执行。

## 四、原生表单提交与可撤销交接（`SubmitPort`）

对于未封装类型化接口、或用户需要在当前页面直接提交的表单与行操作，适配器可实现 `SubmitPort` 与 `SubmitConventions`（`src/web_operation_agent/execution/submit_contract.py`）：

1. **声明记录约定（`SubmitConventions`）**：指定 `volatile_keys`（版本号、更新时间等复读时忽略的字段）、`record_key`、`line_key`、`name_key`、`epoch_zone` 与 `decode_write(status, body)`。
2. **审批后单次点击（`request_submit`）**：先读取整张表单并校验值来源与追加保留，渲染审批卡片；批准后复读表单与后台记录，仅在一次性 `SubmitPermit` 下点击提交按钮一次并回读确认。
3. **回退为预填交接（`MutationPrefillOffer` / `MutationPrefillPort`）**：用户选择“只填到表单，我自己提交”，或表单包含密码/上传控件（`credential_field`）及无法完整核验的字段时，切换为非授权预填，跳过受限控件并将焦点移至该控件，同时在面板保留“撤销本次自动填写”（`undoFormFill(turnId)`）。
