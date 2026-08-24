# Tau Web 轨迹持久化与统一时间线 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 右侧轨迹合并为单一时间线树，事件经服务端内存环缓冲 + `.webtrace.jsonl` 落盘在刷新/重启后完整重放，授权决定闭环广播，并防御 `[object HTMLElement]` 类渲染缺陷。

**Architecture:** 只触及 Web 宿主层：`tau_coding/web.py` 在 `_publish` 处统一入缓冲/落盘并在 `_subscribe` 重放；前端 `data/web/` 删除底部 ticker，`trace-timeline.js` 视图模型新增 `__session__` 合成分组与 `tool_authorization_resolved` 回填。核心层 tau_agent/tau_ai 零改动。

**Tech Stack:** Python 3 stdlib（http.server + asyncio）、原生 ES 浏览器脚本、node:test、pytest。

**Spec:** `dev-notes/web-trace-persistence-design.md`

## Global Constraints

- 所有测试/脚本经 `uv run` 执行（AGENTS.md）。
- 目标 Python 版本以 `pyproject.toml` 为准；核心包不得引入 CLI/Textual/Rich 依赖。
- SSE 契约「只增不改」：旧字段全部保留。
- 提交保持原子：一个任务一个连贯提交；提交信息用英文祈使句（参照 `git log`）。
- UI 中文文案与现有面板风格一致（如「运行中」「待确认」）。
- 缓冲上限常量：`TRACE_BUFFER_LIMIT = 600`；落盘压缩阈值 `1200` 行，保留最近 `600` 行。
- 每个任务结束前运行该任务的测试命令并确认通过。

---

### Task 1: 后端 — trace 环缓冲与 SSE 重放

**Files:**
- Modify: `src/tau_coding/web.py`
- Modify: `tests/test_web.py`
- Modify: `dev-notes/web-trace-persistence-design.md`（修正入缓冲描述）

**Interfaces:**
- Consumes: 现有 `_WebSessionSlot`、`_publish(slot, payload)`、`_subscribe(session_id)`。
- Produces: 常量 `TRACE_BUFFER_LIMIT = 600`；`_WebSessionSlot.trace_buffer: deque[dict[str, object]]`；`TauWebRuntime._record_trace_event(slot, payload) -> None`；SSE 重放帧携带 `"replay": True`。

- [ ] **Step 1: 写失败测试（订阅即重放）**

在 `tests/test_web.py` 中 `test_message_api_tags_trace_events_with_run_id_and_finish_summary` 之后新增：

```python
def test_sse_subscribe_replays_buffered_trace_events(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    cwd = tmp_path / "project"
    cwd.mkdir()
    manager.create_session(
        cwd=cwd,
        model="fake",
        provider_name="fake",
        title="Replay session",
        session_id="session-1",
    )
    provider = _StreamingFakeProvider()

    async def load_session(
        selected: CodingSessionRecord,
        selected_manager: SessionManager,
    ) -> WebSessionHandle:
        return await _load_test_session(selected, selected_manager, provider)

    server = create_web_server(
        host="127.0.0.1",
        port=0,
        session_manager=manager,
        session_loader=load_session,
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    runner_connection = HTTPConnection(host, port, timeout=2)
    late_connection = HTTPConnection(host, port, timeout=2)

    try:
        runner_connection.request("GET", "/api/sessions/session-1/events")
        runner_response = runner_connection.getresponse()
        assert runner_response.status == 200

        status, payload = _post_json(
            runner_connection,
            "/api/sessions/session-1/messages",
            {"message": "Connect the A theme"},
        )
        assert status == 202
        _read_sse_events(runner_response, until="run_finished")

        late_connection.request("GET", "/api/sessions/session-1/events")
        late_response = late_connection.getresponse()
        assert late_response.status == 200

        events = _read_sse_events(late_response, until="run_finished")
        assert events[0]["type"] == "web_connected"
        replayed = events[1:]
        assert replayed[0]["type"] == "run_started"
        assert replayed[-1]["type"] == "run_finished"
        assert all(event.get("replay") is True for event in replayed)
        assert "replay" not in events[0]
        assert [event["type"] for event in replayed].count("message_update") >= 1
    finally:
        runner_connection.close()
        late_connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_trace_buffer_drops_oldest_events_beyond_limit(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    cwd = tmp_path / "project"
    cwd.mkdir()
    record = manager.create_session(
        cwd=cwd,
        model="fake",
        provider_name="fake",
        title="Buffer session",
        session_id="session-1",
    )
    runtime = TauWebRuntime(manager, lambda *args: None)  # type: ignore[arg-type]

    async def publish_many() -> None:
        slot = runtime._slots.setdefault(record.id, _WebSessionSlot())
        for index in range(web_module.TRACE_BUFFER_LIMIT + 100):
            runtime._publish(
                slot,
                {"type": "message_end", "sessionId": record.id, "index": index},
            )

    asyncio.run(publish_many())
    slot = runtime._slots[record.id]
    assert len(slot.trace_buffer) == web_module.TRACE_BUFFER_LIMIT
    assert slot.trace_buffer[0]["index"] == 100
    assert slot.trace_buffer[-1]["index"] == web_module.TRACE_BUFFER_LIMIT + 99
    runtime.close()
```

