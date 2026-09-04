# PRD: Tau TUI 离线部署 — Docker / ARM64 / SAG 知识库查询

Status: draft
Author: tau
Date: 2026-08-28
Upstream: `docs/PRD-sag-agent-sql-planner.md` (implemented 2026-08-26) + `docs/PRD-evidence-bound-data-query.md`

---

## 1. Problem Statement

`tau` 已在开发环境通过 `agent` 模式完成 SAG Agent SQL 规划 + DWS 只读查询的闭环（`data_knowledge_search → data_query_prepare → data_query_execute`），但缺少可交付到内网 ARM64 生产机的离线产物：

- 仓库无 `Dockerfile` / `build-arm64` 脚本，无法产出与 `sql-agent-loop/build-arm64-system.sh` 同标准的 `linux/arm64` 镜像包；
- 生产机 `SAG`（`/home/michale/pgm/rag/SAG`，`compose.prod.yaml`）与 `sql-agent-loop`（`/home/michale/pgm/zhishu/sql-agent-loop`，`.env.production` / `start.sh` / `nginx.conf`）已上线，SAG 仅通过宿主机 Nginx `https://fiams.powerbeijing.com/sag/ → 127.0.0.1:18080` 暴露，`tau` 的 `TAU_SAG_AGENT_ORIGIN` 校验（仅允许 `scheme+host+port`）与该路径前缀冲突，未有定案；
- 线上要求“不上前端、只用 TUI 查询 SAG 知识库 → 查库”，但无明确的镜像、配置、密钥、网络、启动、验证的端到端交付契约；
- `sql-agent-loop` 的 `CORS_ORIGINS=["https://fiams.powerbeijing.com"]` 与 `nginx.conf` 已固化，需明确 `tau` 是否需要 Nginx / CORS / 端口暴露。

需要在不改动 `sql-agent-loop` 与 `SAG` 生产配置的前提下，补齐 `tau` 的 Docker/ARM64/TUI 交付 PRD。

## 2. Solution

将 `tau` 作为**纯 TUI（Textual）+ Print 模式**的单镜像交付，不打包前端。镜像基于 `python:3.12-slim`，`ENTRYPOINT ["tau"]`，内置 `.[dataquery]` 额外依赖（`psycopg[binary]`, `sqlglot`），通过 `--network host` 直连本机 `SAG(18080)` 与 `DWS(35432)`；通过 `--env-file` + `~/.tau/credentials.json` 只读挂载注入配置与密钥；通过 `docker exec -it tau tau` 进入 TUI 完成 SAG 知识库检索与 DWS 查询的完整证据链与审计。

对齐 `sql-agent-loop` 的离线交付标准：`buildx --platform linux/arm64 --load`、`docker save` 导出 `dist/*.tar + .sha256`、`sha256sum -c` 校验后 `docker load`。

SAG 接入沿用 `PRD-sag-agent-sql-planner.md` 的 `agent` 模式契约：`POST /api/v1/openai/{agent_id}/chat/completions`（`Authorization: Bearer <JWT>`），`sag.citations` 转证据包，`data_query_prepare` 冻结参数化 SQL，`data_query_execute` 授权后只读执行；路径前缀问题通过“容器网络直连 `api:8000`”解决，无需改动宿主机 Nginx 与 CORS。

## 3. Goals / Non-Goals

**Goals**

- 产出可在 x86 交叉编译、ARM64 原生构建的 `tau:arm64-tui-latest` 镜像及 `dist/tau-arm64-tui.tar` 离线包；
- 一条命令进入 TUI 完成“自然语言 → SAG 规划 → 引用展示 → 冻结 SQL → 授权执行 →  bounded rows”；
- 配置与密钥与现有 `sql-agent-loop/.env.production` 风格一致，支持 `env > ~/.tau/dataquery.json > 默认值`，密钥不进镜像；
- 与线上 `SAG:intranet`（`compose.prod.yaml`）及 `sql-agent-loop` 共存，`--network host` 零端口冲突；

**Non-Goals**

- 不构建/发布前端镜像或 `tau-web`（`sql-agent-loop` 前端已在 `fiams.powerbeijing.com/`）；
- 不改动 `SAG` 的 `agents/threads/messages` 历史接口语义；
- 不引入 `tau` 的 Nginx/CORS/宿主机端口暴露（TUI 不走浏览器）；
- 不在镜像中烘焙密钥、业务数据或 `embedding` 模型。

## 4. User Stories

