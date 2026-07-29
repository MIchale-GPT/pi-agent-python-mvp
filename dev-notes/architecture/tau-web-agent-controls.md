# Tau Web P1 agent controls

## What was added

Tau Web's second vertical slice adds the controls needed around an active
coding-agent run:

- idle sessions can switch models and supported thinking levels within Tau
  Web's single Provider connection;
- the composer dispatches `/help`, `/compact`, `/session`, `/system`, and
  `/hotkeys`;
- running sessions accept explicit steering and follow-up messages, show both
  queues, and can clear messages that have not run;
- tool calls pause for a browser decision: allow once, deny, or cancel the run.

The browser receives configuration, queue, command-result, and authorization
updates over the existing session SSE stream.

## Why these controls live in the Web adapter

The browser owns presentation and user interaction, but not agent behavior.
`tau_coding.web` translates HTTP requests and browser decisions into public
`CodingSession` operations:

```text
browser controls
    ↓ HTTP / JSON + SSE
TauWebRuntime
    ↓
CodingSession
    ↓
AgentHarness
```

Model and thinking changes reuse the session's provider validation and durable
entries. `/compact` reuses `CodingSession.compact()`. Queue submissions reuse
`CodingSession.prompt(..., streaming_behavior=...)`. Tool authorization uses
the harness's `before_tool_call` seam. No queue, compaction, or tool-execution
policy was reimplemented in JavaScript.

This preserves Pi's separation: `tau_agent` remains the portable brain,
`tau_coding` owns the coding environment and frontend adapter, and the browser
only consumes events and sends explicit choices.

## Session-local configuration

The configuration endpoint returns the current Provider, model, thinking level,
and only the single OpenAI-compatible connection selected for Tau Web. A change
is allowed only while the session is idle.

The selected model and thinking level are appended to the active JSONL branch,
and `SessionManager` metadata records the active Provider/model. Web switches
pass `persist_default=False`, so changing one browser session does not silently
replace Tau's global default Provider, model, or thinking level.

## One editable Web Provider connection

The new-session modal no longer renders Tau's complete built-in Provider
catalog. The Web adapter selects one OpenAI-compatible connection:

1. use the configured default when it has a stored or environment credential;
2. otherwise use the first credentialed OpenAI-compatible Provider;
3. if none is credentialed, show the configured default as the setup target.

The browser may update that connection's base URL, API key, and single model
name through `POST /api/provider`. The first update clones the selected
configuration into a dedicated `tau-web` Provider; later updates replace only
that dedicated entry. The source Provider, `default_provider`, its model
metadata, and scoped models remain unchanged for CLI/TUI and older sessions.
When no OpenAI-compatible source exists, the adapter synthesizes the same
editable setup target instead of failing the new-session form.

An unchanged source URL/model keeps its validated transport and capability
declarations. Editing either switches the dedicated entry to
`openai-completions` and clears source headers and compatibility flags.
Model-level URL overrides are always cleared so the user-entered URL cannot be
bypassed by hidden catalog metadata. A model API override is retained only when
the source URL/model are unchanged.

The key is written through `FileCredentialStore`; read payloads contain only
`apiKeyConfigured`, never the secret. A stored source API key is copied
internally into the dedicated credential on first save, while environment keys
continue to work through the cloned `api_key_env`. OAuth credentials are not
reported as editable API keys or copied to the renamed connection, because
refresh is registered against the original Provider id.

New-session Thinking uses the selected Provider/model's declared levels.
Choosing a level eagerly checkpoints the empty session's initial model and
thinking entries, so the choice survives a restart before the first prompt.
Unsupported Providers keep the control visible but disabled rather than sending
an unvalidated reasoning parameter.

## Commands and queues

Read-only commands return an immediate JSON command result. Manual compaction is
different because it calls the model: it receives a normal Web run id and emits
`run_started`, `command_result`, and `run_finished`.

An unmarked message submitted during a run still receives HTTP 409. The browser
must explicitly send `behavior: "steer"` or `behavior: "follow_up"`. Queue
updates are derived from the canonical session queues and are sent on enqueue,
clear, reconnect, drain, and run completion.

## Tool authorization safety

Each loaded Web session installs a host callback before tool execution. The
callback publishes `tool_authorization_requested` with an opaque request id,
the tool name, and arguments, then waits for the first decision.

Safe defaults are deliberate:

- no connected SSE subscriber means deny;
- losing the last subscriber while a decision is pending means deny;
- an unanswered request times out and denies;
- reconnecting republishes only requests that are still pending;
- duplicate or late responses are rejected.

Choosing cancel also signals cancellation to the active `CodingSession`; deny
blocks only that tool call and lets the agent observe the error result.

## How to test

The deterministic Web suite uses fake providers and fake tools:

```bash
uv run pytest tests/test_web.py
```

It covers configuration validation and persistence, immediate `/help`, async
`/compact`, both queue modes and clearing, allow/deny/cancel tool decisions,
and denial after the browser disconnects.

Run the project-wide checks before merging:

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
```
