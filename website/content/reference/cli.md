---
title: CLI reference
description: Every Tau command-line command and flag.
---

The `tau` command launches the interactive TUI by default; subcommands and flags
cover everything else. The separate `tau-web` entry point starts the local
browser workspace.

```text
tau [OPTIONS] [PROMPT] [COMMAND] [ARGS]
```

- With no arguments, `tau` opens the interactive [TUI]({{< relref "../guides/tui.md" >}}).
- A positional `PROMPT` opens the TUI and submits it as the first turn.
- `-p/--print` (or `--mode`) runs that same positional prompt in [print mode]({{< relref "../guides/print-mode.md" >}}) instead of the TUI.
- Put flags before the prompt — Tau treats everything after the last recognized flag as prompt text, including tokens that look like flags.

On TUI and text print-mode startup, Tau may show a non-blocking notice when a
newer `tau-ai` release is available on PyPI. In the TUI, this notice is the first
transcript item and appears in bright yellow. Run `tau update` to upgrade. Disable
the check with `TAU_NO_UPDATE_CHECK=1`; utility commands such as `tau --version`,
`tau update`, `tau sessions`, and `tau export` do not run it. After an upgrade,
the TUI also adds a one-time release-notes message to the transcript with the new
features and fixes.

## Commands

| Command | What it does |
| --- | --- |
| `tau` | Open the interactive TUI |
| `tau "<prompt>"` | Open the TUI with an initial prompt |
| `tau update` | Upgrade Tau with the installer that owns its environment |
| `tau sessions` | List indexed sessions (id, title, model, cwd) |
| `tau export <ref> [dest] [--format html\|jsonl]` | Export a session id or JSONL path (HTML default) |
| `tau --export <ref> [dest]` | Same as `tau export`, as a top-level flag |
| `tau providers` | List configured providers and how each authenticates |
| `tau [setup options] setup` | Create/update an OpenAI-compatible provider |
| `tau-web` | Create, manage, and run sessions in the local Trace Workbench at `127.0.0.1:8080` |

## Options

| Flag | Description |
| --- | --- |
| `-p, --print` | Run the positional prompt in non-interactive print mode |
| `-m, --model TEXT` | Model to request from the provider |
| `--provider TEXT` | Configured provider name to use |
| `--cwd PATH` | Working directory for the built-in tools |
| `--mode [text\|json\|transcript]` | Output mode for print mode (default `text`); also triggers print mode on its own |
| `--session TEXT` | Resume a session id in the TUI |
| `--new-session` | Start a new session instead of resuming the default |
| `--auto-compact-threshold INT` | Auto-compact above this rough token estimate |
| `--temperature FLOAT` | Sampling temperature from `0` through `2`; omitted means provider default |
| `-e, --extension PATH` | Load an [extension]({{< relref "../guides/extensions.md" >}}) file or directory (repeatable) |
| `--no-extensions` | Disable extension directory discovery (explicit `-e` paths still load) |
| `--project-extensions` | Also load `<project>/.tau/extensions` (runs project-supplied code at startup) |
| `-v, --version` | Print the version and exit |

Temperature is currently supported only for OpenAI-compatible Chat Completions
models. Tau rejects an explicit value for Codex, Responses API models, and
other provider adapters instead of silently ignoring it. On resume, omitting
the flag preserves the temperature stored in the session while the configured
provider/model still supports it; otherwise Tau resets the session to the
provider default.

`--resume`, `--prompt`, `-o/--output`, and `-x` are removed; each now exits
with an error naming its replacement (`--session`, `--print`, `--mode`, and
`-e/--extension`, respectively).

### Provider setup options

Tau's setup mode registers an OpenAI-compatible provider. Put these flags before the final `setup` argument:

| Flag | Default | Description |
| --- | --- | --- |
| `--provider TEXT` | `openai` | Provider name to create/update |
| `--model TEXT` | default model | Default model for the provider |
| `--base-url TEXT` | OpenAI URL | OpenAI-compatible base URL |
| `--api-key-env TEXT` | `OPENAI_API_KEY` | Env var holding the API key |
| `--timeout-seconds FLOAT` | `60.0` | HTTP timeout |
| `--max-retries INT` | `2` | Retry count for transient failures |
| `--max-retry-delay-seconds FLOAT` | `1.0` | Delay between retries |
| `--set-default / --no-set-default` | set-default | Make this the default provider |

Example:

```bash
tau --provider local \
  --base-url http://localhost:11434/v1 \
  --api-key-env LOCAL_API_KEY \
  --model qwen \
  setup
```

See also: [Slash commands]({{< relref "./slash-commands.md" >}}) (in-session) and
[Keyboard shortcuts]({{< relref "./keybindings.md" >}}).

## `tau-web`

```text
tau-web [--host HOST] [--port PORT] [--no-open]
```

`tau-web` binds to `127.0.0.1:8080` and opens the A-theme Trace Workbench by
default. It exposes one credentialed OpenAI-compatible connection whose
URL/key/model can be edited while creating a session, supports a session-level
Thinking choice, manages indexed sessions (rename, HTML/JSONL export, confirmed
delete), accepts prompts, streams their events over SSE, supports cancellation,
and reloads the durable active JSONL branch after each run. Non-loopback hosts
are rejected until Tau Web has application-level authentication and a reviewed
remote security boundary.
