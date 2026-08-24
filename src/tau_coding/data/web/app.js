const state = {
  sessions: [],
  activeSessionId: null,
  eventSource: null,
  eventStreamConnected: false,
  running: false,
  submitting: false,
  activeRunId: null,
  liveMessage: null,
  sessionOptions: null,
  configuration: null,
  queue: { steering: [], followUp: [] },
  pendingToolAuthorization: null,
  settledRunIds: new Set(),
  traceEvents: [],
  traceAuthorizationDecisions: {},
  traceExpandAll: false,
  traceOpenToolIds: new Set(),
};
const {
  configureAndCreateSession,
  resolveTemperature,
  shouldActivateAcceptedRun,
} = globalThis.TauSessionActions;

const shell = document.querySelector(".shell");
const sessionList = document.querySelector("#session-list");
const transcript = document.querySelector("#transcript");
const transcriptContent = document.querySelector("#transcript-content");
const reloadButton = document.querySelector(".reload-button");
const toast = document.querySelector(".toast");
const composer = document.querySelector("#composer");
const promptInput = document.querySelector("#prompt-input");
const sendButton = document.querySelector("#send-button");
const cancelButton = document.querySelector("#cancel-button");
const steerButton = document.querySelector("#steer-button");
const followUpButton = document.querySelector("#follow-up-button");
const queuePanel = document.querySelector("#queue-panel");
const steeringQueue = document.querySelector("#steering-queue");
const followUpQueue = document.querySelector("#follow-up-queue");
const clearQueueButton = document.querySelector("#clear-queue-button");
const composerState = document.querySelector("#composer-state");
const sessionMenu = document.querySelector(".session-menu");
const sessionSettingsButton = document.querySelector("#session-settings-button");
const renameSessionButton = document.querySelector("#rename-session-button");
const deleteSessionButton = document.querySelector("#delete-session-button");
const exportHtmlLink = document.querySelector("#export-html-link");
const exportJsonlLink = document.querySelector("#export-jsonl-link");
const newSessionDialog = document.querySelector("#new-session-dialog");
const newSessionForm = document.querySelector("#new-session-form");
const newSessionCwd = document.querySelector("#new-session-cwd");
const newSessionProvider = document.querySelector("#new-session-provider");
const newSessionProviderUrl = document.querySelector("#new-session-provider-url");
const newSessionApiKey = document.querySelector("#new-session-api-key");
const newSessionApiKeyHelp = document.querySelector("#new-session-api-key-help");
const newSessionModel = document.querySelector("#new-session-model");
const newSessionThinking = document.querySelector("#new-session-thinking");
const newSessionThinkingHelp = document.querySelector("#new-session-thinking-help");
const newSessionTemperatureMode = document.querySelector("#new-session-temperature-mode");
const newSessionTemperatureField = document.querySelector("#new-session-temperature-field");
const newSessionTemperature = document.querySelector("#new-session-temperature");
const newSessionTemperatureHelp = document.querySelector("#new-session-temperature-help");
const createSessionSubmit = document.querySelector("#create-session-submit");
const renameSessionDialog = document.querySelector("#rename-session-dialog");
const renameSessionForm = document.querySelector("#rename-session-form");
const renameSessionTitle = document.querySelector("#rename-session-title");
const renameSessionSubmit = document.querySelector("#rename-session-submit");
const deleteSessionDialog = document.querySelector("#delete-session-dialog");
const deleteSessionForm = document.querySelector("#delete-session-form");
const deleteSessionConfirmation = document.querySelector("#delete-session-confirmation");
const deleteSessionSubmit = document.querySelector("#delete-session-submit");
const sessionSettingsDialog = document.querySelector("#session-settings-dialog");
const sessionSettingsForm = document.querySelector("#session-settings-form");
const sessionProvider = document.querySelector("#session-provider");
const sessionModel = document.querySelector("#session-model");
const sessionThinking = document.querySelector("#session-thinking");
const sessionThinkingHelp = document.querySelector("#session-thinking-help");
const sessionSettingsSubmit = document.querySelector("#session-settings-submit");
const toolAuthorizationDialog = document.querySelector("#tool-authorization-dialog");
const toolAuthorizationName = document.querySelector("#tool-authorization-name");
const toolAuthorizationArguments = document.querySelector(
  "#tool-authorization-arguments",
);
let toastTimer;

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) {
    if (typeof text === "string" || typeof text === "number") {
      node.textContent = text;
    } else {
      console.warn("[tau-web] element() received non-primitive text", tag, className, text);
    }
  }
  return node;
}

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}

function showToast(message) {
  window.clearTimeout(toastTimer);
  toast.textContent = message;
  toast.classList.add("is-visible");
  toastTimer = window.setTimeout(() => toast.classList.remove("is-visible"), 1800);
}

async function getJson(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || `HTTP ${response.status}`);
  }
  return payload;
}

async function commandJson(path, body, method = "POST") {
  const response = await fetch(path, {
    method,
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      "X-Tau-Web": "1",
    },
    body: JSON.stringify(body),
  });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.message || payload.error || `HTTP ${response.status}`);
  }
  return payload;
}

async function postJson(path, body) {
  return commandJson(path, body);
}

async function deleteJson(path, body) {
  return commandJson(path, body, "DELETE");
}

function basename(path) {
  const normalized = path.replaceAll("\\", "/").replace(/\/+$/, "");
  return normalized.split("/").pop() || path;
}

