"""Local A-theme web workspace for Tau coding sessions."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import queue
import sys
import threading
import time
import webbrowser
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import suppress
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import uuid4

from tau_agent.events import MessageEndEvent, MessageStartEvent, TurnEndEvent
from tau_agent.messages import (
    AssistantMessage,
    ToolCall,
    ToolResultMessage,
    current_timestamp_ms,
    message_text,
)
from tau_agent.session import (
    BranchSummaryEntry,
    CompactionEntry,
    LeafEntry,
    MessageEntry,
    SessionEntry,
    SessionTreeError,
    ThinkingLevelChangeEntry,
    entries_from_json_lines,
    path_to_entry,
)
from tau_coding.credentials import FileCredentialStore, credentials_path
from tau_coding.events import CodingSessionEvent
from tau_coding.extensions.api import NullUiBridge
from tau_coding.provider_config import (
    MAX_TEMPERATURE,
    MIN_TEMPERATURE,
    OpenAICompatibleProviderConfig,
    ProviderConfigError,
    ProviderSelection,
    ProviderSettings,
    compatible_temperature,
    load_provider_settings,
    normalize_temperature,
    provider_has_usable_api_key,
    provider_supports_temperature,
    provider_thinking_levels,
    provider_thinking_unavailable_reason,
    resolve_provider_selection,
    resolve_startup_thinking_level,
    save_provider_settings,
    set_openai_compatible_provider_connection,
)
from tau_coding.provider_runtime import ClosableModelProvider, create_model_provider
from tau_coding.resources import TauResourcePaths
from tau_coding.session import (
    CodingSession,
    CodingSessionConfig,
    ModelChoice,
    StreamingBehavior,
    jsonl_session_storage,
)
from tau_coding.session_export import (
    SessionExportError,
    normalize_export_format,
    render_session_html,
    render_session_jsonl,
)
from tau_coding.session_manager import CodingSessionRecord, SessionManager
from tau_coding.shell_config import load_shell_settings
from tau_coding.version import current_version

logger = logging.getLogger(__name__)

DEFAULT_WEB_HOST = "127.0.0.1"
DEFAULT_WEB_PORT = 8080
_MAX_MESSAGE_TEXT = 100_000
_MAX_REQUEST_BYTES = 256 * 1024
_RUNTIME_CALL_TIMEOUT_SECONDS = 15.0
_SSE_HEARTBEAT_SECONDS = 15.0
_SSE_QUEUE_ITEMS = 2_048
_TOOL_AUTHORIZATION_TIMEOUT_SECONDS = 120.0
_WEB_PROVIDER_NAME = "tau-web"
_WEB_IMMEDIATE_COMMANDS = frozenset(
    {
        "hotkeys",
        "session",
        "system",
    }
)
_ASSET_CONTENT_TYPES = {
    "index.html": "text/html; charset=utf-8",
    "styles.css": "text/css; charset=utf-8",
    "trace-timeline.js": "text/javascript; charset=utf-8",
    "session-actions.js": "text/javascript; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "favicon.svg": "image/svg+xml",
}
TRACE_BUFFER_LIMIT = 600
TRACE_FILE_COMPACT_LINES = 1200
TRACE_FILE_KEEP_LINES = 600
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


WebSessionLoader = Callable[
    [CodingSessionRecord, SessionManager],
    Awaitable["WebSessionHandle"],
]
RunStatus = Literal["completed", "failed", "cancelled"]
ToolAuthorizationDecision = Literal["allow", "deny", "cancel"]


@dataclass(slots=True)
class WebSessionHandle:
    """Coding session and provider resources owned by the Web adapter."""

    session: CodingSession
    provider: ClosableModelProvider | None = None

    async def aclose(self) -> None:
        """Close the coding environment and its startup provider."""
        try:
            await self.session.aclose()
        finally:
            if self.provider is not None:
                await self.provider.aclose()


@dataclass(frozen=True, slots=True)
class _StreamItem:
    sequence: int
    payload: dict[str, object]


@dataclass(frozen=True, slots=True)
class WebSessionExport:
    """In-memory session artifact returned as an HTTP download."""

    body: bytes
    content_type: str
    filename: str


@dataclass(frozen=True, slots=True)
class WebProviderUpdate:
    """Editable connection values accepted from the local Web frontend."""

    base_url: str
    api_key: str | None
    model: str


@dataclass(slots=True)
class _PendingToolAuthorization:
    request_id: str
    call: ToolCall
    decision: asyncio.Future[ToolAuthorizationDecision]


# Read-only knowledge and preparation steps never prompt a browser dialog
# (decision 16); only data_query_execute is host-confirmed.
_DATAQUERY_AUTO_APPROVED_TOOLS = frozenset(
    {"data_knowledge_search", "data_knowledge_read", "data_query_prepare"}
)


class _WebConfirmingUiBridge(NullUiBridge):
    """Web host UI bridge: the browser dialog is the single authorizer.

    Extension dialogs resolve to their no-op defaults except ``confirm``,
    which returns True so an extension's inline confirmation never double-gates
    after the host's ``before_tool_call`` dialog (decision 16).
    """

    async def confirm(self, title: str, message: str, *, timeout: float | None = None) -> bool:
        del title, message, timeout
        return True


@dataclass(slots=True)
class _WebSessionSlot:
    handle: WebSessionHandle | None = None
    run_task: asyncio.Task[None] | None = None
    run_id: str | None = None
    run_kind: Literal["prompt", "compact"] | None = None
    cancel_requested: bool = False
    subscribers: dict[int, queue.Queue[_StreamItem | None]] = field(default_factory=dict)
    next_subscriber_id: int = 1
    next_sequence: int = 1
    last_queue_state: tuple[tuple[str, ...], tuple[str, ...]] = ((), ())
    pending_tool_authorizations: dict[str, _PendingToolAuthorization] = field(default_factory=dict)
    run_started_ms: int | None = None
    trace_event_count: int = 0
    trace_turn_count: int = 0
    trace_buffer: deque[dict[str, object]] = field(
        default_factory=lambda: deque(maxlen=TRACE_BUFFER_LIMIT)
    )
    trace_file_lines: int = 0
    trace_backfilled: bool = False


class WebSessionBusyError(RuntimeError):
    """Raised when a second turn is submitted while a session is running."""


class WebRuntimeClosedError(RuntimeError):
    """Raised after the Web runtime has begun shutting down."""


class WebSessionValidationError(ValueError):
    """Raised when a Web session mutation contains invalid user input."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class TauWebRuntime:
    """Own CodingSession instances on one background asyncio event loop."""

    def __init__(self, manager: SessionManager, session_loader: WebSessionLoader) -> None:
        self._manager = manager
        self._session_loader = session_loader
        self._slots: dict[str, _WebSessionSlot] = {}
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._closed = False
        self._close_lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="tau-web-runtime",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait()

    def subscribe(
        self,
        session_id: str,
    ) -> tuple[int, queue.Queue[_StreamItem | None]]:
        """Subscribe a request thread to one session's live event stream."""
        return self._call(self._subscribe(session_id))

    def unsubscribe(self, session_id: str, subscriber_id: int) -> None:
        """Remove a live event subscriber without blocking request cleanup."""
        if self._closed:
            return
        self._loop.call_soon_threadsafe(self._unsubscribe, session_id, subscriber_id)

    def submit(
        self,
        session_id: str,
        message: str,
        *,
        streaming_behavior: StreamingBehavior | None = None,
    ) -> dict[str, object]:
        """Dispatch a Web command or start one coding-session turn."""
        return self._call(
            self._submit(
                session_id,
                message,
                streaming_behavior=streaming_behavior,
            )
        )

    def cancel(self, session_id: str) -> bool:
        """Request cancellation of the active run, returning whether one existed."""
        return self._call(self._cancel(session_id))

    def clear_queue(self, session_id: str) -> dict[str, object]:
        """Clear steering and follow-up messages waiting in one session."""
        return self._call(self._clear_queue(session_id))

    def respond_tool_authorization(
        self,
        session_id: str,
        request_id: str,
        decision: ToolAuthorizationDecision,
    ) -> bool:
        """Resolve one pending browser tool-authorization request."""
        return self._call(self._respond_tool_authorization(session_id, request_id, decision))

    def session_list(self) -> dict[str, object]:
        """Read the session index on the runtime thread."""
        return self._call(self._session_list())

    def session_detail(self, session_id: str) -> dict[str, object] | None:
        """Read one active branch on the runtime thread."""
        return self._call(self._session_detail(session_id))

    def session_options(self) -> dict[str, object]:
        """Read the configured project, provider, and model choices."""
        return self._call(self._session_options())

    def dataquery_config(self) -> dict[str, object]:
        """Read the data-query configuration (no secret values)."""
        return self._call(self._dataquery_config())

    def dataquery_update(self, payload: Mapping[str, object]) -> dict[str, object]:
        """Apply a data-query configuration update."""
        return self._call(self._dataquery_update(payload))

    def dataquery_test_connection(self) -> dict[str, object]:
        """Probe DWS and SAG connectivity on the runtime thread."""
        return self._call(self._dataquery_test_connection())

    def create_session(
        self,
        *,
        cwd: str,
        provider_name: str,
        model: str,
        thinking_level: str | None = None,
        temperature: float | None = None,
    ) -> dict[str, object]:
        """Create and index a session from browser-selected options."""
        return self._call(
            self._create_session(
                cwd=cwd,
                provider_name=provider_name,
                model=model,
                thinking_level=thinking_level,
                temperature=temperature,
            )
        )

    def update_provider(
        self,
        update: WebProviderUpdate,
    ) -> dict[str, object]:
        """Update the single OpenAI-compatible connection exposed by Tau Web."""
        return self._call(self._update_provider(update))

    def rename_session(self, session_id: str, title: str) -> dict[str, object]:
        """Rename one indexed session."""
        return self._call(self._rename_session(session_id, title))

    def update_session_configuration(
        self,
        session_id: str,
        *,
        provider_name: str,
        model: str,
        thinking_level: str | None,
    ) -> dict[str, object]:
        """Switch one idle session's provider, model, and thinking level."""
        return self._call(
            self._update_session_configuration(
                session_id,
                provider_name=provider_name,
                model=model,
                thinking_level=thinking_level,
            )
        )

    def delete_session(self, session_id: str) -> None:
        """Delete one idle session and close its owned resources."""
        self._call(self._delete_session(session_id))

    def export_session(self, session_id: str, format: str) -> WebSessionExport:
        """Render a complete session artifact for browser download."""
        return self._call(self._export_session(session_id, format))

    def close(self) -> None:
        """Stop active runs, close providers, and terminate the runtime loop."""
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        future = asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop)
        with suppress(FutureTimeoutError, RuntimeError):
            future.result(timeout=_RUNTIME_CALL_TIMEOUT_SECONDS)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=_RUNTIME_CALL_TIMEOUT_SECONDS)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()
        self._loop.close()

    def _call[T](self, awaitable: Coroutine[Any, Any, T]) -> T:
        if self._closed:
            awaitable.close()
            raise WebRuntimeClosedError("Tau Web runtime is closed")
        future = asyncio.run_coroutine_threadsafe(awaitable, self._loop)
        try:
            return future.result(timeout=_RUNTIME_CALL_TIMEOUT_SECONDS)
        except FutureTimeoutError:
            if not future.cancel():
                return future.result()
            raise

    def _require_session(self, session_id: str) -> CodingSessionRecord:
        record = self._manager.get_session(session_id)
        if record is None:
            raise KeyError(session_id)
        return record

    async def _subscribe(
        self,
        session_id: str,
    ) -> tuple[int, queue.Queue[_StreamItem | None]]:
        record = self._require_session(session_id)
        slot = self._slots.setdefault(session_id, _WebSessionSlot())
        self._backfill_trace_from_disk(record, slot)
        subscriber_id = slot.next_subscriber_id
        slot.next_subscriber_id += 1
        subscriber: queue.Queue[_StreamItem | None] = queue.Queue(maxsize=_SSE_QUEUE_ITEMS)
        slot.subscribers[subscriber_id] = subscriber
        subscriber.put(
            self._stream_item(
                slot,
                {
                    "type": "web_connected",
                    "sessionId": session_id,
                    "running": slot.run_task is not None and not slot.run_task.done(),
                },
            )
        )
        for buffered in list(slot.trace_buffer):
            subscriber.put(self._stream_item(slot, {**buffered, "replay": True}))
        if slot.run_task is not None and not slot.run_task.done() and slot.run_id is not None:
            subscriber.put(
                self._stream_item(
                    slot,
                    {
                        "type": "run_summary",
                        "sessionId": session_id,
                        "runId": slot.run_id,
                        "status": "running",
                        **_run_trace_snapshot(slot),
                    },
                )
            )
        if slot.handle is not None:
            subscriber.put(
                self._stream_item(
                    slot,
                    _queue_event_payload(slot.handle.session),
                )
            )
        for pending in slot.pending_tool_authorizations.values():
            if pending.decision.done():
                continue
            subscriber.put(
                self._stream_item(
                    slot,
                    _trace_payload(slot, _tool_authorization_event_payload(pending)),
                )
            )
        return subscriber_id, subscriber

    def _unsubscribe(self, session_id: str, subscriber_id: int) -> None:
        slot = self._slots.get(session_id)
        if slot is not None:
            slot.subscribers.pop(subscriber_id, None)
            if not slot.subscribers:
                self._resolve_pending_tool_authorizations(session_id, slot, "deny")

    async def _submit(
        self,
        session_id: str,
        message: str,
        *,
        streaming_behavior: StreamingBehavior | None,
    ) -> dict[str, object]:
        record = self._require_session(session_id)
        slot = self._slots.setdefault(record.id, _WebSessionSlot())
        handle = await self._ensure_handle(record, slot)
        command_name = _slash_command_name(message)
        if command_name == "help":
            return {
                "status": "command",
                "command": message,
                "message": _web_help_message(handle.session),
            }
        if command_name in _WEB_IMMEDIATE_COMMANDS:
            command = handle.session.handle_command(message)
            if command.handled:
                return {
                    "status": "command",
                    "command": message,
                    "message": command.message or "",
                }
        if slot.run_task is not None and not slot.run_task.done():
            if streaming_behavior is None or slot.run_kind != "prompt" or command_name == "compact":
                raise WebSessionBusyError("Tau is already running in this session")
            async for _event in handle.session.prompt(
                message,
                streaming_behavior=streaming_behavior,
            ):
                pass
            self._publish_queue_update(slot, record=record)
            return {
                "status": "queued",
                "behavior": streaming_behavior,
                "queue": _queue_payload(handle.session),
            }

        run_id = uuid4().hex
        slot.run_id = run_id
        slot.cancel_requested = False
        slot.run_started_ms = current_timestamp_ms()
        slot.trace_event_count = 0
        slot.trace_turn_count = 0
        self._publish(
            slot,
            {
                "type": "run_started",
                "sessionId": record.id,
                "runId": run_id,
            },
            record=record,
        )
        if command_name == "compact":
            command = handle.session.handle_command(message)
            if command.handled and command.compact_summary is not None:
                slot.run_kind = "compact"
                slot.run_task = asyncio.create_task(
                    self._run_compaction(
                        record.id,
                        slot,
                        run_id,
                        command.compact_summary,
                    ),
                    name=f"tau-web-compact-{run_id}",
                )
                return {"status": "accepted", "runId": run_id}

        slot.run_kind = "prompt"
        slot.run_task = asyncio.create_task(
            self._run_prompt(record.id, slot, run_id, message),
            name=f"tau-web-run-{run_id}",
        )
        return {"status": "accepted", "runId": run_id}

    async def _ensure_handle(
        self,
        record: CodingSessionRecord,
        slot: _WebSessionSlot,
    ) -> WebSessionHandle:
        if slot.handle is not None:
            return slot.handle
        handle = await self._session_loader(record, self._manager)
        try:
            await handle.session.emit_pending_session_start()
        except BaseException:
            await handle.aclose()
            raise
        handle.session.set_before_tool_call(
            lambda call: self._authorize_tool_call(record.id, slot, call)
        )
        # The browser authorization dialog is the single gate in Web (decision
        # 16); the extension's inline confirm must pass after the host approves.
        handle.session.extension_runtime.set_ui_bridge(_WebConfirmingUiBridge())
        slot.handle = handle
        return handle

    async def _run_prompt(
        self,
        session_id: str,
        slot: _WebSessionSlot,
        run_id: str,
        message: str,
    ) -> None:
        assert slot.handle is not None
        status: RunStatus = "completed"
        record = self._require_session(session_id)
        try:
            async for event in slot.handle.session.prompt(message):
                payload = _coding_event_payload(event)
                payload["runId"] = run_id
                payload["timestamp"] = current_timestamp_ms()
                self._publish(slot, payload, record=record)
                slot.trace_event_count += 1
                if isinstance(event, TurnEndEvent):
                    slot.trace_turn_count += 1
                if isinstance(event, MessageStartEvent):
                    self._publish_queue_update(slot, only_if_changed=True, record=record)
                if (
                    isinstance(event, MessageEndEvent)
                    and isinstance(event.message, AssistantMessage)
                    and event.message.stop_reason in {"error", "aborted"}
                ):
                    status = (
                        "cancelled"
                        if event.message.stop_reason == "aborted" or slot.cancel_requested
                        else "failed"
                    )
                    if status == "failed":
                        self._publish(
                            slot,
                            {
                                "type": "run_error",
                                "sessionId": session_id,
                                "runId": run_id,
                                "message": (
                                    event.message.error_message or "The provider run failed"
                                ),
                            },
                            record=record,
                        )
        except Exception as exc:
            status = "failed"
            self._publish(
                slot,
                {
                    "type": "run_error",
                    "sessionId": session_id,
                    "runId": run_id,
                    "message": str(exc) or type(exc).__name__,
                },
                record=record,
            )
        finally:
            if slot.cancel_requested and status == "completed":
                status = "cancelled"
            self._finish_run(session_id, slot, run_id, status)

    async def _run_compaction(
        self,
        session_id: str,
        slot: _WebSessionSlot,
        run_id: str,
        instructions: str,
    ) -> None:
        assert slot.handle is not None
        status: RunStatus = "completed"
        record = self._require_session(session_id)
        try:
            message = await slot.handle.session.compact(instructions or None)
            self._publish(
                slot,
                {
                    "type": "command_result",
                    "sessionId": session_id,
                    "runId": run_id,
                    "command": "/compact",
                    "message": message,
                },
                record=record,
            )
        except asyncio.CancelledError:
            status = "cancelled"
        except Exception as exc:
            status = "failed"
            self._publish(
                slot,
                {
                    "type": "run_error",
                    "sessionId": session_id,
                    "runId": run_id,
                    "message": str(exc) or type(exc).__name__,
                },
                record=record,
            )
        finally:
            self._finish_run(session_id, slot, run_id, status)

    def _finish_run(
        self,
        session_id: str,
        slot: _WebSessionSlot,
        run_id: str,
        status: RunStatus,
    ) -> None:
        record = self._require_session(session_id)
        snapshot = _run_trace_snapshot(slot)
        self._publish(
            slot,
            {
                "type": "run_finished",
                "sessionId": session_id,
                "runId": run_id,
                "status": status,
                "timestamp": current_timestamp_ms(),
                "turnCount": snapshot["turnCount"],
                "eventCount": snapshot["eventCount"],
                "durationMs": snapshot["elapsedMs"],
            },
            record=record,
        )
        slot.run_task = None
        slot.run_id = None
        slot.run_kind = None
        slot.cancel_requested = False
        slot.run_started_ms = None
        slot.trace_event_count = 0
        slot.trace_turn_count = 0
        self._publish_queue_update(slot, only_if_changed=True, record=record)

    async def _cancel(self, session_id: str) -> bool:
        record = self._require_session(session_id)
        slot = self._slots.get(session_id)
        if slot is None or slot.handle is None or slot.run_task is None or slot.run_task.done():
            return False
        slot.cancel_requested = True
        self._resolve_pending_tool_authorizations(session_id, slot, "cancel", record=record)
        slot.handle.session.cancel()
        if slot.run_kind == "compact":
            slot.run_task.cancel()
        self._publish(
            slot,
            {
                "type": "cancel_requested",
                "sessionId": session_id,
                "runId": slot.run_id,
            },
            record=record,
        )
        return True

    async def _clear_queue(self, session_id: str) -> dict[str, object]:
        record = self._require_session(session_id)
        slot = self._slots.setdefault(session_id, _WebSessionSlot())
        handle = await self._ensure_handle(record, slot)
        handle.session.clear_queued_messages()
        self._publish_queue_update(slot, record=record)
        return {
            "status": "cleared",
            "queue": _queue_payload(handle.session),
        }

    async def _authorize_tool_call(
        self,
        session_id: str,
        slot: _WebSessionSlot,
        call: ToolCall,
    ) -> tuple[bool, str | None]:
        record = self._require_session(session_id)
        if not slot.subscribers:
            return True, "Tool execution denied because no Tau Web client is connected"

        # Read-only knowledge and plan-preparation tools run without a browser
        # confirmation; only execute prompts the dialog (decision 16).
        if call.name in _DATAQUERY_AUTO_APPROVED_TOOLS:
            return False, None

        request_id = uuid4().hex
        pending = _PendingToolAuthorization(
            request_id=request_id,
            call=call,
            decision=self._loop.create_future(),
        )
        slot.pending_tool_authorizations[request_id] = pending
        payload = _tool_authorization_event_payload(pending)
        if call.name == "data_query_execute" and slot.handle is not None:
            view = slot.handle.session.extension_runtime.authorization_view(
                call.name, call.arguments
            )
            if view:
                payload = {**payload, "dataQuery": view}
        self._publish(slot, _trace_payload(slot, payload), record=record)
        try:
            decision = await asyncio.wait_for(
                pending.decision,
                timeout=_TOOL_AUTHORIZATION_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            self._publish(
                slot,
                _trace_payload(
                    slot,
                    _tool_authorization_resolved_payload(request_id, call.id, "timeout"),
                ),
                record=record,
            )
            return True, "Tool execution denied because authorization timed out"
        finally:
            slot.pending_tool_authorizations.pop(request_id, None)

        self._publish(
            slot,
            _trace_payload(
                slot,
                _tool_authorization_resolved_payload(request_id, call.id, decision),
            ),
            record=record,
        )

        if decision == "allow":
            return False, None
        if decision == "cancel":
            slot.cancel_requested = True
            if slot.handle is not None:
                slot.handle.session.cancel()
            return True, "Tool execution cancelled by the user"
        return True, "Tool execution denied by the user"

    async def _respond_tool_authorization(
        self,
        session_id: str,
        request_id: str,
        decision: ToolAuthorizationDecision,
    ) -> bool:
        self._require_session(session_id)
        slot = self._slots.get(session_id)
        if slot is None:
            return False
        pending = slot.pending_tool_authorizations.get(request_id)
        if pending is None or pending.decision.done():
            return False
        pending.decision.set_result(decision)
        return True

    def _resolve_pending_tool_authorizations(
        self,
        session_id: str,
        slot: _WebSessionSlot,
        decision: ToolAuthorizationDecision,
        *,
        record: CodingSessionRecord | None = None,
    ) -> None:
        published = "no_subscriber" if decision == "deny" else decision
        if record is None:
            record = self._manager.get_session(session_id)
        for pending in slot.pending_tool_authorizations.values():
            if pending.decision.done():
                continue
            pending.decision.set_result(decision)
            self._publish(
                slot,
                _trace_payload(
                    slot,
                    _tool_authorization_resolved_payload(
                        pending.request_id,
                        pending.call.id,
                        published,
                    ),
                ),
                record=record,
            )

    async def _session_list(self) -> dict[str, object]:
        return session_list_payload(self._manager)

    async def _dataquery_config(self) -> dict[str, object]:
        """Return the read-only data-query configuration payload (decision 13)."""
        from tau_coding.dataquery.config import config_api_payload

        return config_api_payload(self._manager.paths)

    async def _dataquery_update(self, payload: Mapping[str, object]) -> dict[str, object]:
        """Apply a data-query configuration update and return the read payload."""
        from tau_coding.dataquery.config import (
            DataQueryConfigError,
            apply_config_api_update,
        )

        try:
            return apply_config_api_update(payload, self._manager.paths)
        except DataQueryConfigError as exc:
            raise WebSessionValidationError("dataquery_config_invalid", str(exc)) from exc

    async def _dataquery_test_connection(self) -> dict[str, object]:
        """Probe DWS and SAG connectivity with sanitized results (decision 15)."""
        from tau_coding.dataquery.config import resolve_data_query_config

        resolved = resolve_data_query_config(self._manager.paths)
        dws: dict[str, object] = {"ok": False, "elapsedMs": 0, "error": None}
        planner: dict[str, object] = {
            "mode": resolved.planning_mode,
            "ok": False,
            "elapsedMs": 0,
            "error": None,
        }
        citation_expansion: dict[str, object] = {
            "configured": False,
            "ok": False,
            "elapsedMs": 0,
            "error": None,
        }

        if resolved.complete:
            from tau_coding.dataquery.backends.dws import DwsPostgresQueryBackend
            from tau_coding.dataquery.backends.sag import SagMcpKnowledgeBackend

            backend = DwsPostgresQueryBackend(
                host=resolved.host,
                port=resolved.port,
                database=resolved.database,
                username=resolved.username,
                password=resolved.secrets.dws_password or "",
                sslmode=resolved.sslmode,
                connect_timeout=resolved.dws_connect_timeout,
                probe_query=resolved.dws_probe_query,
            )
            elapsed, error = await backend.test_connection()
            await backend.close()
            dws = {"ok": error is None, "elapsedMs": elapsed, "error": error}

            if resolved.planning_mode == "legacy":
                knowledge = SagMcpKnowledgeBackend(
                    endpoint=resolved.sag_endpoint,
                    token=resolved.secrets.sag_token or "",
                    source_id=resolved.sag_source_id,
                    search_tool=resolved.sag_search_tool,
                    read_tool=resolved.sag_read_tool,
                    arg_query=resolved.sag_arg_query,
                    arg_source=resolved.sag_arg_source,
                    arg_document=resolved.sag_arg_document,
                    timeout_seconds=resolved.sag_rpc_timeout_seconds,
                    protocol_version=resolved.sag_protocol_version,
                    probe_query=resolved.sag_probe_query,
                    search_summary_max_bytes=resolved.sag_search_summary_max_bytes,
                )
                started = time.monotonic()
                try:
                    await knowledge.test()
                    elapsed_ms = int((time.monotonic() - started) * 1000)
                    planner = {
                        "mode": "legacy",
                        "ok": True,
                        "elapsedMs": elapsed_ms,
                        "error": None,
                    }
                    citation_expansion = {
                        "configured": True,
                        "ok": True,
                        "elapsedMs": elapsed_ms,
                        "error": None,
                    }
                except Exception as exc:  # noqa: BLE001 - sanitized probe result
                    elapsed_ms = int((time.monotonic() - started) * 1000)
                    error = str(exc) or type(exc).__name__
                    planner = {
                        "mode": "legacy",
                        "ok": False,
                        "elapsedMs": elapsed_ms,
                        "error": error,
                    }
                    citation_expansion = {
                        "configured": True,
                        "ok": False,
                        "elapsedMs": elapsed_ms,
                        "error": error,
                    }
                finally:
                    await knowledge.close()
            else:
                from tau_coding.dataquery.backends.sag_agent import SagAgentSqlPlanner

                agent_planner = SagAgentSqlPlanner(
                    origin=resolved.sag_agent_origin,
                    agent_id=resolved.sag_agent_id,
                    token=resolved.secrets.sag_token or "",
                    timeout_seconds=resolved.sag_agent_timeout_seconds,
                    max_response_bytes=resolved.sag_planner_transcript_max_bytes,
                )
                started = time.monotonic()
                try:
                    await agent_planner.test()
                    planner = {
                        "mode": "agent",
                        "ok": True,
                        "elapsedMs": int((time.monotonic() - started) * 1000),
                        "error": None,
                    }
                except Exception as exc:  # noqa: BLE001 - adapter returns bounded errors
                    planner = {
                        "mode": "agent",
                        "ok": False,
                        "elapsedMs": int((time.monotonic() - started) * 1000),
                        "error": str(exc) or type(exc).__name__,
                    }
                finally:
                    await agent_planner.close()

                if resolved.sag_endpoint:
                    knowledge = SagMcpKnowledgeBackend(
                        endpoint=resolved.sag_endpoint,
                        token=resolved.secrets.sag_token or "",
                        source_id=resolved.sag_source_id,
                        search_tool=resolved.sag_search_tool,
                        read_tool=resolved.sag_read_tool,
                        arg_query=resolved.sag_arg_query,
                        arg_source=resolved.sag_arg_source,
                        arg_document=resolved.sag_arg_document,
                        timeout_seconds=resolved.sag_rpc_timeout_seconds,
                        protocol_version=resolved.sag_protocol_version,
                        probe_query=resolved.sag_probe_query,
                        search_summary_max_bytes=resolved.sag_search_summary_max_bytes,
                    )
                    started = time.monotonic()
                    try:
                        await knowledge.test()
                        citation_expansion = {
                            "configured": True,
                            "ok": True,
                            "elapsedMs": int((time.monotonic() - started) * 1000),
                            "error": None,
                        }
                    except Exception as exc:  # noqa: BLE001 - bounded probe result
                        citation_expansion = {
                            "configured": True,
                            "ok": False,
                            "elapsedMs": int((time.monotonic() - started) * 1000),
                            "error": str(exc) or type(exc).__name__,
                        }
                    finally:
                        await knowledge.close()
                else:
                    citation_expansion["error"] = "not configured"
        else:
            missing: list[str] = []
            if not resolved.username:
                missing.append("dws username")
            if not resolved.secrets.dws_password:
                missing.append("dws password")
            if not resolved.secrets.sag_token:
                missing.append("sag token")
            dws["error"] = "not configured"
            planner["error"] = "not configured"
            citation_expansion["error"] = "not configured"
            dws["missing"] = missing

        return {
            "dws": dws,
            "planner": planner,
            "citationExpansion": citation_expansion,
            "planningMode": resolved.planning_mode,
            "configurationDiagnostics": list(resolved.configuration_diagnostics),
            "complete": resolved.complete,
        }

    async def _session_detail(self, session_id: str) -> dict[str, object] | None:
        payload = session_detail_payload(self._manager, session_id)
        if payload is None:
            return None
        record = self._require_session(session_id)
        slot = self._slots.get(session_id)
        payload["configuration"] = (
            _runtime_configuration_payload(slot.handle.session, self._manager)
            if slot is not None and slot.handle is not None
            else _stored_configuration_payload(record, self._manager)
        )
        return payload

    async def _session_options(self) -> dict[str, object]:
        return session_options_payload(self._manager)

    async def _update_provider(
        self,
        update: WebProviderUpdate,
    ) -> dict[str, object]:
        normalized_base_url = _normalize_web_provider_base_url(update.base_url)
        normalized_model = _normalize_web_provider_model(update.model)
        settings = load_provider_settings(self._manager.paths)
        source_provider = _select_web_provider_connection(settings, self._manager)
        updated_settings = set_openai_compatible_provider_connection(
            settings,
            provider_name=_WEB_PROVIDER_NAME,
            source_provider_name=source_provider.name,
            base_url=normalized_base_url,
            model=normalized_model,
        )
        updated_provider = updated_settings.get_provider(_WEB_PROVIDER_NAME)
        if not isinstance(updated_provider, OpenAICompatibleProviderConfig):
            raise AssertionError("Tau Web provider must be OpenAI-compatible")
        credential_store = _web_credential_store(self._manager)
        if update.api_key is not None and update.api_key.strip():
            credential_store.set(_WEB_PROVIDER_NAME, update.api_key)
        elif source_provider.name != _WEB_PROVIDER_NAME and source_provider.credential_name:
            existing_key = credential_store.get(source_provider.credential_name)
            if existing_key:
                credential_store.set(_WEB_PROVIDER_NAME, existing_key)
        if not provider_has_usable_api_key(
            updated_provider,
            credential_reader=credential_store,
        ):
            raise WebSessionValidationError(
                "provider_api_key_required",
                "Configure an API key before using this Provider",
            )
        save_provider_settings(updated_settings, self._manager.paths)
        return {
            "provider": _web_provider_payload(
                updated_provider,
                credential_store=credential_store,
            )
        }

    async def _create_session(
        self,
        *,
        cwd: str,
        provider_name: str,
        model: str,
        thinking_level: str | None,
        temperature: float | None,
    ) -> dict[str, object]:
        requested_cwd = Path(cwd).expanduser()
        try:
            resolved_cwd = requested_cwd.resolve(strict=True)
        except OSError as exc:
            raise WebSessionValidationError(
                "project_directory_not_found",
                f"Project directory does not exist: {requested_cwd}",
            ) from exc
        if not resolved_cwd.is_dir():
            raise WebSessionValidationError(
                "project_directory_required",
                f"Project path is not a directory: {resolved_cwd}",
            )

        settings = load_provider_settings(self._manager.paths)
        selection = _resolve_web_session_selection(
            settings,
            self._manager,
            provider_name=provider_name,
            model=model,
            thinking_level=thinking_level,
        )
        try:
            normalized_temperature = normalize_temperature(temperature)
        except ProviderConfigError as exc:
            raise WebSessionValidationError("temperature_invalid", str(exc)) from exc
        if normalized_temperature is not None and not provider_supports_temperature(
            selection.provider, selection.model
        ):
            raise WebSessionValidationError(
                "temperature_unsupported",
                f"Temperature is not supported for {selection.provider.name}:{selection.model}",
            )
        record = self._manager.create_session(
            cwd=resolved_cwd,
            provider_name=selection.provider.name,
            model=selection.model,
            temperature=normalized_temperature,
        )
        if thinking_level is not None:
            slot = self._slots.setdefault(record.id, _WebSessionSlot())
            handle: WebSessionHandle | None = None
            try:
                handle = await self._ensure_handle(record, slot)
                if thinking_level != handle.session.thinking_level:
                    await handle.session.set_thinking_level(
                        thinking_level,
                        persist_default=False,
                    )
                await handle.session.persist_initial_state()
                record = self._manager.get_session(record.id) or record
            except BaseException:
                self._slots.pop(record.id, None)
                if handle is not None:
                    with suppress(BaseException):
                        await handle.aclose()
                with suppress(OSError, ValueError):
                    self._manager.delete_session(record.id)
                raise
        return {"session": _session_metadata(record)}

    async def _rename_session(self, session_id: str, title: str) -> dict[str, object]:
        self._require_session(session_id)
        updated = self._manager.touch_session(session_id, title=title)
        if updated is None:
            raise KeyError(session_id)
        return {"session": _session_metadata(updated)}

    async def _update_session_configuration(
        self,
        session_id: str,
        *,
        provider_name: str,
        model: str,
        thinking_level: str | None,
    ) -> dict[str, object]:
        record = self._require_session(session_id)
        slot = self._slots.setdefault(session_id, _WebSessionSlot())
        if slot.run_task is not None and not slot.run_task.done():
            raise WebSessionBusyError("A running session cannot change configuration")

        settings = load_provider_settings(self._manager.paths)
        _resolve_web_session_selection(
            settings,
            self._manager,
            provider_name=provider_name,
            model=model,
            thinking_level=thinking_level,
        )

        handle = await self._ensure_handle(record, slot)
        try:
            await handle.session.switch_model_choice(
                ModelChoice(provider_name=provider_name, model=model),
                persist_default=False,
            )
            if thinking_level is not None and thinking_level != handle.session.thinking_level:
                await handle.session.set_thinking_level(
                    thinking_level,
                    persist_default=False,
                )
        except (ProviderConfigError, RuntimeError, ValueError) as exc:
            raise WebSessionValidationError("session_configuration_invalid", str(exc)) from exc

        updated = self._manager.get_session(session_id)
        if updated is None:
            raise KeyError(session_id)
        configuration = _runtime_configuration_payload(handle.session, self._manager)
        payload: dict[str, object] = {
            "session": _session_metadata(updated),
            "configuration": configuration,
        }
        self._publish(
            slot,
            {
                "type": "configuration_updated",
                **configuration,
            },
            record=record,
        )
        return payload

    async def _delete_session(self, session_id: str) -> None:
        record = self._require_session(session_id)
        slot = self._slots.get(session_id)
        if slot is not None and slot.run_task is not None and not slot.run_task.done():
            raise WebSessionBusyError("A running session cannot be deleted")
        webtrace = self._webtrace_path(record)
        try:
            webtrace.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Tau Web could not remove %s: %s", webtrace, exc)
        if slot is not None:
            for subscriber in slot.subscribers.values():
                _replace_subscriber_items(subscriber, None)
            slot.subscribers.clear()
            if slot.handle is not None:
                await slot.handle.aclose()
                slot.handle = None
            self._slots.pop(session_id, None)
        if self._manager.delete_session(session_id) is None:
            raise KeyError(session_id)

    async def _export_session(self, session_id: str, format: str) -> WebSessionExport:
        record = self._require_session(session_id)
        try:
            export_format = normalize_export_format(format)
        except SessionExportError as exc:
            raise WebSessionValidationError("export_format_invalid", str(exc)) from exc
        entries = _read_session_entries(record.path)
        if export_format == "jsonl":
            return WebSessionExport(
                body=render_session_jsonl(entries).encode("utf-8"),
                content_type="application/x-ndjson; charset=utf-8",
                filename="tau-session.jsonl",
            )
        title = record.title or "Untitled session"
        return WebSessionExport(
            body=render_session_html(
                entries,
                title=title,
                source=str(record.path),
            ).encode("utf-8"),
            content_type="text/html; charset=utf-8",
            filename="tau-session.html",
        )

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

    def _persist_trace_event(
        self, record: CodingSessionRecord, slot: _WebSessionSlot, payload: dict[str, object]
    ) -> None:
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
        if slot.trace_file_lines > 0 and slot.trace_file_lines > TRACE_FILE_COMPACT_LINES:
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

    def _record_trace_event(
        self,
        record: CodingSessionRecord | None,
        slot: _WebSessionSlot,
        payload: dict[str, object],
    ) -> None:
        """Buffer a forwarded session event so late subscribers can replay it."""
        slot.trace_buffer.append(dict(payload))
        if record is not None and slot.trace_file_lines >= 0:
            self._persist_trace_event(record, slot, payload)

    def _publish(
        self,
        slot: _WebSessionSlot,
        payload: dict[str, object],
        *,
        record: CodingSessionRecord | None = None,
    ) -> None:
        if payload.get("type") != "web_connected":
            self._record_trace_event(record, slot, payload)
        item = self._stream_item(slot, payload)
        for subscriber in slot.subscribers.values():
            try:
                subscriber.put_nowait(item)
            except queue.Full:
                resync = _StreamItem(
                    sequence=item.sequence,
                    payload={
                        "type": "stream_resync_required",
                        "message": "The browser fell behind the live event stream",
                    },
                )
                _replace_subscriber_items(
                    subscriber,
                    resync,
                    item,
                )

    def _publish_queue_update(
        self,
        slot: _WebSessionSlot,
        *,
        only_if_changed: bool = False,
        record: CodingSessionRecord | None = None,
    ) -> None:
        if slot.handle is None:
            return
        state = (
            slot.handle.session.queued_steering_messages,
            slot.handle.session.queued_follow_up_messages,
        )
        if only_if_changed and state == slot.last_queue_state:
            return
        slot.last_queue_state = state
        self._publish(
            slot, _trace_payload(slot, _queue_event_payload(slot.handle.session)), record=record
        )

    def _stream_item(self, slot: _WebSessionSlot, payload: dict[str, object]) -> _StreamItem:
        item = _StreamItem(sequence=slot.next_sequence, payload=payload)
        slot.next_sequence += 1
        return item

    async def _shutdown(self) -> None:
        active_tasks: list[asyncio.Task[None]] = []
        for session_id, slot in self._slots.items():
            self._resolve_pending_tool_authorizations(session_id, slot, "cancel")
            if slot.handle is not None and slot.run_task is not None and not slot.run_task.done():
                slot.cancel_requested = True
                slot.handle.session.cancel()
                active_tasks.append(slot.run_task)
        if active_tasks:
            _, pending = await asyncio.wait(active_tasks, timeout=2)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        for slot in self._slots.values():
            for subscriber in slot.subscribers.values():
                _replace_subscriber_items(subscriber, None)
            slot.subscribers.clear()
            if slot.handle is not None:
                with suppress(Exception):
                    await slot.handle.aclose()
                slot.handle = None


def _replace_subscriber_items(
    subscriber: queue.Queue[_StreamItem | None],
    *items: _StreamItem | None,
) -> None:
    """Discard queued stream items and replace them with the given sequence."""
    with suppress(queue.Empty):
        while True:
            subscriber.get_nowait()
    for item in items:
        subscriber.put_nowait(item)


def session_list_payload(manager: SessionManager) -> dict[str, object]:
    """Return browser-safe metadata for indexed Tau sessions."""
    return {"sessions": [_session_metadata(record) for record in manager.list_sessions()]}


def session_detail_payload(manager: SessionManager, session_id: str) -> dict[str, object] | None:
    """Return the active transcript branch for one indexed session."""
    record = manager.get_session(session_id)
    if record is None:
        return None
    entries = _read_session_entries(record.path)
    active_entries = _active_session_entries(entries)
    return {
        "session": _session_metadata(record),
        "messages": [
            message for entry in active_entries if (message := _entry_payload(entry)) is not None
        ],
    }


def session_options_payload(manager: SessionManager) -> dict[str, object]:
    """Return configured choices used by Tau Web's new-session form."""
    settings = load_provider_settings(manager.paths)
    provider = _select_web_provider_connection(settings, manager)
    credential_store = _web_credential_store(manager)
    current_cwd = Path.cwd().resolve()
    recent_projects = list(
        dict.fromkeys([str(record.cwd) for record in manager.list_sessions()] + [str(current_cwd)])
    )
    return {
        "defaultProjectDirectory": str(current_cwd),
        "recentProjectDirectories": recent_projects,
        "defaultProvider": provider.name,
        "provider": _web_provider_payload(
            provider,
            credential_store=credential_store,
        ),
        "providers": [
            _configuration_provider_payload(
                provider,
                include_temperature=True,
            )
        ],
    }


def _web_credential_store(manager: SessionManager) -> FileCredentialStore:
    return FileCredentialStore(credentials_path(manager.paths))


def _select_web_provider_connection(
    settings: ProviderSettings,
    manager: SessionManager,
) -> OpenAICompatibleProviderConfig:
    compatible = [
        provider
        for provider in settings.providers
        if isinstance(provider, OpenAICompatibleProviderConfig)
    ]
    if not compatible:
        return OpenAICompatibleProviderConfig(
            name=_WEB_PROVIDER_NAME,
            credential_name=_WEB_PROVIDER_NAME,
        )
    dedicated = next(
        (provider for provider in compatible if provider.name == _WEB_PROVIDER_NAME),
        None,
    )
    if dedicated is not None:
        return dedicated
    default = next(
        (provider for provider in compatible if provider.name == settings.default_provider),
        None,
    )
    credential_store = _web_credential_store(manager)
    usable = [
        provider
        for provider in compatible
        if provider_has_usable_api_key(provider, credential_reader=credential_store)
    ]
    if default is not None and default in usable:
        return default
    if usable:
        return usable[0]
    return default or compatible[0]


def _resolve_web_session_selection(
    settings: ProviderSettings,
    manager: SessionManager,
    *,
    provider_name: str,
    model: str,
    thinking_level: str | None,
) -> ProviderSelection:
    provider = _select_web_provider_connection(settings, manager)
    try:
        if provider_name != provider.name:
            raise ProviderConfigError(f"Tau Web exposes only provider {provider.name}")
        selection = resolve_provider_selection(
            settings,
            provider_name=provider_name,
            model=model,
        )
    except ProviderConfigError as exc:
        raise WebSessionValidationError("provider_selection_invalid", str(exc)) from exc

    available_thinking_levels = provider_thinking_levels(
        selection.provider,
        model=selection.model,
    )
    if thinking_level is not None and thinking_level not in available_thinking_levels:
        available = ", ".join(available_thinking_levels) or "none"
        raise WebSessionValidationError(
            "thinking_level_invalid",
            f"Thinking level is not available for {provider_name}:{model}. "
            f"Available levels: {available}",
        )
    return selection


def _web_provider_payload(
    provider: OpenAICompatibleProviderConfig,
    *,
    credential_store: FileCredentialStore,
) -> dict[str, object]:
    model = provider.default_model
    levels = provider_thinking_levels(provider, model=model)
    return {
        "name": provider.name,
        "baseUrl": provider.base_url.rstrip("/"),
        "model": model,
        "apiKeyConfigured": provider_has_usable_api_key(
            provider,
            credential_reader=credential_store,
        ),
        "thinkingLevels": list(levels),
        "defaultThinkingLevel": resolve_startup_thinking_level(provider, model),
        "temperatureSupported": provider_supports_temperature(provider, model),
        "temperatureRange": {
            "min": MIN_TEMPERATURE,
            "max": MAX_TEMPERATURE,
            "step": "any",
        },
    }


def _configuration_provider_payload(
    provider: OpenAICompatibleProviderConfig,
    *,
    include_temperature: bool = False,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": provider.name,
        "models": list(provider.models),
        "defaultModel": provider.default_model,
        "thinkingLevels": {
            model: list(provider_thinking_levels(provider, model=model))
            for model in provider.models
        },
    }
    if include_temperature:
        payload.update(
            {
                "temperatureModels": [
                    model
                    for model in provider.models
                    if provider_supports_temperature(provider, model)
                ],
                "temperatureRange": {
                    "min": MIN_TEMPERATURE,
                    "max": MAX_TEMPERATURE,
                    "step": "any",
                },
            }
        )
    return payload


def _configuration_providers_payload(
    settings: ProviderSettings,
    manager: SessionManager,
) -> list[dict[str, object]]:
    return [_configuration_provider_payload(_select_web_provider_connection(settings, manager))]


def _normalize_web_provider_base_url(value: str) -> str:
    normalized = value.strip().rstrip("/")
    parsed = urlsplit(normalized)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise WebSessionValidationError(
            "provider_base_url_invalid",
            "Provider URL must be an HTTP(S) URL without credentials, query, or fragment",
        )
    return normalized


def _normalize_web_provider_model(value: str) -> str:
    normalized = value.strip()
    if not normalized or any(character in normalized for character in "\r\n\t"):
        raise WebSessionValidationError(
            "provider_model_invalid",
            "Model name must be a non-empty single line",
        )
    return normalized


def _runtime_configuration_payload(
    session: CodingSession,
    manager: SessionManager,
) -> dict[str, object]:
    settings = load_provider_settings(manager.paths)
    return {
        "providerName": session.provider_name,
        "model": session.model,
        "thinkingLevel": session.thinking_level,
        "availableThinkingLevels": list(session.available_thinking_levels),
        "thinkingUnavailableReason": session.thinking_unavailable_reason,
        "providers": _configuration_providers_payload(settings, manager),
    }


def _stored_configuration_payload(
    record: CodingSessionRecord,
    manager: SessionManager,
) -> dict[str, object]:
    settings = load_provider_settings(manager.paths)
    provider_name = record.provider_name or settings.default_provider
    try:
        selection = resolve_provider_selection(
            settings,
            provider_name=provider_name,
            model=record.model,
        )
    except ProviderConfigError:
        return {
            "providerName": provider_name,
            "model": record.model,
            "thinkingLevel": None,
            "availableThinkingLevels": [],
            "thinkingUnavailableReason": "Session provider/model is not currently configured",
            "providers": _configuration_providers_payload(settings, manager),
        }

    active_entries = _active_session_entries(_read_session_entries(record.path))
    thinking_level = next(
        (
            entry.thinking_level or "off"
            for entry in reversed(active_entries)
            if isinstance(entry, ThinkingLevelChangeEntry)
        ),
        resolve_startup_thinking_level(selection.provider, selection.model),
    )
    available_thinking_levels = provider_thinking_levels(
        selection.provider,
        model=selection.model,
    )
    unavailable_reason = (
        None
        if available_thinking_levels
        else provider_thinking_unavailable_reason(
            selection.provider,
            model=selection.model,
        )
    )
    return {
        "providerName": selection.provider.name,
        "model": selection.model,
        "thinkingLevel": thinking_level,
        "availableThinkingLevels": list(available_thinking_levels),
        "thinkingUnavailableReason": unavailable_reason,
        "providers": _configuration_providers_payload(settings, manager),
    }


def create_web_server(
    *,
    host: str = DEFAULT_WEB_HOST,
    port: int = DEFAULT_WEB_PORT,
    session_manager: SessionManager | None = None,
    session_loader: WebSessionLoader | None = None,
) -> TauWebServer:
    """Create a configured local Tau web server without starting its loop."""
    return TauWebServer(
        (host, port),
        session_manager or SessionManager(),
        session_loader or _load_web_session,
    )


class TauWebServer(ThreadingHTTPServer):
    """HTTP server carrying the Tau application services used by request handlers."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        session_manager: SessionManager,
        session_loader: WebSessionLoader,
    ) -> None:
        super().__init__(server_address, TauWebRequestHandler)
        self.web_runtime = TauWebRuntime(session_manager, session_loader)
        self.asset_versions = _asset_versions()

    def server_close(self) -> None:
        """Close live coding sessions before releasing the listening socket."""
        self.web_runtime.close()
        super().server_close()


class TauWebRequestHandler(BaseHTTPRequestHandler):
    """Serve the bundled A-theme workspace and live session APIs."""

    server_version = "TauWeb"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        request_url = urlsplit(self.path)
        path = unquote(request_url.path)
        if path == "/api/health":
            self._send_json({"status": "ok", "version": current_version()})
            return
        if path == "/api/session-options":
            try:
                payload = self._tau_server.web_runtime.session_options()
            except (FutureTimeoutError, OSError, ProviderConfigError, RuntimeError, ValueError):
                self._send_json(
                    {"error": "session_options_unavailable"},
                    status=HTTPStatus.SERVICE_UNAVAILABLE,
                )
                return
            self._send_json(payload)
            return
        if path == "/api/sessions":
            try:
                payload = self._tau_server.web_runtime.session_list()
            except (FutureTimeoutError, OSError, RuntimeError, ValueError):
                self._send_json(
                    {"error": "session_index_unreadable"},
                    status=HTTPStatus.SERVICE_UNAVAILABLE,
                )
                return
            self._send_json(payload)
            return
        if path == "/api/dataquery":
            try:
                payload = self._tau_server.web_runtime.dataquery_config()
            except (FutureTimeoutError, OSError, RuntimeError, ValueError):
                self._send_json(
                    {"error": "dataquery_config_unavailable"},
                    status=HTTPStatus.SERVICE_UNAVAILABLE,
                )
                return
            self._send_json(payload)
            return
        if path.startswith("/api/sessions/"):
            session_path = path.removeprefix("/api/sessions/")
            if session_path.endswith("/events"):
                session_id = session_path.removesuffix("/events").rstrip("/")
                self._serve_session_events(session_id)
                return
            if session_path.endswith("/export"):
                session_id = session_path.removesuffix("/export").rstrip("/")
                formats = parse_qs(request_url.query).get("format", ["html"])
                self._serve_session_export(session_id, formats[0])
                return
            session_id = session_path
            self._serve_session(session_id)
            return

        if path == "/favicon.ico":
            asset_name = "favicon.svg"
        else:
            asset_name = "index.html" if path in {"", "/", "/index.html"} else path.lstrip("/")
        if asset_name not in _ASSET_CONTENT_TYPES:
            self._send_json({"error": "not_found"}, status=HTTPStatus.NOT_FOUND)
            return
        try:
            body = files("tau_coding").joinpath("data", "web", asset_name).read_bytes()
        except FileNotFoundError:
            self._send_json({"error": "asset_not_found"}, status=HTTPStatus.NOT_FOUND)
            return
        if asset_name == "index.html":
            body = _cache_busted_index_html(body, self._tau_server.asset_versions)
        self._send_bytes(body, content_type=_ASSET_CONTENT_TYPES[asset_name])

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = unquote(urlsplit(self.path).path)
        if path == "/api/provider":
            self._update_provider()
            return
        if path == "/api/dataquery":
            self._update_dataquery_config()
            return
        if path == "/api/dataquery/test":
            self._test_dataquery_connection()
            return
        if path == "/api/sessions":
            self._create_session()
            return
        session_path = path.removeprefix("/api/sessions/")
        if session_path == path:
            self._send_json({"error": "not_found"}, status=HTTPStatus.NOT_FOUND)
            return
        if session_path.endswith("/messages"):
            session_id = session_path.removesuffix("/messages").rstrip("/")
            self._submit_message(session_id)
            return
        if "/tool-authorizations/" in session_path:
            session_id, _separator, request_id = session_path.partition("/tool-authorizations/")
            self._respond_tool_authorization(
                session_id.rstrip("/"),
                request_id.strip("/"),
            )
            return
        if session_path.endswith("/cancel"):
            session_id = session_path.removesuffix("/cancel").rstrip("/")
            self._cancel_session(session_id)
            return
        if session_path.endswith("/queue/clear"):
            session_id = session_path.removesuffix("/queue/clear").rstrip("/")
            self._clear_session_queue(session_id)
            return
        if session_path.endswith("/configuration"):
            session_id = session_path.removesuffix("/configuration").rstrip("/")
            self._update_session_configuration(session_id)
            return
        if session_path.endswith("/rename"):
            session_id = session_path.removesuffix("/rename").rstrip("/")
            self._rename_session(session_id)
            return
        self._send_json({"error": "not_found"}, status=HTTPStatus.NOT_FOUND)

    def do_DELETE(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = unquote(urlsplit(self.path).path)
        session_id = path.removeprefix("/api/sessions/")
        if session_id == path or not self._valid_session_id(session_id):
            self.close_connection = True
            self._send_json({"error": "not_found"}, status=HTTPStatus.NOT_FOUND)
            return
        body = self._read_command_json()
        if body is None:
            return
        if body.get("confirmation") != "DELETE":
            self._send_json(
                {"error": "delete_confirmation_required"},
                status=HTTPStatus.UNPROCESSABLE_ENTITY,
            )
            return
        try:
            self._tau_server.web_runtime.delete_session(session_id)
        except KeyError:
            self._send_json({"error": "session_not_found"}, status=HTTPStatus.NOT_FOUND)
            return
        except WebSessionBusyError as exc:
            self._send_json(
                {"error": "session_busy", "message": str(exc)},
                status=HTTPStatus.CONFLICT,
            )
            return
        except (FutureTimeoutError, OSError, RuntimeError, ValueError) as exc:
            self._send_json(
                {"error": "session_delete_failed", "message": str(exc)},
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        self._send_json({"status": "deleted", "sessionId": session_id})

    def _create_session(self) -> None:
        body = self._read_command_json()
        if body is None:
            return
        cwd = body.get("cwd")
        provider_name = body.get("providerName")
        model = body.get("model")
        thinking_level = body.get("thinkingLevel")
        temperature = body.get("temperature")
        required_options = (cwd, provider_name, model)
        if not all(isinstance(value, str) and value.strip() for value in required_options):
            self._send_json(
                {"error": "session_options_required"},
                status=HTTPStatus.UNPROCESSABLE_ENTITY,
            )
            return
        if temperature is not None and (
            isinstance(temperature, bool) or not isinstance(temperature, (int, float))
        ):
            self._send_json(
                {
                    "error": "temperature_invalid",
                    "message": "Temperature must be a number or null",
                },
                status=HTTPStatus.UNPROCESSABLE_ENTITY,
            )
            return
        if thinking_level is not None and (
            not isinstance(thinking_level, str) or not thinking_level.strip()
        ):
            self._send_json(
                {"error": "thinking_level_invalid"},
                status=HTTPStatus.UNPROCESSABLE_ENTITY,
            )
            return
        try:
            payload = self._tau_server.web_runtime.create_session(
                cwd=cast(str, cwd).strip(),
                provider_name=cast(str, provider_name).strip(),
                model=cast(str, model).strip(),
                thinking_level=(
                    thinking_level.strip() if isinstance(thinking_level, str) else None
                ),
                temperature=cast(float | None, temperature),
            )
        except WebSessionValidationError as exc:
            self._send_json(
                {"error": exc.code, "message": str(exc)},
                status=HTTPStatus.UNPROCESSABLE_ENTITY,
            )
            return
        except (FutureTimeoutError, OSError, RuntimeError, ValueError) as exc:
            self._send_json(
                {"error": "session_creation_failed", "message": str(exc)},
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        self._send_json(payload, status=HTTPStatus.CREATED)

    def _update_dataquery_config(self) -> None:
        body = self._read_command_json()
        if body is None:
            return
        try:
            payload = self._tau_server.web_runtime.dataquery_update(body)
        except WebSessionValidationError as exc:
            self._send_json(
                {"error": exc.code, "message": str(exc)},
                status=HTTPStatus.UNPROCESSABLE_ENTITY,
            )
            return
        except (FutureTimeoutError, OSError, RuntimeError, ValueError) as exc:
            self._send_json(
                {"error": "dataquery_update_failed", "message": str(exc)},
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        self._send_json(payload)

    def _test_dataquery_connection(self) -> None:
        try:
            payload = self._tau_server.web_runtime.dataquery_test_connection()
        except (FutureTimeoutError, OSError, RuntimeError, ValueError) as exc:
            self._send_json(
                {"error": "dataquery_test_failed", "message": str(exc)},
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        self._send_json(payload)

    def _update_provider(self) -> None:
        body = self._read_command_json()
        if body is None:
            return
        base_url = body.get("baseUrl")
        model = body.get("model")
        api_key = body.get("apiKey")
        if not isinstance(base_url, str) or not base_url.strip():
            self._send_json(
                {"error": "provider_base_url_required"},
                status=HTTPStatus.UNPROCESSABLE_ENTITY,
            )
            return
        if not isinstance(model, str) or not model.strip():
            self._send_json(
                {"error": "provider_model_required"},
                status=HTTPStatus.UNPROCESSABLE_ENTITY,
            )
            return
        if api_key is not None and not isinstance(api_key, str):
            self._send_json(
                {"error": "provider_api_key_invalid"},
                status=HTTPStatus.UNPROCESSABLE_ENTITY,
            )
            return
        try:
            payload = self._tau_server.web_runtime.update_provider(
                WebProviderUpdate(
                    base_url=base_url,
                    api_key=api_key,
                    model=model,
                )
            )
        except WebSessionValidationError as exc:
            self._send_json(
                {"error": exc.code, "message": str(exc)},
                status=HTTPStatus.UNPROCESSABLE_ENTITY,
            )
            return
        except (FutureTimeoutError, OSError, ProviderConfigError, RuntimeError, ValueError) as exc:
            self._send_json(
                {"error": "provider_update_failed", "message": str(exc)},
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        self._send_json(payload)

    def _rename_session(self, session_id: str) -> None:
        if not self._valid_session_id(session_id):
            self._send_json({"error": "session_not_found"}, status=HTTPStatus.NOT_FOUND)
            return
        body = self._read_command_json()
        if body is None:
            return
        title = body.get("title")
        if not isinstance(title, str) or not title.strip():
            self._send_json(
                {"error": "session_title_required"},
                status=HTTPStatus.UNPROCESSABLE_ENTITY,
            )
            return
        normalized_title = title.strip()
        if any(character in normalized_title for character in "\r\n\t"):
            self._send_json(
                {
                    "error": "session_title_invalid",
                    "message": "Session title must be a single line",
                },
                status=HTTPStatus.UNPROCESSABLE_ENTITY,
            )
            return
        try:
            payload = self._tau_server.web_runtime.rename_session(
                session_id,
                normalized_title,
            )
        except KeyError:
            self._send_json({"error": "session_not_found"}, status=HTTPStatus.NOT_FOUND)
            return
        except (FutureTimeoutError, OSError, RuntimeError, ValueError) as exc:
            self._send_json(
                {"error": "session_rename_failed", "message": str(exc)},
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        self._send_json(payload)

    def _update_session_configuration(self, session_id: str) -> None:
        if not self._valid_session_id(session_id):
            self._send_json({"error": "session_not_found"}, status=HTTPStatus.NOT_FOUND)
            return
        body = self._read_command_json()
        if body is None:
            return
        provider_name = body.get("providerName")
        model = body.get("model")
        thinking_level = body.get("thinkingLevel")
        if not isinstance(provider_name, str) or not provider_name.strip():
            self._send_json(
                {"error": "provider_name_required"},
                status=HTTPStatus.UNPROCESSABLE_ENTITY,
            )
            return
        if not isinstance(model, str) or not model.strip():
            self._send_json(
                {"error": "model_required"},
                status=HTTPStatus.UNPROCESSABLE_ENTITY,
            )
            return
        if thinking_level is not None and (
            not isinstance(thinking_level, str) or not thinking_level.strip()
        ):
            self._send_json(
                {"error": "thinking_level_invalid"},
                status=HTTPStatus.UNPROCESSABLE_ENTITY,
            )
            return
        try:
            payload = self._tau_server.web_runtime.update_session_configuration(
                session_id,
                provider_name=provider_name.strip(),
                model=model.strip(),
                thinking_level=(
                    thinking_level.strip() if isinstance(thinking_level, str) else None
                ),
            )
        except KeyError:
            self._send_json({"error": "session_not_found"}, status=HTTPStatus.NOT_FOUND)
            return
        except WebSessionBusyError as exc:
            self._send_json(
                {"error": "session_busy", "message": str(exc)},
                status=HTTPStatus.CONFLICT,
            )
            return
        except WebSessionValidationError as exc:
            self._send_json(
                {"error": exc.code, "message": str(exc)},
                status=HTTPStatus.UNPROCESSABLE_ENTITY,
            )
            return
        except (FutureTimeoutError, OSError, RuntimeError, ValueError) as exc:
            self._send_json(
                {"error": "session_configuration_failed", "message": str(exc)},
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        self._send_json(payload)

    @property
    def _tau_server(self) -> TauWebServer:
        return cast(TauWebServer, self.server)

    def _serve_session(self, session_id: str) -> None:
        if not session_id or "/" in session_id:
            self._send_json({"error": "session_not_found"}, status=HTTPStatus.NOT_FOUND)
            return
        try:
            payload = self._tau_server.web_runtime.session_detail(session_id)
        except (FutureTimeoutError, OSError, RuntimeError, ValueError):
            self._send_json({"error": "session_unreadable"}, status=HTTPStatus.UNPROCESSABLE_ENTITY)
            return
        if payload is None:
            self._send_json({"error": "session_not_found"}, status=HTTPStatus.NOT_FOUND)
            return
        self._send_json(payload)

    def _serve_session_events(self, session_id: str) -> None:
        if not self._valid_session_id(session_id):
            self._send_json({"error": "session_not_found"}, status=HTTPStatus.NOT_FOUND)
            return
        try:
            subscriber_id, subscriber = self._tau_server.web_runtime.subscribe(session_id)
        except KeyError:
            self._send_json({"error": "session_not_found"}, status=HTTPStatus.NOT_FOUND)
            return
        except (FutureTimeoutError, RuntimeError):
            self._send_json({"error": "runtime_unavailable"}, status=HTTPStatus.SERVICE_UNAVAILABLE)
            return

        self.send_response(HTTPStatus.OK)
        self._send_security_headers()
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.wfile.flush()
        try:
            while True:
                try:
                    item = subscriber.get(timeout=_SSE_HEARTBEAT_SECONDS)
                except queue.Empty:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    continue
                if item is None:
                    break
                payload = json.dumps(
                    item.payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                self.wfile.write(f"id: {item.sequence}\n".encode())
                self.wfile.write(b"data: " + payload + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.close_connection = True
            self._tau_server.web_runtime.unsubscribe(session_id, subscriber_id)

    def _serve_session_export(self, session_id: str, format: str) -> None:
        if not self._valid_session_id(session_id):
            self._send_json({"error": "session_not_found"}, status=HTTPStatus.NOT_FOUND)
            return
        try:
            artifact = self._tau_server.web_runtime.export_session(session_id, format)
        except KeyError:
            self._send_json({"error": "session_not_found"}, status=HTTPStatus.NOT_FOUND)
            return
        except WebSessionValidationError as exc:
            self._send_json(
                {"error": exc.code, "message": str(exc)},
                status=HTTPStatus.UNPROCESSABLE_ENTITY,
            )
            return
        except (FutureTimeoutError, OSError, RuntimeError, ValueError):
            self._send_json(
                {"error": "session_export_failed"},
                status=HTTPStatus.UNPROCESSABLE_ENTITY,
            )
            return
        self._send_bytes(
            artifact.body,
            content_type=artifact.content_type,
            attachment_filename=artifact.filename,
        )

    def _submit_message(self, session_id: str) -> None:
        if not self._valid_session_id(session_id):
            self._send_json({"error": "session_not_found"}, status=HTTPStatus.NOT_FOUND)
            return
        body = self._read_command_json()
        if body is None:
            return
        message = body.get("message")
        streaming_behavior = body.get("behavior")
        if not isinstance(message, str) or not message.strip():
            self._send_json(
                {"error": "message_required"},
                status=HTTPStatus.UNPROCESSABLE_ENTITY,
            )
            return
        if streaming_behavior is not None and streaming_behavior not in {
            "steer",
            "follow_up",
        }:
            self._send_json(
                {"error": "streaming_behavior_invalid"},
                status=HTTPStatus.UNPROCESSABLE_ENTITY,
            )
            return
        try:
            payload = self._tau_server.web_runtime.submit(
                session_id,
                message.strip(),
                streaming_behavior=cast(StreamingBehavior | None, streaming_behavior),
            )
        except KeyError:
            self._send_json({"error": "session_not_found"}, status=HTTPStatus.NOT_FOUND)
            return
        except WebSessionBusyError:
            self._send_json({"error": "session_busy"}, status=HTTPStatus.CONFLICT)
            return
        except (FutureTimeoutError, RuntimeError, OSError, ValueError) as exc:
            self._send_json(
                {"error": "session_unavailable", "message": str(exc)},
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        status = (
            HTTPStatus.OK if payload.get("status") in {"command", "queued"} else HTTPStatus.ACCEPTED
        )
        self._send_json(payload, status=status)

    def _clear_session_queue(self, session_id: str) -> None:
        if not self._valid_session_id(session_id):
            self._send_json({"error": "session_not_found"}, status=HTTPStatus.NOT_FOUND)
            return
        body = self._read_command_json(allow_empty=True)
        if body is None:
            return
        try:
            payload = self._tau_server.web_runtime.clear_queue(session_id)
        except KeyError:
            self._send_json({"error": "session_not_found"}, status=HTTPStatus.NOT_FOUND)
            return
        except (FutureTimeoutError, RuntimeError):
            self._send_json({"error": "runtime_unavailable"}, status=HTTPStatus.SERVICE_UNAVAILABLE)
            return
        self._send_json(payload)

    def _respond_tool_authorization(self, session_id: str, request_id: str) -> None:
        if not self._valid_session_id(session_id) or not request_id or "/" in request_id:
            self._send_json({"error": "tool_authorization_not_found"}, status=HTTPStatus.NOT_FOUND)
            return
        body = self._read_command_json()
        if body is None:
            return
        decision = body.get("decision")
        if decision not in {"allow", "deny", "cancel"}:
            self._send_json(
                {"error": "tool_authorization_decision_invalid"},
                status=HTTPStatus.UNPROCESSABLE_ENTITY,
            )
            return
        try:
            resolved = self._tau_server.web_runtime.respond_tool_authorization(
                session_id,
                request_id,
                cast(ToolAuthorizationDecision, decision),
            )
        except KeyError:
            self._send_json({"error": "session_not_found"}, status=HTTPStatus.NOT_FOUND)
            return
        except (FutureTimeoutError, RuntimeError):
            self._send_json({"error": "runtime_unavailable"}, status=HTTPStatus.SERVICE_UNAVAILABLE)
            return
        if not resolved:
            self._send_json(
                {"error": "tool_authorization_not_pending"},
                status=HTTPStatus.CONFLICT,
            )
            return
        self._send_json(
            {
                "status": "resolved",
                "requestId": request_id,
                "decision": decision,
            }
        )

    def _cancel_session(self, session_id: str) -> None:
        if not self._valid_session_id(session_id):
            self._send_json({"error": "session_not_found"}, status=HTTPStatus.NOT_FOUND)
            return
        body = self._read_command_json(allow_empty=True)
        if body is None:
            return
        try:
            cancelled = self._tau_server.web_runtime.cancel(session_id)
        except KeyError:
            self._send_json({"error": "session_not_found"}, status=HTTPStatus.NOT_FOUND)
            return
        except (FutureTimeoutError, RuntimeError):
            self._send_json({"error": "runtime_unavailable"}, status=HTTPStatus.SERVICE_UNAVAILABLE)
            return
        self._send_json(
            {"status": "cancel_requested" if cancelled else "idle"},
            status=HTTPStatus.ACCEPTED if cancelled else HTTPStatus.CONFLICT,
        )

    def _read_command_json(self, *, allow_empty: bool = False) -> dict[str, object] | None:
        if self.headers.get("X-Tau-Web") != "1":
            self.close_connection = True
            self._send_json({"error": "command_header_required"}, status=HTTPStatus.FORBIDDEN)
            return None
        content_type = self.headers.get("Content-Type", "").partition(";")[0].strip().lower()
        if content_type != "application/json":
            self.close_connection = True
            self._send_json(
                {"error": "json_content_type_required"},
                status=HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
            )
            return None
        try:
            content_length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            content_length = -1
        if content_length < 0 or content_length > _MAX_REQUEST_BYTES:
            self.close_connection = True
            self._send_json(
                {"error": "invalid_content_length"},
                status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )
            return None
        if content_length == 0 and allow_empty:
            return {}
        try:
            payload = json.loads(self.rfile.read(content_length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json({"error": "invalid_json"}, status=HTTPStatus.BAD_REQUEST)
            return None
        if not isinstance(payload, dict):
            self._send_json({"error": "json_object_required"}, status=HTTPStatus.BAD_REQUEST)
            return None
        return cast(dict[str, object], payload)

    @staticmethod
    def _valid_session_id(session_id: str) -> bool:
        return bool(session_id) and "/" not in session_id

    def _send_json(
        self,
        payload: dict[str, object],
        *,
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send_bytes(body, content_type="application/json; charset=utf-8", status=status)

    def _send_bytes(
        self,
        body: bytes,
        *,
        content_type: str,
        status: HTTPStatus = HTTPStatus.OK,
        attachment_filename: str | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if attachment_filename is not None:
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{attachment_filename}"',
            )
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_security_headers(self) -> None:
        self.send_header("Content-Security-Policy", _content_security_policy())
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")

    def log_message(self, format: str, *args: Any) -> None:
        """Write concise request logs to stderr using Tau's server name."""
        sys.stderr.write(f"tau-web: {format % args}\n")


def main(argv: Sequence[str] | None = None) -> None:
    """Run Tau's local web workspace."""
    parser = _argument_parser()
    args = parser.parse_args(argv)
    if args.host not in {"127.0.0.1", "localhost"}:
        parser.error(
            "tau-web only supports loopback hosts until remote authentication is implemented"
        )

    server = create_web_server(host=args.host, port=args.port)
    url = f"http://{args.host}:{server.server_address[1]}/"
    print(f"Tau Web is available at {url}")
    if not args.no_open:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Tau Web.")
    finally:
        server.server_close()


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tau-web",
        description="Create, manage, and run Tau sessions in the A-theme web workspace.",
    )
    parser.add_argument("--host", default=DEFAULT_WEB_HOST, help="Host to bind.")
    parser.add_argument("--port", default=DEFAULT_WEB_PORT, type=_port, help="Port to bind.")
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Do not open the workspace in the default browser.",
    )
    return parser


