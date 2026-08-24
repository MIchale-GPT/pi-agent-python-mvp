# Spec: Tau Web 轨迹统一时间线与持久化

Date: 2026-08-24 · Status: approved design, pending implementation
前置：`docs/PRD-web-trace.md`、`dev-notes/web-per-run-trace-timeline.md`

## 问题陈述

当前 Tau Web 的轨迹信息分散在三处，且不可持久：

1. 中间 transcript：对话消息（已随会话 JSONL 持久化，刷新可恢复）；
2. 右侧 `#run-timeline`：按 run 分组的工具/消息节点，数据源是浏览器内存
   `state.traceEvents`，刷新即清空，服务端重连只补发活动 run 的 `run_summary`；
3. 右侧底部 `#event-timeline`：最多保留 9 条的滚动事件 ticker，同样刷新即丢。

用户感知为「割裂」：同一次运行的相关信息要在多个列表间跳转；刷新页面后历史
轨迹全部消失。另外界面上出现过 `[object HTMLElement]` 字样（右侧轨迹面板节点内），
现有代码未发现可复现路径，推断与浏览器缓存旧版 JS 或某次中间版本有关，需防御。

## 目标 / 非目标

**目标**

- G1 右侧只保留一棵统一时间线树：run 分组节点 + 「会话事件」分组，删除底部 ticker。
- G2 刷新页面/断线重连后，最近的轨迹历史完整恢复（服务端内存缓冲 + SSE 重放）。
- G3 tau-web 进程重启后，轨迹历史仍可回看（落盘 `.webtrace.jsonl`）。
- G4 工具授权决定闭环广播，刷新后授权状态不再停留在「待确认」。
- G5 前端渲染层杜绝 `[object …]` 字样；静态资源加 cache-busting 排除旧缓存。

**非目标**

- 不改动中间 transcript 的渲染与数据流。
- 不改变 trace 事件的内容语义（仍不含流式 text delta）。
- 不做轨迹的跨设备同步、检索、导出（导出仍走既有 HTML/JSONL 通道）。
- 核心层（tau_agent / tau_ai）零改动。

## 架构

分层不变：browser (live UI) ← HTTP/JSON + SSE → tau_coding.web（runtime adapter）
→ tau_agent。本设计只触及 `tau_coding/web.py`（投影 + 存储）与 `data/web/*`（渲染）。

### 1. 后端：缓冲、重放、落盘、授权闭环（src/tau_coding/web.py）

- **环缓冲**：`_WebSessionSlot` 新增 `trace_buffer: deque[dict[str, object]]`，
  上限 `TRACE_BUFFER_LIMIT = 600` 条，超出淘汰最旧。
- **入缓冲范围**：凡经 `_publish()` 转发的会话事件均入缓冲并落盘
  （含 run_started/run_finished/run_error/cancel_requested）；连接层帧
  （`web_connected`）与按订阅者生成的控制帧不入。新增事件类型：
  - `tool_authorization_resolved {requestId, toolCallId, decision}`：
    在 `_respond_tool_authorization` 成功递交决定、以及
    `_resolve_pending_tool_authorizations`（超时/无订阅者拒绝）时发布。
- **订阅时序**（`_subscribe`）：`web_connected` → 按序重放 `trace_buffer`
  （每帧附加 `"replay": true`）→ 活动 run 的 `run_summary`（保留现状，兼容既有
  测试与语义）→ 未决授权请求重发。
- **落盘**：
  - 文件：会话 JSONL 同目录 `<session_id>.webtrace.jsonl`，每行一个 JSON 对象
    （与 SSE data 字段一致，含 `replay` 写入时恒为缺省）；
  - 写入时机：事件入缓冲时在 runtime 事件循环内同步追加（本地单用户场景，
    追加量小，可接受阻塞）；打开失败/写失败 → 降级为纯内存缓冲，
    记录 warning，不影响会话运行；
  - 压缩：追加后若总行数 > `1200`（2 × 上限），原子重写仅保留最近 600 行
    （临时文件 + `os.replace`）；
  - 回填：runtime 启动后首次访问某会话 slot 时，若内存缓冲为空则从文件尾部
    读回最近 600 条；
  - 生命周期：删除会话（`delete_session`）连带删除 webtrace 文件。
- **并发与有序性**：入队、重放均在 runtime 事件循环内串行执行，天然有序；
  重放期间新事件只会排在重放序列之后。

### 2. 前端：统一时间线（src/tau_coding/data/web/）

- **index.html**
  - 删除 `<ol id="event-timeline">` 区块及其静态条目；
  - 保留 `#trace-state` 状态 chip 与「展开全部」按钮；
  - `<script>` 标签加 `?v=<内容短哈希>` 实现 cache-busting：服务端启动时对各
    JS 文件计算 SHA-256 前 8 位；index.html 由服务端返回时做字符串替换注入
    （静态文件本身不落盘修改）。
