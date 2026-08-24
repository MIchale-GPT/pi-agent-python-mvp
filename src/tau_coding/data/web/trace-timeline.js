"use strict";

// Pure trace-timeline view-model builder for the Tau Web Trace Workbench.
//
// Consumes the raw SSE event stream (see docs/PRD-web-trace.md) and produces a
// run-grouped timeline. No DOM access: this module is unit tested with
// `node --test tests/web/` and loaded as a classic browser script.

const MAX_TITLE_LENGTH = 60;
const SESSION_RUN_ID = "__session__";
const AUTHORIZATION_STATUS_BY_DECISION = {
  allow: "allowed",
  deny: "denied",
  cancel: "cancelled",
  timeout: "cancelled",
  no_subscriber: "denied",
};

function textFromContent(content) {
  if (typeof content === "string") {
    return content;
  }
  if (Array.isArray(content)) {
    return content
      .filter((block) => block && block.type === "text" && typeof block.text === "string")
      .map((block) => block.text)
      .join("");
  }
  return "";
}

function excerpt(text, maxLength = MAX_TITLE_LENGTH) {
  const flat = String(text ?? "").replace(/\s+/g, " ").trim();
  if (flat.length <= maxLength) {
    return flat;
  }
  return `${flat.slice(0, Math.max(0, maxLength - 1))}…`;
}

function createRun(runId, isSession = false) {
  return {
    runId,
    isSession,
    title: null,
    status: "running",
    turnCount: null,
    eventCount: null,
    durationMs: null,
    elapsedMs: null,
    items: [],
    toolsByCallId: new Map(),
    lastTurn: 0,
  };
}

function applyRunCounts(run, event, keys) {
  for (const target of keys) {
    const value = event[target];
    if (typeof value === "number") {
      run[target] = value;
    }
  }
}

function ensureToolItem(run, toolCallId, toolName, args) {
  let item = run.toolsByCallId.get(toolCallId);
  if (item === undefined) {
    item = {
      kind: "tool",
      toolCallId,
      toolName,
      args: args ?? {},
      state: "running",
      isError: false,
      resultText: null,
      authorization: null,
      rawJson: "",
      timestamp: null,
      turn: run.lastTurn,
    };
    run.toolsByCallId.set(toolCallId, item);
    run.items.push(item);
  }
  return item;
}

function rawJson(event) {
  try {
    return JSON.stringify(event);
  } catch {
    return "{}";
  }
}

function applyRunEvent(run, event, decisions) {
  switch (event.type) {
    case "message_start":
    case "message_end": {
      const message = event.message || {};
      const role = message.role;
      const text = textFromContent(message.content);
      if (role === "user") {
        if (run.title === null && event.type === "message_start") {
          run.title = excerpt(text);
          run.items.push({
            kind: "user",
            text,
            rawJson: rawJson(event),
            timestamp: event.timestamp,
            turn: 0,
          });
        }
        return;
      }
      if (event.type !== "message_end") {
        return;
      }
      const hasToolCalls = Array.isArray(message.content)
        ? message.content.some((block) => block && block.type === "toolCall")
        : false;
      if (!text && hasToolCalls) {
        return;
      }
      run.items.push({
        kind: "assistant",
        text: excerpt(text),
        rawJson: rawJson(event),
        timestamp: event.timestamp ?? null,
        turn: run.lastTurn,
      });
      return;
    }
    case "message_update":
      // Streaming deltas are transcript concerns; the timeline keeps boundaries only.
      return;
    case "tool_authorization_requested": {
      const item = ensureToolItem(run, event.toolCallId, event.toolName, event.arguments);
      const decision = decisions?.[event.requestId];
      const status = decision ?? "requested";
      item.authorization = {
        requestId: event.requestId,
        rawJson: rawJson(event),
        status,
      };
      if ((status === "denied" || status === "cancelled") && item.state === "running") {
        item.state = status;
      }
      return;
    }
    case "tool_execution_start": {
      const item = ensureToolItem(run, event.toolCallId, event.toolName, event.args);
      item.state = "running";
      item.rawJson = rawJson(event);
      item.timestamp = event.timestamp ?? null;
      return;
    }
    case "tool_execution_update": {
      const item = ensureToolItem(run, event.toolCallId, event.toolName, event.args);
      item.rawJson = rawJson(event);
      return;
    }
    case "tool_execution_end": {
      const item = ensureToolItem(run, event.toolCallId, event.toolName, {});
      item.state = "done";
      item.isError = Boolean(event.isError);
      const output = event.result?.output;
      item.resultText = output === undefined || output === null ? "" : String(output);
      item.rawJson = rawJson(event);
      return;
    }
    case "tool_authorization_resolved": {
      const item = run.toolsByCallId.get(event.toolCallId);
      if (item === undefined || !item.authorization) {
        return;
      }
      const status =
        AUTHORIZATION_STATUS_BY_DECISION[event.decision] ?? String(event.decision ?? "");
      item.authorization.status = status;
      if ((status === "denied" || status === "cancelled") && item.state === "running") {
        item.state = status === "denied" ? "denied" : "cancelled";
      }
      return;
    }
    case "turn_start":
    case "turn_end": {
      if (event.type === "turn_start") {
        run.lastTurn += 1;
      }
      return;
    }
    case "run_error": {
      run.items.push({
        kind: "error",
        message: event.message ?? "",
        rawJson: rawJson(event),
        timestamp: event.timestamp,
        turn: run.lastTurn,
      });
      return;
    }
    case "run_finished": {
      run.status = event.status ?? "completed";
      applyRunCounts(run, event, ["turnCount", "eventCount"]);
      if (typeof event.durationMs === "number") {
        run.durationMs = event.durationMs;
      }
      return;
    }
    case "run_summary": {
      run.status = event.status ?? run.status;
      applyRunCounts(run, event, ["turnCount", "eventCount", "elapsedMs"]);
      return;
    }
    default:
      run.items.push({
        kind: "event",
        type: event.type,
        detail: excerpt(event.toolName || event.message || ""),
        rawJson: rawJson(event),
        timestamp: event.timestamp ?? null,
        turn: run.lastTurn,
      });
  }
}

