# Tau Web live session adapter

## What was added

Tau now ships a local browser workspace started with:

```bash
uv run tau-web
```

The A / **Trace Workbench** direction now has a complete first vertical slice:

- the new-session modal reads the configured providers/models, accepts an
  existing project directory, creates the indexed record, and immediately
  opens its live event stream;
- the browser lists real session metadata from `SessionManager`;
- selecting a session reads its JSONL file and renders only the active branch;
- the composer submits prompts to a real `CodingSession`;
- coding-session events stream to the browser over server-sent events (SSE);
- the active run can be cancelled from the composer;
- a completed or cancelled turn is reloaded from durable JSONL, so the
  transcript displayed after a run is the same branch Tau will resume;
- tool calls, tool results, thinking blocks, compaction summaries, and branch
  summaries have distinct transcript treatments;
- the right-side trace panel includes live run and tool events;
- the current-session menu can rename a session, download its complete tree as
  HTML or JSONL, and permanently delete an idle session after an explicit
  danger confirmation;
- the server binds to `127.0.0.1` by default and serves a restrictive content
  security policy.

The discarded Focus and Engineering Notebook variants and the prototype
switcher were deleted after the A direction was selected.

## Why the runtime belongs in `tau_coding`

`CodingSession.prompt()` is async, while the small bundled HTTP server handles
each request on a normal thread. `TauWebRuntime` bridges those worlds with one
background asyncio loop. Every live `CodingSession` and its provider are created,
used, cancelled, and closed on that loop.

The HTTP request threads only submit commands and consume thread-safe event
queues. They do not run the agent loop or persist messages themselves. This is
important: browser code is a frontend, not a second implementation of Tau.

## Architecture

```text
Browser
    ├─ GET  /api/session-options
    ├─ GET  /api/sessions
    ├─ POST /api/sessions
    ├─ GET  /api/sessions/<id>
    ├─ GET  /api/sessions/<id>/export
    ├─ GET  /api/sessions/<id>/events       (SSE)
    ├─ POST /api/sessions/<id>/messages
    ├─ POST /api/sessions/<id>/cancel
    ├─ POST /api/sessions/<id>/rename
    └─ DELETE /api/sessions/<id>
             ↓
tau_coding.web
    ├─ SessionManager + active-branch reader
    └─ TauWebRuntime (async loop + subscribers)
             ↓
tau_coding.CodingSession
             ↓
tau_agent.AgentHarness
```

The existing `CodingSessionEvent` models are serialized directly with their
Pi-compatible aliases. Synthetic Web lifecycle events (`web_connected`,
`run_started`, `cancel_requested`, `run_error`, and `run_finished`) describe
the adapter itself.

Static assets are bundled under `tau_coding/data/web/`, so the installed
`tau-web` console script does not depend on a source checkout.

## Session lifecycle operations

`GET /api/session-options` turns the durable `ProviderSettings` into the small
provider/model catalog needed by the creation modal. The directory field starts
with the server process's current directory and offers directories from recent
sessions. It remains editable because a browser directory input cannot reveal
an absolute path on the machine running the server. The server expands `~`,
resolves the submitted path, requires it to be an existing directory, and
validates the provider/model pair again before calling
`SessionManager.create_session()`.

Rename updates only indexed metadata. Export reads the complete JSONL entry
sequence on the runtime loop and renders the existing self-contained session
HTML or JSONL representation, preserving branches rather than exporting only
the active transcript. No temporary export file is left behind.

Deletion is intentionally stricter:

1. the modal explains that the operation is permanent;
2. the user must type `DELETE`;
3. the API independently requires that exact confirmation value;
4. an active run returns HTTP 409 instead of being cancelled implicitly;
5. an idle runtime handle is closed before `SessionManager.delete_session()`
   removes both the project index record and its JSONL transcript.

## Run and disconnect lifecycle

A single `tau-web` process allows only one prompt per indexed session. A second
submission to that process receives HTTP 409 instead of accidentally starting a
concurrent agent loop. This is not a cross-process file lock: do not run the
same session simultaneously from the TUI, another `tau-web` process, or another
Tau process.

An SSE disconnect removes that subscriber but does not cancel the coding task:
closing a browser tab should not silently stop filesystem or tool work. The
browser reconnects automatically; if it missed the end of a run, it reloads the
durable active branch. Server shutdown is different: it requests cancellation,
closes all `CodingSession` instances and provider clients, then stops the async
loop.

## Safety boundary

- Loopback is the default bind.
- Non-loopback hosts are rejected until Tau Web has application-level
  authentication and its remote security boundary has been reviewed.
- Tau Web currently has no authentication.
- Mutation requests must be same-origin JSON carrying `X-Tau-Web: 1`. That
  custom header forces cross-origin browsers through a CORS preflight, which
  this server does not grant.
- Request bodies are capped at 256 KiB.
- Session details are resolved through indexed session ids; the URL never
  becomes a filesystem path.
- API responses omit the JSONL storage path.
- Project paths submitted during creation must resolve to an existing directory.
- Delete requires an explicit command confirmation and refuses active runs.
- Static responses use CSP, same-origin, no-sniff, frame-denial, and no-store
  headers.

## How to test

```bash
uv run pytest tests/test_web.py
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv build
```

For a browser check:

```bash
uv run tau-web --no-open
```

Then open <http://127.0.0.1:8080/>.

## Deliberately deferred

Built-in slash-command dispatch, changing provider/model/thinking on an
existing session, steering/follow-up queues, permission prompts, branch
switching, attachments, and file browsing remain later Web capabilities.