def _port(value: str) -> int:
    port = int(value)
    if not 0 < port < 65_536:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _session_metadata(record: CodingSessionRecord) -> dict[str, object]:
    return {
        "id": record.id,
        "cwd": str(record.cwd),
        "model": record.model,
        "providerName": record.provider_name,
        "temperature": record.temperature,
        "title": record.title,
        "createdAt": record.created_at,
        "updatedAt": record.updated_at,
    }


def _read_session_entries(path: Path) -> list[SessionEntry]:
    if not path.exists():
        return []
    return entries_from_json_lines(path.read_text(encoding="utf-8").split("\n"))


def _active_session_entries(entries: list[SessionEntry]) -> list[SessionEntry]:
    active_leaf_id: str | None = None
    for entry in reversed(entries):
        if isinstance(entry, LeafEntry):
            active_leaf_id = entry.entry_id
            break
    if active_leaf_id is None:
        active_leaf_id = next(
            (entry.id for entry in reversed(entries) if not isinstance(entry, LeafEntry)),
            None,
        )
    if active_leaf_id is None:
        return []
    try:
        return path_to_entry(entries, active_leaf_id)
    except SessionTreeError:
        return [entry for entry in entries if not isinstance(entry, LeafEntry)]


def _entry_payload(entry: SessionEntry) -> dict[str, object] | None:
    if isinstance(entry, MessageEntry):
        message = entry.message
        text = message_text(message)
        payload: dict[str, object] = {
            "id": entry.id,
            "role": message.role,
            "text": text[:_MAX_MESSAGE_TEXT],
            "truncated": len(text) > _MAX_MESSAGE_TEXT,
            "timestamp": message.timestamp,
        }
        if isinstance(message, AssistantMessage):
            payload["model"] = message.model
            payload["provider"] = message.provider
            payload["thinking"] = message.thinking_text[:_MAX_MESSAGE_TEXT]
            payload["stopReason"] = message.stop_reason
            payload["errorMessage"] = message.error_message
            payload["toolCalls"] = [
                {"id": call.id, "name": call.name, "arguments": call.arguments}
                for call in message.tool_calls
            ]
        elif isinstance(message, ToolResultMessage):
            payload["toolName"] = message.tool_name
            payload["toolCallId"] = message.tool_call_id
            payload["isError"] = message.is_error
        return payload
    if isinstance(entry, CompactionEntry):
        return {
            "id": entry.id,
            "role": "compaction",
            "text": entry.summary[:_MAX_MESSAGE_TEXT],
            "truncated": len(entry.summary) > _MAX_MESSAGE_TEXT,
            "timestamp": round(entry.timestamp * 1000),
        }
    if isinstance(entry, BranchSummaryEntry):
        return {
            "id": entry.id,
            "role": "branchSummary",
            "text": entry.summary[:_MAX_MESSAGE_TEXT],
            "truncated": len(entry.summary) > _MAX_MESSAGE_TEXT,
            "timestamp": round(entry.timestamp * 1000),
        }
    return None


