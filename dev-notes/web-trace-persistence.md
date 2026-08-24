# Dev Notes: Tau Web Trace Persistence & Unified Timeline

Date: 2026-08-24 · Spec: `dev-notes/web-trace-persistence-design.md` · Plan:
`docs/superpowers/plans/2026-08-24-web-trace-persistence.md` · Status: implemented

## What was added

Tau Web 的轨迹面板现在是一棵**统一时间线树**，并且事件历史可以跨刷新、
跨进程重启恢复：

- **后端（tau_coding/web.py）**
  - `_WebSessionSlot.trace_buffer`：有界环缓冲（`TRACE_BUFFER_LIMIT = 600`）。
    `_publish` 转发的所有会话事件（除连接层 `web_connected`）自动入缓冲；
  - `_subscribe` 在 `web_connected` 之后按序重放缓冲，每帧附加 `"replay": true`
    （重放之后仍补发活动 run 的 `run_summary` 与未决授权，契约只增不改）；
  - 落盘 `<session_id>.webtrace.jsonl`（与会话 JSONL 同目录，由 `record.path`
    派生）：追加写；行数超过 `TRACE_FILE_COMPACT_LINES = 1200` 时原子压缩为
    最近 `TRACE_FILE_KEEP_LINES = 600` 行；runtime 启动后首次订阅且尚未回填时
    从文件尾部读回；删除会话连带删除（忙碌拒绝时不删）；
    文件损坏/不可写降级为纯内存缓冲并 warning；
  - 新事件 `tool_authorization_resolved {requestId, toolCallId, decision}`：
    用户决定（allow/deny/cancel）、超时（timeout）、最后订阅者断开自动拒绝
    （no_subscriber）都会广播并入缓冲/落盘——授权状态不再停留在「待确认」。
- **前端（data/web/）**
  - 底部滚动 ticker `#event-timeline` 已删除；右侧只剩一棵时间线：
    若干 run 分组 + 固定在最末的「会话事件」分组（`runId === "__session__"`，
    收纳无 runId 的连接/配置/命令结果等事件）；
  - `trace-timeline.js` 处理 resolved 事件按 requestId 回填工具授权状态；
    `maxRuns` 只限制真实 run 数量；
  - 带 `"replay": true` 的帧只进视图模型，不触发 toast / 授权弹窗 /
    running 状态机 / transcript 重载；
  - `element()` 只接受 string/number 文本，其余丢弃并 console.warn——
    杜绝 `[object HTMLElement]` 类渲染产物；
  - 静态脚本 URL 带内容哈希（`/app.js?v=<sha256 前 8 位>`），服务端启动时计算、
    返回 index.html 时注入，排除浏览器缓存旧 JS。

## Why it exists

此前 trace 事件只存在于打开的那个浏览器标签页内存里：刷新页面历史全丢，
右侧还分成 run 时间线与事件 ticker 两处，信息割裂。会话 JSONL 是持久真相，
但缺少运行时投影（授权状态、耗时等）。本设计让宿主进程成为轨迹的单一事实来源
（内存缓冲 + 磁盘镜像），前端保持无状态投影。完整问题陈述与决策见 spec。

## Design decisions (ADR 摘要)

1. **重放放服务端而非 localStorage**：多标签页/换浏览器一致，避免前端拼接。
2. **旁路 `.webtrace.jsonl` 而非塞进会话 JSONL**：webtrace 是 Web 宿主展示
   投影，不是 agent 记忆；独立清理，不污染核心格式。
3. **单条 SSE 流承载实时 + 重放**（显式 `replay` 字段隔离副作用），不建第二条连接。
4. **授权 resolved 广播补齐闭环**：原实现把决定留在点击浏览器内存里，是刷新后
   状态失真的根因之一。

## Testing seams

```bash
uv run pytest tests/test_web.py          # 后端（含重放/落盘/跨重启/压缩/删除）
node --test tests/web/trace-timeline.test.mjs   # 视图模型（分组/resolved 回填/maxRuns）
```

## How to verify manually

1. `uv run tau-web --no-open`，打开 http://127.0.0.1:8080；
2. 发送一条触发工具的提示（如「用 bash 执行 ls」），观察右侧出现新 run 分组
   与底部「会话事件」分组；
3. **刷新页面（F5）**：run 历史（含工具参数/结果节点）应完整恢复，无闪烁重建；
4. 重启 tau-web 再刷新：最近约 600 条事件从 `.webtrace.jsonl` 回填，依然可见；
5. 打开两个标签页，在其中一个允许授权：另一个的工具节点状态同步变为已授权；
6. 删除一个非运行中会话，确认其 `.webtrace.jsonl` 一并被移除。
