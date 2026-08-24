---
title: The local web workspace
description: Create, manage, and run Tau coding sessions in the local Trace Workbench.
---

Tau includes an early local browser frontend called **Trace Workbench**. Start
it with:

```bash
tau-web
```

Tau prints the URL and opens it in your default browser. By default it is:

```text
http://127.0.0.1:8080/
```

Use `tau-web --no-open` when you only want to start the server.

## Create or open a session

To start fresh, select **New session** in the sidebar:

1. choose a recent project directory or enter another directory path on the
   machine running `tau-web`;
2. review or edit the single Provider connection's **URL**, **API Key**, and
   **model name**;
3. choose a session-level **Thinking** value when the Provider declares
   supported levels;
4. optionally open **Advanced settings** to choose a temperature;
5. select **Create and enter**.

Tau Web deliberately exposes one OpenAI-compatible connection instead of the
complete built-in Provider catalog. It prefers the configured default when that
Provider has usable credentials; otherwise it uses the first credentialed
OpenAI-compatible Provider. If no connection is credentialed yet, it displays
the configured default so you can add a key. If Tau has no OpenAI-compatible
Provider at all, the form starts with an editable `tau-web` setup target.

On the first save, Tau clones those editable values into a dedicated
`tau-web` Provider entry. Later Web sessions reuse that entry. Editing it does
not remove models from the source Provider, change Tau's global default, or
alter CLI/TUI sessions that use the original Provider.

When the URL and model still match the source, Tau preserves its validated
transport and capability declarations. Changing either turns the dedicated
entry into a generic OpenAI-compatible Chat Completions connection and drops
source headers and compatibility flags. Model-level URL overrides are always
removed, so the URL shown in this form is the endpoint that will actually
receive requests; a validated model API override is retained only for an
unchanged URL/model. Providers that require another transport remain available
in the CLI/TUI but are not configurable through this three-field Web form.

The API Key field never displays an existing secret. The dialog reports only
whether a key is configured: leave the field empty to preserve the current
credential, or enter a value to replace it. Tau stores entered keys in
`~/.tau/credentials.json` with user-only file permissions.
OAuth subscriptions are not copied into the dedicated connection; an
OAuth-only source therefore appears as needing an API key in this form.

Thinking is a per-session choice and is persisted before the first prompt. If
the selected Provider/model does not declare `thinking_levels`, the control
remains visible but disabled; Tau does not invent a reasoning parameter that
the endpoint may reject.

Temperature defaults to **Auto**, which means Tau omits the request parameter
and lets the provider or model choose its default. **Precise** sends `0`;
**Custom** accepts a value from `0` through `2`. Tau currently enables these
controls only for models using an OpenAI-compatible Chat Completions endpoint.
They remain disabled for Codex, Responses API models, and providers whose
adapter does not safely support the parameter.

Tau validates the directory and provider/model pair, creates the session under
the normal Tau session home, and immediately opens it. A usable Provider
credential is required before the connection can be saved.

Existing indexed sessions remain in the sidebar. Selecting one loads its active
JSONL branch.

## Run a session

The live workspace:

- lists the sessions already indexed under `~/.tau/sessions`;
- selecting a session loads its active JSONL branch;
- the composer sends a prompt through that session's configured provider and
  model;
- agent messages and tool activity arrive live over server-sent events (SSE);
- **Cancel** requests cancellation through the same coding session used by the
  TUI;
- user, assistant, tool-result, compaction, and branch-summary entries have
  distinct treatments;
- saved thinking and tool calls can be inspected;
- after a run, the transcript reloads the durable active branch;
- the trace panel groups the live event stream into per-run timelines (see
  below);
- dark and light themes are available.

Select a session, wait until the composer says **Session ready**, type a task,
and press Enter. Use Shift+Enter for a newline. The same composer accepts
Web-supported slash commands:

