# Issue tracker: Local Markdown

Tau does not publish engineering-skill output to a remote issue tracker.
Product requirement documents are durable project documentation under `docs/`;
implementation tickets are temporary local Markdown under `.scratch/`.

## PRD conventions

- One PRD per feature: `docs/PRD-<feature-slug>.md`.
- A skill that says “publish the PRD” writes or updates that file only.
- Do not create a GitHub/GitLab issue or pull request unless the user explicitly
  requests it.

## Implementation-ticket conventions

- One feature per directory: `.scratch/<feature-slug>/`.
- Implementation tickets are
  `.scratch/<feature-slug>/issues/<NN>-<slug>.md`, numbered from `01`.
- Triage state is recorded as a `Status:` line near the top of each ticket
  (see `triage-labels.md`).
- Comments and conversation history append under a `## Comments` heading.

## When a skill says “publish to the issue tracker”

For a PRD, write `docs/PRD-<feature-slug>.md`. For an implementation ticket,
write under `.scratch/<feature-slug>/issues/`. Do not make a remote write.

## When a skill says “fetch the relevant ticket”

Read the local file passed by the user. If only a feature name and number are
provided, resolve it under `.scratch/<feature-slug>/issues/`.

## Wayfinding operations

- Map: `.scratch/<effort>/map.md`.
- Child ticket: `.scratch/<effort>/issues/NN-<slug>.md`.
- A `Type:` line records `research`, `prototype`, `grilling`, or `task`.
- A `Status:` line records `claimed` or `resolved`.
- `Blocked by: NN, NN` lists dependencies.
- Claim a ticket by setting `Status: claimed`; resolve it by appending an
  `## Answer`, setting `Status: resolved`, and linking the result from the map.
