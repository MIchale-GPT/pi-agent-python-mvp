# PRD: SAG Agent SQL Planner for Tau Data Queries

Status: implemented

## Problem Statement

Tau's current data-query integration treats SAG primarily as a knowledge-search
backend. Tau rewrites the user's question and sends it to a SAG MCP `search`
tool, receives ranked chunks or plain text, and then asks Tau's own model to
assemble SQL from that evidence.

This does not exercise the full SAG Agent capability. SAG Agent chat can apply
its configured instructions, search its bound sources, resolve entity aliases
and reporting caliber, generate a cited answer, and revise that answer in a
multi-turn conversation. Sending a SQL-writing prompt to a retrieval tool does
not turn that tool into the configured SAG Agent, even when the prompt itself is
detailed.

The mismatch causes avoidable work and inconsistent answers:

- Tau may split entity, caliber, table, and field discovery across several MCP
  calls even though SAG Agent can resolve them together.
- A correct SQL answer produced by SAG's own Agent path is not necessarily the
  answer Tau receives from MCP search.
- SQL execution errors are reconstructed by Tau's model instead of being sent
  back through one explicit SAG Agent conversation.
- A SAG thread-history URL can display prior messages, but reading message
  history does not itself submit a question or run the Agent.
- Reusing one fixed SAG thread across unrelated Tau runs would contaminate
  business context and make results difficult to audit.

The existing safety requirements remain necessary. SAG-generated SQL is
untrusted input: Tau must parameterize it, bind it to current knowledge
citations, validate it against the read-only SQL policy, freeze it before
authorization, and execute only the frozen plan through the DWS adapter.

## Solution

Make a configured SAG Agent the primary SQL-planning backend for business data
questions. Tau rewrites the complete user question once and sends it to SAG's
OpenAI-compatible Agent chat endpoint. SAG performs its own grounded retrieval
and returns a complete SQL-oriented answer plus citations. Tau converts the
answer and citations into the existing run-scoped evidence bundle, then retains
the existing prepare, authorization, and execute gates.

SAG produces a cited, SQL-oriented planning answer. It does not produce a
trusted executable plan. Tau's model treats the whole SAG answer as untrusted
generated evidence and writes the final parameterized SQL passed to
`data_query_prepare`. SAG text cannot change tool instructions, policy, source
configuration, authorization, or execution behavior.

Tau owns one planner conversation for each initial planning search in a
data-query run. Every initial SAG Agent request starts with empty planner
history, so a multi-caliber, period, or entity question may use independent
searches and evidence bundles before Tau aggregates their query results. Tau
stores each bounded user request and SAG assistant response in run memory. If
DWS rejects frozen SQL, Tau appends the parameterized SQL and sanitized database
error to the conversation associated with that SQL's evidence bundle. A new Tau
run clears all planner conversations.

The public data-tool workflow in Agent mode remains:

```text
User question
  → for each requested result slice:
      data_knowledge_search (independent SAG Agent SQL-planning conversation)
      → optional data_knowledge_read (expand a cited source through MCP)
      → data_query_prepare (policy-check and freeze that bundle's SQL)
      → data_query_execute (authorized read-only DWS query)
      → on SQL failure, retry in that slice's planner conversation
  → aggregate all slice results in one answer
```

The existing tool name is retained for compatibility. Its description,
guideline, and result contract are selected from the explicit planning mode.
MCP remains an auxiliary citation-reader and an explicit legacy backend; it is
not a fallback from Agent errors.

The TUI shows a compact SAG planning result by default. Expanding tool results
shows the actual Tau-to-SAG request, the raw bounded SAG response, citations,
and whether the exchange was an initial plan or correction. These values live
in display-only tool details and are not duplicated into model-visible content.

## Readiness Gate

The target SAG deployment contract was captured and reviewed on 2026-08-26.
It records the deployed OpenAPI operation, Bearer auth requirement, one
non-streaming success response with `sag.citations`, one authentication error
response, and observed client cancellation behavior.

Store stable fixtures under `tests/fixtures/sag_agent/` and a short capture note
under `dev-notes/`. Remove tokens, business values, and private document text.
Adapter contract tests must consume these fixtures rather than a response shape
reconstructed only from the upstream README.