async def _load_web_session(
    record: CodingSessionRecord,
    manager: SessionManager,
) -> WebSessionHandle:
    """Load one indexed session using Tau's configured provider runtime."""
    settings = load_provider_settings(manager.paths)
    try:
        selection = resolve_provider_selection(
            settings,
            provider_name=record.provider_name,
            model=record.model,
        )
    except ProviderConfigError:
        matching_provider = next(
            (provider for provider in settings.providers if record.model in provider.models),
            None,
        )
        if matching_provider is None:
            raise
        selection = resolve_provider_selection(
            settings,
            provider_name=matching_provider.name,
            model=record.model,
        )

    temperature = compatible_temperature(
        selection.provider,
        selection.model,
        record.temperature,
    )
    provider = create_model_provider(
        selection.provider,
        credential_store=FileCredentialStore(credentials_path(manager.paths)),
        model=selection.model,
        temperature=temperature,
        thinking_level=resolve_startup_thinking_level(
            selection.provider,
            selection.model,
        ),
    )
    resource_paths = TauResourcePaths(
        root=manager.paths.home,
        agents_root=manager.paths.agents_home,
        paths=manager.paths,
    )
    try:
        session = await CodingSession.load(
            CodingSessionConfig(
                provider=provider,
                model=selection.model,
                storage=jsonl_session_storage(record.path),
                cwd=record.cwd,
                resource_paths=resource_paths,
                session_id=record.id,
                session_manager=manager,
                provider_name=selection.provider.name,
                provider_settings=settings,
                runtime_provider_config=selection.provider,
                temperature=temperature,
                shell_command_prefix=load_shell_settings(manager.paths).shell_command_prefix,
            )
        )
    except BaseException:
        await provider.aclose()
        raise
    if temperature != record.temperature:
        manager.touch_session(record.id, temperature=temperature)
    return WebSessionHandle(session=session, provider=provider)