function formatRelative(timestampSeconds) {
  const delta = Math.max(0, Date.now() - timestampSeconds * 1000);
  const minutes = Math.floor(delta / 60_000);
  if (minutes < 1) return "现在";
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h`;
  const days = Math.floor(hours / 24);
  return `${days}d`;
}

function formatDate(timestamp) {
  if (!timestamp) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(timestamp));
}

function sessionSubtitle(session) {
  const provider = session.providerName || "provider";
  return `${provider} · ${session.model}`;
}

function setProjectSummary(session) {
  if (!session) return;
  document.querySelector("#project-name").textContent = basename(session.cwd);
  document.querySelector("#project-path").textContent = session.cwd;
}

function activeSession() {
  return state.sessions.find((session) => session.id === state.activeSessionId) || null;
}

function setSessionActions() {
  const session = activeSession();
  sessionSettingsButton.disabled =
    !session || !state.configuration || state.running;
  renameSessionButton.disabled = !session;
  deleteSessionButton.disabled = !session;
  [
    [exportHtmlLink, "html"],
    [exportJsonlLink, "jsonl"],
  ].forEach(([link, format]) => {
    if (session) {
      link.href = `/api/sessions/${encodeURIComponent(session.id)}/export?format=${format}`;
      link.setAttribute("download", "");
      link.setAttribute("aria-disabled", "false");
    } else {
      link.removeAttribute("href");
      link.removeAttribute("download");
      link.setAttribute("aria-disabled", "true");
    }
  });
}

function renderNoSessionSelected() {
  document.querySelector("#session-title").textContent = "Trace Workbench";
  document.querySelector("#session-cwd").textContent = "新建一个会话以开始工作";
  document.querySelector("#transcript-updated").textContent = "LOCAL";
  document.querySelector("#active-session-state").textContent = "NONE";
  document.querySelector("#fact-provider").textContent = "—";
  document.querySelector("#fact-model").textContent = "—";
  document.querySelector("#fact-thinking").textContent = "—";
  document.querySelector("#fact-temperature").textContent = "—";
  document.querySelector("#fact-messages").textContent = "—";
  document.querySelector("#fact-updated").textContent = "—";
  transcriptContent.replaceChildren();
  const empty = element("div", "empty-transcript");
  empty.append(
    element("strong", null, "还没有打开的会话"),
    element("p", null, "使用左侧“新建会话”选择项目、Provider 和模型。"),
  );
  transcriptContent.append(empty);
  state.configuration = null;
  state.queue = { steering: [], followUp: [] };
  renderQueue();
  setSessionActions();
}

function renderSessionList() {
  sessionList.replaceChildren();
  document.querySelector("#session-count").textContent = String(state.sessions.length);

  if (!state.sessions.length) {
    const empty = element("div", "empty-sessions");
    empty.append(
      element("strong", null, "还没有本地会话"),
      element("p", null, "点击上方“新建会话”，选择项目、Provider 和模型。"),
    );
    sessionList.append(empty);
    return;
  }

  state.sessions.forEach((session) => {
    const button = element("button", "session-button");
    button.type = "button";
    button.dataset.sessionId = session.id;
    button.classList.toggle("is-active", session.id === state.activeSessionId);
    button.classList.toggle(
      "is-running",
      session.id === state.activeSessionId && state.running,
    );

    const status = element("span", "session-state");
    const copy = element("span", "session-copy");
    copy.append(
      element("strong", null, session.title || "Untitled session"),
      element("small", null, sessionSubtitle(session)),
    );
    const time = element("time", null, formatRelative(session.updatedAt));
    button.append(status, copy, time);
    button.addEventListener("click", () => loadSession(session.id));
    sessionList.append(button);
  });
}

function renderSessionError(message) {
  sessionList.replaceChildren();
  const error = element("div", "load-error");
  error.append(element("strong", null, "无法读取会话"), element("p", null, message));
  const retry = element("button", null, "重试");
  retry.type = "button";
  retry.addEventListener("click", loadSessions);
  error.append(retry);
  sessionList.append(error);
}

function renderTranscriptLoading() {
  transcriptContent.replaceChildren();
  const loading = element("div", "transcript-loading");
  loading.append(element("span", null, "正在读取活动分支"));
  transcriptContent.append(loading);
}

function rolePresentation(message) {
  if (message.role === "user") {
    return { className: "message-user", label: "你", avatarClass: "avatar-user", avatar: "M" };
  }
  if (message.role === "assistant") {
    return {
      className: "message-assistant",
      label: "Tau",
      avatarClass: "avatar-assistant",
      avatar: "τ",
    };
  }
  return {
    className: "message-tool",
    label: message.toolName || message.role,
    avatarClass: "avatar-tool",
    avatar: null,
  };
}

function wireContentText(content) {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .filter((item) => item?.type === "text")
    .map((item) => item.text || "")
    .join("");
}

function wireThinkingText(content) {
  if (!Array.isArray(content)) return "";
  return content
    .filter((item) => item?.type === "thinking")
    .map((item) => item.thinking || "")
    .join("");
}

function normalizeWireMessage(message) {
  return {
    role: message.role,
    text: wireContentText(message.content) || message.errorMessage || "",
    timestamp: message.timestamp,
    model: message.model,
    provider: message.provider,
    thinking: wireThinkingText(message.content),
    toolCalls: Array.isArray(message.content)
      ? message.content
          .filter((item) => item?.type === "toolCall")
          .map((item) => ({
            id: item.id,
            name: item.name,
            arguments: item.arguments || {},
          }))
      : [],
    toolName: message.toolName,
    toolCallId: message.toolCallId,
    isError: message.isError,
    errorMessage: message.errorMessage,
    stopReason: message.stopReason,
  };
}

function renderMessage(message) {
  if (message.role === "compaction" || message.role === "branchSummary") {
    const summary = element("article", "summary-message");
    const label = message.role === "compaction" ? "COMPACTION" : "BRANCH SUMMARY";
    summary.textContent = `${label}\n${message.text}`;
    return summary;
  }

  if (message.role === "toolResult") {
    const result = element(
      "article",
      `message message-tool-result${message.isError ? " is-error" : ""}`,
    );
    const body = element("div", "message-body", message.text || "(empty tool result)");
    result.append(body);
    return result;
  }

  const presentation = rolePresentation(message);
  const failed = message.stopReason === "error" || Boolean(message.errorMessage);
  const article = element(
    "article",
    `message ${presentation.className}${failed ? " is-error" : ""}`,
  );
  const header = element("header");
  const avatar = element("span", `avatar ${presentation.avatarClass}`);
  if (presentation.avatar) {
    avatar.textContent = presentation.avatar;
  } else {
    avatar.append(icon("tool"));
  }
  header.append(avatar, element("strong", null, presentation.label));
  if (message.model) {
    header.append(element("span", "model-tag", message.model));
  }
  header.append(element("time", null, formatDate(message.timestamp)));
  article.append(header);

  const body = element(
    "div",
    "message-body",
    message.text || message.errorMessage || "(empty message)",
  );
  article.append(body);

  if (message.thinking) {
    const thinking = element("details", "thinking-block");
    thinking.append(
      element("summary", null, "查看保存的 thinking"),
      element("p", null, message.thinking),
    );
    article.append(thinking);
  }

  if (message.toolCalls?.length) {
    const tools = element("div", "tool-calls");
    message.toolCalls.forEach((call) => {
      const row = element("div", "tool-call");
      const status = element("span", "tool-status");
      status.append(icon("check"));
      row.append(
        status,
        element("strong", null, call.name),
        element("code", null, JSON.stringify(call.arguments)),
      );
      tools.append(row);
    });
    article.append(tools);
  }
  return article;
}

function appendCommandResult(command, message) {
  transcriptContent.querySelector(".empty-transcript")?.remove();
  const result = element("article", "summary-message command-message");
  result.append(
    element("strong", null, command),
    element("pre", null, message || "Command completed."),
  );
  transcriptContent.append(result);
  scrollTranscriptToBottom();
}

function renderTranscript(payload) {
  transcriptContent.replaceChildren();
  if (!payload.messages.length) {
    const empty = element("div", "empty-transcript");
    empty.append(
      element("strong", null, "这个会话还没有消息"),
      element("p", null, "它已被索引，但 JSONL 活动分支目前为空。"),
    );
    transcriptContent.append(empty);
    return;
  }
  payload.messages.forEach((message) => transcriptContent.append(renderMessage(message)));
}

function setSessionFacts(session, messageCount, configuration = state.configuration) {
  document.querySelector("#active-session-state").textContent = "LOADED";
  document.querySelector("#fact-provider").textContent = session.providerName || "—";
  document.querySelector("#fact-model").textContent = session.model || "—";
  document.querySelector("#fact-thinking").textContent =
    configuration?.thinkingLevel || "off";
  document.querySelector("#fact-temperature").textContent =
    session.temperature === null || session.temperature === undefined
      ? "auto"
      : String(session.temperature);
  document.querySelector("#fact-messages").textContent = String(messageCount);
  document.querySelector("#fact-updated").textContent = formatRelative(session.updatedAt);
}

function markIndexLoaded(count) {
  addTraceEvent("session_index_loaded", `${count} indexed sessions`);
}

function markBranchLoaded(messageCount) {
  addTraceEvent("active_branch_loaded", `${messageCount} visible messages`);
}

function setComposerState() {
  const hasSession = Boolean(state.activeSessionId);
  const ready = hasSession && state.eventStreamConnected && !state.submitting;
  promptInput.disabled = !ready;
  sendButton.disabled = !ready || !promptInput.value.trim();
  sendButton.hidden = state.running;
  steerButton.hidden = !state.running;
  followUpButton.hidden = !state.running;
  steerButton.disabled = !ready || !promptInput.value.trim();
  followUpButton.disabled = !ready || !promptInput.value.trim();
  cancelButton.hidden = !state.running;
  sessionSettingsButton.disabled =
    !hasSession || !state.configuration || state.running;
  if (!hasSession) {
    promptInput.placeholder = "选择一个会话后发送任务…";
    composerState.textContent = "未选择会话";
  } else if (!state.eventStreamConnected) {
    promptInput.placeholder = "正在连接事件流…";
    composerState.textContent = "正在连接事件流";
  } else if (state.submitting) {
    promptInput.placeholder = "正在提交任务…";
    composerState.textContent = "正在提交任务";
  } else if (state.running) {
    promptInput.placeholder = "输入转向消息，或选择“跟进”…";
    composerState.textContent = "Tau 正在运行，可继续排队";
  } else {
    promptInput.placeholder = "发送任务给 Tau…";
    composerState.textContent = "会话已就绪";
  }
  renderQueue();
  renderSessionList();
}

function renderQueue() {
  const steering = state.queue.steering || [];
  const followUp = state.queue.followUp || [];
  const hasQueuedMessages = steering.length + followUp.length > 0;
  queuePanel.hidden = !state.running && !hasQueuedMessages;
  clearQueueButton.disabled = !hasQueuedMessages;
  steeringQueue.replaceChildren(
    ...steering.map((message) => element("li", null, message)),
  );
  followUpQueue.replaceChildren(
    ...followUp.map((message) => element("li", null, message)),
  );
  if (!steering.length) {
    steeringQueue.append(element("li", "queue-empty", "无"));
  }
  if (!followUp.length) {
    followUpQueue.append(element("li", "queue-empty", "无"));
  }
}

function addTraceEvent(type, detail = "") {
  recordTraceEvent({ type, message: typeof detail === "string" ? detail : "" });
  renderRunTimeline();
  document.querySelector("#trace-state").textContent = type;
}

const TRACE_EVENT_BUFFER_LIMIT = 600;
const RUN_STATUS_LABELS = {
  running: "运行中",
  completed: "已完成",
  cancelled: "已取消",
  failed: "失败",
};
const TOOL_STATE_LABELS = {
  running: "运行中…",
  done: "完成",
  denied: "已拒绝（未执行）",
  cancelled: "已取消",
};
const AUTHORIZATION_STATUS_LABELS = {
  requested: "待确认",
  allowed: "已授权",
  denied: "已拒绝",
  cancelled: "已取消",
};

function formatTraceDuration(ms) {
  if (typeof ms !== "number") return "";
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`;
}