The sanitized fixtures are stored under `tests/fixtures/sag_agent/` and the
capture record is in `dev-notes/sag-agent-contract-readiness.md`. Adapter
contract tests consume those fixtures. The gate validates the local deployment
contract; it does not require production DWS access.

## User Stories

1. As a Tau user, I want each requested result slice to produce one complete SAG
   SQL-planning request, so that multiple calibers, periods, or entities can be
   planned independently and aggregated without splitting discovery within a
   slice into wasteful searches.
2. As a Tau user, I want SAG's configured Agent instructions to participate in
   SQL generation, so that the result matches behavior already validated in the
   SAG chat interface.
3. As a Tau user, I want SAG Agent to resolve company aliases such as “京能技术”
   from its bound knowledge, so that I do not have to provide an entity code.
4. As a Tau user, I want SAG Agent to determine single-company versus
   consolidated reporting caliber from knowledge, so that Tau does not ask an
   unnecessary follow-up question.
5. As a Tau user, I want report periods normalized consistently, so that
   “2025年4月” is planned with the database's expected period representation.
6. As a Tau user, I want the generated SQL to include only fields relevant to
   my question, so that results are concise and auditable.
7. As a Tau user, I want comparison requests such as month-over-month or
   year-over-year to be planned by SAG from its approved SQL templates.
8. As a Tau user, I want Tau to show citations returned by SAG, so that I can
   inspect which knowledge supported the SQL.
9. As a Tau user, I want to expand a citation to its original source content,
   so that I can verify tables, fields, and business definitions.
10. As a Tau user, I want SQL derived from a SAG answer to pass the same Tau
    policy checks as other model-authored SQL, so that switching planners does
    not weaken safety.
11. As a Tau user, I want SQL values bound as parameters, so that user values
    are not inlined into the statement shown or executed.
12. As a Tau user, I want to approve the frozen SQL before execution, so that
    SAG cannot change the statement after I review it.
13. As a Tau user, I want DWS execution errors sent back to SAG Agent with the
    failed parameterized SQL, so that SAG can correct schema or field mistakes.
14. As a Tau user, I want correction to retain the original business question,
    so that SAG does not fix the SQL while losing the requested entity, period,
    or indicator.
15. As a Tau user, I want corrected SQL to pass prepare and authorization again,
    so that a repair cannot bypass policy or approval.
16. As a Tau user, I want a bounded number of correction attempts, so that an
    incorrect planner cannot create an endless loop.
17. As a Tau user, I want each Tau run to have an isolated SAG planner
    conversation, so that unrelated questions cannot contaminate each other.
18. As a Tau user, I want follow-up correction within a run to preserve SAG
    context, so that the error log is interpreted against the original answer.
19. As a TUI user, I want the completed SAG exchange summarized inline, so that
    normal transcripts remain readable.
20. As a TUI user, I want Ctrl+O to show the actual rewritten request and raw SAG
    response, so that I can diagnose prompt and planner behavior.
21. As a TUI user, I want citations and correction-attempt information in the
    expanded exchange, so that I can audit the full SQL-planning path.
22. As a TUI user, I want the SAG exchange to remain visible after session
    resume, so that the audit view is not limited to live runs.
23. As a TUI user, I want provider-returned Tau thinking blocks controlled by
    Ctrl+T, so that I can inspect available reasoning separately from SAG tool
    interaction.
24. As a TUI user, I want a clear notice when no thinking output was returned,
    so that I do not confuse hidden output with unsupported provider behavior.
25. As a security administrator, I want SAG tokens and DWS passwords removed
    from recorded exchanges, so that TUI observability does not expose secrets.
26. As a security administrator, I want bound parameter values omitted from SQL
    repair feedback, so that low-level error recovery does not expand data
    exposure.
27. As an operator, I want configurable SAG API origin, Agent id, timeout, and
    response limits, so that Tau can target the intended deployment safely.
28. As an operator, I want configuration diagnostics when the Agent id or token
    is missing, so that Tau does not silently fall back to a different behavior.
29. As an operator, I want legacy MCP planning to remain an explicit migration
    choice, so that I can deliberately retain existing behavior.
30. As an operator, I want the active planning mode visible in configuration and
    diagnostics, so that I can tell whether a query used SAG Agent or MCP.