1. 作为运维，我希望在 x86 开发机一条脚本打出 `linux/arm64` 的 `tau` 镜像并导出 `tar+sha256`，拷贝到内网 ARM64 机 `docker load` 即可用。
2. 作为运维，我希望 `tau` 的构建、导出、校验流程与 `sql-agent-loop/build-arm64-system.sh` 完全对齐，便于同一套发版手册执行。
3. 作为内网用户，我希望不打开浏览器，仅 `docker exec -it tau tau` 进 TUI 就能用自然语言查 SAG 知识库并查 DWS。
4. 作为业务用户，我希望提问“2025年4月京能集团合并口径营收”时，`data_knowledge_search` 一次把实体、口径、期间、指标发给 SAG，SAG 返回带引用的 SQL 草案。
5. 作为审计员，我希望 `Ctrl+O` 能看到 `Tau→SAG` 的真实重写请求与 `SAG→Tau` 的原始回复及 `citations`，`Ctrl+T` 看模型 thinking。
6. 作为安全员，我希望 `token/password/param values` 永不进模型可见内容、日志或镜像层。
7. 作为 DBA，我希望 `TAU_DWS_ALLOWED_OBJECTS` 默认 fail-closed，仅允许 `exchange_service` 下白名单表。
8. 作为运维，我希望 `SAG` 的 `/sag/` 路径前缀不导致 `tau` 的 `TAU_SAG_AGENT_ORIGIN` 校验失败（通过容器网络直连解决）。
9. 作为运维，我希望 `tau` 与 `SAG`、`sql-agent-loop` 同机 `--network host` 共存，无需额外端口或 CORS 配置。
10. 作为开发者，我希望 `tau --print --mode json "…"` 可脚本化，用于 CI 探活与回归。

## 5. System Architecture & Deployment Topology

```
浏览器 ──https(443)──> 宿主机 Nginx (nginx.conf)
  ├─ /            → /mnt/data/web/sql-agent/dist  (sql-agent-loop 前端)
  ├─ /api/ ,/mcp/ → 127.0.0.1:8000                  (sql-agent-loop backend)
  └─ /sag/        → 127.0.0.1:18080                  (SAG compose.prod.yaml: nginx:80 → api:8000/web:3000)

内网直连（--network host）:
  tau  ──http──> 127.0.0.1:18080/sag/api/v1/openai/{agent_id}/chat/completions  (或容器网络 api:8000)
  tau  ──psycopg──> 127.0.0.1:55432/35432  (DWS openGauss, .env.production: DATABASE_URL)
  SAG: sag-api:intranet / sag-web:intranet / sag-embedding:intranet / sag-nginx:intranet (compose.prod.yaml)
  sql-agent-loop: sql-agent:arm64-system-latest (--network host --env-file .env.production)
```

关键约束：

- `SAG` 生产仅暴露 `18080` 给宿主机 Nginx；`tau` 若用 `127.0.0.1:18080` 需带 `/sag` 前缀，但 `tau_coding/dataquery/config.py::_agent_origin_error()` 禁止 `origin` 含路径 → 采用方案 A（容器网络直连 `http://api:8000`）规避（见 §9）。
- `CORS_ORIGINS=["https://fiams.powerbeijing.com"]` 仅约束浏览器，不影响 `tau` TUI 的服务端直连。

## 6. Implementation Decisions

1. **单镜像、双入口**：镜像 `ENTRYPOINT ["tau"]`，默认 `CMD ["--help"]`；交互 `docker exec -it tau tau` 进 TUI，非交互 `docker exec tau tau --print "…"` 走 `run_openai_print_mode`。
2. **对齐 `build-arm64-system.sh` 标准**：`TARGET_PLATFORM=linux/arm64`、`buildx --load`、三重试、`docker save + sha256`、`.dockerignore` 排除 `.env`、`*credentials*`。
3. **不新增 Nginx/CORS**：`tau` 无 `listen` 端口；`nginx.conf` 保持不变。
4. **配置三级优先级不变**：`env > ~/.tau/dataquery.json > 默认值`（`src/tau_coding/dataquery/config.py:resolve_data_query_config`）。
5. **密钥永不进镜像**：`DWS password` 与 `SAG token` 仅来自 `TAU_SAG_TOKEN` 环境变量或只读挂载的 `~/.tau/credentials.json`（`dataquery.dws.password` / `dataquery.sag.token`）。
6. **网络采用 `--network host`**：与 `sql-agent-loop/start.sh` 一致，省去端口映射与额外网桥；`tau` 容器常驻 `tail -f /dev/null`，交互用 `exec`。
7. **SAG 路径问题用容器网络解决**：`tau` 加入 `sag` compose 网络后以 `http://api:8000` 直连，`TAU_SAG_AGENT_ORIGIN` 保持 `http://api:8000`（合法 origin），`TAU_SAG_ENDPOINT` 同理 `http://api:8000/mcp/` 用于 citation 展开。
8. **保留 `agent` 模式完整契约**：一次重写提问 + citation 证据包 + 冻结 `planId` + 授权执行 + `retryContext` 同会话修正，不引入新工具名。
9. **失败分类不变**：仅 DWS SQL 执行错误触发 `retryContext` 修正；鉴权/连接/缺 citation 等为不可重试错误。
10. **可观测性不变**：`Ctrl+O` 展开真实 `sagExchange`（request/response/citations/attempt），`provider` thinking 由 `Ctrl+T` 控制。

