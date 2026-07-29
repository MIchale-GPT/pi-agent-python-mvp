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
};
const { createAndEnterSession } = globalThis.TauSessionActions;

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
const composerState = document.querySelector("#composer-state");
const sessionMenu = document.querySelector(".session-menu");
const renameSessionButton = document.querySelector("#rename-session-button");
const deleteSessionButton = document.querySelector("#delete-session-button");
const exportHtmlLink = document.querySelector("#export-html-link");
const exportJsonlLink = document.querySelector("#export-jsonl-link");
const newSessionDialog = document.querySelector("#new-session-dialog");
const newSessionForm = document.querySelector("#new-session-form");
const newSessionCwd = document.querySelector("#new-session-cwd");
const newSessionProvider = document.querySelector("#new-session-provider");
const newSessionModel = document.querySelector("#new-session-model");
const createSessionSubmit = document.querySelector("#create-session-submit");
const renameSessionDialog = document.querySelector("#rename-session-dialog");
const renameSessionForm = document.querySelector("#rename-session-form");
const renameSessionTitle = document.querySelector("#rename-session-title");
const renameSessionSubmit = document.querySelector("#rename-session-submit");
const deleteSessionDialog = document.querySelector("#delete-session-dialog");
const deleteSessionForm = document.querySelector("#delete-session-form");
const deleteSessionConfirmation = document.querySelector("#delete-session-confirmation");
const deleteSessionSubmit = document.querySelector("#delete-session-submit");
let toastTimer;

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
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
  document.querySelector("#fact-messages").textContent = "—";
  document.querySelector("#fact-updated").textContent = "—";
  transcriptContent.replaceChildren();
  const empty = element("div", "empty-transcript");
  empty.append(
    element("strong", null, "还没有打开的会话"),
    element("p", null, "使用左侧“新建会话”选择项目、Provider 和模型。"),
  );
  transcriptContent.append(empty);
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

function setSessionFacts(session, messageCount) {
  document.querySelector("#active-session-state").textContent = "LOADED";
  document.querySelector("#fact-provider").textContent = session.providerName || "—";
  document.querySelector("#fact-model").textContent = session.model || "—";
  document.querySelector("#fact-messages").textContent = String(messageCount);
  document.querySelector("#fact-updated").textContent = formatRelative(session.updatedAt);
}

function markIndexLoaded(count) {
  const item = document.querySelector("#session-index-event");
  item.classList.remove("is-current");
  item.classList.add("is-done");
  item.querySelector("strong").textContent = "session_index_loaded";
  item.querySelector("small").textContent = `${count} indexed sessions`;
  item.querySelector("time").textContent = "ok";
}

function markBranchLoaded(messageCount) {
  document.querySelector("#event-timeline .branch-event")?.remove();
  const item = element("li", "is-current branch-event");
  const node = element("span", "event-node");
  const content = element("div");
  content.append(
    element("strong", null, "active_branch_loaded"),
    element("small", null, `${messageCount} visible messages`),
  );
  item.append(node, content, element("time", null, "now"));
  document.querySelector("#event-timeline").append(item);
  document.querySelector("#trace-state").textContent = "session_ready";
}

function setComposerState() {
  const hasSession = Boolean(state.activeSessionId);
  const ready =
    hasSession && state.eventStreamConnected && !state.running && !state.submitting;
  promptInput.disabled = !ready;
  sendButton.disabled = !ready || !promptInput.value.trim();
  cancelButton.hidden = !state.running;
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
    promptInput.placeholder = "Tau 正在运行…";
    composerState.textContent = "Tau 正在运行";
  } else {
    promptInput.placeholder = "发送任务给 Tau…";
    composerState.textContent = "会话已就绪";
  }
  renderSessionList();
}