31. As a developer, I want deterministic fake SAG Agent and fake DWS tests, so
    that planner and correction behavior can be developed without production
    credentials.
32. As a developer, I want Tau's portable agent harness to remain independent of
    SAG, HTTP, DWS, Rich, and Textual, so that the integration remains an
    application-layer extension.
33. As a maintainer, I want existing evidence, plan, authorization, and audit
    invariants preserved, so that this migration changes planning capability
    without redesigning the safety boundary.
34. As a maintainer, I want the published data-query and TUI documentation to
    describe Agent planning, correction, citations, and observability, so that
    deployment behavior is not inferred from implementation details.
35. As a security administrator, I want Tau to pin the configured SAG origin
    and Agent id while allowing that Agent to select across its financial
    knowledge sources, so Tau does not duplicate SAG source configuration.
36. As a Web user, I want to choose and diagnose the planning mode and its
    mode-specific settings, so that the active backend is explicit.

## Implementation Decisions

1. **Use an explicit three-state planning mode.** `planning_mode` is one of
   `unconfigured`, `legacy`, or `agent`, with `unconfigured` as the default.
   `unconfigured` emits a configuration diagnostic and does not register data
   tools. `legacy` explicitly selects MCP. `agent` explicitly selects SAG Agent
   chat. Agent failure never falls back to MCP.
2. **Tau owns planner history.** Each initial planning search creates an
   independent bounded in-memory planner conversation within the Tau run. The
   extension, not the model, reconstructs Agent messages. On correction, the
   model supplies `retryContext`; the extension selects the unique pending
   conversation for the exact original question and appends its stored prior
   SAG answer and failed parameterized SQL plus sanitized DWS error.

   Each stored initial question is authoritative for its conversation. A
   correction cannot replace its entity, period, indicator, or caliber. An
   ambiguous correction is rejected rather than guessed. All histories are
   cleared on a new run, extension reload, or shutdown.
3. **Do not use the message-list endpoint to ask questions.** A request that
   lists `/agents/{agent}/threads/{thread}/messages` is an observability/history
   operation. It is not the SQL-planning invocation. Native SAG thread creation,
   polling, and message listing are not required for the preferred backend.
4. **Keep native SAG threads out of the initial implementation.** This avoids
   two authoritative conversation stores, cross-session thread reuse, cleanup
   policy, and server-side history races. A future adapter may support native
   threads behind the same planner port if a deployment requires SAG-side
   durable conversations.
5. **Keep the existing four-tool safety workflow.** In Agent mode, knowledge
   search is a SAG planning turn. Citation reading remains optional. Prepare
   still validates and freezes Tau-authored parameterized SQL. Execute still
   accepts only an opaque plan id.
6. **Introduce an application-layer SQL planner port.** The Agent planner
   returns a bounded answer, citations, provider metadata, and sanitized
   request/response details. It accepts a cancellation token and never executes
   SQL. The legacy MCP knowledge adapter remains a separate compatibility port;
   it is not forced into an answer-oriented Agent result type.
7. **Treat citations as evidence.** A usable Agent citation was returned by the
   configured Agent in the current turn and has a Tau-issued stable
   `evidenceId` plus a non-empty bounded snippet. A provider chunk/document id
   is optional and controls expandability, not usability.

   Business SQL requires at least one usable citation. The existing exception
   remains: a policy-recognized schema-inspection query limited to
   `pg_catalog` and `information_schema` may prepare with empty `evidenceIds`.
8. **Use MCP only for cited-source expansion and explicit legacy mode.** Agent
   mode does not require an MCP endpoint. A citation without a readable
   chunk/document id remains visible as a snippet and is marked
   `expandable: false`.

   `data_knowledge_read` remains registered to preserve the four-tool contract.
   Reading a non-expandable citation, or reading when MCP is unavailable,
   returns a clean non-retryable `citation_expansion_unavailable` error. Tau
   does not invent an id or fall back to a new search.
9. **Preserve the existing question rewrite policy.** The planner request asks
   SAG to use its SQL templates; resolve entity alias/code and reporting
   caliber internally; normalize the period; include only requested fields;
   handle requested comparisons; and return a complete SQL answer with
   citations rather than table-structure notes or follow-up questions.
