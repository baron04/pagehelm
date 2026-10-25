# 芋道本地验收环境

在需要运行的源码目录执行：

```bash
./local                 # 环境1，默认
./local --env 2          # 环境2
```

| 环境 | 浏览器入口 | Compose项目 |
| --- | --- | --- |
| 环境1 | **http://localhost:40001** | `woa-yudao-env1` |
| 环境2 | **http://localhost:40002** | `woa-yudao-env2` |

每套环境包含助手、芋道后端、nginx、MySQL和Redis，网络、数据库卷、会话与费用账本分别独立，只在本机回环地址发布浏览器入口。

## 日常命令

| 命令 | 用途 |
| --- | --- |
| `./local` | 启动或更新环境1，无变化且健康时复用 |
| `./local --env 2` | 启动或更新环境2 |
| `./local --env 2 restart` | 重启环境2助手，重新读取模型配置 |
| `./local --env 2 status` | 核验环境2业务接口、助手、源码和实际Runtime |
| `./local --env 2 check` | 只读预检，不创建状态目录、不启动服务、不调用模型 |
| `./local --env 2 logs` | 环境2助手最近80行日志 |
| `./local --env 2 logs app` | 环境2基础服务最近80行日志 |
| `./local --env 2 stop` | 停止环境2助手，保留业务服务和数据 |

省略`--env`始终选择环境1，不记忆上次选择或自动换到空闲环境；诊断和修复命令始终带上任务选定的环境编号。

`local`只读挂载脚本所在源码目录的源码和构建产物，不挂载`.env`和内部材料；普通checkout、worktree和独立clone用法相同，并行修改代码时使用两个源码目录。运行中的环境属于启动它的源码目录，其他目录不能接管或停止：切换时先从原目录停止该环境助手，再在新目录用相同编号启动，数据保留。

修改后再次执行对应命令：普通Python修改只重启所选助手，前端输入变化才构建，依赖变化才重建镜像，模型`.env`变化会重建助手容器。构建失败时保留原助手，业务服务不随助手更新重启。

## 首次准备

1. 按根目录README准备Python 3.12的`.venv`并安装依赖与芋道适配器：`.venv/bin/python -m pip install --no-deps --editable examples/yudao`。
2. 安装Node.js、npm、Docker Engine及Docker Compose 2.30或更新版本，并启动Docker。
3. 按[芋道构建说明](../../examples/yudao/deploy/README.md#build-and-run)准备固定缓存`.q0-cache/yudao/`：带Java补丁的JAR、管理前端`ui/dist-prod/`、初始化SQL及上游源码。管理前端的`VITE_BASE_URL`保持空值。页面控制器见[部署指南](deploy-yudao.md#固定缓存)。
4. 在Git忽略的`.env`填写`AGENT_OPENAI_BASE_URL`、`AGENT_OPENAI_API_KEY`、`AGENT_OPENAI_MODEL`及可选`AGENT_PROVIDER`，模型须有已登记价格。地址可以指向本机回环地址；配置只以私有文件注入容器，不进入镜像。
5. 执行`./local check`，再执行`./local`；环境2用对应的`--env 2`命令。首次需要构建依赖镜像。

实例配置和运行状态位于`$XDG_STATE_HOME/web-operation-agent/yudao/env1/`与`env2/`（未设置时为`~/.local/state/web-operation-agent/yudao/`），各源码目录共用，包含数据库配置`application.env`、模型配置`agent.env`和`state/`，不进入Git。已有数据卷但数据库配置缺失时拒绝启动，不生成替代密码或重置数据。新卷按缓存SQL建立表结构和初始系统数据，不补充业务样本；日常启动不备份、恢复、seed或迁移数据库。

需要登录芋道的工具默认从当前源码目录的`.private/yudao-accounts.json`读取账号。文件是名字到`{"username","password","tenant"}`的映射，用`--account 名字`选择条目，`--accounts`可改用其他文件（例如让其他源码目录使用同一文件）；两套实例密码不同时用不同条目名区分。启动器不读取它；不要提交或输出文件内容。

## 合成数据工具

部署目录的`seed_scale.py`补充分页和长列表数据，`demo_data.py`准备跨模块演示数据和只读探索账号`demoexplorer`，数据范围、阶段与参数见各自`--help`。两者都不由`./local`运行，只接受本机回环地址、已有MySQL压缩备份和`--apply`。执行前释放冻结测评占用，备份所选实例并演练恢复；写入记录不含请求正文的回执，结果未知时停止，应用拒绝的写入记为失败，都不自动重试，再次执行前先在应用里核对实际状态。

```bash
.venv/bin/python examples/yudao/deploy/seed_scale.py --base http://localhost:40001 --account admin \
  --backup .q0-cache/yudao/snapshots/before-scale.sql.gz \
  --report ~/.local/state/web-operation-agent/yudao/env1/scale-report.json --apply
```

`demo_data.py menus`把探索账号凭据写入账号文件的`demoexplorer`条目；应用里已有该账号而文件缺少条目时停止，不生成新密码。可用性以应用读回和实际页面为准，不以预定数量或数据库行数代替：`verify_demo_data.py`以探索账号核对数量、关联、订单金额和借贷平衡，`verify_demo_browser.py`在全新浏览器会话抽查核心列表，二者都不证明所有菜单、业务闭环或模型问答已通过，页面覆盖率也只代表所用账号。补数会改变数量、合计和排名：旧快照及其金标保留，后续验收使用新数据重新核对的金标。

## 地址、权限和费用

浏览器使用所选环境的`localhost:40001`或`localhost:40002`，旧IP形式的页面请求会跳转到规范地址；同一浏览器可同时访问两套环境，登录状态按来源隔离。

模型按当前登录账号读取。本地宿主关闭类型化写入和编辑预填，提交由用户手动完成。每环境每账号日限额¥20，费用记在所选实例的`state/`。本地入口安装仓库附带的金额策略，预检不匹配时拒绝启动，规则见[芋道行为参考](../reference/yudao-behavior.md)。

健康检查核对业务成功响应、助手就绪、环境编号、源码目录和实际Runtime，页面200或端口监听不算通过。检查不调用模型，不证明自然语言题目的成功率、调用数或耗时；完整验收需登录后逐题进行。

## 按错误处理

| 现象 | 下一步，以环境2为例 |
| --- | --- |
| 工具、模型配置、价格或缓存缺失 | `./local --env 2 check`，只补明确缺项 |
| 基础应用未就绪 | `./local --env 2 logs app`，不循环重建或恢复数据库 |
| 助手不可用 | `./local --env 2 status`后查看`logs`，修正后`restart` |
| 源码或Runtime不一致 | `./local --env 2`，构建必要资源并更新助手 |
| 环境由其他源码目录运行 | 先从原目录停止该环境，再从当前目录启动；不接管对方 |
| 对外端口被占用 | 识别服务归属，不杀未知进程或自动更换环境 |
| 数据库配置缺失但数据卷已存在 | 恢复所选实例的原`application.env`，不重置密码或数据 |
| 登录后401 | 在对应环境入口重新登录，保留实际认证错误 |

业务服务最多等五分钟、助手最多等45秒，超时以非零退出码结束并保留日志，不盲目重试。助手重启会中断当前回合，安排在验收题之间。退出终端后容器继续运行，机器或Docker重启后再次执行对应`./local`命令。
