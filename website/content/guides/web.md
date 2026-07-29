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
2. select a configured Provider;
3. select one of that Provider's configured models;
4. optionally open **Advanced settings** to choose a temperature;
5. select **Create and enter**.

Temperature defaults to **Auto**, which means Tau omits the request parameter
and lets the provider or model choose its default. **Precise** sends `0`;
**Custom** accepts a value from `0` through `2`. Tau currently enables these
controls only for models using an OpenAI-compatible Chat Completions endpoint.
They remain disabled for Codex, Responses API models, and providers whose
adapter does not safely support the parameter.

Tau validates the directory and provider/model pair, creates the session under
the normal Tau session home, and immediately opens it. Provider credentials
still need to be configured before that session can run a task.

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
- the trace panel shows live run and tool events;
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

- change the current session's configured **Provider**, **model**, and
  **Thinking level**;
- **Rename** the indexed session;
- **Export HTML** for a self-contained, human-readable view of the complete
  session tree;
- **Export JSONL** for the complete durable entry sequence;
- **Delete session**.

Provider/model/thinking changes are available only while the session is idle.
The choices come from the current Tau provider configuration and are appended
to the session's durable branch; they do not replace the global default model.

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
