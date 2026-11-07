# 独立 Core 开发环境

使用 `compose.yaml` 和显式选择的 `deploy/compose.dev.yaml`，在独立 Compose project 中运行
Demo API、聊天 worker 和 PostgreSQL 16。需要 Docker Compose 2.30 及以上和可用的本地 Linux Docker engine。

覆盖层固定合成 owner/bot、Demo 模型、空模型 key 和零预算，使用 internal 网络，不发布宿主端口。
基础六个 Core 服务和维护备份服务均使用开发配置、独立镜像 tag 与 project 命名的数据卷。
数据库使用项目专属卷，不发布端口。其他 worker 按原 profile 显式选择。

## 准备与首次初始化

以下为 PowerShell 命令。先检查本机 context，明确选择连接本机 Linux engine 的 context；
下例使用 Docker Desktop 的 `desktop-linux`。Linux 主机通常使用本机 `default` context。
不要将这些命令指向远端环境。

```powershell
docker context ls
$devContext = 'desktop-linux'
$devProject = 'companion-dev-alice'
$devDirectory = Join-Path (Get-Location) ".local/dev/$devProject"
New-Item -ItemType Directory -Force $devDirectory | Out-Null
$devEnv = Join-Path $devDirectory 'compose.env'
```

在专用 `compose.env` 中设置独立随机值，限制此文件的本机访问权限，保持在 Git 外：

```dotenv
COMPANION_DEV_BRIDGE_TOKEN=<至少32字符的独立随机token>
COMPANION_DEV_BACKUP_KEY=<Fernet.generate_key()生成的独立开发备份key>
COMPANION_DEV_ADMIN_PASSWORD=<独立随机数据库管理密码，至少24字符>
COMPANION_DEV_CORE_PASSWORD=<独立随机Core数据库密码，至少24字符>
COMPANION_DEV_ARCHIVE_PASSWORD=<独立随机归档数据库密码，至少24字符>
```

环境文件用于 Compose 插值。Core 使用 dev_core 角色。dev_archive 在源库中只读业务数据，并拥有两个演练目标库。
备份 key 仅进入 dev-backup。命令显式选择这个文件，覆盖层清除基础 `.env`、根 `config.toml` 及其继承环境。
源码中的 Demo 示例配置挂入 `/app/config.toml`。

```powershell
$devCompose = @('--context', $devContext, 'compose', '--env-file', $devEnv,
    '-p', $devProject, '-f', 'compose.yaml', '-f', 'deploy/compose.dev.yaml')
# config 输出包含开发 token/key，只在本机检查，不收集到公共日志。
docker @devCompose config --services
docker @devCompose pull dev-postgres
docker @devCompose build companion-api
docker @devCompose up -d --wait --no-build --pull never dev-postgres
docker @devCompose run --rm --no-deps --pull never migrate init
docker @devCompose up -d --no-deps --no-build --pull never companion-api
docker @devCompose exec -T companion-api /app/.venv/bin/python -c "import os,urllib.request; r=urllib.request.Request('http://127.0.0.1:8765/internal/v1/health/ready',headers={'Authorization':'Bearer '+os.environ['COMPANION_BRIDGE_TOKEN']}); print(urllib.request.urlopen(r,timeout=3).status)"
docker @devCompose up -d --no-deps --no-build --pull never companion-worker
```

