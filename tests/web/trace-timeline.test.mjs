import test from "node:test";
import assert from "node:assert/strict";

import { buildTraceTimeline } from "../../src/tau_coding/data/web/trace-timeline.js";

const RUN_A = "run-a";
const RUN_B = "run-b";

function userStart(text) {
  return { type: "message_start", runId: RUN_A, message: { role: "user", content: text } };
}

function toolStart(toolCallId, name, args) {
  return {
    type: "tool_execution_start",
    runId: RUN_A,
    toolCallId,
    toolName: name,
    args,
  };
}

function toolEnd(toolCallId, name, result, isError = false) {
  return {
    type: "tool_execution_end",
    runId: RUN_A,
    toolCallId,
    toolName: name,
    result: { output: result },
    isError,
  };
}

function runFinished(overrides = {}) {
  return {
    type: "run_finished",
    runId: RUN_A,
    status: "completed",
    turnCount: 2,
    eventCount: 9,
    durationMs: 4200,
    ...overrides,
  };
}

test("groups events by runId, titles runs from the first user prompt, and caps history", () => {
  const events = [
    { type: "web_connected", running: false },
    { type: "run_started", runId: RUN_A },
    userStart("上海天气如何"),
    { type: "message_end", runId: RUN_A, message: { role: "assistant", content: "好的" } },
    runFinished(),
    { type: "run_started", runId: RUN_B },
    {
      type: "message_start",
      runId: RUN_B,
      message: { role: "user", content: "第二问" },
    },
  ];
  const timeline = buildTraceTimeline(events, { maxRuns: 1 });

  assert.equal(timeline.runs.length, 1);
  assert.equal(timeline.runs[0].runId, RUN_B);
  assert.equal(timeline.runs[0].title, "第二问");
  assert.equal(timeline.runs[0].status, "running");
  assert.equal(timeline.ungrouped.length, 1);
  assert.equal(timeline.ungrouped[0].type, "web_connected");

  const full = buildTraceTimeline(events);
  assert.deepEqual(
    full.runs.map((run) => run.runId),
    [RUN_A, RUN_B],
  );
});

test("merges authorization and execution events into one tool item lifecycle", () => {
  const events = [
    { type: "run_started", runId: RUN_A },
    userStart("写文件"),
    {
      type: "tool_authorization_requested",
      runId: RUN_A,
      requestId: "auth-1",
      toolCallId: "call-1",
      toolName: "write_file",
      arguments: { path: "notes.md" },
    },
    toolStart("call-1", "write_file", { path: "notes.md" }),
    toolEnd("call-1", "write_file", "wrote 12 bytes"),
    runFinished({ eventCount: 5 }),
  ];

  const { runs } = buildTraceTimeline(events);
  const tools = runs[0].items.filter((item) => item.kind === "tool");
  assert.equal(tools.length, 1);

  const tool = tools[0];
  assert.equal(tool.toolName, "write_file");
  assert.equal(tool.toolCallId, "call-1");
  assert.deepEqual(tool.args, { path: "notes.md" });
  assert.equal(tool.state, "done");
  assert.equal(tool.isError, false);
  assert.equal(tool.resultText, "wrote 12 bytes");
  assert.equal(tool.authorization.requestId, "auth-1");
  assert.equal(tool.authorization.status, "requested");
});

test("marks failed tool results as errors and keeps running tools unfinished", () => {
  const events = [
    { type: "run_started", runId: RUN_A },
    toolStart("call-1", "bash", { command: "ls" }),
    toolEnd("call-1", "bash", "permission denied", true),
    toolStart("call-2", "bash", { command: "pwd" }),
  ];

  const { runs } = buildTraceTimeline(events);
  const tools = runs[0].items.filter((item) => item.kind === "tool");
  assert.equal(tools.length, 2);
  assert.equal(tools[0].state, "done");
  assert.equal(tools[0].isError, true);
  assert.equal(tools[1].state, "running");
  assert.equal(tools[1].resultText, null);
});

test("ignores streaming deltas but numbers turns and keeps assistant boundaries", () => {
  const events = [
    { type: "run_started", runId: RUN_A },
    userStart("第一轮"),
    { type: "turn_start", runId: RUN_A },
    {
      type: "message_update",
      runId: RUN_A,
      assistantMessageEvent: { type: "text_delta", delta: "流式片段" },
    },
    {
      type: "message_end",
      runId: RUN_A,
      message: { role: "assistant", content: [{ type: "text", text: "回答一" }] },
    },
    { type: "turn_end", runId: RUN_A },
    { type: "turn_start", runId: RUN_A },
    {
      type: "message_end",
      runId: RUN_A,
      message: { role: "assistant", content: [{ type: "text", text: "最终回答" }] },
    },
    { type: "turn_end", runId: RUN_A },
    runFinished({ turnCount: 2 }),
  ];

  const { runs } = buildTraceTimeline(events);
  const run = runs[0];
  const kinds = run.items.map((item) => item.kind);
  assert.ok(!kinds.includes("delta"));
  assert.deepEqual(kinds, ["user", "assistant", "assistant"]);
  assert.equal(run.items[0].turn, 0);
  assert.equal(run.items[1].turn, 1);
  assert.equal(run.items[1].text, "回答一");
  assert.equal(run.items[2].turn, 2);
  assert.equal(run.turnCount, 2);
});