同时在文件顶部 import 区补 `from tau_coding.web import TauWebRuntime, _WebSessionSlot`（并入现有 `from tau_coding.web import (...)` 块）。

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_web.py::test_sse_subscribe_replays_buffered_trace_events tests/test_web.py::test_trace_buffer_drops_oldest_events_beyond_limit -v`
Expected: FAIL（`replay` 断言失败 / `trace_buffer` 属性不存在）

- [ ] **Step 3: 实现缓冲与重放**

`src/tau_coding/web.py`：

3a. 常量区（`_ASSET_CONTENT_TYPES` 附近）加：

```python
TRACE_BUFFER_LIMIT = 600
```

3b. `_WebSessionSlot` 字段追加：

```python
    trace_buffer: deque[dict[str, object]] = field(
        default_factory=lambda: deque(maxlen=TRACE_BUFFER_LIMIT)
    )
```

（`typing.Deque` 不需要；文件顶部确认已 `from collections import deque`，没有则新增 import。）

3c. 新增方法（放在 `_publish` 上方）：

```python
    def _record_trace_event(
        self,
        slot: _WebSessionSlot,
        payload: dict[str, object],
    ) -> None:
        """Buffer a forwarded session event so late subscribers can replay it."""
        slot.trace_buffer.append(dict(payload))
```

3d. `_publish` 开头插入：

```python
        if payload.get("type") != "web_connected":
            self._record_trace_event(slot, payload)
```

3e. `_subscribe` 中，`web_connected` 入队之后、`run_summary` 分支之前插入：

```python
        for buffered in list(slot.trace_buffer):
            subscriber.put(
                self._stream_item(slot, {**buffered, "replay": True})
            )
```

- [ ] **Step 4: 更新受影响的既有断言**

`test_sse_reconnect_mid_run_receives_active_run_summary` 中：

```python
        events = _read_sse_events(second_response, until="run_summary")
```

其后的严格序列断言改为：

```python
        assert events[0] == {"type": "web_connected", ...} 改为：
        assert events[0]["type"] == "web_connected"
        assert events[0]["running"] is True
        replayed = events[1:-1]
        assert all(event.get("replay") is True for event in replayed)
        summary = events[-1]