def _run_trace_snapshot(slot: _WebSessionSlot) -> dict[str, object]:
    """Event/turn counts and elapsed time for the slot's active run."""
    now_ms = current_timestamp_ms()
    started_ms = slot.run_started_ms
    return {
        "eventCount": slot.trace_event_count,
        "turnCount": slot.trace_turn_count,
        "elapsedMs": max(0, now_ms - started_ms) if started_ms is not None else 0,
    }


def _trace_payload(slot: _WebSessionSlot, payload: dict[str, object]) -> dict[str, object]:
    """Stamp the active run id and a timestamp onto a trace-worthy payload."""
    if slot.run_id is not None:
        payload.setdefault("runId", slot.run_id)
        payload.setdefault("timestamp", current_timestamp_ms())
    return payload


def _coding_event_payload(event: CodingSessionEvent) -> dict[str, object]:
    return cast(
        dict[str, object],
        event.model_dump(mode="json", by_alias=True),
    )


def _content_security_policy() -> str:
    return (
        "default-src 'self'; "
        "base-uri 'none'; "
        "connect-src 'self'; "
        "font-src 'self'; "
        "form-action 'none'; "
        "frame-ancestors 'none'; "
        "img-src 'self' data:; "
        "object-src 'none'; "
        "script-src 'self'; "
        "style-src 'self'"
    )


