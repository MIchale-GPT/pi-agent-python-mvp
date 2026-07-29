# Session temperature controls

## What was added

Tau can now attach an optional sampling temperature to a coding session:

- `tau --temperature 0.2` sets it for print mode or a newly opened TUI session;
- Tau Web exposes **Auto**, **Precise**, and **Custom** choices while creating a
  session;
- session metadata persists the chosen numeric value;
- the OpenAI-compatible Chat Completions adapter includes `temperature` in its
  JSON request only when a value was explicitly selected.

The accepted range is `0` through `2`. **Auto** is represented by `None`, not by
an invented numeric default. This preserves the model service's own default and
keeps Tau from changing behavior merely by upgrading.

## Why capability checks are explicit

Temperature is not a universal provider option. Codex and models routed through
the Responses API do not share the same safe request contract, and other Tau
provider adapters do not currently expose the parameter.

`tau_coding.provider_config` therefore owns validation and the
provider/model capability decision. Unsupported explicit values produce a
clear error. Tau Web uses the same capability result to disable controls rather
than offering a setting the runtime would ignore.

This is intentionally not a new method or argument on
`tau_agent.ModelProvider`. The portable harness still consumes one streaming
provider interface and remains unaware of HTTP sampling fields. A future
provider-neutral generation-options abstraction can be added when multiple
adapters have a genuinely shared contract.

## Layer mapping

```text
CLI / TUI / Tau Web
        │ select or display session preference
        ▼
tau_coding
        │ validate capability and persist temperature
        ▼
tau_ai OpenAI-compatible adapter
        │ serialize an explicitly selected value
        ▼
Chat Completions request
```

Changing model or provider preserves the session value when the destination
supports it and resets to **Auto** otherwise. Resuming a session restores its
stored value while the provider/model still supports temperature. If provider
configuration changed while Tau was closed, resume reconciles the stored value
and persists **Auto** instead of failing the session load. Omitting
`--temperature` while resuming does not otherwise overwrite the stored value.

## How to test

The deterministic tests cover omission versus explicit serialization, range
and capability validation, session persistence, CLI forwarding, Web API
metadata, and browser control behavior:

```bash
uv run pytest \
  tests/test_tau_ai.py \
  tests/test_provider_runtime.py \
  tests/test_session_manager.py \
  tests/test_coding_session.py \
  tests/test_cli.py \
  tests/test_web.py
```

For a browser check:

```bash
uv run tau-web --no-open
```

Open <http://127.0.0.1:8080/>, create a session, and expand **Advanced
settings**.