10. **Use a stable response contract.** The Agent adapter accepts standard
    non-streaming Chat Completions responses plus `sag.citations`. Streaming is
    deferred. It may be added only with the same final answer, citation,
    cancellation, and truncation semantics.
11. **Use concrete UTF-8 byte budgets.** Defaults are: rewritten request 8 KiB;
    repair context and sanitized HTTP error body 8 KiB each; Agent answer and
    raw-response display 64 KiB each; at most 5 citations; citation title 1 KiB;
    citation snippet 8 KiB; one MCP expansion 64 KiB; and one evidence bundle or
    planner transcript 256 KiB.

    All limits are applied by encoded UTF-8 bytes. Truncation is explicit and
    deterministic. The total transcript budget includes stored user requests,
    prior bounded answers, repair messages, citations, and display details.
12. **Sanitize at the service boundary.** Configured tokens and passwords are
    removed before an exchange reaches audit records, session messages, tool
    details, logs, or frontend renderers. Planner HTTP errors use sanitized
    categories and bounded server messages.
13. **Never execute planner output directly.** Tau's model writes the final
    parameterized SQL from the cited SAG planning answer. The complete answer is
    untrusted generated input. Prepare still validates the SQL AST, allowed
    objects, functions, placeholders, evidence ids, and current run scope.
14. **Repair uses parameterized SQL without bound values.** An execution failure
    produces a copyable repair context containing the frozen SQL and sanitized
    database error. Parameter arrays are not sent to SAG or rendered in the
    repair transcript.
15. **Bound and serialize planner turns.** A run permits multiple independent
    initial planning turns, each with at most two correction turns. A run-scoped
    conversation store maps evidence bundles to stable internal conversation
    ids and uses one lock around each planner turn. Concurrent initial calls are
    serialized; a correction is accepted only after that conversation's latest
    frozen plan has a DWS SQL failure.

    Each turn records its `tool_call_id` and attempt number. Current sequential
    execution is an implementation detail, not a conversation-integrity
    guarantee. Cancellation and infrastructure failure do not consume a SQL
    correction attempt.
16. **Distinguish failure classes.** Authentication, connection, malformed
    response, missing answer, missing citations, cancellation, policy rejection,
    and DWS SQL errors produce different sanitized messages. Only SQL execution
    errors invite a planner correction turn.
17. **Make completeness mode-aware.** `ResolvedDataQueryConfig.complete` first
    requires the common DWS fields and password, then mode-specific fields.
    `unconfigured` is always incomplete. `legacy` requires MCP endpoint, source
    id, and SAG token. `agent` requires Agent origin, Agent id, and SAG token;
    MCP fields are optional.

    Missing or invalid mode-specific fields produce named diagnostics and no
    tool registration. An Agent request error remains an Agent error; it never
    changes the effective mode or uses legacy MCP implicitly.
18. **Reuse the existing SAG credential.** The Agent adapter sends the SAG JWT
    in the Authorization header. Tokens remain in the credential store or
    deployment environment and never enter model tool arguments.
19. **Keep display data out of model-visible duplication.** Agent-mode model
    content has `mode`, `attempt`, `bundleId`, bounded `answer`, and `citations`.
    Each citation has `evidenceId`, `title`, `snippet`, and `expandable`.
    Provider ids stay in trusted ledger metadata unless needed for expansion.

    Legacy-mode model content retains its exact existing shape:
    `bundleId` plus `evidence[{evidenceId,title,summary}]`. It does not pretend
    MCP hits are an Agent answer. Tool descriptions and `_SEARCH_GUIDELINE` are
    selected per mode. Compatibility tests assert the unchanged legacy shape.

    Exact request/response metadata, planner mode, and provider metadata live in
    display-only tool details for rendering and restoration. They are not
    duplicated into model-visible content.
20. **Render the planner exchange through extension renderers.** Collapsed TUI
    output identifies initial planning versus correction and citation count.
    Expanded output shows Tau→SAG request, SAG→Tau response, normalized
    citations, and sanitized error feedback. Dynamic text is escaped as literal
    Rich content.
21. **Thinking remains provider-owned.** Tau displays only thinking/reasoning
    content actually returned by Tau's active model provider. The SAG planner
    view shows request, response, citations, and progress—not hidden SAG chain of
    thought. Tau never fabricates or attempts to recover private reasoning.