## 7. Docker Image Contract

### 7.1 Dockerfile（`tau/Dockerfile`）

```dockerfile
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 TAU_NO_UPDATE_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN pip install --upgrade pip && pip install .[dataquery] && tau --help >/dev/null
ENTRYPOINT ["tau"]
CMD ["--help"]
```

### 7.2 .dockerignore（`tau/.dockerignore`）

```
.git
.venv
__pycache__
*.pyc
.env
.env.*
dist
logs
.mypy_cache
.pytest_cache
```

### 7.3 构建脚本（`tau/build-tau-arm64.sh`，对齐 `build-arm64-system.sh`）

```bash
#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
TAG=${1:-"tau:arm64-tui-latest"}
PLATFORM=${TARGET_PLATFORM:-"linux/arm64"}
ARCHIVE=${IMAGE_ARCHIVE:-"dist/tau-arm64-tui.tar"}
mkdir -p dist
echo "==> buildx $PLATFORM $TAG"
for i in 1 2 3; do
  if docker buildx build --platform "$PLATFORM" --progress=plain --load -t "$TAG" -f Dockerfile .; then break; fi
  [[ $i -lt 3 ]] && { echo "retry $i..."; sleep 5; } || exit 1
done
docker image inspect "$TAG" --format '{{.Id}} {{.RepoTags}} {{.Size}} bytes'
echo "==> save $ARCHIVE"
docker save --output "$ARCHIVE" "$TAG"
(cd dist && sha256sum "$(basename "$ARCHIVE")" > "$(basename "$ARCHIVE").sha256")
echo "done: $ARCHIVE + .sha256"
```

本地 x86 交叉编译：`./build-tau-arm64.sh`；ARM64 本机：`docker build -t tau:arm64-tui-latest -f Dockerfile .`

### 7.4 镜像标签与产物

- `TAG`: `tau:arm64-tui-latest`（`BACKEND_TAG` 风格，可覆盖 `TAG=tau:1.2.3-arm64`）
- `ARCHIVE`: `dist/tau-arm64-tui.tar` + `dist/tau-arm64-tui.tar.sha256`（`IMAGE_ARCHIVE` 可覆盖）
- 基础镜像：`python:3.12-slim`（`pyproject.toml: requires-python >=3.12`）

## 8. Configuration Contract

### 8.1 运行时 env（`tau/tau.env.production`，示例）

```bash
# DWS
TAU_DWS_HOST=127.0.0.1
TAU_DWS_PORT=35432
TAU_DWS_DATABASE=exchange_service
TAU_DWS_USER=hubble
TAU_DWS_SSL_MODE=prefer
TAU_DWS_TIMEOUT_SECONDS=180
TAU_DWS_MAX_ROWS=1000
TAU_DWS_MAX_RESULT_BYTES=1048576
TAU_DWS_MAX_CELL_BYTES=65536
TAU_DWS_CONNECT_TIMEOUT=10
TAU_DWS_PROBE_QUERY=SELECT 1
TAU_DWS_ALLOWED_OBJECTS=exchange_service.bpc_zbpc_con_s001,exchange_service.bpc_zbpc_con_s005

# SAG — agent 模式（推荐）
TAU_SAG_PLANNING_MODE=agent
TAU_SAG_AGENT_ORIGIN=http://api:8000          # 容器网络直连；--network host 时可用 http://127.0.0.1:8100
TAU_SAG_AGENT_ID=4cffe50252cc47d7a55e5a46b0fe247e
TAU_SAG_AGENT_TIMEOUT_SECONDS=60
TAU_SAG_AGENT_ANSWER_MAX_BYTES=65536
TAU_SAG_CITATION_LIMIT=5
TAU_SAG_CITATION_SNIPPET_MAX_BYTES=8192
TAU_SAG_PLANNER_TRANSCRIPT_MAX_BYTES=262144
# MCP 仅用于 citation 展开（可选，配了更好）
TAU_SAG_ENDPOINT=http://api:8000/mcp/
TAU_SAG_SOURCE_ID=e1807e688bc449439f2af60c2fad9bd9
TAU_SAG_RPC_TIMEOUT_SECONDS=60
TAU_SAG_SEARCH_SUMMARY_MAX_BYTES=8192

# 行为
TAU_DATA_AUTO_APPROVE_EXECUTE=0
TAU_NO_UPDATE_CHECK=1
```