def _slash_command_name(text: str) -> str | None:
    stripped = text.strip()
    if not stripped.startswith("/") or stripped.startswith("/skill:"):
        return None
    command = stripped[1:].partition(" ")[0].strip().lower()
    return command or None


def _web_help_message(session: CodingSession) -> str:
    supported = {"compact", *_WEB_IMMEDIATE_COMMANDS}
    lines = ["Available commands:", "/help\tShow commands available in Tau Web."]
    lines.extend(
        f"{command.usage}\t{command.description}"
        for command in session.command_registry.list_commands()
        if command.name in supported
    )
    return "\n".join(lines)


def _queue_payload(session: CodingSession) -> dict[str, object]:
    return {
        "steering": list(session.queued_steering_messages),
        "followUp": list(session.queued_follow_up_messages),
    }


def _queue_event_payload(session: CodingSession) -> dict[str, object]:
    return {
        "type": "queue_update",
        **_queue_payload(session),
    }


def _tool_authorization_event_payload(
    pending: _PendingToolAuthorization,
) -> dict[str, object]:
    return {
        "type": "tool_authorization_requested",
        "requestId": pending.request_id,
        "toolCallId": pending.call.id,
        "toolName": pending.call.name,
        "arguments": pending.call.arguments,
    }


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


if __name__ == "__main__":
    main()