async function copyTraceText(label, text) {
  try {
    await navigator.clipboard.writeText(text);
    showToast(`${label}已复制`);
  } catch (error) {
    showToast(`复制失败：${error.message}`);
  }
}

function copyButton(label, getText) {
  const button = element("button", "trace-copy-button", label);
  button.addEventListener("click", () => {
    void copyTraceText(label, getText());
  });
  return button;
}

function toolStateLabel(item) {
  if (item.state === "denied" || item.state === "cancelled") {
    return TOOL_STATE_LABELS[item.state];
  }
  if (item.authorization?.status === "requested" && item.state === "running") {
    return AUTHORIZATION_STATUS_LABELS.requested;
  }
  return TOOL_STATE_LABELS[item.state] ?? item.state;
}

function renderRunTimeline() {
  const container = document.querySelector("#run-timeline");
  if (!container || typeof window.TraceTimeline !== "object") return;
  const { runs } = window.TraceTimeline.buildTraceTimeline(state.traceEvents, {
    maxRuns: 6,
    authorizationDecisions: state.traceAuthorizationDecisions,
  });

  container.replaceChildren();
  for (const run of runs.slice().reverse()) {
    const statusLabel = RUN_STATUS_LABELS[run.status] ?? run.status;
    const metaParts = [statusLabel];
    if (typeof run.turnCount === "number" && run.turnCount > 0) {
      metaParts.push(`${run.turnCount} 轮`);
    }
    const duration = run.durationMs ?? run.elapsedMs;
    const durationText = formatTraceDuration(duration);
    if (durationText) metaParts.push(durationText);

    const head = element("div", "run-head");
    const title = element("strong", null, run.title);
    title.title = run.title;
    head.append(
      element("span", `event-node run-dot is-${run.status}`),
      element("div", "run-head-text", title),
      element("small", "run-meta", metaParts.join(" · ")),
    );

    const items = element("ul", "run-items");
    for (const item of run.items) {
      items.append(renderRunItem(item));
    }
    const group = element(
      "li",
      `run-group is-${run.status}${run.isSession ? " is-session" : ""}`,
    );
    group.append(head);
    if (run.items.length > 0) group.append(items);
    container.append(group);
  }
}

