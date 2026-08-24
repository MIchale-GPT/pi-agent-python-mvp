# PRD: Web Trace Workbench 完整运行轨迹（Per-Run Full Trace）

状态: draft · 关联: `docs/tau-web-todo.md`、roadmap issue huggingface/tau#1

## Problem Statement（问题陈述）

用户在 Tau Web（Trace Workbench）里发起一次提问后，Trace 面板只能看到粗粒度的生命周期事件
（`server_ready`、`session_index_loaded`、`tool_execution_start/end`、`tool_authorization_requested`、
`agent_settled`、`run_finished`、`queue_update` 等）。用户无法回答这些问题：

- 我这一次提问，agent 到底经历了哪些步骤？
- 模型调用了哪个工具、传了什么参数、返回了什么结果？
- 从用户输入到最终结果返回，中间的完整交互序列是什么？

现有会话 JSONL 记录和 HTML 导出是事后查看手段；用户需要的是**当前会话、实时、按次分组**的完整轨迹视图。

## Solution（解决方案）

将 Trace 面板升级为 **按 run（一次提问→结果返回）分组的完整时间线**：

```
Run #3 「上海天气如何」                    ✓ completed · 2 turns · 12.4s
├─ user      上海天气如何
├─ turn 1
│  ├─ assistant  toolCall: bash
│  ├─ ⚙ bash    {"command": "curl wttr.in/shanghai"}   ← 参数可展开
│  │   └─ ✓ 结果  "Beijing: ☀ +31°C …"                （120s 授权窗口内确认后执行）
│  ├─ toolResult 回传给模型
│  └─ turn end
├─ turn 2
│  ├─ assistant  流式文本回答（边界事件入轨，正文在 transcript 展示）
│  └─ turn end
└─ agent_settled → run_finished
```

- 后端复用已有的 `AgentEvent` 流（消息边界、工具调用参数/结果本就在其中），在现有 SSE
  trace 通道上补充发布 message/tool 级事件，并给每个 run 分配 `runId` 用于前端分组。
- 前端按 `runId` 渲染时间线：工具节点默认折叠参数与结果、点击展开；授权请求与对应的
  工具执行通过 `toolCallId` 关联展示。
- 文本流式 delta 不逐条进 trace（transcript 面板已负责正文渲染），只记录边界与统计。

## User Stories（用户故事）

1. 作为 Tau Web 用户，我希望 Trace 面板按我每次提问分组展示事件，以便我能对应「哪次输入产生了哪些行为」。
2. 作为 Tau Web 用户，我希望看到每次 run 从用户输入到结果返回的完整事件序列，以便理解 agent 的完整执行路径。
3. 作为 Tau Web 用户，我希望看到每次工具调用的名称和完整参数，以便知道 agent 打算执行什么命令。
4. 作为 Tau Web 用户，我希望展开查看每个工具调用的原始结果文本，以便核对工具实际拿到了什么。
5. 作为 Tau Web 用户，我希望看到 turn 的起止边界，以便区分「多轮工具循环」和「最终回答」两个阶段。
6. 作为 Tau Web 用户，我希望在 trace 中看到工具授权请求及其后续执行状态，以便确认我批准的就是实际执行的那次调用。
7. 作为 Tau Web 用户，我希望失败的 run 在时间线上有明确的错误终点（含 provider 错误信息），以便快速定位失败原因。
8. 作为 Tau Web 用户，我希望取消的 run 在时间线上标记为 cancelled 而非 completed，以便区分主动停止与正常完成。
9. 作为 Tau Web 用户，我希望 run 摘要行显示轮次数、耗时与完成状态，以便一眼判断该 run 的健康度。
10. 作为 Tau Web 用户，我希望点击 trace 中的任意事件可以复制其原始 JSON payload，以便报告问题时提供精确数据。
11. 作为 Tau Web 用户，我希望在运行进行中就能实时看到事件追加（而非结束后一次性出现），以便观察长任务的进展。
12. 作为 Tau Web 用户，我希望页面断线重连后仍能看到本次 run 已发生的事件摘要，以便不因刷新丢失全部上下文。
13. 作为 Tau Web 用户，我希望 steering / follow-up 入队时出现在对应 run 的轨迹中，以便知道追加指令何时被消费。
14. 作为 Tau Web 用户，我希望切换会话时 trace 时间线只显示当前会话的 run，以便不被其他会话的事件干扰。
15. 作为 Tau Web 用户，我希望长 run（几十个事件）下时间线保持流畅（虚拟化或折叠策略），以便面板不卡顿。
16. 作为 Tau Web 用户，我希望工具参数/结果默认折叠以控制视觉噪音，但我可以一键展开全部细节，以便兼顾概览与深查。
17. 作为开发者，我希望新增的 trace 事件是既有 `AgentEvent` 的投影而非核心层新事件类型，以便不影响 TUI/print 模式与核心架构。
18. 作为开发者，我希望 SSE 契约向后兼容（旧事件类型不变、只新增），以便旧版前端页面不会因后端升级而崩溃。
19. 作为开发者，我希望前端渲染逻辑有纯函数级的测试接缝，以便时间线组装规则可以被自动化验证。
20. 作为维护者，我希望本功能的行为变化同步更新到发布文档（Use Tau / Web 指南），以便文档与实现一致。

