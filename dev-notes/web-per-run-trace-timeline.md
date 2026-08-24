# Dev Notes: Per-Run Trace Timeline for Tau Web

Date: 2026-08-24 · PRD: `docs/PRD-web-trace.md` · Status: implemented

## What was added

Tau Web 的 Trace 面板现在按 run（一次提问 → 结果返回）分组展示完整事件时间线：

- **后端（tau_coding/web.py）**
  - 运行期间转发的每个 `CodingSessionEvent` 都附加 `runId` 和 `timestamp`
    （毫秒 epoch），前端据此分组排序；
  - `run_finished` 新增摘要字段：`turnCount`、`eventCount`、`durationMs`；
  - 断线重连时，若仍有活动 run，在 `web_connected` 之后补发一条
    `run_summary {runId, status, eventCount, turnCount, elapsedMs}`，
    不回放历史事件。
- **前端（data/web/）**
  - 新增纯函数模块 `trace-timeline.js`：把 SSE 事件流折叠成
    「run → items」视图模型。无 DOM 依赖，浏览器与 Node 双端可用
    （浏览器挂 `window.TraceTimeline`，Node 走 CommonJS 导出）；
  - `app.js` 维护 600 条事件的滚动缓冲区，每次非 delta 事件后重建时间线 DOM；
  - 工具调用渲染为可折叠节点：工具名 + 授权状态 + 参数 JSON + 原始结果，
    提供「复制参数 / 复制原始事件」按钮；授权决定通过 requestId 回填状态。

## Why it exists

此前 trace 面板只有粗粒度生命周期事件，无法回答"这次提问 agent 经历了什么、
工具传了什么参数、返回了什么"。会话 JSONL 是事后手段；用户需要实时、按次分组
的轨迹视图（PRD 中的 Problem Statement）。

## Design decisions (ADR-flavored)

1. **核心层零改动**：trace 所需信息已由 portable 层 AgentEvent 携带，Web 后端
   只做投影（加 runId/timestamp），符合 `AgentHarness = 大脑 / 宿主决定展示`
   的分层原则。
2. **文本流式 delta 不进 trace**：transcript 面板负责正文；timeline 只保留
   消息边界，控制带宽与视觉噪音。
3. **SSE 契约只增不改**：旧字段全部保留，新增字段对旧客户端无害。
4. **重连不回放**：完整历史以 JSONL 为准，重连只补活动 run 的摘要。

## Testing seams

- 后端：`tests/test_web.py` 现有模式 —— 假 provider 发脚本化事件，断言 SSE 帧
  内容（`test_message_api_tags_trace_events_with_run_id_and_finish_summary`、
  `test_sse_reconnect_mid_run_receives_active_run_summary`）。
- 前端：`tests/web/trace-timeline.test.mjs` 表驱动测试视图模型规则
  （分组/标题/工具生命周期/授权回填/delta 忽略/run_summary 合并）。

```bash
uv run pytest tests/test_web.py          # 后端
node --test tests/web/                   # 前端视图模型
```

## How to verify manually

1. `uv run tau-web --no-open`，打开 http://127.0.0.1:8080；
2. 选一个会话发一条会触发工具的提示（如"用 bash 执行 ls"）；
3. 观察右侧轨迹面板出现新 run 分组：用户输入 → ⚙ 工具节点（展开看参数/结果）
   → 助手回答；头部显示状态 · 轮次 · 耗时；
4. 授权弹窗点允许后，工具节点上的"待确认"应变为执行态；
5. 运行中刷新页面：面板通过 run_summary 恢复计数与"运行中"状态；
6. 点击工具节点的「复制原始事件」，粘贴出的应是该条 SSE payload JSON。