function renderRunItem(item) {
  const row = element("li", `run-item kind-${item.kind}`);
  row.append(element("span", "event-node"));
  if (item.kind === "user") {
    const text = element("div", "run-item-body");
    text.append(element("strong", null, "用户"), element("p", null, item.text));
    row.append(text);
  } else if (item.kind === "assistant") {
    const text = element("div", "run-item-body");
    text.append(element("strong", null, "助手回答"), element("p", null, item.text));
    row.append(text);
  } else if (item.kind === "error") {
    const body = element("div", "run-item-body");
    body.append(element("strong", null, "错误"), element("p", null, item.message));
    row.append(body);
    row.classList.add("is-error");
  } else if (item.kind === "tool") {
    row.classList.add(item.state === "done" ? "is-done" : "is-live");
    if (item.isError) row.classList.add("is-error");
    const summaryLine = element("summary");
    summaryLine.append(
      element("strong", null, `⚙ ${item.toolName}`),
      element("small", null, toolStateLabel(item)),
    );
    const detailsNode = element("details", "tool-details");
    detailsNode.open =
      state.traceExpandAll || state.traceOpenToolIds.has(item.toolCallId);
    detailsNode.addEventListener("toggle", () => {
      if (state.traceExpandAll) return;
      if (detailsNode.open) {
        state.traceOpenToolIds.add(item.toolCallId);
      } else {
        state.traceOpenToolIds.delete(item.toolCallId);
      }
    });
    detailsNode.append(summaryLine);
    const body = element("div", "tool-detail-body");
    const argsJson = JSON.stringify(item.args ?? {}, null, 2);
    body.append(element("pre", null, argsJson));
    if (item.resultText !== null && item.resultText !== undefined && item.resultText !== "") {
      body.append(element("pre", null, item.resultText));
    }
    const actions = element("div", "tool-detail-actions");
    actions.append(copyButton("复制参数", () => argsJson));
    if (item.rawJson) {
      actions.append(copyButton("复制原始事件", () => item.rawJson));
    }
    body.append(actions);
    detailsNode.append(body);
    row.append(detailsNode);
  } else {
    const body = element("div", "run-item-body");
    body.append(
      element("strong", null, item.type),
      element("p", null, item.detail || ""),
    );
    row.append(body);
  }
  if (item.rawJson && item.kind !== "tool") {
    row.append(copyButton("复制", () => item.rawJson));
  }
  return row;
}

function recordTraceEvent(event) {
  state.traceEvents.push(event);
  if (state.traceEvents.length > TRACE_EVENT_BUFFER_LIMIT) {
    state.traceEvents.splice(0, state.traceEvents.length - TRACE_EVENT_BUFFER_LIMIT);
  }
}

function scrollTranscriptToBottom() {
  transcript.scrollTop = transcript.scrollHeight;
}

function beginLiveMessage(message) {
  const normalized = normalizeWireMessage(message);
  const node = renderMessage(normalized);
  node.classList.add("is-live");
  transcriptContent.querySelector(".empty-transcript")?.remove();
  transcriptContent.append(node);
  state.liveMessage = { node, message: normalized };
  scrollTranscriptToBottom();
}

function updateLiveMessage(message, delta = "") {
  const normalized = normalizeWireMessage(message);
  if (!state.liveMessage) {
    beginLiveMessage(message);
  }
  const live = state.liveMessage;
  const body = live.node.querySelector(".message-body");
  if (normalized.text) {
    live.message.text = normalized.text;
  } else if (delta) {
    live.message.text += delta;
  }
  if (normalized.stopReason === "error" || normalized.errorMessage) {
    live.node.classList.add("is-error");
  }
  body.textContent = live.message.text || "…";
  scrollTranscriptToBottom();
}

function finishLiveMessage(message) {
  if (!state.liveMessage) {
    beginLiveMessage(message);
  }
  updateLiveMessage(message);
  state.liveMessage.node.classList.remove("is-live");
  state.liveMessage = null;
}

function updateConfiguration(configuration) {
  state.configuration = configuration;
  const session = activeSession();
  if (session) {
    session.providerName = configuration.providerName;
    session.model = configuration.model;
    setSessionFacts(
      session,
      document.querySelector("#fact-messages").textContent || "0",
      configuration,
    );
    renderSessionList();
  }
}

function showToolAuthorization(event, sessionId) {
  state.pendingToolAuthorization = { ...event, sessionId };
  toolAuthorizationName.textContent = event.toolName;
  toolAuthorizationArguments.textContent = JSON.stringify(event.arguments || {}, null, 2);
  setFormError("#tool-authorization-error");
  toolAuthorizationDialog
    .querySelectorAll("[data-tool-decision]")
    .forEach((button) => (button.disabled = false));
  if (!toolAuthorizationDialog.open) toolAuthorizationDialog.showModal();
  addTraceEvent("tool_authorization_requested", event.toolName);
}