`TAU_SAG_PLANNING_MODE`: `unconfigured|legacy|agent`（`unconfigured` 不注册工具；本部署固定 `agent`）。
`--network host` 下 `TAU_SAG_AGENT_ORIGIN=http://127.0.0.1:8100` 亦可（若 SAG API 直暴露该端口）。

### 8.2 密钥（`~/.tau/credentials.json`，挂载只读）

```json
{
  "dataquery.dws.password": "hubble@123456",
  "dataquery.sag.token": "eyJhbG..."
}
```

或 `tau.env.production` 中 `TAU_SAG_TOKEN=eyJ...`（`env` 优先于 `credentials.json`）。

### 8.3 用户级配置（`~/.tau/dataquery.json`，可选替代 env）

`examples/dataquery.env.example` / `examples/dataquery.json.example` 为权威模板；生产推荐 `env`。

### 8.4 LLM Provider

沿用 `~/.tau/providers.json` + `QWEN36_27B_API_KEY`（或 `OPENAI_API_KEY`），挂载只读即可。

## 9. SAG Integration Contract（关键）

- **Agent 端点**：`POST {origin}/api/v1/openai/{agent_id}/chat/completions`，`Authorization: Bearer <JWT>`，`{"messages":[{"role":"user","content":"…"}],"stream":false}`，返回 `choices[0].message.content` + `sag.citations[]`（见 `SAG/apps/api/sag_api/api/v1/openai.py`）。
- **Origin 校验**：`src/tau_coding/dataquery/config.py::_agent_origin_error()` 仅允许 `http(s)://host[:port]`，拒绝路径/query/fragment/`userinfo`，末尾 `/` 自动归一化；`sag_agent_id` 需 `^[A-Za-z0-9_-]{1,128}$`。
- **`/sag` 前缀冲突的三种解法（本 PRD 选 A）**：
  - **A. 容器网络直连（推荐，无代码改动）**：`tau` 加入 `sag` compose 网络，`TAU_SAG_AGENT_ORIGIN=http://api:8000`，完全绕过宿主机 Nginx 的 `/sag/` 前缀。
    ```bash
    docker network ls | grep sag
    docker network connect sag tau   # sag 为 compose name，视实际网络名
    ```
  - **B. 宿主机 Nginx 加根代理**：`location ^~ /api/v1/openai/ { proxy_pass http://127.0.0.1:18080/sag/api/v1/openai/; }`，则可用 `TAU_SAG_AGENT_ORIGIN=https://fiams.powerbeijing.com`。
  - **C. 改 Tau 校验**：允许 `sag_agent_origin` 为 `/sag` 路径（改一行后重打镜像）。
- **MCP 展开**：`TAU_SAG_ENDPOINT` 保留用于 `data_knowledge_read` 展开 `chunk_id`；不配置时 citation 仍可见但不可展开。
- **已上线 SAG**：`compose.prod.yaml` 的 `api: sag-api:intranet / nginx:80` 仅通过 `宿主机Nginx:18080` 对外；`CORS_ORIGINS` 已为 `https://fiams.powerbeijing.com`，对 `tau` 直连无影响。

## 10. Security

- 镜像不含 `.env` / `credentials.json` / 日志 / 大文件（`.dockerignore` 强制）；
- `SAG token` / `DWS password` / `param values` 在 `tool details`、审计、`sagExchange` 展示前脱敏；
- `execute` 需用户确认（`TAU_DATA_AUTO_APPROVE_EXECUTE=0` 时弹窗，`1` 仅限可信内网）；
- `DWS` 账号为只读，`ALLOWED_OBJECTS` 白名单 + `SqlPolicyChecker` + 冻结 `planId`；
- `SAG` 返回的 SQL 视为不可信输入，仍需 `prepare` 的 AST/函数/占位符/证据校验。

## 11. Deployment Procedure（对齐 `start.sh` / `build-arm64-system.sh`）

### 11.1 构建（开发机 x86）

```bash
cd /home/michale/pgm/zhishu/tau
chmod +x build-tau-arm64.sh
./build-tau-arm64.sh tau:arm64-tui-latest
# 产物：dist/tau-arm64-tui.tar + dist/tau-arm64-tui.tar.sha256
# 拷到上线机：scp dist/tau-arm64-tui.tar* user@fiams:/home/michale/pgm/zhishu/tau/dist/
```