function toPublicRun(run) {
  return {
    runId: run.runId,
    title: run.isSession ? "会话事件" : (run.title ?? "(无用户输入)"),
    status: run.status,
    turnCount: run.turnCount,
    eventCount: run.eventCount,
    durationMs: run.durationMs,
    elapsedMs: run.elapsedMs,
    items: run.items,
  };
}

function buildTraceTimeline(events, options = {}) {
  const maxRuns = options.maxRuns ?? 8;
  const authorizationDecisions = options.authorizationDecisions ?? {};
  const runsById = new Map();
  const runOrderIds = [];
  let sessionSeen = false;

  for (const event of events) {
    const runId = event?.runId;
    if (event.type === "run_started") {
      if (!runsById.has(runId)) {
        runsById.set(runId, createRun(runId));
        runOrderIds.push(runId);
      }
      continue;
    }
    if (typeof runId === "string" && runsById.has(runId)) {
      applyRunEvent(runsById.get(runId), event, authorizationDecisions);
      continue;
    }
    if (
      typeof runId === "string" &&
      ["message_start", "tool_execution_start", "tool_execution_end", "run_error", "run_summary"].includes(
        event.type,
      )
    ) {
      // Events observed before run_started (e.g. after reconnect): open a run lazily.
      runsById.set(runId, createRun(runId));
      runOrderIds.push(runId);
      applyRunEvent(runsById.get(runId), event, authorizationDecisions);
      continue;
    }
    // Events outside any run (connection/command/config chatter) land in a
    // synthetic session bucket that never consumes a maxRuns slot.
    if (!runsById.has(SESSION_RUN_ID)) {
      runsById.set(SESSION_RUN_ID, createRun(SESSION_RUN_ID, true));
      sessionSeen = true;
    }
    const sessionRun = runsById.get(SESSION_RUN_ID);
    sessionRun.items.push({
      kind: "session",
      type: event.type,
      detail: excerpt(event.toolName || event.message || ""),
      rawJson: rawJson(event),
      timestamp: event.timestamp ?? null,
      turn: 0,
    });
  }

  const recentRunIds = [
    ...runOrderIds.slice(-maxRuns),
    ...(sessionSeen ? [SESSION_RUN_ID] : []),
  ];
  return {
    runs: recentRunIds.map((runId) => toPublicRun(runsById.get(runId))),
    ungrouped: [],
  };
}

const TraceTimeline = { buildTraceTimeline };

if (typeof module !== "undefined" && module.exports) {
  module.exports = { buildTraceTimeline };
}
if (typeof window !== "undefined") {
  window.TraceTimeline = TraceTimeline;
}