async function handleLiveEvent(event, sessionId) {
  if (sessionId !== state.activeSessionId) return;
  if (event.replay) {
    recordTraceEvent(event);
    if (event.type !== "message_update") {
      renderRunTimeline();
    }
    return;
  }
  recordTraceEvent(event);
  const shouldRerenderTimeline = event.type !== "message_update";
  if (shouldRerenderTimeline) {
    renderRunTimeline();
  }
  if (event.type === "web_connected") {
    const missedFinish = state.running && !event.running;
    state.eventStreamConnected = true;
    state.running = Boolean(event.running);
    setComposerState();
    addTraceEvent("event_stream_connected", event.running ? "run active" : "session idle");
    if (missedFinish) {
      await loadSession(sessionId, { reconnect: false, scrollToBottom: true });
    }
    return;
  }
  if (event.type === "run_started") {
    state.running = true;
    state.activeRunId = event.runId;
    state.liveMessage = null;
    setComposerState();
    addTraceEvent("run_started", event.runId.slice(0, 8));
    return;
  }
  if (event.type === "queue_update") {
    state.queue = {
      steering: event.steering || [],
      followUp: event.followUp || [],
    };
    renderQueue();
    addTraceEvent(
      "queue_update",
      `${state.queue.steering.length} steering · ${state.queue.followUp.length} follow-up`,
    );
  } else if (event.type === "configuration_updated") {
    updateConfiguration(event);
    addTraceEvent("configuration_updated", `${event.providerName} · ${event.model}`);
  } else if (event.type === "tool_authorization_requested") {
    showToolAuthorization(event, sessionId);
  } else if (event.type === "command_result") {
    appendCommandResult(event.command, event.message);
    addTraceEvent("command_result", event.command);
  } else if (event.type === "message_start") {
    beginLiveMessage(event.message);
  } else if (event.type === "message_update") {
    const assistantEvent = event.assistantMessageEvent;
    const textDelta = assistantEvent?.type === "text_delta" ? assistantEvent.delta : "";
    updateLiveMessage(event.message, textDelta);
  } else if (event.type === "message_end") {
    finishLiveMessage(event.message);
  } else if (event.type.startsWith("tool_execution_")) {
    addTraceEvent(event.type, event.toolName || "");
  } else if (event.type === "cancel_requested") {
    addTraceEvent("cancel_requested", event.runId?.slice(0, 8) || "");
  } else if (event.type === "run_error") {
    addTraceEvent("run_error", event.message);
    showToast(`运行失败：${event.message}`);
  } else if (event.type === "stream_resync_required") {
    addTraceEvent("stream_resync_required", "live updates were compacted");
    showToast("实时事件过快，运行结束后将刷新完整记录");
  } else if (event.type === "agent_settled") {
    addTraceEvent("agent_settled", "transcript persisted");
  } else if (event.type === "run_finished") {
    state.settledRunIds.add(event.runId);
    if (state.settledRunIds.size > 32) {
      state.settledRunIds.delete(state.settledRunIds.values().next().value);
    }
    state.running = false;
    state.submitting = true;
    state.activeRunId = null;
    state.liveMessage = null;
    if (state.pendingToolAuthorization) {
      state.pendingToolAuthorization = null;
      toolAuthorizationDialog.close();
    }
    setComposerState();
    addTraceEvent("run_finished", event.status);
    try {
      await loadSession(sessionId, { reconnect: false, scrollToBottom: true });
    } finally {
      state.submitting = false;
      setComposerState();
    }
  }
}

function connectEventStream(sessionId) {
  if (state.pendingToolAuthorization) {
    state.pendingToolAuthorization = null;
    toolAuthorizationDialog.close();
  }
  state.eventSource?.close();
  state.eventSource = null;
  state.eventStreamConnected = false;
  state.running = false;
  state.activeRunId = null;
  state.liveMessage = null;
  state.queue = { steering: [], followUp: [] };
  state.traceEvents = [];
  state.traceAuthorizationDecisions = {};
  state.traceOpenToolIds = new Set();
  setComposerState();
  renderRunTimeline();

  const source = new EventSource(`/api/sessions/${encodeURIComponent(sessionId)}/events`);
  state.eventSource = source;
  source.onmessage = async (message) => {
    if (state.eventSource !== source) return;
    try {
      await handleLiveEvent(JSON.parse(message.data), sessionId);
    } catch (error) {
      showToast(`事件处理失败：${error.message}`);
    }
  };
  source.onerror = () => {
    if (state.eventSource !== source) return;
    state.eventStreamConnected = false;
    if (state.pendingToolAuthorization) {
      state.pendingToolAuthorization = null;
      toolAuthorizationDialog.close();
    }
    setComposerState();
    addTraceEvent("event_stream_reconnecting", "SSE connection lost");
  };
}

async function loadSession(sessionId, options = {}) {
  const { reconnect = true, scrollToBottom = false } = options;
  const session = state.sessions.find((item) => item.id === sessionId);
  if (!session) return;
  state.activeSessionId = sessionId;
  state.configuration = null;
  if (reconnect) connectEventStream(sessionId);
  renderSessionList();
  setSessionActions();
  setProjectSummary(session);
  document.querySelector("#session-title").textContent = session.title || "Untitled session";
  document.querySelector("#session-cwd").textContent = session.cwd;
  document.querySelector("#transcript-updated").textContent = formatRelative(session.updatedAt);
  renderTranscriptLoading();

  const params = new URLSearchParams(window.location.search);
  params.set("session", sessionId);
  window.history.replaceState({}, "", `${window.location.pathname}?${params}`);

  try {
    const payload = await getJson(`/api/sessions/${encodeURIComponent(sessionId)}`);
    if (state.activeSessionId !== sessionId) return;
    renderTranscript(payload);
    state.configuration = payload.configuration;
    const index = state.sessions.findIndex((item) => item.id === sessionId);
    if (index >= 0) state.sessions[index] = payload.session;
    setSessionFacts(payload.session, payload.messages.length, payload.configuration);
    setSessionActions();
    markBranchLoaded(payload.messages.length);
    renderSessionList();
    if (scrollToBottom) {
      scrollTranscriptToBottom();
    } else {
      transcript.scrollTop = 0;
    }
  } catch (error) {
    if (state.activeSessionId !== sessionId) return;
    transcriptContent.replaceChildren();
    const failure = element("div", "empty-transcript");
    failure.append(
      element("strong", null, "无法读取活动分支"),
      element("p", null, error.message),
    );
    transcriptContent.append(failure);
  }
}

async function loadHealth() {
  try {
    const health = await getJson("/api/health");
    document.querySelector("#server-version").textContent = `v${health.version}`;
  } catch {
    document.querySelector("#server-version").textContent = "unavailable";
  }
}

function setFormError(id, message = "") {
  const error = document.querySelector(id);
  error.textContent = message;
  error.hidden = !message;
}

function selectedTemperatureCapability() {
  const provider = state.sessionOptions?.provider;
  const range = provider?.temperatureRange || { min: 0, max: 2, step: "any" };
  return {
    supported: Boolean(
      provider?.temperatureSupported && provider.model === newSessionModel.value.trim(),
    ),
    min: range.min,
    max: range.max,
    step: range.step,
  };
}