- **trace-timeline.js**（保持纯函数、无 DOM）
  - 无 `runId` 的事件归入合成分组 `runId: "__session__"`（标题「会话事件」，
    items 按到达顺序排列），不再进入 `ungrouped`；`ungrouped` 输出保留但预期为空；
  - 新增处理 `tool_authorization_resolved`：按 `requestId` 找到对应工具项，
    更新 `authorization.status` 与 `state`（denied/cancelled 回填，allowed 保持执行态）；
  - 多 run 历史由现有分组逻辑重建；`maxRuns` 仅影响展示条数；
  - 兼容：`authorizationDecisions` options 参数保留（旧路径），resolved 事件优先。
- **app.js**
  - 删除对 `#event-timeline` 的直写：`addTraceEvent`、`markIndexLoaded`、
    `markBranchLoaded` 改为向 `state.traceEvents` 注入合成会话级事件
    （沿用现有 type 字符串，附 `timestamp`），随后 `renderRunTimeline()` 统一渲染；
  - SSE 处理：带 `"replay": true` 的帧只走 `recordTraceEvent` +
    `renderRunTimeline()`，跳过一切 UI 副作用（toast、授权弹窗、running 状态机、
    `loadSession` 重载）——重放帧由视图模型消化；
  - 本地 `state.traceAuthorizationDecisions` 逻辑保留作即时反馈，权威状态以
    resolved 事件为准。

### 3. 渲染防御（app.js）

- `element(tag, className, text)`：仅当 `typeof text === "string"` 或
  `"number"` 时设置 `textContent`；其余类型忽略并 `console.warn`
  （含调用参数摘要），使未来同类问题可在控制台定位而非渲染成 `[object …]`。

## 数据契约（SSE / 文件）

```jsonc
// 重放帧示例（SSE data 与 webtrace 行一致）
{
  "type": "tool_execution_end",
  "sessionId": "…", "runId": "…", "timestamp": 1756000000123,
  "replay": true,          // 仅实时转发时缺省
  "result": { "output": "…" }
}
// 新增事件
{ "type": "tool_authorization_resolved", "sessionId": "…", "runId": "…",
  "timestamp": 1756000000456, "requestId": "…", "toolCallId": "…",
  "decision": "allow" | "deny" | "cancel" | "timeout" | "no_subscriber" }
```

契约原则延续「只增不改」：旧字段全部保留，新字段对旧客户端无害。

## 错误处理

| 场景 | 行为 |
|---|---|
| webtrace 文件损坏/不可写 | 降级纯内存缓冲，warning 日志一次，不重复刷屏 |
| 压缩重写中途失败 | 保留原文件，本次放弃压缩，下次再试 |
| 会话目录不存在（异常态） | 同上降级，不抛出 |
| 重放帧在前端触发异常 | 现有 onmessage try/catch 兜底，不中断连接 |

## 测试

- 后端 `tests/test_web.py`（沿用假 provider 脚本化事件模式）：
  - 订阅即按序收到缓冲重放，帧含 `replay: true`；
  - 缓冲超过 600 条截断最旧；
  - 授权 allow/deny 后订阅者收到 `tool_authorization_resolved`；
    断线自动拒绝路径同样发布；
  - 预置 `.webtrace.jsonl` → 新建 TauWebRuntime → subscribe 收到回填重放（跨重启）；
  - 删除会话后 webtrace 文件不存在；行数超限触发压缩。
- 前端 `tests/web/trace-timeline.test.mjs`（表驱动）：
  - 无 runId 事件归入 `__session__` 分组且保序；
  - `tool_authorization_resolved` 按 requestId 回填状态；
  - 混合重放多 run 重建、`maxRuns` 截取、`ungrouped` 为空。
- 命令：`uv run pytest tests/test_web.py`；`node --test tests/web/`。

## 文档

- 本 spec（dev-notes）+ 实现完成后补充 `dev-notes/web-trace-persistence.md`
  的 what/why/how-to-verify；
- `website/content/guides/web.md`：轨迹面板说明更新（统一时间线、刷新恢复）；
- `docs/PRD-web-trace.md` 附录记录持久化扩展。

## 决策记录（ADR 摘要）

1. **重放放服务端而非前端 localStorage**：单一事实来源在宿主进程，多标签页/
   换浏览器一致，前端保持无状态投影；localStorage 方案存在配额与拼接一致性缺陷。
2. **落盘选会话目录旁路文件而非塞进会话 JSONL**：webtrace 是 Web 宿主的展示
   投影，不是 agent 记忆的一部分；独立文件可独立清理，不污染核心会话格式。
3. **replay 标记而非双端点**：一条 SSE 流同时承载实时与重放，避免前端管理两条
   连接的竞态；副作用隔离靠显式字段。
4. **授权 resolved 广播补齐闭环**：原实现把决定留在点击浏览器内存中，是刷新后
   状态失真的根因之一，属必要修正而非新特性。
