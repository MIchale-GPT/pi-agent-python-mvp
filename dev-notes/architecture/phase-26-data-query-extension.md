---
title: "Phase 26: Evidence-bound DWS data-query extension"
---

The data-query extension turns a Tau session into a read-only DWS analyst. In
Agent mode it asks one fixed SAG Agent to plan across that Agent's configured
financial knowledge sources; in explicit legacy mode it retrieves knowledge
from one fixed MCP source. Tau then freezes a policy-checked parameterized
SELECT and executes it against Huawei Cloud DWS in a read-only transaction,
with the SQL bound to the current run's evidence. Design decisions are recorded
in `docs/PRD-evidence-bound-data-query.md` and its incremental SAG Agent PRD;
this note explains how the implementation maps onto Tau's architecture.

The Phase 26 baseline supports explicit `planning_mode="legacy"` and
`planning_mode="agent"`. The Agent planner uses the reviewed target-deployment
contract recorded in `dev-notes/sag-agent-contract-readiness.md`; its real HTTP
adapter, run-scoped conversation state, evidence normalization, renderer, and
correction workflow are enabled without falling back to MCP on Agent errors.

## What was added

- `tau_coding/dataquery/` — a bundled extension package:
  - `config.py` — durable user config (`~/.tau/dataquery.json`), env overrides
    (`TAU_DWS_*`, `TAU_SAG_*`, `TAU_DATA_AUTO_APPROVE_EXECUTE`), write-only
    secrets via the credential store, per-field `source` provenance.
  - `policy.py` — fail-closed SQL policy on sqlglot's PostgreSQL AST:
    single read-only SELECT roots, schema-qualified allowlisted tables, an
    approved-function allowlist plus a dangerous-function denylist, and
    token-level rejection of unapproved schema-qualified calls.
  - `ledger.py` — run-scoped `EvidenceLedger`/`QueryPlanStore` with opaque
    128-bit handles; every lookup validates session + run + stage.
  - `service.py` — `DataQuestionService`: the workflow state machine
    (search → optional read → prepare → execute), bounded evidence bundles,
    positional-parameter validation, truncation-aware execution and a
    bounded audit trail (no parameter values, no secrets).
  - `backends/` — `sag.py` (minimal Streamable HTTP MCP client over httpx,
    Bearer injection, JSON/SSE handling, question rewriting, ranked-hit
    parsing and cached direct-answer evidence) and `dws.py` (psycopg backend
    with per-transaction `READ ONLY`, `statement_timeout`, safe `search_path`,
    a cancel-monitor task calling driver `cancel()`), plus fakes for tests.
  - `extension.py` — the `setup(tau)` entry point registering the four tools
    `data_knowledge_search`, `data_knowledge_read`, `data_query_prepare`,
    `data_query_execute`, plus an authorization view for the execute dialog.
- `src/tau_coding/data/extensions/data_query/` — the packaged extension entry
  (manifest + loader stub) so the feature reloads like any extension.
- Extension API additions (`tau_coding/extensions/`): `register_diagnostic`
  and `register_authorization_view` for host confirmation enrichment.
- Web: `/api/dataquery` GET/POST and `/api/dataquery/test`, the browser
  "数据库连接" settings dialog, per-tool authorization classification
  (search/read/prepare auto-approved; execute host-confirmed with a plan
  payload), and a confirming UI bridge so the host dialog is the single gate.
- Session wiring: the bundled extension is discovered only once the user
  begins configuring data queries (config file or env), so unconfigured
  sessions behave exactly as before (PRD decision 20).

## SAG Q&A adaptation and SQL repair

The configured SAG tool is a Q&A surface, not only a ranked document search.
Tau rewrites one complete user question into an instruction that asks SAG to
use its SQL templates, resolve entity aliases/codes and single/consolidated
caliber internally, normalize report periods, and return only the requested
SQL fields. The agent guideline explicitly avoids separate searches for each
of those facts.

SAG deployments return either ranked blocks with `chunk_id` values or a direct
SQL answer as a plain MCP text block. Ranked blocks retain their remote ids. A
plain answer is bounded to 64 KiB, assigned an opaque content-derived id, and
cached inside the backend so it can participate in the same evidence ledger
and optional read flow without sending a synthetic id to `get_chunk`.

