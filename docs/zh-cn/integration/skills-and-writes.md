---
title: Skill 规范与类型化写入
description: 编写并校验包含 SKILL.md 与 agents/woa.toml 的业务 Skill 包，掌握两阶段 prepare/execute 写入、ConventionWritePort 对称更新及 SubmitConventions 交接协议。
---

> **概要：** 在 PageHelm 中，Skill 负责承载组织级业务规程与白名单声明，**自身不具备独立运行权限**。安装包在加载期被校验为不可变内容寻址快照（`SkillArtifact`，单文件 `<= 64 KiB`、整包 `<= 512 KiB`、最多 `64` 文件）；而所有实质写操作都绑定到适配器实现的类型化写入契约（`prepare_mutation` / `execute_mutation`、`ConventionWritePort` 或 `SubmitPort`），受 `WritePipeline` 统一约束。

[English](../../integration/skills-and-writes.md)

## 一、Skill 包结构与校验规则（`SKILL.md` + `agents/woa.toml`）

系统在加载 Skill 目录时会通过 `src/web_operation_agent/skills/artifacts.py` 构建不可变 `SkillArtifact` 摘要，运行时禁止动态篡改文件或沿软链接越界读取：

### 目录布局与硬性限制

```text
skills/<kebab-case-name>/
├── SKILL.md                # 必填：YAML 元数据头 + 五段式业务操作规程
├── agents/
│   └── woa.toml            # 可选：宿主作用域与 write_operations 声明（schema_version = 1）
├── references/             # 可选：UTF-8 辅助参考文档（经 context.read_resource() 懒读取）
│   └── ...
└── assets/                 # 可选：小型静态资源
```

`SkillArtifact.from_files` 强制校验：

- **容量与文件边界：** `MAX_FILE_BYTES = 65536`（`64 KiB`）、`MAX_PACKAGE_BYTES = 524288`（`512 KiB`）、`MAX_FILES = 64`；仅允许安全扩展名（`.md`, `.txt`, `.csv`, `.json`, `.toml`, `.yaml`, `.yml`, `.png`, `.jpg`, `.jpeg`, `.webp`, `.svg`），禁止软链接、反斜杠或路径穿越。
- **`SKILL.md` YAML 头规范：** 体积不超过 `8 KiB`，禁止 YAML 锚点/别名/自定义 Tag；仅允许 `{"name", "description", "license", "compatibility", "metadata", "allowed-tools"}` 六个键，`name` 须满足小写连字符标识 `[a-z0-9]+(?:-[a-z0-9]+)*` 且与目录名一致。（注意：`allowed-tools` 仅作描述性提示，实际授权仍以宿主作用域与审批门禁为准。）
- **标准五段式 `SKILL.md` 正文约定（参考 `examples/yudao/`）：**
  1. **适用目标**：明确本 Skill 覆盖的实体创建或维护场景
  2. **相邻目标**：点明极易混淆但**不归本 Skill 管辖**的相邻业务对象
  3. **所需用户信息**：必填字段、缺失必问规则与原文保留要求
  4. **利用当前事实的流程**：人名/字典/层级路径定位、默认字段保留规则
  5. **完成条件**：批准后仅派发一次、同账号回读核对及跨负责人权限可见性边界
- **`agents/woa.toml` 策略声明：**
  ```toml
  schema_version = 1
  scope_entities = ["crm_clue"]
  scope_pages = ["crm/clue"]
  write_operations = ["create_clue", "update_clue"]
  ```
  当工具发起写入提案时，`SkillContext.support(skill_id, operation)` 要求对应 Skill 在本轮已加载且将 `operation` 列入 `write_operations`，随后签发 `artifact_sha256` 支撑摘要。

---

## 二、两阶段类型化写入（`prepare_mutation` / `execute_mutation`）

所有类型化业务写工具都严格拆分为**提案准备**与**不可逆执行**两步，使 `WritePipeline` 得以在派发前完成差异展示、人工确认与漂移核查：

