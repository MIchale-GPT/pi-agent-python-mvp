# Tau TUI ARM64 离线上线手册

本文是 `docs/PRD-tau-tui-docker-deployment.md` 的可执行上线流程。交付物是
纯 TUI/Print 模式镜像，不包含 Web 前端，也不暴露宿主机端口。

## 交付物

- 镜像标签：`tau:arm64-tui-latest`
- 离线镜像：`dist/tau-arm64-tui.tar`
- 校验文件：`dist/tau-arm64-tui.tar.sha256`
- 启动脚本：`tau-start.sh`
- 配置模板：`tau.env.production.example`

## 网络拓扑

生产默认采用 SAG Compose 的 bridge 网络：

```text
Tau 容器 ── sag_default ──> api:8000 (SAG Agent/MCP)
    │
    └── host.docker.internal:35432 ──> 宿主机发布的 DWS
```

Tau 不发布端口，不需要 Nginx 或 CORS。`tau-start.sh` 会注入
`host.docker.internal:host-gateway`，让容器可以访问宿主机发布的 DWS。

不要同时使用 `--network host` 和 `docker network connect sag_default`：Docker
不允许 host 网络容器再加入 bridge 网络。只有现场已经提供不含路径前缀的、
宿主机可达 SAG API 地址时，才适合单独采用 host 网络方案。

## 1. 外网构建机生成 ARM64 包

要求 Docker buildx 的活动 builder 支持 `linux/arm64`：

```bash
cd /home/michale/pgm/zhishu/tau
docker buildx ls
DRY_RUN=1 ./build-tau-arm64.sh
./build-tau-arm64.sh tau:arm64-tui-latest
sha256sum -c dist/tau-arm64-tui.tar.sha256
```

脚本会进行三次以内的构建重试，确认镜像平台，运行 `tau --help` 和
`tau --print --help` ARM64 烟测，再原子写入 tar 和 SHA-256。

将以下文件拷贝到 ARM64 上线机的 Tau 目录：

```text
dist/tau-arm64-tui.tar
dist/tau-arm64-tui.tar.sha256
tau-start.sh
tau.env.production.example
```

## 2. 上线机校验并载入

```bash
cd /home/michale/pgm/zhishu/tau
sha256sum -c dist/tau-arm64-tui.tar.sha256
docker load --input dist/tau-arm64-tui.tar
docker image inspect tau:arm64-tui-latest \
  --format 'image={{.Id}} platform={{.Os}}/{{.Architecture}} size={{.Size}}'
```

期望平台为 `linux/arm64`。

## 3. 准备运行配置

复制环境模板并只允许部署账号读取：

```bash
cp tau.env.production.example tau.env.production
chmod 600 tau.env.production
```

至少确认这些值：

- `TAU_DWS_HOST=host.docker.internal`；如果 DWS 是远端服务，改为真实内网地址；
- `TAU_DWS_ALLOWED_OBJECTS` 是 DBA 审批后的精确白名单，不能留空；
- `TAU_SAG_AGENT_ID`、`TAU_SAG_SOURCE_ID` 使用生产值；
- `TAU_SAG_AGENT_ORIGIN=http://api:8000`；
- `TAU_SAG_ENDPOINT=http://api:8000/mcp/`；
- `TAU_SAG_PLANNING_MODE=agent`；
- `TAU_DATA_AUTO_APPROVE_EXECUTE=0`。

如果 `providers.json` 引用环境变量形式的 LLM 密钥，把对应变量写入受保护的
`tau.env.production`，不要写进镜像或提交到 Git。

准备 Tau 用户配置：

```bash
mkdir -p "$HOME/.tau"
cp examples/credentials.json.example "$HOME/.tau/credentials.json"
chmod 700 "$HOME/.tau"
chmod 600 "$HOME/.tau/credentials.json" tau.env.production
```

编辑 `credentials.json`，写入：

```json
{
  "dataquery.dws.password": "<DWS 只读账号密码>",
  "dataquery.sag.token": "<SAG JWT>"
}
```

同时准备可用的 `$HOME/.tau/providers.json`。启动脚本把
`credentials.json`、`providers.json`、`dataquery.json`、`catalog.toml`
逐文件只读挂载；session 和日志写入 `tau-runtime/.tau`，不会因只读凭据挂载而失败。

## 4. 确认 SAG 网络并启动

先启动 SAG，再确认 Compose 网络。SAG 仓库声明了 `name: sag`，默认网络应为
`sag_default`：

```bash
docker network inspect sag_default >/dev/null
docker ps --filter label=com.docker.compose.project=sag
```

启动 Tau：

```bash
cd /home/michale/pgm/zhishu/tau
./tau-start.sh
```

如现场 Compose 项目名不同，显式覆盖网络名：

```bash
SAG_NETWORK=my_sag_default ./tau-start.sh
```

脚本会验证镜像、网络和 `TAU_SAG_PLANNING_MODE=agent`，用
`/bin/sleep infinity` 覆盖镜像的 `tau` entrypoint 以保持容器常驻，然后执行
一次 `tau --help`。重复运行会替换同名 `tau` 容器，但保留
`tau-runtime/.tau` 的会话与日志。

## 5. 上线验证

```bash
# 容器与网络
docker inspect tau --format '{{.State.Status}} {{.HostConfig.NetworkMode}}'

# SAG 容器网络可达；不经过 /sag 前缀
docker exec tau curl -fsS http://api:8000/api/v1/system/ready

# Tau CLI 与 JSON print 模式
docker exec tau tau --version
docker exec tau tau --print --mode json "查询 2025 年 4 月京能集团合并口径营收"

# 交互 TUI
docker exec -it tau tau
```

在 TUI 中确认：

1. `data_knowledge_search` 返回 SAG 引用；
2. `data_query_prepare` 展示冻结后的参数化 SQL；
3. 执行前出现授权确认；
4. `data_query_execute` 返回 bounded rows；
5. `Ctrl+O` 能看到真实 SAG exchange 和 citations；
6. `Ctrl+T` 只显示 provider 实际返回的 thinking。

## 6. 回滚

上线前保留上一版镜像标签，例如 `tau:arm64-tui-previous`。回滚只替换容器，
不删除 `tau-runtime/.tau`：

```bash
docker rm -f tau
IMAGE_TAG=tau:arm64-tui-previous ./tau-start.sh
```

## 常见故障

- `network sag_default not found`：SAG 未启动或 Compose 项目名不同，用
  `docker network ls` 找到真实网络后设置 `SAG_NETWORK`。
- `agent_origin_invalid`：Agent origin 含 `/sag` 路径；在 SAG 网络内必须使用
  `http://api:8000`。
- DWS 连接失败：确认宿主机端口对 Docker gateway 可达；若数据库只监听
  `127.0.0.1`，改为可被容器访问的绑定地址或使用远端内网地址。
- 工具未出现：确认 `TAU_SAG_PLANNING_MODE=agent`，然后新建 session 或 `/reload`。
- 非交互执行被拒绝：这是默认安全行为；生产探活不要开启自动 SQL 执行，只有在
  可信、受控的批处理场景才显式设置 `TAU_DATA_AUTO_APPROVE_EXECUTE=1`。
