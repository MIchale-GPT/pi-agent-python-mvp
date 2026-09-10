# Domain Docs

Tau uses a single-context domain-documentation layout.

## When terminology or architecture matters

- Read relevant terms in root `CONTEXT.md` when the task depends on domain language.
- Read ADRs under `dev-notes/adr/` that constrain the proposed change.
- If a referenced file does not exist, proceed silently. Domain-modeling
  workflows create or extend it when terminology or decisions are resolved.

## Layout

```text
/
├── CONTEXT.md
├── dev-notes/
│   └── adr/
└── src/
```

Do not introduce `CONTEXT-MAP.md` or per-package context files unless Tau later
develops genuinely independent domain vocabularies.

## Vocabulary

Use terms as defined in `CONTEXT.md` in issues, PRDs, hypotheses, test names,
and architectural proposals. If a required concept is missing, either avoid
inventing an unnecessary synonym or record the gap for domain modeling.

## ADR conflicts

When proposed work contradicts an ADR in `dev-notes/adr/`, cite the ADR and
surface the conflict explicitly rather than silently overriding it.