22. **Extend audit records compatibly.** Add optional `planner_mode`,
    `planner_attempt`, `planner_tool_call_id`, and `citation_count` fields to the
    existing `AuditRecord`. Defaults preserve older records and readers. The
    audit trail still excludes secrets, bound values, and full responses.
23. **Preserve architectural layering.** SAG Agent and MCP adapters, credentials,
    data-query configuration, Rich rendering, and TUI projection remain in the
    coding-application layer. The portable provider/model and agent-harness
    packages receive no SAG-specific types or dependencies.
24. **Document migration explicitly.** Missing `planning_mode` resolves to
    `unconfigured`, including for an existing config file. The Web UI and logs
    show a one-time migration diagnostic asking the operator to select
    `legacy` or `agent`; Tau does not infer a mode from old MCP fields.

    Connection testing reports DWS, Agent planning, and optional MCP citation
    expansion separately. A fresh session or extension reload applies changed
    planning configuration.
25. **Separate Agent origin from the legacy MCP URL.** Existing `sag_endpoint`
    continues to mean the complete legacy MCP URL, such as
    `http://localhost:8100/mcp/`. New `sag_agent_origin` contains only scheme,
    host, and optional port. It defaults to empty and is never derived from the
    MCP URL.

    Agent mode constructs
    `/api/v1/openai/{agent_id}/chat/completions` from the normalized origin. Tau
    supplies no default port: deployments must explicitly configure their
    actual origin rather than assume either 8000 or 8100.
26. **Validate URL path inputs.** `sag_agent_origin` permits only `http` or
    `https`, rejects userinfo, query, fragment, and non-root paths, and removes
    a trailing slash. `sag_agent_id` must match
    `^[A-Za-z0-9_-]{1,128}$`; slashes, dots, percent escapes, and whitespace are
    rejected.
27. **Expose mode settings through the existing Web configuration boundary.**
    The Web settings and API include planning mode, Agent origin/id, Agent
    timeout, response budgets, and conditional legacy MCP fields. Environment
    overrides remain read-only and secret responses remain boolean-only.
28. **Propagate cancellation through the planner port.** The search tool passes
    `ToolCancellationToken` to the Agent adapter. Cancellation aborts the
    in-flight HTTP request, releases response resources, returns the standard
    cancellation outcome, and does not consume a correction attempt.
29. **Move source selection to the configured Agent.** In legacy mode, Tau
    still pins `source_id`. In Agent mode, Tau pins the SAG origin and Agent id
    and delegates selection across the Agent's financial knowledge sources to
    SAG. Tau requires usable citations but does not duplicate SAG source
    configuration in a per-source allowlist. Returned `source_id` values remain
    observable provider metadata rather than an authorization boundary.

## Configuration Contract

The existing environment → user config → default precedence remains. New
fields follow the same source metadata and Web write rules as existing fields.
Planner byte settings may be lowered but cannot exceed the hard limits below.

| Field | Default | Required in | Contract |
| --- | --- | --- | --- |
| `planning_mode` | `unconfigured` | all configured use | `unconfigured`, `legacy`, or `agent` |
| `sag_endpoint` | `http://localhost:8100/mcp/` in `legacy`; empty otherwise | `legacy`; optional citation expansion in `agent` | Complete MCP URL; retains current meaning |
| `sag_source_id` | existing default | `legacy` | Trusted fixed MCP source |
| `sag_agent_origin` | empty | `agent` | Explicit normalized HTTP(S) origin; no inferred port |
| `sag_agent_id` | empty | `agent` | Trusted config; `^[A-Za-z0-9_-]{1,128}$` |
| `sag_agent_timeout_seconds` | `60` | `agent` | Integer from 1 through 300 |
| `sag_agent_answer_max_bytes` | `65536` | `agent` | May be lowered; hard maximum 64 KiB |
| `sag_citation_limit` | `5` | `agent` | May be lowered; hard maximum 5 |
| `sag_citation_snippet_max_bytes` | `8192` | `agent` | May be lowered; hard maximum 8 KiB |
| `sag_planner_transcript_max_bytes` | `262144` | `agent` | May be lowered; hard maximum 256 KiB |
| SAG token | no default | `legacy`, `agent` | Shared credential-store secret; never returned |

