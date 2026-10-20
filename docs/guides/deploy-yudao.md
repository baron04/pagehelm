# 部署芋道演示站

在仓库根目录执行：

```bash
./deploy
```

日常发布无需参数，也无需设置 `YUDAO_CACHE`。本地配置只做一次。

随仓库提供的云端配置关闭原生持久写入、编辑表单预填及用户态源码工具。新增表单仍可按已审核页面预填后交给用户提交；源码目录保留供版本核验和应用元数据使用。已有站点须重新发布后才应用这些配置。

云端与本地入口加载同一份逐字段金额核验策略。启动时核对应用版本、金额格式化函数、页面字段和单位的源码摘要；不匹配就拒绝启用，不能仅凭接口字段名推断金额单位。

## 一次性配置

本机需要 Python 3.12 的 `.venv`、符合 `frontend/package.json` 的 Node.js、npm、uv、Git、SSH、rsync、unzip 和 OpenSSL。按根目录 README 创建 Python 环境；安装 uv 后确保命令在 `PATH` 中。

```bash
cp deploy.example.toml deploy.local.toml
```

填写这两个值：

```toml
ssh_target = "ubuntu@example.com"
public_origin = "https://yudao.example.com"
```

`ssh_target` 可以是已有 SSH 别名或 `user@host`。`public_origin` 必须与服务器的 `YUDAO_DOMAIN`、证书和 nginx 站点一致。配置已被 Git 忽略；不要放入密码、私钥或模型密钥。每个 checkout 使用自己的配置。

SSH 使用密钥认证并严格校验主机密钥。首次连接先人工核实服务器指纹并完成 SSH 连接；脚本不会绕过主机密钥检查，也不会等待输入密码。

### 固定缓存

本地缓存只有一个位置：**仓库根目录的 `.q0-cache/yudao/`**。版本由其中两个 Git 仓库的提交记录确定，不通过目录名区分。当前要求：

| 部分 | 提交前缀 | 发布所需内容 |
|---|---|---|
| 服务端 | `d3d00639c0` | `server/` 与 `server/yudao-server/target/yudao-server.jar` |
| 管理前端 | `9323ffa2ca` | `ui/` 与 `ui/dist-cloud/` |
| 业务表结构 | 从该服务端生成 | `business-schema.sql` |
| 页面控制器 | 核心 `page_controller.json` 锁定的版本及摘要 | `woa/` 下的入口、依赖块和许可证 |

已有这些产物可直接运行 `./deploy check`。不要新建带日期或版本后缀的缓存。升级上游是单独的维护操作：更新固定目录中的源码、重建产物、验证数据库迁移，再更新版本约束；日常发布不切换上游版本。