## Implementation Decisions（实现决策）

1. **不新增核心事件类型**。trace 所需信息已由 portable 层的 agent 事件携带（message 边界、
   toolcall delta、工具执行参数与结果）；Web 后端在转发层做投影，符合
   `AgentHarness = 大脑 / 宿主决定展示策略` 的分层原则。
2. **runId 分组**：Web 后端在受理一次 prompt 时生成 runId；该 run 期间发布的所有 trace
   事件都携带同一 runId；`run_finished`/`agent_settled` 为该 run 的收尾事件。
3. **SSE 契约增量演进**：现有事件类型（`tool_authorization_requested`、`queue_update` 等）
   payload 不变；新增 message 边界事件、工具执行明细事件、run 生命周期摘要事件。所有新
   事件带 `type`、`runId`、`timestamp`、`toolCallId`（工具类）字段。
4. **文本 delta 不进 trace**：为控制带宽与噪音，仅发布 `text_start`/`text_end` 边界；
   正文渲染职责留在 transcript 面板。工具调用参数与结果是 trace 的一等公民，完整下发。
5. **授权关联**：`tool_authorization_requested` 通过 `toolCallId` 与工具执行事件配对，
   前端在同一节点上呈现「待确认 → 批准/拒绝 → 执行中 → 完成」状态机。
6. **重连语义**：SSE 重连后补发当前活动 run 的摘要（已完成事件计数 + 状态），不回放全量
   历史事件；完整历史仍以会话 JSONL 为准。
7. **前端接缝**：把「事件列表 → 时间线视图模型」的组装逻辑抽成无 DOM 依赖的纯函数模块，
   渲染组件只消费视图模型；为此引入轻量 JS 测试设施（Node 内置 `node:test`，不引入打包器）。
8. **文档义务**：更新 Use Tau 的 Web 指南与 `docs/tau-web-todo.md` 勾除对应条目。

## Testing Decisions（测试决策）

好的测试只验证外部行为：SSE 帧的实际内容与前端视图模型的输出，不测内部调用图。

1. **后端（主接缝）**：沿用现有 web 测试模式——用假 session 构造脚本化的 agent 事件流，
   断言 SSE 输出包含：新事件的 type/runId/timestamp 字段完整性、工具事件的参数与结果
   原文、授权请求与执行的 toolCallId 配对、cancelled/error run 的收尾事件、旧事件类型
   payload 未变（兼容性回归）。
2. **前端（新增接缝）**：对抽出的纯函数做表驱动测试：给定事件序列，断言时间线视图模型
   的分组、排序、折叠状态与复制 payload 结构；覆盖跨 turn、多工具、失败/取消等序列。
3. **手动验收清单**：真实模型下的实时性（事件边跑边出）、长 run 流畅度、断线重连摘要、
   中文界面文案。前端交互行为不做浏览器自动化。

## Out of Scope（不在范围内）

- 服务端 trace 历史持久化（trace 是实时视图，持久真相仍是会话 JSONL）
- TUI / print 模式的 trace 能力（独立需求，走扩展方案）
- provider 层原始 HTTP 请求/响应日志
- 远程（非 loopback）托管与鉴权
- 核心 `tau_agent` 事件 schema 变更
- 历史会话的追溯性 trace 回放

## Further Notes

- 术语遵循仓库既有词汇：run / turn / trace / Trace Workbench / coding session。
- 实现时按 AGENTS.md 纪律：先测试后扩展、原子提交、更新 `website/content/` 用户文档。

## Persistence Extension (2026-08-24)

本文档最初将「服务端 trace 历史持久化」列为 Out of Scope。该决定已被
`dev-notes/web-trace-persistence-design.md` 修订并实现：

- 服务端按会话维护有界事件环缓冲（约 600 条），订阅时以 `replay` 帧重放，
  页面刷新即可恢复最近轨迹；
- 事件镜像落盘至会话目录 `<session-id>.webtrace.jsonl`（超限原子压缩），
  tau-web 重启后仍可回看；
- 工具授权决定通过 `tool_authorization_resolved` 广播闭环；
- 右侧面板合并为单一时间线树（run 分组 + 「会话事件」分组）。

持久真相仍是会话 JSONL；webtrace 是 Web 宿主的展示投影。