Environment names use the existing `TAU_SAG_*` convention, including
`TAU_SAG_PLANNING_MODE`, `TAU_SAG_AGENT_ORIGIN`, and `TAU_SAG_AGENT_ID`.
Specific names for all new fields are part of the implementation and published
configuration reference.

`complete` is false for `unconfigured`. For `legacy`, it is the conjunction of
common DWS completeness plus legacy MCP requirements. For `agent`, it is common
DWS completeness plus Agent requirements. Optional MCP expansion never affects
Agent-mode completeness.

The Web settings page is in scope. It conditionally shows Agent or legacy
fields, displays the effective mode and per-field provenance, and reports
configuration diagnostics. The Web API never returns the SAG token value.

The read payload retains `complete` and adds `planningMode` plus a bounded
`configurationDiagnostics` list of `{code, field, message}` objects. Canonical
codes include `planning_mode_unconfigured`, `agent_origin_missing`,
`agent_origin_invalid`, `agent_id_missing`, `agent_id_invalid`,
`sag_token_missing`, `legacy_mcp_endpoint_missing`, and
`legacy_source_id_missing`.

## Tool Result Contracts

Agent mode returns an answer-oriented payload. This is model-visible content;
provider ids and the raw exchange remain in trusted ledger/details data.

```json
{
  "mode": "agent",
  "attempt": 1,
  "bundleId": "opaque",
  "answer": "bounded cited SQL-planning answer",
  "citations": [
    {
      "evidenceId": "opaque",
      "title": "bounded title",
      "snippet": "bounded non-empty snippet",
      "expandable": true
    }
  ]
}
```

Legacy mode preserves the current payload exactly. It does not add `mode`,
`attempt`, or `answer` keys because existing model and compatibility behavior
depends on ranked evidence.

```json
{
  "bundleId": "opaque",
  "evidence": [
    {
      "evidenceId": "opaque",
      "title": "bounded title",
      "summary": "bounded summary"
    }
  ]
}
```

In Agent mode, a non-empty `retryContext` requests a correction. The extension
looks up the unique pending conversation whose stored original question exactly
matches the supplied question and builds this message list:

```text
user      exact bounded rewritten original question
assistant prior bounded SAG answer
user      bounded failed parameterized SQL + sanitized DWS error
```

The model does not resend the prior assistant answer. The extension rejects a
correction with no eligible failed plan, no prior Agent turn, an ambiguous or
changed original question, or an exhausted per-conversation attempt budget.

## Testing Decisions

1. Tests assert external behavior through the highest practical seams rather
   than private parsers, internal call order, or implementation-specific class
   state. Expected requests, responses, SQL, citations, visible TUI text, and
   security boundaries are literal fixtures independent of production logic.
2. **Contract-fixture gate:** capture sanitized success and error responses from
   the target SAG deployment before adapter work. Fixture tests lock the HTTP
   method, path, auth header shape, content/citation fields, provider ids, source
   ids when present, error body, and non-streaming behavior.
3. **Primary workflow seam:** execute the real registered Data Query AgentTools
   against an in-process fake SAG Agent HTTP server and fake DWS backend. Cover
   one-turn success and the complete failure-repair path: initial rewritten
   request, cited SQL answer, evidence bundle, prepare, execute failure,
   same-conversation correction request, corrected bundle, prepare, and
   successful execution.
4. The primary seam also verifies that multiple initial searches have isolated
   bundles and correction histories, concurrent planner calls are serialized,
   and a new Tau run clears all planner histories; correction attempts are
   bounded per conversation; stale bundle/plan ids fail; authentication and
   malformed Agent responses are sanitized; missing citations block business
   prepare; parameter values and credentials never appear in SAG requests or
   tool details.
5. The fake SAG Agent records HTTP method, path, headers, request messages, and
   returns deterministic Chat Completions payloads with SAG citations. Tests
   assert the Agent id is selected from trusted configuration and cannot be
   overridden by model tool arguments.
6. Existing fake-MCP tests remain as compatibility and citation-read coverage.
   They no longer stand in for SAG Agent SQL generation. A compatibility test
   proves explicit legacy mode retains the exact current result shape and
   mode-specific `_SEARCH_GUIDELINE`.