function syncTemperatureControls() {
  const capability = selectedTemperatureCapability();
  if (!capability.supported) newSessionTemperatureMode.value = "auto";
  newSessionTemperatureMode.disabled = !capability.supported;
  const custom = capability.supported && newSessionTemperatureMode.value === "custom";
  newSessionTemperatureField.hidden = !custom;
  newSessionTemperature.disabled = !custom;
  newSessionTemperature.required = custom;
  newSessionTemperature.min = String(capability.min);
  newSessionTemperature.max = String(capability.max);
  newSessionTemperature.step = String(capability.step);
  newSessionTemperatureHelp.textContent = capability.supported
    ? "自动模式不会发送温度参数；精确模式发送 0。"
    : "该模型由服务端控制随机性，Tau 不会发送温度参数。";
}

function populateThinkingChoices(select, levels, selectedLevel, unavailableText) {
  select.replaceChildren();
  levels.forEach((level) => {
    const option = element("option", null, level);
    option.value = level;
    select.append(option);
  });
  if (!levels.length) {
    const option = element("option", null, unavailableText);
    option.value = "";
    select.append(option);
  }
  select.disabled = !levels.length;
  if (levels.includes(selectedLevel)) select.value = selectedLevel;
}

function populateNewSessionThinking(provider, selectedLevel) {
  const preferredLevel = provider.thinkingLevels.includes(selectedLevel)
    ? selectedLevel
    : provider.defaultThinkingLevel;
  populateThinkingChoices(
    newSessionThinking,
    provider.thinkingLevels,
    preferredLevel,
    "当前连接不支持 Thinking",
  );
  newSessionThinkingHelp.textContent = provider.thinkingLevels.length
    ? "会话级选择会随新会话保存，不会改动 Provider 的全局默认值。"
    : "当前 Provider/模型未声明可调 Thinking；该会话将使用服务端行为。";
}

function populateSessionOptions(options) {
  state.sessionOptions = options;
  const provider = options.provider;
  const directories = document.querySelector("#project-directory-options");
  directories.replaceChildren();
  options.recentProjectDirectories.forEach((directory) => {
    const option = element("option");
    option.value = directory;
    directories.append(option);
  });
  if (!newSessionCwd.value) {
    newSessionCwd.value = options.defaultProjectDirectory;
  }

  newSessionProvider.value = provider.name;
  newSessionProviderUrl.value = provider.baseUrl;
  newSessionModel.value = provider.model;
  newSessionApiKey.value = "";
  newSessionApiKey.required = !provider.apiKeyConfigured;
  newSessionApiKeyHelp.textContent = provider.apiKeyConfigured
    ? "已配置凭据；留空会保留现有 Key，填写则替换。"
    : "尚未配置凭据；创建会话前必须填写 Key。";
  populateNewSessionThinking(provider, provider.defaultThinkingLevel);
  newSessionTemperatureMode.value = "auto";
  syncTemperatureControls();
}

async function loadSessionOptions() {
  createSessionSubmit.disabled = true;
  setFormError("#new-session-error");
  try {
    populateSessionOptions(await getJson("/api/session-options"));
    createSessionSubmit.disabled = false;
  } catch (error) {
    setFormError("#new-session-error", `无法读取 Provider 配置：${error.message}`);
    throw error;
  }
}

function selectedSessionProviderConfiguration() {
  return state.configuration?.providers.find(
    (provider) => provider.name === sessionProvider.value,
  );
}

function populateSessionThinking(selectedLevel) {
  const provider = selectedSessionProviderConfiguration();
  const levels = provider?.thinkingLevels?.[sessionModel.value] || [];
  populateThinkingChoices(
    sessionThinking,
    levels,
    selectedLevel,
    "当前选择不支持 Thinking",
  );
  sessionThinkingHelp.textContent = levels.length
    ? `可用强度：${levels.join("、")}`
    : "当前选择的 Provider/模型不支持 thinking。";
}

function populateSessionModels(selectedModel, selectedThinking) {
  const provider = selectedSessionProviderConfiguration();
  sessionModel.replaceChildren();
  (provider?.models || []).forEach((model) => {
    const option = element("option", null, model);
    option.value = model;
    sessionModel.append(option);
  });
  const preferredModel = provider?.models.includes(selectedModel)
    ? selectedModel
    : provider?.defaultModel;
  if (provider?.models.includes(preferredModel)) sessionModel.value = preferredModel;
  populateSessionThinking(selectedThinking);
}

function populateSessionSettings() {
  const configuration = state.configuration;
  if (!configuration) return;
  sessionProvider.replaceChildren();
  configuration.providers.forEach((provider) => {
    const option = element("option", null, provider.name);
    option.value = provider.name;
    sessionProvider.append(option);
  });
  const currentProviderAvailable = configuration.providers.some(
    (provider) => provider.name === configuration.providerName,
  );
  sessionProvider.value = currentProviderAvailable
    ? configuration.providerName
    : configuration.providers[0]?.name || "";
  populateSessionModels(
    currentProviderAvailable ? configuration.model : null,
    currentProviderAvailable ? configuration.thinkingLevel : null,
  );
}

async function loadSessions() {
  reloadButton.classList.add("is-spinning");
  try {
    const payload = await getJson("/api/sessions");
    state.sessions = payload.sessions;
    markIndexLoaded(state.sessions.length);
    const requestedId = new URLSearchParams(window.location.search).get("session");
    const requestedExists = state.sessions.some((session) => session.id === requestedId);
    const nextId = requestedExists ? requestedId : state.activeSessionId || state.sessions[0]?.id;
    state.activeSessionId = nextId || null;
    renderSessionList();
    if (nextId) {
      await loadSession(nextId);
    } else {
      state.eventSource?.close();
      state.eventSource = null;
      state.eventStreamConnected = false;
      renderNoSessionSelected();
      setComposerState();
    }
  } catch (error) {
    renderSessionError(error.message);
    showToast("会话索引读取失败");
  } finally {
    reloadButton.classList.remove("is-spinning");
  }
}

document.querySelectorAll("[data-sidebar-tab]").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll("[data-sidebar-tab]").forEach((item) => {
      const active = item === tab;
      item.classList.toggle("is-active", active);
      item.setAttribute("aria-selected", String(active));
    });
    document.querySelectorAll("[data-sidebar-view]").forEach((view) => {
      view.classList.toggle("is-active", view.dataset.sidebarView === tab.dataset.sidebarTab);
    });
  });
});