function addTraceEvent(type, detail = "") {
  const timeline = document.querySelector("#event-timeline");
  const item = element("li", "is-current live-event");
  const node = element("span", "event-node");
  const content = element("div");
  content.append(element("strong", null, type), element("small", null, detail));
  item.append(node, content, element("time", null, "now"));
  timeline.querySelectorAll(".live-event").forEach((event) => event.classList.remove("is-current"));
  timeline.append(item);
  const liveEvents = timeline.querySelectorAll(".live-event");
  if (liveEvents.length > 9) liveEvents[0].remove();
  document.querySelector("#trace-state").textContent = type;
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

async function handleLiveEvent(event, sessionId) {
  if (sessionId !== state.activeSessionId) return;
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
  if (event.type === "message_start") {
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
    state.running = false;
    state.submitting = true;
    state.activeRunId = null;
    state.liveMessage = null;
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
  state.eventSource?.close();
  state.eventSource = null;
  state.eventStreamConnected = false;
  state.running = false;
  state.activeRunId = null;
  state.liveMessage = null;
  setComposerState();

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
    setComposerState();
    addTraceEvent("event_stream_reconnecting", "SSE connection lost");
  };
}

async function loadSession(sessionId, options = {}) {
  const { reconnect = true, scrollToBottom = false } = options;
  const session = state.sessions.find((item) => item.id === sessionId);
  if (!session) return;
  state.activeSessionId = sessionId;
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
    const index = state.sessions.findIndex((item) => item.id === sessionId);
    if (index >= 0) state.sessions[index] = payload.session;
    setSessionFacts(payload.session, payload.messages.length);
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

function populateModelChoices(selectedModel) {
  const provider = state.sessionOptions?.providers.find(
    (item) => item.name === newSessionProvider.value,
  );
  newSessionModel.replaceChildren();
  (provider?.models || []).forEach((model) => {
    const option = element("option", null, model);
    option.value = model;
    newSessionModel.append(option);
  });
  const preferredModel =
    selectedModel && provider?.models.includes(selectedModel)
      ? selectedModel
      : provider?.defaultModel;
  if (preferredModel) newSessionModel.value = preferredModel;
}

function populateSessionOptions(options) {
  state.sessionOptions = options;
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

  newSessionProvider.replaceChildren();
  options.providers.forEach((provider) => {
    const option = element("option", null, provider.name);
    option.value = provider.name;
    newSessionProvider.append(option);
  });
  newSessionProvider.value = options.defaultProvider;
  populateModelChoices();
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

newSessionProvider.addEventListener("change", () => populateModelChoices());

newSessionForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const cwd = newSessionCwd.value.trim();
  const providerName = newSessionProvider.value;
  const model = newSessionModel.value;
  if (!cwd || !providerName || !model || createSessionSubmit.disabled) return;

  createSessionSubmit.disabled = true;
  createSessionSubmit.textContent = "正在创建…";
  setFormError("#new-session-error");
  try {
    await createAndEnterSession(
      { cwd, providerName, model },
      {
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

[newSessionDialog, renameSessionDialog, deleteSessionDialog].forEach((dialog) => {
  dialog.querySelectorAll(".dialog-close, .dialog-cancel").forEach((button) => {
    button.addEventListener("click", () => dialog.close());
  });
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
});

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
    composer.requestSubmit();
  }
});

composer.addEventListener("submit", async (event) => {
  event.preventDefault();
  const sessionId = state.activeSessionId;
  const message = promptInput.value.trim();
  if (!sessionId || !message || state.running || state.submitting) return;

  state.submitting = true;
  state.running = true;
  setComposerState();
  try {
    await postJson(`/api/sessions/${encodeURIComponent(sessionId)}/messages`, { message });
    promptInput.value = "";
    promptInput.style.height = "auto";
  } catch (error) {
    state.running = false;
    showToast(`任务发送失败：${error.message}`);
  } finally {
    state.submitting = false;
    setComposerState();
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

shell.dataset.theme = window.localStorage.getItem("tau-web-theme") || "dark";
setComposerState();
setSessionActions();
loadHealth();
loadSessions();