### 11.2 校验与载入（上线机 ARM64）

```bash
cd /home/michale/pgm/zhishu/tau
sha256sum -c dist/tau-arm64-tui.tar.sha256
docker load --input dist/tau-arm64-tui.tar
docker image inspect tau:arm64-tui-latest --format '{{.Id}} {{.Size}}'
```

### 11.3 启动（与 `sql-agent-loop/start.sh` 同风格）

```bash
# 准备
mkdir -p tau-runtime/logs
# tau.env.production 与 ~/.tau/credentials.json 已就绪（§8）

docker rm -f tau 2>/dev/null || true
docker run -d --name tau --network host --user root \
  --env-file /home/michale/pgm/zhishu/tau/tau.env.production \
  -e QWEN36_27B_API_KEY="${QWEN36_27B_API_KEY:-}" \
  -v "$HOME/.tau:/root/.tau:ro" \
  -v "$(pwd)/tau-runtime:/app/runtime:rw" \
  --restart unless-stopped \
  tau:arm64-tui-latest tail -f /dev/null

# 加入 SAG 网络以直连 api:8000（方案 A）
docker network connect sag tau 2>/dev/null || docker network connect rag_sag tau 2>/dev/null || true

echo "exec: docker exec -it tau tau"
```

`tau-start.sh`（可选，同 `sql-agent-loop/start.sh`）：

```bash
#!/bin/bash
set -Eeuo pipefail
[[ -f tau.env.production ]] || { echo "缺 tau.env.production"; exit 1; }
docker run -d --name tau --network host --user root \
  --env-file tau.env.production \
  -e QWEN36_27B_API_KEY="${QWEN36_27B_API_KEY:-}" \
  -v "$HOME/.tau:/root/.tau:ro" \
  --restart unless-stopped tau:arm64-tui-latest tail -f /dev/null
docker network connect sag tau 2>/dev/null || true
```

### 11.4 使用

```bash
docker exec -it tau tau
# TUI 内：输入“2025年4月京能集团合并口径营收” → data_knowledge_search → prepare → 授权 → execute
# 非交互：
docker exec tau tau --print "查询 2024 年京能集团合并口径营收"
docker exec tau tau --print --mode json "查 s001 表结构" | jq .
```

## 12. Verification

```bash
# SAG
curl -fsS http://127.0.0.1:18080/sag/api/v1/system/ready
curl -H "Authorization: Bearer $SAG_TOKEN" http://api:8000/api/v1/openai/4cffe50252cc47d7a55e5a46b0fe247e/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"__probe__"}],"stream":false}' | jq .
# DWS
psql "postgresql://hubble:hubble@123456@127.0.0.1:35432/exchange_service?sslmode=prefer" -c "select 1"
# Tau
docker exec tau tau --help
docker exec tau tau --print "test" 2>&1 | head -n 30
```

TUI 内验证：`Ctrl+O` 展开 `SAG exchange`（含 `mode=agent, attempt, citations`），`0 rows` 为终态不重试，SQL 失败自动同会话 `retryContext` 修正。

## 13. Testing Decisions

- 复用 `PRD-sag-agent-sql-planner.md` 的测试策略：`tests/fixtures/sag_agent/` 契约固件 + `fake SAG Agent HTTP + fake DWS` 的主工作流缝合测试 + `TuiEventAdapter` 投影测试；
- 新增：`Dockerfile` 烟雾测试（`tau --help` / `tau --print` 在镜像内可执行）、`build-tau-arm64.sh` 干跑校验。

## 14. Out of Scope

- `tau-web`、`frontend/Dockerfile`、宿主机 Nginx 的 `tau` 反代；
- SAG 的 `threads/messages` 轮询、Agent/知识源的创建与权限管理；
- 宿主机 `CORS`、`TLS` 证书、`fiams.powerbeijing.com` 域名变更；
- 多 Agent 并发、SAG 线程跨会话复用。

## 15. Further Notes

- 本 PRD 继承 `PRD-evidence-bound-data-query.md` 的证据/冻结/审计/只读执行边界，仅变更交付形态与部署拓扑；
- 线上 `TAU_SAG_PLANNING_MODE` 必须显式为 `agent`，否则诊断 `planning_mode_unconfigured` 且不注册工具；
- 若 `TAU_SAG_AGENT_ORIGIN` 需承载 `/sag`，必须执行 §9 方案 A/B/C 之一，否则启动即 `agent_origin_invalid`。
