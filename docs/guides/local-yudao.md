# 芋道本地验收环境

在要运行的源码目录执行 `./local`。本地有两套环境：

| 环境 | 启动或更新 | 浏览器入口 | Compose 项目 |
| --- | --- | --- | --- |
| 环境 1（默认） | `./local` | **http://localhost:40001** | `woa-yudao-env1` |
| 环境 2 | `./local --env 2` | **http://localhost:40002** | `woa-yudao-env2` |

优先使用两个环境并行执行，同一环境内串行。`--env` 只对本次命令生效，省略时始终选择环境 1；操作环境 2 的每条命令都要加 `--env 2`。

每套环境包含助手、芋道后端、nginx、MySQL 和 Redis，网络、数据库卷、会话与费用账本分别独立，只在本机回环地址发布浏览器入口。同一浏览器可同时访问两套环境，登录状态按来源隔离；旧 IP 形式的页面请求会跳转到表中的规范地址。

## 首次准备

1. 按根目录 [README](../../README.zh-CN.md#开发) 准备 Python 3.13 的 `.venv`，安装依赖与芋道适配器。
2. 安装 Node.js、npm、Docker Engine 及 Docker Compose 2.30 或更新版本，并启动 Docker。
3. 按[缓存构建说明](../../examples/yudao/deploy/README.md#build-cache-artifacts)准备固定缓存 `.q0-cache/yudao/`：带 Java 补丁的 JAR、管理前端 `ui/dist-prod/`、初始化 SQL 及上游源码。管理前端的 `VITE_BASE_URL` 保持空值。页面控制器按[共用构建说明](../../examples/yudao/deploy/README.md#prepare-pagecontroller-assets)准备。
4. 在 Git 忽略的 `.env` 填写 `AGENT_OPENAI_BASE_URL`、`AGENT_OPENAI_API_KEY`、`AGENT_OPENAI_MODEL` 及可选的 `AGENT_PROVIDER`，模型须有已登记价格。地址可以指向本机回环地址；配置只以私有文件注入容器，不进入镜像。
5. 运行所选环境的 `check`，通过后执行启动命令。环境 2 依次执行 `./local --env 2 check` 和 `./local --env 2`；首次启动会构建依赖镜像。

## 启动、更新和停止

以下命令以环境 2 为例：

| 命令 | 用途 |
| --- | --- |
| `./local --env 2` | 启动或更新；无变化且健康时复用 |
| `./local --env 2 restart` | 强制重启助手，并重新读取源码和模型配置 |
| `./local --env 2 stop` | 停止助手，保留业务服务和数据 |
| `./local --env 2 check` | 只读预检，不创建状态目录、不启动服务、不调用模型 |
| `./local --env 2 status` | 核验业务服务、助手、源码目录和实际 Runtime |
| `./local --env 2 logs` | 查看助手最近 80 行日志 |
| `./local --env 2 logs app` | 查看基础服务最近 80 行日志 |

退出终端后容器继续运行。机器或 Docker 重启后，再次执行所选环境的启动命令。

### 源码更新与目录切换

启动器只读挂载 `local` 所在源码目录的源码和构建产物，不挂载 `.env` 和内部材料。普通 checkout、worktree 和独立 clone 用法相同；并行修改两份实现时，使用两个源码目录。

修改后重新执行所选环境的启动命令，启动器按变化更新：

| 变化 | 更新方式 |
| --- | --- |
| 普通 Python 源码 | 重启所选助手 |
| 前端输入 | 构建前端资源，再更新助手 |
| 依赖 | 重建助手镜像 |
| 模型 `.env` | 重建助手容器 |

构建失败时保留原助手。助手更新不会连带重启正常运行的业务服务；助手重启会中断当前回合，安排在验收题之间。启用测试时钟后，后续启动和重启的参数处理见[测试时钟](#测试时钟默认关闭)。

运行中的环境属于启动它的源码目录。要切换到另一目录，先在原目录执行所选环境的 `stop`，再从新目录用同一环境编号启动；其他目录不能接管或停止正在运行的助手。

## 配置和数据库

实例配置位于 `$XDG_STATE_HOME/web-operation-agent/yudao/env1/` 和 `env2/`；未设置 `XDG_STATE_HOME` 时，根目录为 `~/.local/state/web-operation-agent/yudao/`。各源码目录共用同一套实例配置，其中 `application.env` 保存数据库配置，`agent.env` 保存模型配置，`state/` 保存会话和费用账本，均不进入 Git。

MySQL 数据保存在各环境独立的 Docker 数据卷中。启动、重启和切换源码目录都保留现有数据；新数据卷才按缓存 SQL 创建表结构和初始系统数据，不补充业务样本。已有数据卷但缺少 `application.env` 时，恢复该环境的原配置；启动器会拒绝生成替代密码或重置数据。

`./local` 不备份、恢复、填充或迁移数据库。冻结题集的备份、快照恢复和逐题重置由测评装置按其协议执行。补充业务数据见[合成数据工具](#合成数据工具)。

## 健康检查与故障排查

`check` 核对工具、缓存、模型配置和准入策略；`status` 核对业务成功响应、助手就绪、环境编号、源码目录和实际 Runtime。页面返回 200 或端口监听不足以说明环境就绪。这些检查不调用模型；助手的正确率、调用数和耗时需登录后另做模型测评。

开始模型测评前，还需等待 `/__local__/health` 中的 `api_docs_loaded` 为 `true`。接口文档预热单次直连内部后端，最多等待 300 秒，避免经过普通代理时被 120 秒超时中断；预热不计入题目的时间预算。

| 现象 | 下一步，以环境 2 为例 |
| --- | --- |
| 工具、模型配置、价格或缓存缺失 | `./local --env 2 check`，补齐明确缺项 |
| 基础应用未就绪 | `./local --env 2 logs app`，不循环重建或恢复数据库 |
| 助手不可用 | 先运行 `./local --env 2 status`，再查看 `logs`，修正后 `restart` |
| 源码或 Runtime 不一致 | `./local --env 2`，构建必要资源并更新助手 |
| 环境由其他源码目录运行 | 按[目录切换流程](#源码更新与目录切换)操作 |
| 对外端口被占用 | 识别服务归属，不终止未知进程或自动更换环境 |
| 数据库配置缺失但数据卷已存在 | 恢复所选实例的原 `application.env` |
| 登录后 401 | 在对应环境入口重新登录，保留实际认证错误 |

启动时，业务服务最多等待五分钟，助手最多等待 45 秒。超时以非零退出码结束并保留日志，先排查原因再重试。

## 权限和费用

助手以当前登录账号的权限读取数据。正常本地配置默认关闭写入和编辑预填；在本机 `.env` 设置 `WOA_NATIVE_WRITES=true` 后用 `./local` 更新，集成同时启用类型化写入、业务编辑预填和批准后的原生提交。页面动作默认使用只读窗口及方法围栏；所有保存仍须新的批准。原生表单不能建立契约时核验后交接，详见[写入模型](../concepts/writes.md#原生表单批准与交接)。每环境每账号日限额 ¥20，费用记在所选实例的 `state/`。

本地入口使用仓库附带的金额策略，预检不匹配时拒绝启动，规则见[芋道行为参考](../reference/yudao-behavior.md)。

## 合成数据工具

以下工具位于 `examples/yudao/deploy/`，不由 `./local` 自动执行。运行时显式指定所选环境的 `--base` 地址：

| 工具 | 用途 |
| --- | --- |
| `seed_scale.py` | 补充分页和长列表数据 |
| `demo_data.py` | 准备跨模块演示数据和只读探索账号 `demoexplorer` |
| `verify_demo_data.py` | 以探索账号核对数量、关联、订单金额和借贷平衡 |
| `verify_demo_browser.py` | 在全新浏览器会话中抽查核心列表 |

### 工具账号

需要登录的工具默认从当前源码目录的 `.private/yudao-accounts.json` 读取账号。文件是名字到 `{"username","password","tenant"}` 的映射，用 `--account 名字` 选择条目，`--accounts` 可改用其他文件，例如供另一源码目录复用。两套实例密码不同时，使用不同条目名区分。启动器不读取账号文件；不要提交或输出其内容。

`demo_data.py menus` 把探索账号凭据写入账号文件的 `demoexplorer` 条目；应用里已有该账号而文件缺少条目时停止，不生成新密码。

### 补数和核验

`seed_scale.py` 和 `demo_data.py` 的数据范围、阶段及参数见各自 `--help`。两者只接受本机回环地址、已有 MySQL 压缩备份和显式的 `--apply`。执行前确认所选环境没有冻结测评占用，备份并演练恢复。

```bash
.venv/bin/python examples/yudao/deploy/seed_scale.py --base http://localhost:40001 --account admin \
  --backup .q0-cache/yudao/snapshots/before-scale.sql.gz \
  --report ~/.local/state/web-operation-agent/yudao/env1/scale-report.json --apply
```

写入记录不含请求正文的回执。结果未知时停止，应用拒绝的写入记为失败，均不自动重试；再次执行前，先在应用里核对实际状态。

补数结果以应用读回和实际页面为准，不能只看预定数量或数据库行数。两个核验工具的结论限于所用账号和检查范围，不代表所有菜单、业务闭环或模型问答都已通过。补数会改变数量、合计和排名：保留旧快照及其金标，后续验收使用新数据重新核对的金标。

## 测试时钟（默认关闭）

相对日期验收可为本地实例单独指定一个起点，并挂载与容器架构及 libc 兼容的 `libfaketimeMT.so.1`。启动后 Java 和 MySQL 的墙上时钟从该起点按正常速度前进，单调时钟保持真实。库由测试者在 Git 外准备，不修改业务镜像；不注入 Redis、nginx 或助手。

```bash
./local --env 1 --test-clock '2026-10-25T09:00:00+08:00' \
  --test-clock-library /absolute/path/to/libfaketimeMT.so.1
./local --env 1                 # 关闭测试时钟，恢复真实时间
```

同一配置再次启动会保留原偏移量，继续走时；要重新从起点开始，先关闭再开启。启用或关闭会重建所选实例的 Java/MySQL 容器，保留原数据库卷；只在空闲、隔离的合成测试环境操作。MySQL 启用时直接使用镜像原有的 mysql 用户，避免入口脚本切换用户后无法读取 libfaketime 的共享信号量。已有库目录需对该用户可读写；不自动修改权限。启动失败按日志排查，不把失败启动当作时钟已生效。

实例私有状态中的 `test-clock.json` 保存起点、真实启用时间、偏移量及库摘要。`status` 和 `logs` 保持现状；普通启动和 `restart` 不带时钟参数会关闭。浏览器由测试宿主另行使用相同偏移量；手工打开的浏览器仍用真实时间。部署入口不读取本选项。首次使用新镜像或库必须核对 Java 接口、`SELECT NOW()`、前端默认范围、登录续登和业务指纹。