document.querySelector("#new-session-button").addEventListener("click", async () => {
  newSessionDialog.showModal();
  try {
    await loadSessionOptions();
    newSessionCwd.focus();
  } catch {
    showToast("无法读取新建会话选项");
  }
});

newSessionModel.addEventListener("change", syncTemperatureControls);
newSessionModel.addEventListener("input", syncTemperatureControls);
newSessionTemperatureMode.addEventListener("change", syncTemperatureControls);

sessionSettingsButton.addEventListener("click", () => {
  if (!state.configuration || state.running) return;
  sessionMenu.removeAttribute("open");
  setFormError("#session-settings-error");
  populateSessionSettings();
  sessionSettingsDialog.showModal();
});

sessionProvider.addEventListener("change", () => {
  populateSessionModels(null, null);
});
sessionModel.addEventListener("change", () => populateSessionThinking(null));

sessionSettingsForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const session = activeSession();
  if (!session || state.running || sessionSettingsSubmit.disabled) return;
  sessionSettingsSubmit.disabled = true;
  setFormError("#session-settings-error");
  try {
    const payload = await postJson(
      `/api/sessions/${encodeURIComponent(session.id)}/configuration`,
      {
        providerName: sessionProvider.value,
        model: sessionModel.value,
        thinkingLevel: sessionThinking.disabled ? null : sessionThinking.value,
      },
    );
    const index = state.sessions.findIndex((item) => item.id === session.id);
    if (index >= 0) state.sessions[index] = payload.session;
    updateConfiguration(payload.configuration);
    sessionSettingsDialog.close();
    showToast("会话模型设置已更新");
  } catch (error) {
    setFormError("#session-settings-error", error.message);
  } finally {
    sessionSettingsSubmit.disabled = false;
    setSessionActions();
  }
});

newSessionForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const cwd = newSessionCwd.value.trim();
  const baseUrl = newSessionProviderUrl.value.trim();
  const model = newSessionModel.value.trim();
  const apiKey = newSessionApiKey.value.trim();
  if (
    !cwd ||
    !baseUrl ||
    !model ||
    (!state.sessionOptions?.provider.apiKeyConfigured && !apiKey) ||
    createSessionSubmit.disabled
  ) {
    return;
  }
  let temperature;
  try {
    temperature = resolveTemperature(
      newSessionTemperatureMode.value,
      newSessionTemperature.value,
      selectedTemperatureCapability(),
    );
  } catch (error) {
    setFormError("#new-session-error", error.message);
    return;
  }

  createSessionSubmit.disabled = true;
  createSessionSubmit.textContent = "正在创建…";
  setFormError("#new-session-error");
  try {
    await configureAndCreateSession(
      {
        cwd,
        connection: { baseUrl, apiKey, model },
        thinkingLevel: newSessionThinking.disabled ? null : newSessionThinking.value,
        temperature,
      },
      {
        updateProvider: async (connection) => {
          const payload = await postJson("/api/provider", connection);
          state.sessionOptions.provider = payload.provider;
          populateNewSessionThinking(payload.provider, newSessionThinking.value);
          newSessionApiKey.value = "";
          return payload;
        },
        createSession: (options) => postJson("/api/sessions", options),
        registerSession: (session) => {
          state.sessions = [
            session,
            ...state.sessions.filter((item) => item.id !== session.id),
          ];
          renderSessionList();
        },
        enterSession: async (sessionId) => {
          newSessionDialog.close();
          shell.classList.remove("sidebar-is-open");
          await loadSession(sessionId);
        },
      },
    );
    showToast("新会话已创建");
  } catch (error) {
    setFormError("#new-session-error", error.message);
  } finally {
    createSessionSubmit.disabled = false;
    createSessionSubmit.textContent = "创建并进入";
  }
});

renameSessionButton.addEventListener("click", () => {
  const session = activeSession();
  if (!session) return;
  sessionMenu.removeAttribute("open");
  renameSessionTitle.value = session.title || "";
  setFormError("#rename-session-error");
  renameSessionDialog.showModal();
  renameSessionTitle.focus();
  renameSessionTitle.select();
});

renameSessionForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const session = activeSession();
  const title = renameSessionTitle.value.trim();
  if (!session || !title || renameSessionSubmit.disabled) return;

  renameSessionSubmit.disabled = true;
  setFormError("#rename-session-error");
  try {
    const payload = await postJson(
      `/api/sessions/${encodeURIComponent(session.id)}/rename`,
      { title },
    );
    const index = state.sessions.findIndex((item) => item.id === session.id);
    if (index >= 0) state.sessions[index] = payload.session;
    document.querySelector("#session-title").textContent = payload.session.title;
    document.querySelector("#fact-updated").textContent = formatRelative(
      payload.session.updatedAt,
    );
    renameSessionDialog.close();
    renderSessionList();
    setSessionActions();
    showToast("会话名称已更新");
  } catch (error) {
    setFormError("#rename-session-error", error.message);
  } finally {
    renameSessionSubmit.disabled = false;
  }
});

deleteSessionButton.addEventListener("click", () => {
  const session = activeSession();
  if (!session) return;
  sessionMenu.removeAttribute("open");
  document.querySelector("#delete-session-name").textContent =
    session.title || "Untitled session";
  deleteSessionConfirmation.value = "";
  deleteSessionSubmit.disabled = true;
  setFormError("#delete-session-error");
  deleteSessionDialog.showModal();
  deleteSessionConfirmation.focus();
});

deleteSessionConfirmation.addEventListener("input", () => {
  deleteSessionSubmit.disabled =
    deleteSessionConfirmation.value !==
    deleteSessionConfirmation.dataset.confirmation;
});

deleteSessionForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const session = activeSession();
  if (
    !session ||
    deleteSessionConfirmation.value !== deleteSessionConfirmation.dataset.confirmation ||
    deleteSessionSubmit.disabled
  ) {
    return;
  }

  deleteSessionSubmit.disabled = true;
  setFormError("#delete-session-error");
  try {
    await deleteJson(`/api/sessions/${encodeURIComponent(session.id)}`, {
      confirmation: deleteSessionConfirmation.value,
    });
    state.eventSource?.close();
    state.eventSource = null;
    state.eventStreamConnected = false;
    state.running = false;
    state.activeRunId = null;
    state.liveMessage = null;
    state.sessions = state.sessions.filter((item) => item.id !== session.id);
    state.activeSessionId = null;
    deleteSessionDialog.close();
    renderSessionList();
    const nextSession = state.sessions[0];
    if (nextSession) {
      await loadSession(nextSession.id);
    } else {
      renderNoSessionSelected();
      setComposerState();
    }
    showToast("会话及其记录已删除");
  } catch (error) {
    setFormError("#delete-session-error", error.message);
  } finally {
    deleteSessionSubmit.disabled =
      deleteSessionConfirmation.value !== deleteSessionConfirmation.dataset.confirmation;
  }
});