7. **TUI projection seam:** feed real tool start/end events and planner results
   through `TuiEventAdapter`, state, and the registered renderer. Verify compact
   default text, Ctrl+O-expanded request/response/citations, correction labels,
   Rich escaping, secret redaction, UTF-8 bounds, and identical rendering after
   restoring persisted tool-result details.
8. TUI thinking tests verify Ctrl+T visibility for actual provider thinking
   blocks and the no-output notice when no thinking was received. They do not
   assert or expose SAG's hidden reasoning.
9. Thin configuration tests cover all three modes, mode-aware `complete`, no
   implicit fallback, environment → user config → default precedence, URL and
   Agent-id validation, Web payloads,
   secret write-only behavior, and reload requirements.
10. Adapter tests cover non-streaming success, usable/non-expandable citations,
    source metadata, all byte budgets, HTTP/JSON failures, timeout,
    cancellation, response shape validation, and client shutdown. Real SAG/DWS
    integration remains opt-in and never runs in the default suite.
11. Concurrency tests issue overlapping planner calls in one run. They prove
    turns cannot interleave, a duplicate initial call is rejected, repair is
    allowed only after an eligible DWS failure, and cancellation does not
    consume the correction budget.
12. Schema-inspection tests preserve the empty-evidence exemption only for
    policy-recognized `pg_catalog` and `information_schema` queries. Business
    SQL tests require at least one current usable citation.
13. Reuse the existing Data Query extension workflow tests, fake MCP server,
    fake query backend, run-scoped ledger tests, TUI renderer tests, and Web
    configuration API patterns as prior art. Do not duplicate lower-level tests
    when the primary seams already cover the behavior.

## Out of Scope

- Managing SAG Agents, prompts, bound sources, or model credentials from Tau.
- Reusing one native SAG thread across Tau sessions or users.
- Implementing native SAG thread creation, polling, message-history pagination,
  deletion, or retention in the first version.
- Displaying or attempting to extract hidden chain-of-thought from Tau or SAG.
- Allowing SAG Agent to execute DWS queries or arbitrary local tools directly.
- Automatically approving or executing SQL returned by SAG.
- Replacing Tau's SQL policy, allowed-object configuration, read-only database
  account, transaction controls, frozen plans, or authorization UI.
- Persisting unbounded SAG responses, full query results, credentials, or bound
  parameter values.
- General-purpose use of SAG Agent as Tau's main coding model.
- Multiple simultaneous SAG Agents or automatic Agent selection in one data
  query run.
- Removing legacy MCP planning before existing deployments have a documented
  migration path.

## Further Notes

- This PRD is an incremental successor to the evidence-bound DWS data-query PRD.
  It changes the SQL-planning backend and observability contract, not the
  evidence ledger, frozen-plan, authorization, or read-only execution boundary.
- The predecessor is the implemented Phase 26 baseline and remains authoritative
  for shared safety behavior. This successor overrides it only for planner mode,
  SAG integration, planner conversation state, and related UI/configuration.
- PRDs retain the language in which they were authored. Tau does not maintain
  line-by-line bilingual mirrors. Scope/successor notes identify the current
  contract when documents overlap.
- SAG documents Search, Agent chat, OpenAI-compatible Agent chat, and MCP as
  distinct integration surfaces. The OpenAI-compatible endpoint provides the
  same retrieval and citation behavior as built-in Agent chat:
  https://github.com/Zleap-AI/SAG#use-sag-as-a-model-openai-compatible
- The native `/agents/{agent}/threads/{thread}/messages?limit=...` route should
  be treated as message-history observability unless the deployment's OpenAPI
  explicitly documents a write method. Deployment-specific contracts should be
  verified against its `/openapi.json` before implementation.
- The target SAG deployment contract was captured on 2026-08-26 and satisfies
  the explicit readiness gate above. The implementation and its full Tau test
  suite were completed on 2026-08-26.
- Deployment acceptance must verify the configured Agent id, its financial
  knowledge scope, token scope,
  non-streaming endpoint, timeout/cancellation behavior, and optional MCP
  citation expansion separately from DWS connectivity.
