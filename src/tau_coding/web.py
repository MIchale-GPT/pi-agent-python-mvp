"""Local A-theme web workspace for Tau coding sessions."""

from __future__ import annotations

import argparse
import asyncio
import json
import queue
import sys
import threading
import webbrowser
from collections.abc import Awaitable, Callable, Coroutine, Sequence
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

from tau_agent.events import MessageEndEvent
from tau_agent.messages import AssistantMessage, ToolResultMessage, message_text
from tau_agent.session import (
    BranchSummaryEntry,
    CompactionEntry,
    LeafEntry,
    MessageEntry,
    SessionEntry,
    SessionTreeError,
    entries_from_json_lines,
    path_to_entry,
)
from tau_coding.credentials import FileCredentialStore, credentials_path
from tau_coding.events import CodingSessionEvent
from tau_coding.provider_config import (
    MAX_TEMPERATURE,
    MIN_TEMPERATURE,
    ProviderConfigError,
    compatible_temperature,
    load_provider_settings,
    normalize_temperature,
    provider_supports_temperature,
    resolve_provider_selection,
    resolve_startup_thinking_level,
)
from tau_coding.provider_runtime import ClosableModelProvider, create_model_provider
from tau_coding.resources import TauResourcePaths
from tau_coding.session import CodingSession, CodingSessionConfig, jsonl_session_storage
from tau_coding.session_export import (
    SessionExportError,
    normalize_export_format,
    render_session_html,
    render_session_jsonl,
)
from tau_coding.session_manager import CodingSessionRecord, SessionManager
from tau_coding.shell_config import load_shell_settings
from tau_coding.version import current_version

DEFAULT_WEB_HOST = "127.0.0.1"
DEFAULT_WEB_PORT = 8080
_MAX_MESSAGE_TEXT = 100_000
_MAX_REQUEST_BYTES = 256 * 1024
_RUNTIME_CALL_TIMEOUT_SECONDS = 15.0
_SSE_HEARTBEAT_SECONDS = 15.0
_SSE_QUEUE_ITEMS = 2_048
_ASSET_CONTENT_TYPES = {
    "index.html": "text/html; charset=utf-8",
    "styles.css": "text/css; charset=utf-8",
    "session-actions.js": "text/javascript; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "favicon.svg": "image/svg+xml",
}
WebSessionLoader = Callable[
    [CodingSessionRecord, SessionManager],
    Awaitable["WebSessionHandle"],
]
RunStatus = Literal["completed", "failed", "cancelled"]


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