[exportHtmlLink, exportJsonlLink].forEach((link) => {
  link.addEventListener("click", (event) => {
    if (link.getAttribute("aria-disabled") === "true") {
      event.preventDefault();
      return;
    }
    sessionMenu.removeAttribute("open");
  });
});

[newSessionDialog, sessionSettingsDialog, renameSessionDialog, deleteSessionDialog].forEach(
  (dialog) => {
  dialog.querySelectorAll(".dialog-close, .dialog-cancel").forEach((button) => {
    button.addEventListener("click", () => dialog.close());
  });
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
  },
);

document.querySelector(".theme-toggle").addEventListener("click", () => {
  const next = shell.dataset.theme === "dark" ? "light" : "dark";
  shell.dataset.theme = next;
  window.localStorage.setItem("tau-web-theme", next);
});

document.querySelector(".mobile-sidebar-open").addEventListener("click", () => {
  shell.classList.add("sidebar-is-open");
});

document.querySelector(".mobile-sidebar-close").addEventListener("click", () => {
  shell.classList.remove("sidebar-is-open");
});

reloadButton.addEventListener("click", async () => {
  await loadSessions();
  showToast("会话已刷新");
});

promptInput.addEventListener("input", () => {
  promptInput.style.height = "auto";
  promptInput.style.height = `${Math.min(promptInput.scrollHeight, 160)}px`;
  setComposerState();
});

promptInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    if (state.running && event.altKey) {
      submitComposerMessage("follow_up");
    } else {
      composer.requestSubmit();
    }
  }
});

async function submitComposerMessage(requestedBehavior = null) {
  const sessionId = state.activeSessionId;
  const message = promptInput.value.trim();
  if (!sessionId || !message || state.submitting) return;
  const behavior = state.running ? requestedBehavior || "steer" : null;

  state.submitting = true;
  setComposerState();
  try {
    const payload = await postJson(
      `/api/sessions/${encodeURIComponent(sessionId)}/messages`,
      behavior ? { message, behavior } : { message },
    );
    promptInput.value = "";
    promptInput.style.height = "auto";
    if (payload.status === "queued") {
      state.queue = payload.queue;
      renderQueue();
      showToast(behavior === "follow_up" ? "跟进消息已排队" : "转向消息已排队");
    } else if (payload.status === "command") {
      appendCommandResult(payload.command, payload.message);
      addTraceEvent("command_result", payload.command);
    } else if (payload.status === "accepted") {
      if (shouldActivateAcceptedRun(payload.runId, state.settledRunIds)) {
        state.running = true;
        state.activeRunId = payload.runId;
      }
    }
  } catch (error) {
    showToast(`任务发送失败：${error.message}`);
  } finally {
    state.submitting = false;
    setComposerState();
  }
}

composer.addEventListener("submit", async (event) => {
  event.preventDefault();
  await submitComposerMessage();
});

steerButton.addEventListener("click", () => submitComposerMessage("steer"));
followUpButton.addEventListener("click", () => submitComposerMessage("follow_up"));

clearQueueButton.addEventListener("click", async () => {
  const sessionId = state.activeSessionId;
  if (!sessionId || clearQueueButton.disabled) return;
  clearQueueButton.disabled = true;
  try {
    const payload = await postJson(
      `/api/sessions/${encodeURIComponent(sessionId)}/queue/clear`,
      {},
    );
    state.queue = payload.queue;
    renderQueue();
    showToast("排队消息已清空");
  } catch (error) {
    showToast(`清空队列失败：${error.message}`);
  } finally {
    renderQueue();
  }
});

cancelButton.addEventListener("click", async () => {
  const sessionId = state.activeSessionId;
  if (!sessionId || !state.running) return;
  cancelButton.disabled = true;
  composerState.textContent = "正在请求取消";
  try {
    await postJson(`/api/sessions/${encodeURIComponent(sessionId)}/cancel`, {});
  } catch (error) {
    showToast(`取消失败：${error.message}`);
  } finally {
    cancelButton.disabled = false;
  }
});

toolAuthorizationDialog
  .querySelectorAll("[data-tool-decision]")
  .forEach((button) => {
    button.addEventListener("click", async () => {
      const pending = state.pendingToolAuthorization;
      const sessionId = pending?.sessionId;
      if (!pending || !sessionId) return;
      toolAuthorizationDialog
        .querySelectorAll("[data-tool-decision]")
        .forEach((item) => (item.disabled = true));
      setFormError("#tool-authorization-error");
      try {
        await postJson(
          `/api/sessions/${encodeURIComponent(sessionId)}/tool-authorizations/${encodeURIComponent(pending.requestId)}`,
          { decision: button.dataset.toolDecision },
        );
        const decisionLabels = { allow: "allowed", deny: "denied", cancel: "cancelled" };
        state.traceAuthorizationDecisions[pending.requestId] =
          decisionLabels[button.dataset.toolDecision] ?? button.dataset.toolDecision;
        renderRunTimeline();
        state.pendingToolAuthorization = null;
        toolAuthorizationDialog.close();
      } catch (error) {
        setFormError("#tool-authorization-error", error.message);
        toolAuthorizationDialog
          .querySelectorAll("[data-tool-decision]")
          .forEach((item) => (item.disabled = false));
      }
    });
  });
toolAuthorizationDialog.addEventListener("cancel", (event) => {
  event.preventDefault();
});

document.querySelector("#trace-expand-all").addEventListener("click", () => {
  state.traceExpandAll = !state.traceExpandAll;
  const button = document.querySelector("#trace-expand-all");
  button.textContent = state.traceExpandAll ? "收起全部" : "展开全部";
  renderRunTimeline();
});

shell.dataset.theme = window.localStorage.getItem("tau-web-theme") || "dark";
setComposerState();
setSessionActions();
loadHealth();
loadSessions();