```text
  收到模型写工具调用
         |
         v
  prepare_mutation(args, context)
    - 校验当前登录主体并读取目标实体最新状态
    - 核验新值确实来自用户原话或已提供候选，且未误覆盖“追加”文本
    - 构建 BusinessMutationProposal（精确 diff、目标实体与 content_digest）
         |
         v
  WritePipeline 展示审批卡 -> 消费一次性审批 -> 进入单飞锁 -> 派发前复读核验
         |
         v
  execute_mutation(proposal, approval, context, claimed=...)
    - 在真实调用外部写接口前立即回调 claimed() 落盘意图
    - 若已标记 claimed 后超时或断连，抛出 BusinessMutationIndeterminate
    - 通过 ResultPostconditionVerifier 执行同主体回读核验
```

仓内参考实现可查看 `testapps/contacts/adapter.py` 与 `src/web_operation_agent/business/write_pipeline.py`（含 `change_detail`、`create_detail`、`preserved_detail`、`side_effect_detail` 等辅助构造函数）。

---

## 三、扁平对称约定更新（`ConventionWritePort`）

为海量普通编辑实体重复手写类型化写入工具成本过高。在 `GoalPort.capabilities()` 中声明 `"updates"` 的适配器可接入 `ConventionWritePort`（`src/web_operation_agent/business/convention_writes.py`）：

- **`ConventionFieldSpec` 字段规范：** 面向具备严格 1:1 读写往返对称性的顶层标量字段（`string`、`integer`、`number`、`boolean`、`id_array`），内置定点单位缩放（`unit_scale`，例如人民币分 `100`）与时间戳表示（`epoch_ms`、`datetime`、`date`）。
- **自动屏蔽敏感控件：** 凡字段名或标签匹配 `DEFAULT_SECRET_FIELD_PATTERN`（密码、密钥、Token 等）一律拒绝进入约定更新面。
- **全链路安全等价保护：** 自动派生的 `update_record` 与严格两态可逆 `toggle_record_status` 均执行：`verify_actor` -> `resolve_target` -> `read_record` -> 审批卡呈现精确 `before -> after` 与保留项 -> 派发前复读比对（`recheck_bindings`）-> 单飞唯一派发（`dispatch_update` / `dispatch_status`）-> 同主体回读核验全量非易失字段。

---

## 四、原生表单提交与可撤销交接（`SubmitConventions`）

对于尚无类型化写工具，或操作员明确需要在当前页面提交的表单/行按钮，适配器可通过 `SubmitPort` 与 `SubmitConventions`（`src/web_operation_agent/execution/submit_contract.py`）获得同等防护：

1. **声明记录约定（`SubmitConventions`）：** 提供 `volatile_keys`（版本号、更新时间戳等派发前复读忽略键）、`record_key`、`line_key`、`name_key`、`epoch_zone` 与 `decode_write(status, body)`。
2. **两种安全落点：**
   - **程序批准后单次点击（`request_submit`）：** 先完整读取整张表单并校验值来源与追加保留，渲染审批卡；批准后实时复读表单与后台记录，仅在一次性 `SubmitPermit` 许可下点击提交钮一次并回读核验。
   - **预填交接与一键撤销（`MutationPrefillOffer` / `MutationPrefillPort`）：** 当用户选择“只填到表单，我自己提交”，或检测到密码/上传控件（`credential_field`）及不可完整核验表单时，系统回退为非授权草稿预填，自动跳过受限控件并保留 `undoFormFill(turnId)` 一键复原修改前内容。

---

## 下一步阅读

- 查阅内置工具清单、`GoalPort` 能力表与核心自检规格：[内置工具与能力参考](../reference/tools-and-capabilities.md)。
- 查阅配置键与 `/v1/sessions/*` 协议细节：[协议与配置参考](../reference/protocol-and-configuration.md)。