一次性 `run` 使用 `--rm --no-deps --pull never`；`--no-build` 仅保留在支持它的 `up` 调用。
先完成显式镜像构建或复用已核对的不可变 image ID，再运行维护命令。官方
[`run` 参数定义](https://github.com/docker/compose/blob/v2.38.2/cmd/compose/run.go#L215-L245)
未提供 `--no-build`。在本地镜像已存在且未指定强制构建策略时，Compose 的
[构建选择逻辑](https://github.com/docker/compose/blob/v2.38.2/pkg/compose/build.go#L90-L105)
跳过该镜像的构建；`--pull never` 保持禁止拉取。

API 的带认证 ready 检查通过后再启动 worker。liveness 只证明 API 存活；独立 worker 必须通过真实
入站、处理、投递租约和模拟 ACK 证明。首次空卷初始化无需旧数据库快照。若初始化失败，保持服务
停止，检查错误并保留卷。

容器使用 UID 10001。宿主的 `companion/`、`migrations/`、`db/` 和 Demo 示例配置必须允许此 UID
读取和遍历；不会自动放宽宿主权限或创建缺失 bind 路径。源码绑定只读，镜像中的 `/app/.venv`
保持 Linux 依赖环境。镜像内 `/data`、`/privacy` 的初始 UID 用于初始化新卷。

## 增量迭代

`companion/` 中普通 Python 代码变更后，重启实际受影响的服务：

```powershell
docker @devCompose restart --no-deps companion-worker
# API 代码变化时选择 companion-api；两者均受影响时明确列出两者。
```

源码绑定让容器立即看到文件内容，进程通过重启重新导入模块。`restart` 不更新环境或挂载；这些
定义变化后，使用 `up -d --no-deps --force-recreate <服务>`。共享 DTO、Bridge、Reader、锁文件或
依赖变化需要按现有 Dockerfile 重建 dev 镜像，再重建受影响容器。dev 覆盖层只绑定 Core、迁移和
数据库源码目录。schema 变化按下面的维护流程处理。

## 已有数据的快照与显式迁移

先停止输入探针，再停止全部曾启动的 dev worker，最后停止 API。`init` 本身不提供停写锁。
检查 project 中所有正在运行的容器，确认没有写入者或并行维护命令，再创建快照。

```powershell
docker @devCompose stop companion-worker companion-life-worker companion-executive-worker companion-content-worker
docker @devCompose stop companion-api
docker @devCompose --profile '*' ps --status running
# 使用本轮全新文件名；原 backup CLI 拒绝覆盖。
$devSnapshot = '/data/pre-schema-20261026-01.enc'
docker @devCompose run --rm --no-deps --pull never dev-backup backup $devSnapshot
# 核对上一条退出码为0，再记录密文SHA-256。
docker @devCompose run --rm --no-deps --pull never --entrypoint /app/.venv/bin/python dev-backup -c "import hashlib,sys; from pathlib import Path; print(hashlib.sha256(Path(sys.argv[1]).read_bytes()).hexdigest())" $devSnapshot
docker @devCompose run --rm --no-deps --pull never migrate init
```

PostgreSQL 容器保持运行，Core 写入者在这段时间内保持停止。归档角色通过只读 SQL 权限运行
`pg_dump`，加密文件写入 snapshot 卷。独立删除日志以只读方式挂载。
官方镜像在空卷首次启动时执行 `deploy/dev-postgres-init.sql`。更换已有项目的密码时，需要同步更新数据库角色和专用环境文件。[镜像初始化说明](https://hub.docker.com/_/postgres)
迁移成功后依次启动 API、检查认证 ready、启动聊天 worker。迁移失败时保留快照和全部卷，保持
服务停止，不能跳过失败继续启动。

恢复 CLI 读取目标 TOML，并拒绝已含业务表的目标库。下面使用预建的空库 `dev_restore_after`。
归档角色从容器环境读取密码。

```powershell
@'
database = "host=dev-postgres dbname=dev_restore_after user=dev_archive"
state_directory = "/data/dev_restore_after"
erasure_ledger = "/privacy/erasures.jsonl"
owner_qq_id = "10001"
bot_qq_id = "10002"
bridge_token = "offline-dev-restore-config-only"
'@ | docker @devCompose run --rm --no-deps --pull never -T --entrypoint /bin/sh dev-backup -c 'umask 077; cat > /data/restore.toml'
docker @devCompose run --rm --no-deps --pull never dev-backup restore $devSnapshot /data/restore.toml
```

恢复读取当前独立 Ledger，回放快照之后的遗忘。遗忘优先取消不再安全的未发送回复；其余在途
投递置为 `DELIVERY_UNCERTAIN`。原恢复逻辑保留已发生费用和 SENT 记录，将预留调用置为
`UNKNOWN`，撤销未来权限并设置费用核对屏障。
在演练库中检查恢复结果。应用回退必须与当前 schema 兼容，否则保持 Core 写入者停止，在空目标库按
[既有部署恢复合同](deployment.md)完成恢复和核对。

## 验证与证据

常规 pytest 包含原生 Compose 离线配置和本机真实 CLI API/worker/恢复探针，不启动 Docker 容器：

```powershell
./scripts/check.ps1 -TestFiles tests/test_dev_compose.py -SkipModelPreparation
python -B scripts/verify_dev_compose.py
```

检查覆盖所有 Core profiles、根与宿主合成污染、缺失根配置、两组 project 的网络/卷隔离、只读
源码和显式迁移。`OFFLINE_FIXTURE` 结果只证明实际 Compose 合并配置。

覆盖层的 `env_file: !override []` 在旧版 Compose 的共享 anchor 中也能清除基础 `.env`。
旧 loader 的 `!reset` 会先移除 anchor 内的节点，使后续服务丢失 reset 路径；上游的
[共享 alias 修复及测试](https://github.com/compose-spec/compose-go/commit/468e3142d7878f6ec8f0c99e3e64c913cf3d3148)
记录了这个问题。

原生 JSON 的 bind 布尔编码有版本差异。官方 Compose
[2.30.0](https://github.com/docker/compose/blob/v2.30.0/go.mod) 到
[2.40.3](https://github.com/docker/compose/blob/v2.40.3/go.mod) 使用的旧类型为
[`bool` 加 `omitempty`](https://github.com/compose-spec/compose-go/blob/v2.9.1/types/types.go#L594-L601)，
显式 false 会成为既存 `bind` 对象内的缺失字段；容器创建时的
[`CreateMountpoint`](https://github.com/docker/compose/blob/v2.40.3/pkg/compose/create.go#L1208-L1215)
直接使用该原生布尔值。[5.0.0](https://github.com/docker/compose/blob/v5.0.0/go.mod) 起采用
[`OptOut`](https://github.com/compose-spec/compose-go/blob/v2.14.0/types/types.go#L564-L579)，省略的是 true。
JSON、YAML 和 `--no-normalize` 均有这一编码差异。

验证脚本只在精确匹配官方稳定版 2.30.0–2.40.3 时，于 render 边界补回旧 schema 的 false。
既存值保持原样，缺失整个 bind 对象不补；新版或未知版本的缺失字段仍拒绝，路径、只读等其他
隔离条件保持严格校验。此版本区间来自上游 schema 边界；本轮实际 config-only 验证版本为
Windows Compose 2.40.3 和本机 5.5.0，并不代表逐版执行或 Linux 容器通过。Linux 2.38.2 的
完整容器门禁尚待独立 CI 验证。失败报告只增加 `compose_render` 阶段与两个固定错误码
`DEV_COMPOSE_RENDER_FAILED`、`DEV_COMPOSE_VERSION_FAILED`。备份阶段首条恢复结果前的五处调用另记录固定 step；
子进程失败只记录整数退出码与白名单 stderr 类别，退出码不符和预期错误码缺失分别分类。分类器只检查
stderr 最后 4096 个字符，未知内容记为 `UNCLASSIFIED`，不保留 stdout、stderr、异常或命令正文。

真实容器检查需显式运行：

```powershell
python -B -m scripts.smoke_dev_compose --context desktop-linux
# 复用已构建镜像时，传入 docker image inspect 得到的完整 sha256:<64 hex> ID：
python -B -m scripts.smoke_dev_compose --context desktop-linux --image sha256:<实际镜像ID>
```

脚本只接受 Unix 绝对 socket 路径（如 `unix:///var/run/docker.sock`），或本机 canonical named
pipe `npipe:////./pipe/<非空名称>`，要求 engine 为 Linux。context 地址不得包含 query 或 fragment。
Windows 的 [Pipe Names](https://learn.microsoft.com/en-us/windows/win32/ipc/pipe-names) 规定句点
代表本机；[Docker 客户端](https://raw.githubusercontent.com/moby/moby/master/client/client.go)保留
npipe 地址，[go-connections](https://raw.githubusercontent.com/docker/go-connections/master/sockets/sockets_windows.go)
将其交给 [go-winio](https://raw.githubusercontent.com/microsoft/go-winio/main/pipe.go) 的 pipe dial。
因此脚本在 engine 查询之前校验完整地址。它创建随机
`companion-dev-smoke-*` 合成 project 和专用环境文件，校验实际渲染后才启动容器。检查通过真实
HTTP API 和独立 worker 完成，覆盖认证、重复输入的单次任务/结算/投递、Demo 结果模拟 ACK、原
worker 锁、选择性重启、只读源码内容更新、显式停写/备份/迁移、快照不可覆盖、新路径恢复、最新
Ledger 回放，以及 SENT/UNKNOWN/投递不确定性。临时源码探针会在退出时清除。
第二次显式 `init` 后先验证 API 的带认证 ready，再启动独立 worker，用一个新合成输入检查
幂等接收、单次结算和 Outbox，证明迁移后的处理链可用。

脚本在结束时仅清理本轮创建并核对 project 标签的合成容器、网络和三个卷，保留 dev 镜像 tag。
它不会选择已有个人 dev project。回执位于 `.local/verification/dev-compose-smoke.json`，包含
实际 image ID、源码身份和输入哈希、检查结果与清理状态。engine 前提缺失记录 `NOT_RUN` 并返回
非零；任何检查或清理失败返回非零，不能作为容器通过凭据。

分别记录离线配置、原生进程探针和 Linux 容器三个层次的结果。原生探针只验证 API、worker 与
恢复断言的业务合同；只读 bind、实际卷权限、Linux 文件锁和网络边界仍由容器检查证明。

## 保留开发数据后退出

使用同一个 context、project 和环境文件停止开发环境：

```powershell
docker @devCompose stop companion-worker companion-life-worker companion-executive-worker companion-content-worker
docker @devCompose stop companion-api
docker @devCompose --profile '*' down
```

日常退出的 `down` 不加 `--volumes`，保留 Core 数据、最新 Ledger 和快照。自动 smoke 的卷清理
只用于它自己创建的合成 project。后续重新启动前先确认源码与当前 schema 兼容。

本环境复用 Docker 原生 [Compose merge](https://docs.docker.com/reference/compose-file/merge/)、
[project 隔离](https://docs.docker.com/compose/how-tos/project-name/)、
[只读 bind](https://docs.docker.com/reference/compose-file/services/#volumes)、
[profiles](https://docs.docker.com/compose/how-tos/profiles/) 和
[服务重启](https://docs.docker.com/reference/cli/docker/compose/restart/) 行为。