test("records errors and cancellations on the run", () => {
  const failed = buildTraceTimeline([
    { type: "run_started", runId: RUN_A },
    { type: "run_error", runId: RUN_A, message: "provider unavailable" },
    runFinished({ status: "failed" }),
  ]).runs[0];
  assert.equal(failed.status, "failed");
  const errorItem = failed.items.find((item) => item.kind === "error");
  assert.equal(errorItem.message, "provider unavailable");

  const cancelled = buildTraceTimeline([
    { type: "run_started", runId: RUN_A },
    { type: "cancel_requested", runId: RUN_A },
    runFinished({ status: "cancelled" }),
  ]).runs[0];
  assert.equal(cancelled.status, "cancelled");
});

test("attaches raw payload JSON to items for copying", () => {
  const authEvent = {
    type: "tool_authorization_requested",
    runId: RUN_A,
    requestId: "auth-1",
    toolCallId: "call-1",
    toolName: "bash",
    arguments: { command: "ls" },
  };
  const { runs } = buildTraceTimeline([
    { type: "run_started", runId: RUN_A },
    userStart("复制我"),
    authEvent,
    toolStart("call-1", "bash", { command: "ls" }),
    toolEnd("call-1", "bash", "done"),
  ]);

  const tool = runs[0].items.find((item) => item.kind === "tool");
  assert.equal(JSON.parse(tool.rawJson).type, "tool_execution_end");
  const user = runs[0].items.find((item) => item.kind === "user");
  assert.equal(JSON.parse(user.rawJson).type, "message_start");
});

test("stamps recorded authorization decisions onto tool items", () => {
  const events = [
    { type: "run_started", runId: RUN_A },
    {
      type: "tool_authorization_requested",
      runId: RUN_A,
      requestId: "auth-1",
      toolCallId: "call-1",
      toolName: "bash",
      arguments: {},
    },
    toolStart("call-1", "bash", {}),
    toolEnd("call-1", "bash", "ok"),
  ];
  const undecided = buildTraceTimeline(events).runs[0].items.find(
    (item) => item.kind === "tool",
  );
  assert.equal(undecided.authorization.status, "requested");

  const decided = buildTraceTimeline(events, {
    authorizationDecisions: { "auth-1": "allowed" },
  }).runs[0].items.find((item) => item.kind === "tool");
  assert.equal(decided.authorization.status, "allowed");
});

test("a denied authorization ends the tool item without execution", () => {
  const events = [
    { type: "run_started", runId: RUN_A },
    {
      type: "tool_authorization_requested",
      runId: RUN_A,
      requestId: "auth-1",
      toolCallId: "call-1",
      toolName: "bash",
      arguments: {},
    },
  ];
  const run = (decisions) =>
    buildTraceTimeline(events, {
      authorizationDecisions: decisions,
    }).runs[0].items.find((item) => item.kind === "tool");

  const pending = run({});
  assert.equal(pending.state, "running");
  assert.equal(pending.authorization.status, "requested");

  const denied = run({ "auth-1": "denied" });
  assert.equal(denied.state, "denied");
  assert.equal(denied.authorization.status, "denied");

  const cancelled = run({ "auth-1": "cancelled" });
  assert.equal(cancelled.state, "cancelled");

  const allowed = buildTraceTimeline(
    events.concat([toolStart("call-1", "bash", {}), toolEnd("call-1", "bash", "ok")]),
    { authorizationDecisions: { "auth-1": "allowed" } },
  ).runs[0].items.find((item) => item.kind === "tool");
  assert.equal(allowed.state, "done");
});

test("merges a reconnect run_summary into the active run", () => {
  const events = [
    { type: "run_started", runId: RUN_A },
    userStart("长任务"),
    toolStart("call-1", "bash", { command: "sleep 30" }),
    {
      type: "run_summary",
      runId: RUN_A,
      status: "running",
      eventCount: 7,
      turnCount: 1,
      elapsedMs: 9000,
    },
  ];

  const { runs } = buildTraceTimeline(events);
  assert.equal(runs[0].status, "running");
  assert.equal(runs[0].eventCount, 7);
  assert.equal(runs[0].turnCount, 1);
  assert.equal(runs[0].elapsedMs, 9000);
});