@dataclass(slots=True)
class _WebSessionSlot:
    handle: WebSessionHandle | None = None
    run_task: asyncio.Task[None] | None = None
    run_id: str | None = None
    cancel_requested: bool = False
    subscribers: dict[int, queue.Queue[_StreamItem | None]] = field(default_factory=dict)
    next_subscriber_id: int = 1
    next_sequence: int = 1


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

    def submit(self, session_id: str, message: str) -> str:
        """Start one coding-session turn and return its run id."""
        return self._call(self._submit(session_id, message))

    def cancel(self, session_id: str) -> bool:
        """Request cancellation of the active run, returning whether one existed."""
        return self._call(self._cancel(session_id))

    def session_list(self) -> dict[str, object]:
        """Read the session index on the runtime thread."""
        return self._call(self._session_list())

    def session_detail(self, session_id: str) -> dict[str, object] | None:
        """Read one active branch on the runtime thread."""
        return self._call(self._session_detail(session_id))

    def session_options(self) -> dict[str, object]:
        """Read the configured project, provider, and model choices."""
        return self._call(self._session_options())

    def create_session(
        self,
        *,
        cwd: str,
        provider_name: str,
        model: str,
        temperature: float | None = None,
    ) -> dict[str, object]:
        """Create and index a session from browser-selected options."""
        return self._call(
            self._create_session(
                cwd=cwd,
                provider_name=provider_name,
                model=model,
                temperature=temperature,
            )
        )

    def rename_session(self, session_id: str, title: str) -> dict[str, object]:
        """Rename one indexed session."""
        return self._call(self._rename_session(session_id, title))

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
        self._require_session(session_id)
        slot = self._slots.setdefault(session_id, _WebSessionSlot())
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
        return subscriber_id, subscriber

    def _unsubscribe(self, session_id: str, subscriber_id: int) -> None:
        slot = self._slots.get(session_id)
        if slot is not None:
            slot.subscribers.pop(subscriber_id, None)

    async def _submit(self, session_id: str, message: str) -> str:
        record = self._require_session(session_id)
        slot = self._slots.setdefault(record.id, _WebSessionSlot())
        if slot.run_task is not None and not slot.run_task.done():
            raise WebSessionBusyError("Tau is already running in this session")
        if slot.handle is None:
            handle = await self._session_loader(record, self._manager)
            try:
                await handle.session.emit_pending_session_start()
            except BaseException:
                await handle.aclose()
                raise
            slot.handle = handle

        run_id = uuid4().hex
        slot.run_id = run_id
        slot.cancel_requested = False
        self._publish(
            slot,
            {
                "type": "run_started",
                "sessionId": record.id,
                "runId": run_id,
            },
        )
        slot.run_task = asyncio.create_task(
            self._run_prompt(record.id, slot, run_id, message),
            name=f"tau-web-run-{run_id}",
        )
        return run_id

    async def _run_prompt(
        self,
        session_id: str,
        slot: _WebSessionSlot,
        run_id: str,
        message: str,
    ) -> None:
        assert slot.handle is not None
        status: RunStatus = "completed"
        try:
            async for event in slot.handle.session.prompt(message):
                self._publish(slot, _coding_event_payload(event))
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
            )
        finally:
            if slot.cancel_requested and status == "completed":
                status = "cancelled"
            self._publish(
                slot,
                {
                    "type": "run_finished",
                    "sessionId": session_id,
                    "runId": run_id,
                    "status": status,
                },
            )
            slot.run_task = None
            slot.run_id = None
            slot.cancel_requested = False

    async def _cancel(self, session_id: str) -> bool:
        self._require_session(session_id)
        slot = self._slots.get(session_id)
        if slot is None or slot.handle is None or slot.run_task is None or slot.run_task.done():
            return False
        slot.cancel_requested = True
        slot.handle.session.cancel()
        self._publish(
            slot,
            {
                "type": "cancel_requested",
                "sessionId": session_id,
                "runId": slot.run_id,
            },
        )
        return True

    async def _session_list(self) -> dict[str, object]:
        return session_list_payload(self._manager)

    async def _session_detail(self, session_id: str) -> dict[str, object] | None:
        return session_detail_payload(self._manager, session_id)

    async def _session_options(self) -> dict[str, object]:
        return session_options_payload(self._manager)

    async def _create_session(
        self,
        *,
        cwd: str,
        provider_name: str,
        model: str,
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

        try:
            settings = load_provider_settings(self._manager.paths)
            selection = resolve_provider_selection(
                settings,
                provider_name=provider_name,
                model=model,
            )
        except ProviderConfigError as exc:
            raise WebSessionValidationError(
                "provider_selection_invalid",
                str(exc),
            ) from exc
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
        return {"session": _session_metadata(record)}

    async def _rename_session(self, session_id: str, title: str) -> dict[str, object]:
        self._require_session(session_id)
        updated = self._manager.touch_session(session_id, title=title)
        if updated is None:
            raise KeyError(session_id)
        return {"session": _session_metadata(updated)}

    async def _delete_session(self, session_id: str) -> None:
        self._require_session(session_id)
        slot = self._slots.get(session_id)
        if slot is not None and slot.run_task is not None and not slot.run_task.done():
            raise WebSessionBusyError("A running session cannot be deleted")
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

    def _publish(self, slot: _WebSessionSlot, payload: dict[str, object]) -> None:
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

    def _stream_item(self, slot: _WebSessionSlot, payload: dict[str, object]) -> _StreamItem:
        item = _StreamItem(sequence=slot.next_sequence, payload=payload)
        slot.next_sequence += 1
        return item

    async def _shutdown(self) -> None:
        active_tasks: list[asyncio.Task[None]] = []
        for slot in self._slots.values():
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
    current_cwd = Path.cwd().resolve()
    recent_projects = list(
        dict.fromkeys([str(record.cwd) for record in manager.list_sessions()] + [str(current_cwd)])
    )
    return {
        "defaultProjectDirectory": str(current_cwd),
        "recentProjectDirectories": recent_projects,
        "defaultProvider": settings.default_provider,
        "providers": [
            {
                "name": provider.name,
                "models": list(provider.models),
                "defaultModel": provider.default_model,
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
            for provider in settings.providers
        ],
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
        self._send_bytes(body, content_type=_ASSET_CONTENT_TYPES[asset_name])

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = unquote(urlsplit(self.path).path)
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
        if session_path.endswith("/cancel"):
            session_id = session_path.removesuffix("/cancel").rstrip("/")
            self._cancel_session(session_id)
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
        try:
            payload = self._tau_server.web_runtime.create_session(
                cwd=cast(str, cwd).strip(),
                provider_name=cast(str, provider_name).strip(),
                model=cast(str, model).strip(),
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
        if not isinstance(message, str) or not message.strip():
            self._send_json(
                {"error": "message_required"},
                status=HTTPStatus.UNPROCESSABLE_ENTITY,
            )
            return
        try:
            run_id = self._tau_server.web_runtime.submit(session_id, message.strip())
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
        self._send_json(
            {"status": "accepted", "runId": run_id},
            status=HTTPStatus.ACCEPTED,
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


if __name__ == "__main__":
    main()