```

（删除原 `assert [event["type"] for event in events] == ["web_connected", "run_summary"]`；其余 summary 字段断言保持不变。）

同时修正 spec 中「入缓冲范围」一句为：「凡经 `_publish()` 转发的会话事件均入缓冲并落盘（含 run_started/run_finished/run_error/cancel_requested）；连接层帧（`web_connected`）与按订阅者生成的控制帧不入」。`git add dev-notes/web-trace-persistence-design.md` 一并提交。

- [ ] **Step 5: 运行全部后端测试**

Run: `uv run pytest tests/test_web.py -v`
Expected: PASS（含两个新测试）

- [ ] **Step 6: Commit**

```bash
git add src/tau_coding/web.py tests/test_web.py dev-notes/web-trace-persistence-design.md
git commit -m "Add Tau Web trace event ring buffer with SSE replay"
```

---

### Task 2: 后端 — 工具授权决定闭环广播

**Files:**
- Modify: `src/tau_coding/web.py`
- Test: `tests/test_web.py`

**Interfaces:**
- Produces: SSE 事件 `{"type": "tool_authorization_resolved", "sessionId", "runId?", "timestamp?", "requestId", "toolCallId", "decision": "allow"|"deny"|"cancel"|"timeout"|"no_subscriber"}`；`_resolve_pending_tool_authorizations` 由 staticmethod 改为实例方法 `(self, session_id, slot, decision)`。

- [ ] **Step 1: 写失败测试**

在 `test_tool_authorization_defaults_to_deny_when_the_browser_disconnects` 附近新增两个测试：

```python
def test_tool_authorization_decision_is_broadcast_to_subscribers(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    cwd = tmp_path / "project"
    cwd.mkdir()
    manager.create_session(
        cwd=cwd,
        model="fake",
        provider_name="fake",
        title="Auth broadcast session",
        session_id="session-1",
    )
    provider = _ToolCallingFakeProvider()

    async def execute(
        tool_call_id: str,
        arguments: Mapping[str, object],
        signal: CancellationToken | None = None,
        on_update: object = None,
    ) -> AgentToolResult:
        del tool_call_id, arguments, signal, on_update
        return AgentToolResult(content="tool completed")

    tool = AgentTool(
        name="write_file",
        label="Write file",
        description="Write a project file.",
        parameters={"type": "object"},
        execute_fn=execute,
    )

    async def load_session(
        selected: CodingSessionRecord,
        selected_manager: SessionManager,
    ) -> WebSessionHandle:
        return await _load_test_session(
            selected,
            selected_manager,
            provider,
            tools=[tool],
        )

    server = create_web_server(
        host="127.0.0.1",
        port=0,
        session_manager=manager,
        session_loader=load_session,
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    events_connection = HTTPConnection(host, port, timeout=2)
    command_connection = HTTPConnection(host, port, timeout=2)

    try:
        events_connection.request("GET", "/api/sessions/session-1/events")
        events_response = events_connection.getresponse()
        status, _payload = _post_json(
            command_connection,
            "/api/sessions/session-1/messages",
            {"message": "Use the write tool"},
        )
        assert status == 202
        events = _read_sse_events(
            events_response,
            until="tool_authorization_requested",
        )
        request = events[-1]

        status, _resolved = _post_json(
            command_connection,
            f"/api/sessions/session-1/tool-authorizations/{request['requestId']}",
            {"decision": "allow"},
        )
        assert status == 200

        follow_up = _read_sse_events(
            events_response,
            until="tool_authorization_resolved",
        )
        resolved = follow_up[-1]
        assert resolved["requestId"] == request["requestId"]
        assert resolved["toolCallId"] == request["toolCallId"]
        assert resolved["decision"] == "allow"
        assert resolved["runId"] == request["runId"]
    finally:
        events_connection.close()
        command_connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_disconnect_denied_authorization_is_visible_after_reconnect(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    cwd = tmp_path / "project"
    cwd.mkdir()
    manager.create_session(
        cwd=cwd,
        model="fake",
        provider_name="fake",
        title="Disconnect deny session",
        session_id="session-1",
    )

    class NeverEndingToolProvider:
        def stream_response(self, **kwargs: object) -> AsyncIterator[AssistantMessageEvent]:
            del kwargs

            async def iterator() -> AsyncIterator[AssistantMessageEvent]:
                yield assistant_start(model="fake")
                yield tool_call_end(model="fake")

            return iterator()

    async def load_session(
        selected: CodingSessionRecord,
        selected_manager: SessionManager,
    ) -> WebSessionHandle:
        return await _load_test_session(selected, selected_manager, NeverEndingToolProvider())

    server = create_web_server(
        host="127.0.0.1",
        port=0,
        session_manager=manager,
        session_loader=load_session,
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    first_connection = HTTPConnection(host, port, timeout=2)
    second_connection = HTTPConnection(host, port, timeout=2)
    command_connection = HTTPConnection(host, port, timeout=2)

    try:
        first_connection.request("GET", "/api/sessions/session-1/events")
        first_response = first_connection.getresponse()
        status, _payload = _post_json(
            command_connection,
            "/api/sessions/session-1/messages",
            {"message": "Trigger authorization"},
        )
        assert status == 202
        _read_sse_events(first_response, until="tool_authorization_requested")

        # 断开唯一订阅者 → 未决授权被自动拒绝
        first_response.close()
        first_connection.close()
        sleep(0.2)

        second_connection.request("GET", "/api/sessions/session-1/events")
        second_response = second_connection.getresponse()
        events = _read_sse_events(second_response, until="tool_authorization_resolved")
        requested = [e for e in events if e["type"] == "tool_authorization_requested"]
        resolved = events[-1]
        assert requested, "authorization request should be replayed"
        assert resolved["requestId"] == requested[-1]["requestId"]
        assert resolved["decision"] == "no_subscriber"
    finally:
        second_connection.close()
        command_connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
```

注意：若 `NeverEndingToolProvider` 的 `tool_call_end` 形参名与 `pi_event_helpers` 实际签名不符，参照本文件既有 `_ToolCallingFakeProvider` 的写法调整（目标只是让 agent 发起一次工具调用并停在授权等待）。

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_web.py::test_tool_authorization_decision_is_broadcast_to_subscribers tests/test_web.py::test_disconnect_denied_authorization_is_visible_after_reconnect -v`
Expected: FAIL（读不到 `tool_authorization_resolved`，最终超时或 KeyError）

- [ ] **Step 3: 实现**

3a. 新增模块级函数（放在 `_tool_authorization_event_payload` 定义旁边，先找到它确认形参）：

```python
def _tool_authorization_resolved_payload(
    request_id: str,
    tool_call_id: str,
    decision: str,
) -> dict[str, object]:
    return {
        "type": "tool_authorization_resolved",
        "requestId": request_id,
        "toolCallId": tool_call_id,
        "decision": decision,
    }
```

3b. `_authorize_tool_call` 中三处结果路径补发布（`timeout` 分支在 `except TimeoutError:` 内、返回前）：

```python
        except TimeoutError:
            self._publish(
                slot,
                _trace_payload(
                    slot,
                    _tool_authorization_resolved_payload(
                        request_id, call.id, "timeout"
                    ),
                ),
            )
            return True, "Tool execution denied because authorization timed out"
```

成功路径（`pending.decision.set_result(decision)` 之后、return 之前）：

```python
        resolved = "allow" if decision == "allow" else decision
        self._publish(
            slot,
            _trace_payload(
                slot,
                _tool_authorization_resolved_payload(request_id, call.id, resolved),
            ),
        )
```

（`decision` 本身即 `"allow" | "deny" | "cancel"`，直接用即可，无需映射变量——实现时写 `decision`。）

3c. `_respond_tool_authorization` 不重复发布（决定事件由 `_authorize_tool_call` 的 await 返回后统一发），保持该方法原样。

3d. `_resolve_pending_tool_authorizations` 改为实例方法并发布事件：

```python
    def _resolve_pending_tool_authorizations(
        self,
        session_id: str,
        slot: _WebSessionSlot,
        decision: ToolAuthorizationDecision,
    ) -> None:
        published = "no_subscriber" if decision == "deny" else decision
        for pending in slot.pending_tool_authorizations.values():
            if pending.decision.done():
                continue
            pending.decision.set_result(decision)
            self._publish(
                slot,
                _trace_payload(
                    slot,
                    _tool_authorization_resolved_payload(
                        pending.request_id, pending.call.id, published
                    ),
                ),
            )
```

同步更新三个调用点：`_unsubscribe`（改为 `self._resolve_pending_tool_authorizations(session_id, slot, "deny")`）、`_cancel`（`..., slot, "cancel"`）、`_shutdown`（循环里拿到 `session_id` 键：`for session_id, slot in self._slots.items():`）。

注意顺序：`published` 里 `no_subscriber` 映射针对「最后一个订阅者离开」场景（`_unsubscribe` 传 `"deny"`）；`_cancel` 与 `_shutdown` 保持原始 decision 值。

- [ ] **Step 4: 运行后端全量测试**

Run: `uv run pytest tests/test_web.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/tau_coding/web.py tests/test_web.py
git commit -m "Broadcast tool authorization resolutions to Tau Web subscribers"
```

---

### Task 3: 后端 — webtrace 落盘、回填与生命周期

**Files:**
- Modify: `src/tau_coding/web.py`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: Task 1 的 `_record_trace_event`、`trace_buffer`；`CodingSessionRecord.path`（`<project_session_dir>/<id>.jsonl`）。
- Produces: 模块常量 `TRACE_FILE_COMPACT_LINES = 1200`、`TRACE_FILE_KEEP_LINES = 600`；文件 `<session_id>.webtrace.jsonl`（与会话 JSONL 同目录）；`TauWebRuntime._webtrace_path(record) -> Path`；`_WebSessionSlot.trace_file_lines: int`、`trace_backfilled: bool`。

- [ ] **Step 1: 写失败测试**

```python
def test_trace_events_are_persisted_and_backfilled_across_restart(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    cwd = tmp_path / "project"
    cwd.mkdir()
    record = manager.create_session(
        cwd=cwd,
        model="fake",
        provider_name="fake",
        title="Persisted trace session",
        session_id="session-1",
    )
    provider = _StreamingFakeProvider()

    async def load_session(
        selected: CodingSessionRecord,
        selected_manager: SessionManager,
    ) -> WebSessionHandle:
        return await _load_test_session(selected, selected_manager, provider)

    server = create_web_server(
        host="127.0.0.1",
        port=0,
        session_manager=manager,
        session_loader=load_session,
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    connection = HTTPConnection(host, port, timeout=2)
    webtrace_path = record.path.with_name(record.path.stem + ".webtrace.jsonl")

    try:
        connection.request("GET", "/api/sessions/session-1/events")
        response = connection.getresponse()
        status, _payload = _post_json(
            connection,
            "/api/sessions/session-1/messages",
            {"message": "Connect the A theme"},
        )
        assert status == 202
        _read_sse_events(response, until="run_finished")
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert webtrace_path.exists()
    persisted = [
        json.loads(line)
        for line in webtrace_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert persisted[0]["type"] == "run_started"
    assert persisted[-1]["type"] == "run_finished"
    assert all("replay" not in event for event in persisted)

    # —— 重启后的新 runtime 从文件回填 ——
    restarted = TauWebRuntime(manager, lambda *args: None)  # type: ignore[arg-type]

    async def collect_backfill() -> list[dict[str, Any]]:
        _subscriber_id, subscriber = await restarted._subscribe(record.id)
        events: list[dict[str, Any]] = []
        while True:
            item = subscriber.get(timeout=1)
            if item is None:
                break
            events.append(item.payload)
            if item.payload["type"] == "run_finished":
                break
        return events

    try:
        events = asyncio.run(collect_backfill())
    finally:
        restarted.close()
    assert events[0]["type"] == "web_connected"
    replayed = events[1:]
    assert replayed[0]["type"] == "run_started"
    assert replayed[-1]["type"] == "run_finished"
    assert [event["type"] for event in replayed] == [
        event["type"] for event in persisted
    ]


def test_delete_session_removes_the_webtrace_file(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    cwd = tmp_path / "project"
    cwd.mkdir()
    record = manager.create_session(
        cwd=cwd,
        model="fake",
        provider_name="fake",
        title="Deleted trace session",
        session_id="session-1",
    )
    webtrace_path = record.path.with_name(record.path.stem + ".webtrace.jsonl")
    webtrace_path.parent.mkdir(parents=True, exist_ok=True)
    webtrace_path.write_text('{"type": "run_started"}\n', encoding="utf-8")
    runtime = TauWebRuntime(manager, lambda *args: None)  # type: ignore[arg-type]

    asyncio.run(runtime._delete_session(record.id))

    assert not webtrace_path.exists()
    runtime.close()


def test_corrupt_webtrace_file_degrades_to_memory_only(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    manager = _manager(tmp_path)
    cwd = tmp_path / "project"
    cwd.mkdir()
    record = manager.create_session(
        cwd=cwd,
        model="fake",
        provider_name="fake",
        title="Corrupt trace session",
        session_id="session-1",
    )
    webtrace_path = record.path.with_name(record.path.stem + ".webtrace.jsonl")
    webtrace_path.write_text("{not json\n", encoding="utf-8")
    runtime = TauWebRuntime(manager, lambda *args: None)  # type: ignore[arg-type]

    async def subscribe_once() -> None:
        _subscriber_id, subscriber = await runtime._subscribe(record.id)
        item = subscriber.get(timeout=1)
        assert item is not None
        assert item.payload["type"] == "web_connected"

    try:
        with caplog.at_level("WARNING"):
            asyncio.run(subscribe_once())
        assert any("webtrace" in record.message.lower() for record in caplog.records)
    finally:
        runtime.close()
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_web.py::test_trace_events_are_persisted_and_backfilled_across_restart tests/test_web.py::test_delete_session_removes_the_webtrace_file tests/test_web.py::test_corrupt_webtrace_file_degrades_to_memory_only -v`
Expected: FAIL（webtrace 文件不存在 / 文件未删除 / 无降级日志）

- [ ] **Step 3: 实现**

3a. 常量区追加：

```python
TRACE_FILE_COMPACT_LINES = 1200
TRACE_FILE_KEEP_LINES = 600
```

3b. `_WebSessionSlot` 追加字段：

```python
    trace_file_lines: int = 0
    trace_backfilled: bool = False
```

3c. `web.py` 顶部 import 补充（若无）：`import logging`、`import os`、`from pathlib import Path`。模块级 `logger = logging.getLogger(__name__)`。

3d. `TauWebRuntime` 新增方法：

```python
    def _webtrace_path(self, record: CodingSessionRecord) -> Path:
        return record.path.with_name(f"{record.path.stem}.webtrace.jsonl")

    def _backfill_trace_from_disk(self, record: CodingSessionRecord, slot: _WebSessionSlot) -> None:
        """Load the tail of the persisted trace into an empty in-memory buffer."""
        if slot.trace_backfilled:
            return
        slot.trace_backfilled = True
        path = self._webtrace_path(record)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return
        except OSError as exc:
            logger.warning("Tau Web could not read %s: %s", path, exc)
            return
        loaded: list[dict[str, object]] = []
        for line in lines[-TRACE_BUFFER_LIMIT:]:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("Tau Web skipped a corrupt webtrace line in %s", path)
                continue
            if isinstance(payload, dict):
                payload.pop("replay", None)
                loaded.append(payload)
        slot.trace_buffer.extendleft(reversed(loaded))

    def _persist_trace_event(self, record: CodingSessionRecord, slot: _WebSessionSlot, payload: dict[str, object]) -> None:
        path = self._webtrace_path(record)
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        try:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError as exc:
            logger.warning("Tau Web could not append to %s: %s", path, exc)
            slot.trace_file_lines = -1  # 不可写标记：本次运行不再尝试写盘
            return
        slot.trace_file_lines += 1
        if (
            slot.trace_file_lines > 0
            and slot.trace_file_lines > TRACE_FILE_COMPACT_LINES
        ):
            self._compact_webtrace(path, slot)

    @staticmethod
    def _compact_webtrace(path: Path, slot: _WebSessionSlot) -> None:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            kept = lines[-TRACE_FILE_KEEP_LINES:]
            temp = path.with_suffix(".webtrace.jsonl.tmp")
            temp.write_text("".join(line + "\n" for line in kept), encoding="utf-8")
            os.replace(temp, path)
            slot.trace_file_lines = len(kept)
        except OSError as exc:
            logger.warning("Tau Web could not compact %s: %s", path, exc)
```

3e. `_record_trace_event` 扩展（需要 record；由调用方传入）：

```python
    def _record_trace_event(
        self,
        record: CodingSessionRecord,
        slot: _WebSessionSlot,
        payload: dict[str, object],
    ) -> None:
        slot.trace_buffer.append(dict(payload))
        if slot.trace_file_lines >= 0:
            self._persist_trace_event(record, slot, payload)
```

`_publish` 相应改为传入 record。`_publish` 当前只有 `slot` 参数，而所有调用点都持有 `record` 或 `session_id`。为避免大改签名：在 `_publish` 内部用 `self._records` 缓存？——不引入新缓存。做法：给 `_publish` 增加 keyword 参数 `record: CodingSessionRecord | None = None`；当 `record is None` 且 payload 需要 buffered 时，通过 `self._manager.get_session(...)` 不可靠（slot 无 id）。因此改法：`_publish(self, slot, payload, *, record=None)`，并把 `_record_trace_event` 的磁盘分支仅在 `record is not None` 时执行；随后把 `_submit`、`_run_prompt`、`_finish_run`、`_cancel`、`_clear_queue`、`_authorize_tool_call`、`_publish_queue_update`、`_update_session_configuration` 等调用点逐一传 `record=record`（这些方法体内已有 `record`；`_run_prompt`/`_finish_run` 只有 `session_id` 时用 `self._require_session(session_id)` 取一次）。`_shutdown`/`_delete_session` 内的收尾 publish 可不传（允许只进内存）。

3f. `_subscribe` 开头 `slot = self._slots.setdefault(...)` 后加回填：

```python
        record = self._require_session(session_id)
        self._backfill_trace_from_disk(record, slot)
```

（`_require_session` 已在方法第一行调用，复用其返回值。）

3g. `_delete_session` 中，`self._manager.delete_session(...)` 之前：

```python
        record = self._require_session(session_id)
        webtrace = self._webtrace_path(record)
        try:
            webtrace.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Tau Web could not remove %s: %s", webtrace, exc)
```

3h. 文件顶部确认 `import json` 存在（大概率已有）。

- [ ] **Step 4: 运行后端全量测试**

Run: `uv run pytest tests/test_web.py -v`
Expected: PASS（注意既有测试的临时目录隔离不受影响；若有测试因新增 `.webtrace.jsonl` 出现在会话目录而失败，检查其对目录列表的假设并收紧到具体文件断言）

- [ ] **Step 5: Commit**

```bash
git add src/tau_coding/web.py tests/test_web.py
git commit -m "Persist Tau Web trace events across restarts via webtrace files"
```

---

### Task 4: 前端视图模型 — `__session__` 分组与授权回填

**Files:**
- Modify: `src/tau_coding/data/web/trace-timeline.js`
- Test: `tests/web/trace-timeline.test.mjs`

**Interfaces:**
- Produces: `buildTraceTimeline(events, options)` 返回中：无 `runId` 事件归入 `runs` 内 `runId === "__session__"`、`title === "会话事件"` 的分组（items 为 `kind: "session"`）；`ungrouped` 语义保留但预期为空数组；新处理事件类型 `tool_authorization_resolved`。

- [ ] **Step 1: 写失败测试（追加到 trace-timeline.test.mjs 末尾）**

```js
test("groups events without a runId into a synthetic session group", () => {
  const events = [
    { type: "configuration_updated", providerName: "fake", model: "m1", timestamp: 1 },
    { type: "run_started", runId: RUN_A },
    userStart("问题"),
    runFinished({}),
    { type: "command_result", command: "/help", message: "ok", timestamp: 2 },
  ];

  const { runs, ungrouped } = buildTraceTimeline(events, {});
  const sessionRun = runs.find((run) => run.runId === "__session__");
  const normalRun = runs.find((run) => run.runId === RUN_A);

  assert.ok(normalRun);
  assert.deepEqual(ungrouped, []);
  assert.equal(sessionRun.title, "会话事件");
  assert.equal(sessionRun.items.length, 2);
  assert.equal(sessionRun.items[0].kind, "session");
  assert.equal(sessionRun.items[0].type, "configuration_updated");
  assert.equal(sessionRun.items[1].type, "command_result");
});

test("applies authorization resolutions by requestId", () => {
  const base = [
    { type: "run_started", runId: RUN_A },
    {
      type: "tool_authorization_requested",
      runId: RUN_A,
      requestId: "auth-1",
      toolCallId: "call-1",
      toolName: "write_file",
      arguments: {},
    },
  ];

  const denied = buildTraceTimeline([
    ...base,
    {
      type: "tool_authorization_resolved",
      runId: RUN_A,
      requestId: "auth-1",
      toolCallId: "call-1",
      decision: "deny",
    },
  ]).runs[0].items.filter((item) => item.kind === "tool")[0];
  assert.equal(denied.authorization.status, "denied");
  assert.equal(denied.state, "denied");

  const cancelled = buildTraceTimeline([
    ...base,
    {
      type: "tool_authorization_resolved",
      runId: RUN_A,
      requestId: "auth-1",
      toolCallId: "call-1",
      decision: "timeout",
    },
  ]).runs[0].items.filter((item) => item.kind === "tool")[0];
  assert.equal(cancelled.authorization.status, "cancelled");
  assert.equal(cancelled.state, "cancelled");

  const allowed = buildTraceTimeline([
    ...base,
    toolStart("call-1", "write_file", {}),
    {
      type: "tool_authorization_resolved",
      runId: RUN_A,
      requestId: "auth-1",
      toolCallId: "call-1",
      decision: "allow",
    },
  ]).runs[0].items.filter((item) => item.kind === "tool")[0];
  assert.equal(allowed.authorization.status, "allowed");
  assert.equal(allowed.state, "running");
});

test("rebuilds multiple runs from a replayed buffer and keeps maxRuns", () => {
  const events = [
    { type: "run_started", runId: "run-a" },
    userStart("第一问"),
    runFinished({}),
    { type: "run_started", runId: "run-b" },
    userStart("第二问"),
    runFinished({}),
    { type: "run_started", runId: "run-c" },
    userStart("第三问"),
    runFinished({}),
  ];

  const { runs } = buildTraceTimeline(events, { maxRuns: 2 });
  assert.deepEqual(runs.map((run) => run.runId), ["run-b", "run-c"]);
});
```

- [ ] **Step 2: 运行确认失败**

Run: `node --test tests/web/`
Expected: FAIL（新增 3 个测试失败，既有测试通过）

- [ ] **Step 3: 实现 trace-timeline.js**

3a. 常量与映射（`MAX_TITLE_LENGTH` 旁）：

```js
const SESSION_RUN_ID = "__session__";
const AUTHORIZATION_STATUS_BY_DECISION = {
  allow: "allowed",
  deny: "denied",
  cancel: "cancelled",
  timeout: "cancelled",
  no_subscriber: "denied",
};
```

3b. `applyRunEvent` switch 新增 case（放在 `tool_execution_end` 之后）：

```js
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
```

3c. `createRun` 支持 `isSession` 标记：`function createRun(runId, isSession = false)` → 对象增加 `isSession`。`toPublicRun` 标题逻辑改为：

```js
    title: run.isSession ? "会话事件" : (run.title ?? "(无用户输入)"),
```

3d. `buildTraceTimeline` 主循环改造：把末尾 `ungrouped.push(...)` 替换为合成分组：

```js
    if (!runsById.has(SESSION_RUN_ID)) {
      runsById.set(SESSION_RUN_ID, createRun(SESSION_RUN_ID, true));
      orderedRunIds.push(SESSION_RUN_ID);
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
```

同时删除原 `ungrouped` 数组声明与填充，函数返回值改为 `ungrouped: []`（保持形状兼容）。注意：`__session__` 分组必须始终排在 `orderedRunIds` 尾部——由于它懒创建且 run 可能晚于它出现，返回前做一次稳定排序：`orderedRunIds.sort` 不行（需稳定自定义序）。简单方案：收集时用两个列表 `runOrderIds` 与 `sessionSeen`，返回时 `recentRunIds = [...runOrderIds.slice(-maxRuns), ...(sessionSeen ? [SESSION_RUN_ID] : [])]`，即会话分组固定最末、不占 maxRuns 名额。

3e. `recentRunIds` 计算相应调整（见 3d 说明），`maxRuns` 只作用于真实 run。

- [ ] **Step 4: 运行前端测试**

Run: `node --test tests/web/`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/tau_coding/data/web/trace-timeline.js tests/web/trace-timeline.test.mjs
git commit -m "Group non-run web trace events into a session bucket with auth resolution"
```

---

### Task 5: 前端 UI — 统一时间线接线与渲染防御

**Files:**
- Modify: `src/tau_coding/data/web/index.html`（删除 `#event-timeline` 区块）
- Modify: `src/tau_coding/data/web/app.js`
- Modify: `src/tau_coding/data/web/styles.css`

**Interfaces:**
- Consumes: Task 4 的视图模型；Task 1 的 `replay` 帧。
- Produces: `addTraceEvent(type, detail)` 改为纯状态注入 + 统一渲染；`element()` 忽略非 string/number 文本并 console.warn。

- [ ] **Step 1: index.html 删除 ticker 区块**

删除以下整块（约 246-257 行）：

```html
          <ol class="event-timeline" id="event-timeline">
            <li class="is-done">…server_ready…</li>
            <li class="is-current" id="session-index-event">…</li>
          </ol>
```

- [ ] **Step 2: app.js 渲染防御**

`element()` 改为：

```js
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
```

- [ ] **Step 3: app.js 会话级事件改道**

替换 `addTraceEvent`、`markIndexLoaded`、`markBranchLoaded` 三个函数：

```js
function addTraceEvent(type, detail = "") {
  recordTraceEvent({ type, message: typeof detail === "string" ? detail : "" });
  renderRunTimeline();
  document.querySelector("#trace-state").textContent = type;
}

function markIndexLoaded(count) {
  addTraceEvent("session_index_loaded", `${count} indexed sessions`);
}

function markBranchLoaded(messageCount) {
  addTraceEvent("active_branch_loaded", `${messageCount} visible messages`);
}
```

（`recordTraceEvent` 已有 600 条裁剪逻辑；无 runId 的合成事件天然落入 `__session__` 分组。）

- [ ] **Step 4: app.js 重放帧副作用隔离**

`handleLiveEvent` 中，`if (sessionId !== state.activeSessionId) return;` 之后立即插入：

```js
  if (event.replay) {
    recordTraceEvent(event);
    if (event.type !== "message_update") {
      renderRunTimeline();
    }
    return;
  }
```

- [ ] **Step 5: 清理死代码**

- 全文搜索 `#event-timeline`、`live-event`、`branch-event`：删除 `markBranchLoaded` 旧 DOM 版残留引用及 `document.querySelector("#event-timeline")` 相关行；
- 确认 `renderNoSessionSelected`、`connectEventStream` 等处无对已删节点的引用。

- [ ] **Step 6: styles.css 清理与补充**

- 删除 `.event-timeline` 相关规则块（搜索 `.event-timeline`）；
- 追加会话分组样式（沿用现有视觉语言）：

```css
.run-group.is-session .run-head-text strong {
  color: var(--text-muted, #9aa4b2);
  font-weight: 500;
}

.run-item.kind-session p {
  margin: 0;
  font-size: 12px;
  color: var(--text-muted, #9aa4b2);
  overflow-wrap: anywhere;
}
```

（若存在主题 CSS 变量则以实际变量名为准，先查看文件内 `--` 变量定义再取值。）

- [ ] **Step 7: 构建验证与手动冒烟**

Run: `node --test tests/web/ && uv run pytest tests/test_web.py -q`
Expected: PASS

手动冒烟（可选但推荐记录到 PR 描述）：

```bash
uv run tau-web --no-open
# 打开 http://127.0.0.1:8080：右侧仅一棵时间线树；发送一条触发工具的消息；
# F5 刷新后 run 历史（含工具参数/结果节点）完整恢复；底部无滚动 ticker。
```

- [ ] **Step 8: Commit**

```bash
git add src/tau_coding/data/web/index.html src/tau_coding/data/web/app.js src/tau_coding/data/web/styles.css
git commit -m "Unify Tau Web trace panel into a single persistent timeline"
```

---

### Task 6: 静态资源 cache-busting

**Files:**
- Modify: `src/tau_coding/web.py`
- Test: `tests/test_web.py`

**Interfaces:**
- Produces: `TauWebServer.asset_versions: dict[str, str]`（文件名 → SHA-256 前 8 位）；`GET /` 返回的 index.html 中 `<script src="/x.js">` 被改写为 `/x.js?v=<hash>`。

- [ ] **Step 1: 写失败测试**

```python
def test_index_html_scripts_are_cache_busted(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    server = create_web_server(host="127.0.0.1", port=0, session_manager=manager)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    connection = HTTPConnection(host, port, timeout=2)
    try:
        status, body = _get_html(connection, "/")
        assert status == 200
        for name in ("trace-timeline.js", "session-actions.js", "app.js"):
            pattern = f'src="/{name}?v='
            assert pattern in body, f"missing cache bust for {name}"
        version = server.asset_versions["app.js"]
        assert len(version) == 8
        assert f'src="/app.js?v={version}"' in body
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
```

辅助函数（放在其他 `_get_*` 辅助附近）：

```python
def _get_html(connection: HTTPConnection, path: str) -> tuple[int, str]:
    connection.request("GET", path)
    response = connection.getresponse()
    return response.status, response.read().decode("utf-8")
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_web.py::test_index_html_scripts_are_cache_busted -v`
Expected: FAIL（body 中无 `?v=`）

- [ ] **Step 3: 实现**

3a. `web.py` 顶部 import：`import hashlib`。

3b. 模块级函数：

```python
_SCRIPT_ASSETS = ("trace-timeline.js", "session-actions.js", "app.js")


def _asset_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for name in (*_SCRIPT_ASSETS, "index.html"):
        digest = hashlib.sha256()
        digest.update(files("tau_coding").joinpath("data", "web", name).read_bytes())
        versions[name] = digest.hexdigest()[:8]
    return versions


def _cache_busted_index_html(body: bytes, versions: dict[str, str]) -> bytes:
    html = body.decode("utf-8")
    for name in _SCRIPT_ASSETS:
        html = html.replace(
            f'src="/{name}"',
            f'src="/{name}?v={versions[name]}"',
        )
    return html.encode("utf-8")
```

（`files` 已从 `importlib.resources` 导入——确认顶部已有；没有则补 `from importlib import resources` 并统一用现有别名。）

3c. `TauWebServer.__init__` 末尾：

```python
        self.asset_versions = _asset_versions()
```

3d. `do_GET` 静态分支，`body = files(...).read_bytes()` 之后：

```python
        if asset_name == "index.html":
            body = _cache_busted_index_html(body, self.server.asset_versions)
```

- [ ] **Step 4: 运行后端全量测试**

Run: `uv run pytest tests/test_web.py -v`
Expected: PASS（CSP `script-src 'self'` 不受限于 query string；`GET /app.js?v=x` 走原静态分支正常返回）

- [ ] **Step 5: Commit**

```bash
git add src/tau_coding/web.py tests/test_web.py
git commit -m "Cache-bust Tau Web scripts with content hashes"
```

---

### Task 7: 文档与收尾验证

**Files:**
- Create: `dev-notes/web-trace-persistence.md`
- Modify: `website/content/guides/web.md`
- Modify: `docs/PRD-web-trace.md`
- Modify: `docs/tau-web-todo.md`（如有对应条目则勾选）

- [ ] **Step 1: dev-notes（what/why/how-to-verify）**

创建 `dev-notes/web-trace-persistence.md`，结构仿照 `dev-notes/web-per-run-trace-timeline.md`：

- What was added：环缓冲 + SSE 重放（`replay` 帧）、`.webtrace.jsonl` 落盘/压缩/回填、`tool_authorization_resolved` 闭环、右侧统一时间线（`__session__` 分组）、cache-busting、`element()` 渲染防御；
- Why：spec 问题陈述摘要 + 指向 `dev-notes/web-trace-persistence-design.md`；
- Design decisions：引用 spec 的 4 条 ADR；
- Testing seams：列出本计划中的测试命令；
- How to verify manually：发消息 → 刷新恢复 → 重启 tau-web 再刷新仍恢复 → 断网自动拒绝后重连可见 resolved。

- [ ] **Step 2: 更新用户指南与 PRD**

- `website/content/guides/web.md`：在 Trace Workbench 相关小节补充「右侧时间线为单一树；最近约 600 条事件在页面刷新后自动恢复；历史落盘于会话目录 `<id>.webtrace.jsonl`，重启后仍可回看；工具授权决定会广播给所有打开的窗口」；
- `docs/PRD-web-trace.md`：文末追加「Persistence Extension (2026-08-24)」小节，链接 spec 与 dev-notes。

- [ ] **Step 3: 全量验证**

Run:

```bash
uv run pytest tests/test_web.py -q
node --test tests/web/
uv run pytest -q   # 全仓回归
```

Expected: 全部 PASS。

- [ ] **Step 4: Commit**

```bash
git add dev-notes/web-trace-persistence.md website/content/guides/web.md docs/PRD-web-trace.md docs/tau-web-todo.md
git commit -m "Document Tau Web trace persistence and unified timeline"
```

---

## Self-Review 记录

- Spec coverage：G1→Task 4/5；G2→Task 1(+5 重放消费)；G3→Task 3；G4→Task 2(+4 回填)；G5→Task 5(element)/6(cache-bust)。文档义务→Task 7。无遗漏。
- Placeholder scan：无 TBD/TODO；所有代码步骤给出可粘贴实现。
- Type consistency：`_resolve_pending_tool_authorizations` 新签名为 `(self, session_id, slot, decision)`，三个调用点均已列明；`trace_buffer`/`_record_trace_event`/`_webtrace_path` 名称在 Task 1/3 间一致；JS 侧 `SESSION_RUN_ID`/`AUTHORIZATION_STATUS_BY_DECISION` 与测试断言一致。
- 计划期 spec 修正一处：入缓冲范围由「经 `_trace_payload()` 包装的事件」放宽为「凡经 `_publish()` 转发的会话事件」，否则 run_started/run_finished 无法重放重建分组（Task 1 Step 4 同步修订 spec）。
