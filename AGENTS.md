# Tau Agent Instructions

Tau is a Python implementation of Pi's minimalist coding-agent harness architecture. The goal is to develop it incrementally, with each phase clearly documented and tested.

## Current requirements

Use the user's task, local PRDs/tickets, and relevant ADRs for current scope.
The original upstream roadmap is historical context for Pi's design, not the
phase ordering for this SQL Agent workspace.

## Architecture Principles

Preserve Pi's core separation of concerns:

```text
AgentHarness = reusable agent brain
AgentSession = coding-agent environment
TUI = one possible frontend
```

Tau should be organized around these layers:

```text
tau_ai      provider/model streaming layer
tau_agent   portable agent harness, loop, tools, events, sessions
tau_coding  CLI app, resources, skills, extensions, commands, TUI integration
```

Keep the core agent package independent of CLI, Textual, Rich rendering, session file locations, and application-specific resource loading.

## TUI Direction

Use Textual for the full interactive TUI, but only behind an adapter boundary. The agent harness should emit events; UI layers should consume those events.

Do not let Textual become a dependency of the reusable agent harness.

## Development Workflow

- Keep changes scoped to the current task and preserve the existing architecture.
- Add or update docs when introducing architectural concepts.
- Verify changed behavior with focused tests; use test-first development when requested or helpful. Complete the checks required for the affected code.
- Run tests and Python commands through `uv` (for example, `uv run pytest` or `uv run python ...`) so they use the project environment.
- Prefer simple, explicit abstractions over framework-heavy designs.
- Keep commits atomic: one coherent feature, fix, docs update, refactor, or cleanup per commit.

## GitHub Issue and PR Formatting

- When creating or editing GitHub issues and pull requests from the CLI, write multiline Markdown bodies through a temporary file or heredoc and pass them with `--body-file`.
- Do not pass escaped newlines like `\n` inside quoted `--body` strings; GitHub will render them literally instead of as line breaks.
- Use Markdown headings, blank lines, bullets, and backticks for commands/paths so issue and PR descriptions are readable.
- After creating or editing a GitHub issue or PR body, verify the rendered source with `gh issue view ... --json body` or `gh pr view ... --json body` when practical.

## Python Guidelines

- Target the Python version declared in `pyproject.toml`.
- Prefer typed dataclasses or schema models for core messages, events, tools, and sessions.
- Keep async boundaries explicit.
- Use fake providers and fake tools for deterministic agent-loop tests.
- Avoid provider-specific assumptions in core agent code.

## Documentation Expectations

Each substantial phase should leave behind beginner-friendly notes under `dev-notes/` (build journals, design docs, ADRs), explaining:

- what was added
- why it exists
- how it maps to Pi's design
- how to test or use it

When a phase adds or changes user-facing behavior, also update the published docs
under `website/content/` (the "Use Tau" guides and reference).

## Agent skills

### Issue tracker

Implementation tickets use local Markdown under `.scratch/`; PRDs live under
`docs/`. Nothing is published remotely. See `docs/agents/issue-tracker.md`.

### Triage labels

Local tickets use the canonical states `needs-triage`, `needs-info`,
`ready-for-agent`, `ready-for-human`, and `wontfix`. See
`docs/agents/triage-labels.md`.

### Domain docs

Tau uses a single-context layout with root `CONTEXT.md` and ADRs under
`dev-notes/adr/`. See `docs/agents/domain.md`.