- `/help` lists the commands currently exposed by Tau Web;
- `/compact [instructions]` compacts the durable active context;
- `/session`, `/system`, and `/hotkeys` return their read-only session output.

One `tau-web` process allows only one active agent run in a session. While Tau
is running, the composer remains editable:

- press Enter or choose **Steer** to inject a steering message as soon as the
  agent loop can accept it;
- choose **Follow up** to run a message after the current task settles;
- inspect both queues above the composer and choose **Clear queue** to discard
  messages that have not run;
- choose **Cancel** to request cancellation of the active run.

This is not a cross-process lock, so do not run the same session concurrently
in the TUI or a second Tau process. Closing the tab does not cancel ordinary
agent work; it only disconnects that browser's event subscriber. Returning to
the page reconnects the event stream and the durable transcript is refreshed
when the run settles.

## Read the per-run trace timeline

The trace panel groups every event by run — one group per prompt you submit.
Each group shows the prompt excerpt, the run status (running, completed,
cancelled, failed), the number of turns, and the elapsed time.

Inside a group:

- your prompt and each assistant reply appear as boundary entries; streaming
  text stays in the transcript panel;
- every tool call is one collapsible entry showing the tool name, its
  authorization state, the full arguments, and the raw result;
- failed runs show the provider error inline;
- each entry offers copy buttons for the formatted arguments and for the raw
  JSON event payload as delivered over SSE.

If the browser reconnects while a run is still active, the server sends a
`run_summary` event so the panel can restore counts and status without
replaying history. The durable record of the conversation remains the session
JSONL file; the timeline is a live view of the current process.

## Authorize tool calls

Tau Web pauses before every tool execution and displays the tool name and
arguments. Choose **Allow once**, **Deny**, or **Cancel run**. The first browser
response wins when more than one tab is connected.

Tool execution defaults to denied when no browser event subscriber is
connected, when the last subscriber disconnects during a pending request, or
when the request times out. A reconnecting browser never grants a tool call
implicitly.

## Manage a session

Open the current-session menu in the top bar to:

- change the current session's **model** and **Thinking level** within Tau Web's
  single Provider connection;
- **Rename** the indexed session;
- **Export HTML** for a self-contained, human-readable view of the complete
  session tree;
- **Export JSONL** for the complete durable entry sequence;
- **Delete session**.

Model/thinking changes are available only while the session is idle. The
choices come from the single Tau Web Provider configuration and are appended to
the session's durable branch; they do not replace the global default model or
thinking level.

Delete is permanent: it removes the index entry and the session's JSONL file.
The dialog requires typing `DELETE`, and Tau refuses deletion while that session
has an active run. Cancel or wait for the run before deleting it.

The selected temperature is stored with the session and shown in the session
facts. Changing temperature on an existing session and file browsing remain
later Web capabilities.

## How it is connected

The browser does not implement an agent loop. `tau_coding.web` owns a background
async runtime and loads the same `CodingSession` abstraction used by Tau's other
frontends. It streams existing `CodingSessionEvent` values to the page and lets
the session persist messages to its JSONL file.

## Options

```text
tau-web [--host HOST] [--port PORT] [--no-open]
```

| Flag | Default | Description |
| --- | --- | --- |
| `--host` | `127.0.0.1` | Address to bind |
| `--port` | `8080` | HTTP port |
| `--no-open` | off | Do not open the default browser |

{{% caution title="No authentication" %}}
Tau Web has no application-level authentication, so it rejects non-loopback
hosts. Remote binding will remain unavailable until authentication and the
remote security boundary are implemented.
{{% /caution %}}

Mutation requests must be JSON requests from the bundled frontend and
include Tau's command header. This reduces cross-origin browser requests, but it
is not authentication.

## Why it is separate from `tau`

`tau` remains the terminal coding agent. `tau-web` is another frontend owned by
`tau_coding`; it drives the same sessions without adding HTTP, browser, or
static-resource dependencies to the reusable `tau_agent` package.