When DWS rejects a frozen plan, `DataQuestionService` returns a bounded repair
message containing the parameterized SQL and sanitized database error (never
the bound parameter values). The message tells the model to pass that block as
`retryContext` on the same question. `SagMcpKnowledgeBackend` appends it to the
rewrite as explicit correction feedback, after which the normal evidence →
prepare → execute gates apply again.

The knowledge backend also emits a bounded provider-neutral
`KnowledgeExchange` containing the actual rewritten request and raw response.
`DataQuestionService` redacts configured secrets, and the extension stores the
exchange only in `AgentToolResult.details`; the model-visible evidence JSON is
unchanged. The data-query tool renderer shows a compact success line by default
and renders the full Tau→SAG/SAG→Tau exchange when the TUI expands tool results
with Ctrl+O. Because details are part of `ToolResultMessage`, the same view works
after session resume without coupling `tau_agent` or the knowledge backend to
Textual.

## Incremental SAG Agent planning boundary

The configuration model now requires an explicit three-state planning mode:
`unconfigured`, `legacy`, or `agent`. Missing mode never infers legacy from old
MCP fields. Legacy retains its exact model-visible evidence shape. Agent mode
uses an application-layer `SqlPlanner` port and returns a bounded answer plus
Tau-issued citation ids; provider ids remain ledger-only and are used solely
for optional MCP expansion.

Within one run, the service stores the rewritten initial request and prior SAG
answers under a serialized conversation state. After an eligible DWS SQL
error, a non-empty `retryContext` is only a correction trigger: the extension
ignores its text and constructs feedback from the frozen parameterized SQL and
sanitized backend error. Connection/driver failures and cancellation do not
open a correction. A new run clears the planner conversation.

All SAG content is treated as untrusted. Tau's model authors the SQL passed to
prepare, and the existing evidence, policy, freeze, authorization, and
read-only execution gates remain authoritative. Agent planning adds no SAG
types to `tau_agent` and no Textual dependency to the service or adapter ports.

## Why it exists

Tau could already call local tools, but not safely run the
"natural-language question → evidence → frozen SQL → read-only DWS" pipeline.
A plain `db_query(sql)` cannot prove the SQL derives from current knowledge,
cannot freeze it before execution, and leaks connection credentials into tool
arguments. The extension makes the *process* verifiable: evidence must come
from this run's knowledge search, the plan is frozen at prepare, and execute
accepts only an opaque plan id. The database layer stays the real boundary
(read-only account, minimal privileges, read-only transaction); the SQL policy
is an early interception layer.

## How it maps to Pi / Tau design

- Extensions stay in `tau_coding`; `tau_agent` remains portable (only the
  `AgentTool`/`AgentToolResult` contract is used).
- The workflow is plain extension tools + lifecycle subscriptions
  (`agent_start`/`agent_end` for run scope, `session_shutdown` for cleanup) —
  no new loop or harness concepts.
- Hosts differ only in authorization: Web uses `before_tool_call`, TUI uses
  `context.ui.confirm`, print CLI is fail-closed with an env auto-approve
  escape hatch. The Web installs a confirming `UiBridge` so extension-level
  confirmations never double-gate after the browser dialog.

## How to test / use it

- Unit/integration: `uv run pytest tests/test_dataquery_*.py` (policy table,
  workflow with fakes, UTF-8/transcript bounds, fake-driver DWS, fake-MCP SAG,
  registered-tool calls through an in-process SAG Agent HTTP server, Web config
  API, TUI projection, and session gating).
- Manual: configure credentials via the Web "数据库连接" dialog (password is
  write-only), then ask a data question in a fresh session; the trace shows
  search → prepare → execute with a single confirmation before execution.
- Real DWS/SAG integration requires deployment secrets (`TAU_SAG_TOKEN`,
  credential store) and the optional `psycopg` driver. Agent chat is implemented
  by `backends/sag_agent.py`; optional citation expansion and explicit legacy
  planning use `backends/sag.py`.