新机器的服务端、管理前端与业务表结构构建见[芋道构建说明](../../examples/yudao/deploy/README.md#build-and-run)。在仓库根目录设置一次构建用变量：

```bash
export YUDAO_CACHE="$PWD/.q0-cache/yudao"
```

先复制该说明要求的 `patches/server/`，再构建 JAR。管理前端的云端构建如下，其中域名换成配置中的域名；这条只编译，不发布：

```bash
(cd "$YUDAO_CACHE/ui" && \
  VITE_BASE_URL=https://yudao.example.com VITE_OUT_DIR=dist-cloud \
  VITE_APP_CAPTCHA_ENABLE=false VITE_APP_DOC_ENABLE=false VITE_APP_DOCALERT_ENABLE=false \
  VITE_APP_DEFAULT_LOGIN_TENANT= VITE_APP_DEFAULT_LOGIN_USERNAME= VITE_APP_DEFAULT_LOGIN_PASSWORD= \
  NODE_OPTIONS=--max-old-space-size=8192 pnpm exec vite build --mode prod)
```

页面控制器从锁定的 npm 包复制，部署前会再次核对全部文件摘要。在仓库根目录执行：

```bash
controller_tmp=$(mktemp -d)
npm install --ignore-scripts --no-save --prefix "$controller_tmp" @page-agent/page-controller@1.12.4
.venv/bin/python - "$controller_tmp" <<'PY'
import json, shutil, sys
from pathlib import Path
policy = json.loads(Path('src/web_operation_agent/policies/page_controller.json').read_text())
package = Path(sys.argv[1]) / 'node_modules/@page-agent/page-controller'
dest = Path('.q0-cache/yudao/woa')
dest.mkdir(parents=True, exist_ok=True)
for name, entry in policy['served_files'].items():
    shutil.copyfile(package / entry['tarball_path'].removeprefix('package/'), dest / name)
PY
rm -rf "$controller_tmp"
```

### 服务器前置条件

日常发布面向已经准备好的 Ubuntu 主机：Docker Engine 与 Compose 可用，部署账号可运行 Docker 并写入 `/opt/yudao/releases`；nginx、证书和站点代理已安装。

首次准备主机使用 `examples/yudao/deploy/cloud/provision.sh <ssh-target> <domain>`，它会配置 nginx、证书、防火墙和 SSH。此脚本要求主机已有 Docker，并非完整的服务器安装器；应先检查主机和域名，再执行。

`/opt/yudao/.env` 保存数据库配置和 `YUDAO_DOMAIN`。模型配置由运维人员直接保存在服务器的 `/opt/yudao/agent.env`：`AGENT_OPENAI_BASE_URL`、`AGENT_OPENAI_API_KEY`、`AGENT_OPENAI_MODEL`、`AGENT_PROVIDER`。日常发布只检查文件存在，不下载或打印其中的密钥。

该站点还需要按[原生代理接入说明](../../examples/yudao/deploy/README.md#public-deployment)配置原生请求代理和页面事实注入。发布命令不会安装或重写 nginx 配置。

## 日常发布做什么

```bash
./deploy check   # 只读前置检查；不构建、不切换版本、不调用模型
./deploy         # 构建并发布当前 checkout
./deploy status  # 查看线上发布清单、助手就绪状态和实际脚本摘要
```

发布依次执行：

1. 检查私有配置、本地工具、固定缓存版本及产物、SSH 和服务器配置。
2. 首次发布或前端依赖锁变化时自动执行 `npm ci`；随后构建当前助手前端，将生成的 Runtime 同步进 Python 包，再构建核心与芋道适配器 wheel。核对 wheel 内 Runtime 与刚构建的文件一致。
3. 上传到独立发布目录，记录 Git 提交、工作区是否有修改、wheel 与 Runtime 摘要。
4. 在服务器构建助手镜像；构建成功后切换 `current`，启动服务。JAR 未改变时不强制重启业务服务端。
5. 核验业务接口、助手就绪端点、启动服务、站点可达性，以及内部和公网实际提供的 Runtime 摘要。全部通过才报告部署完成。

基础应用的 JAR 与管理前端使用已经准备好的缓存产物，日常命令只重建助手。修改上游源码、Java 补丁或管理前端时，先按首次构建说明重建对应产物。助手的 Python、浏览器 Runtime 和面板修改无需手动补构建。

日常命令不导入快照、不重置账号、不执行数据库升级、不删除数据卷。它会重启助手，正在执行的对话可能中断；安排在验收空档执行。前置检查和健康核验不登录业务账号，不发送付费模型请求，不能代替登录后的功能验收。

## 登录续期

页面持有业务访问令牌和刷新令牌；助手服务端只接收当前访问令牌，不保存刷新令牌。助手除定时续期外，还会在每个新任务发送前通过应用 bootstrap 核验身份并同步当前令牌。芋道返回认证失效时，页面加载器使用原有刷新接口续期，再重试一次 bootstrap；业务请求和已发送任务不会因此自动重放。访问令牌与刷新令牌的服务端有效期保持原配置。

刷新令牌也失效时，面板提示重新登录并保留未发送的问题；登录后由用户重新发送。执行中的任务遇到业务认证失效时，会记录明确的认证错误并提示重新登录，不会自动重试任务。页面退出登录或切换登录期间完成的旧续期响应不会覆盖页面当前身份。

## 失败与核验

失败会返回非零退出码；不要仅凭一条上传成功或业务接口 200 就认定部署完成。

| 现象 | 处理 |
|---|---|
| 配置或产物缺失 | 按错误指向补齐本地配置或首次构建，再运行 `./deploy check` |
| SSH 无法连接、指纹未确认 | 手动核实 SSH 目标、密钥及服务器指纹；不要关闭严格校验 |
| 服务器前置检查失败 | 核对部署账号权限、Docker、两个 env 文件是否存在，以及域名是否一致 |
| 前端或 wheel 构建失败 | 修复本地构建错误；该阶段尚未上传或切换版本 |
| 服务器镜像构建失败 | 线上 `current` 保持原版本；修复镜像构建或网络问题后再发起一次部署 |
| 切换后服务或脚本摘要核验失败 | 脚本恢复原 `current`，重建并启动原助手；退出码仍为失败。回退核验失败会明确提示，需要人工检查主机 |

成功后使用 `./deploy status` 对照发布清单中的提交与 Runtime SHA256。`modified: true` 表示发布包含未提交的修改；轮子摘要标识实际上传的产物。首次改用该流程之前发布的版本可能没有清单，此时命令会明确说明。

每轮服务健康检查最多等待约八分钟，回退后会另做一轮核验；单次网络请求也有超时。服务器只允许一个激活操作，本机同一 checkout 也禁止并发部署。成功后保留当前版本、上一个回退版本及另一个历史版本；数据库不在发布目录中。

若需要手动回退、迁移数据库或恢复快照，应单独规划并核实目标和备份。底层 `cloud/deploy.sh` 的 `--restore` 会替换数据库和重置账号密码，不属于日常发布流程。
