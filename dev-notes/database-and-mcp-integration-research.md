---
title: "Database query and MCP integration research"
---

# Database query and MCP integration research

Research date: 2026-08-24

## Question

How should Tau add database-query capability and Model Context Protocol (MCP)
capability while preserving its Pi-style architecture? In particular, should
these be skills, tools, extensions, a new plugin system, or native core code?

## Recommendation

Use Tau's existing **extension** mechanism. Do not introduce a second "plugin"
runtime, and do not implement either feature as a skill.

1. Add database access first as a project or user extension that registers two
   narrowly scoped `AgentTool` values: schema discovery and read-only query.
2. Add MCP as a packaged `tau_coding` extension that acts as an MCP host/client
   and initially exposes one stable gateway tool (`mcp`) for discovery and calls.
3. If cross-client reuse matters more than the shortest Tau implementation,
   put the database policy behind a database MCP server and let the Tau MCP
   extension consume it. Do not maintain both a direct DB implementation and an
   MCP DB implementation as independent security boundaries.
4. Keep database drivers, credentials, MCP configuration, transport lifecycle,
   consent policy, and server process management out of `tau_agent`. The
   provider-neutral `AgentTool` contract remains in `tau_agent`; the application
   integrations belong in `tau_coding`.

This follows both upstream direction and Tau's current seams. Pi deliberately
keeps MCP out of its built-in core and says workflow-specific behavior belongs
in extensions or packages ([Pi usage, “Design Principles”](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/usage.md#design-principles)).
Tau's roadmap assigns extension-contributed tools, commands, prompt guidance,
and event subscriptions to `tau_coding`, while keeping `tau_agent` portable
([Tau roadmap issue #1](https://github.com/huggingface/tau/issues/1)). Tau has
already implemented that phase: extensions register executable tools with
`register_tool`, and session lifecycle events provide startup/shutdown hooks
([Tau Phase 21 design](architecture/phase-21-extensions.md),
[published extension guide](../website/content/guides/extensions.md)).

## Why this is not a skill

Tau skills are Markdown instructions that the model reads when relevant; skill
invocation expands text into a normal prompt. They do not create a trusted
execution boundary or a database/MCP connection
([Tau skills guide](../website/content/guides/skills-and-prompts.md)). A skill is
useful later for teaching the model domain terminology or recommended query
patterns, but the capability itself must be an executable tool.

Pi makes the same conceptual split: extensions can register custom tools that
the LLM calls, while skills provide task-specific instructions
([Pi extensions](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/extensions.md),
[Pi skills](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/skills.md)).

“Package” and “extension” should also not be conflated. A Python package can be
the distribution unit, with `[tool.tau] extensions = [...]` identifying its
entry point, but its runtime capability is still a Tau extension. Tau does not
need another plugin API for these features.

## Where the integration belongs

The existing split is already sufficient:

```text
tau_agent
  AgentTool / AgentToolResult / portable tool execution
                         ^
                         | register_tool
tau_coding extension runtime
  +----------------------+----------------------+
  |                                             |
DB extension                                  MCP extension
  DB adapter + policy                          MCP client manager
  read-only query                              one client per server
                                                |
                                      stdio / Streamable HTTP
                                                |
                                           MCP servers
```

`AgentTool` already provides a JSON Schema, async executor, cancellation token,
progress callback, execution mode, and prompt/render metadata
([tool contract](../src/tau_agent/tools.py)). The extension runtime merges
registered tools into the live coding session and wraps them with Tau's tool
hooks ([extension runtime](../src/tau_coding/extensions/runtime.py)). Those are
the only portable-agent seams the integrations need.

The MCP side is explicitly a host concern. MCP defines a host as the AI
application coordinating one or more MCP clients, with one dedicated client per
server. It separates its JSON-RPC data layer from stdio or Streamable HTTP
transports ([MCP architecture](https://modelcontextprotocol.io/docs/2026-07-28/learn/architecture)).
Tau's coding application is therefore the host; `tau_agent` is not.

## Database extension design

### Tool surface

Start with two tools instead of a general database administration tool:

```text
db_schema(database?, schema?, table?)
  -> bounded table/column/view metadata

db_query(sql, params?, max_rows?)
  -> columns, rows, truncation metadata, duration
```

`db_schema` should read portable metadata such as `information_schema` and
apply configured database/schema/table allowlists. PostgreSQL documents the
information schema as SQL-standard and more portable/stable than its internal
catalogs ([PostgreSQL information schema](https://www.postgresql.org/docs/current/information-schema.html)).
Do not inject an entire production schema into every model turn; discover only
what is needed and cap the returned text/structured data.

`db_query` should accept values separately from SQL. For PostgreSQL, Psycopg
sends bound values separately and explicitly warns against `%`, `+`, or other
manual string composition. Dynamic identifiers require its `sql.Identifier`
composition API rather than value placeholders
([Psycopg parameter binding](https://www.psycopg.org/psycopg3/docs/basic/params.html),
[Psycopg safe SQL composition](https://www.psycopg.org/psycopg3/docs/api/sql.html)).

### Security boundary

Enforce safety in the database, not only by inspecting model-generated SQL:

- Use a dedicated least-privilege login. Grant only connection, schema usage,
  and `SELECT` on approved tables or, preferably, curated views. PostgreSQL's
  privileges are enforced per object and command
  ([PostgreSQL privileges](https://www.postgresql.org/docs/current/ddl-priv.html),
  [GRANT](https://www.postgresql.org/docs/current/sql-grant.html)).
- Start every query in a database-enforced read-only transaction. PostgreSQL
  disallows data-changing and DDL commands against non-temporary objects in a
  read-only transaction, while documenting that this is a high-level notion and
  not a promise of zero disk writes
  ([PostgreSQL `SET TRANSACTION`](https://www.postgresql.org/docs/current/sql-set-transaction.html)).
- Treat “SQL begins with `SELECT`” as a usability check, never the security
  boundary. PostgreSQL permits data-modifying statements inside a `WITH` clause
  of `SELECT`, and `VOLATILE` functions can modify the database
  ([PostgreSQL `SELECT`](https://www.postgresql.org/docs/current/sql-select.html),
  [function volatility](https://www.postgresql.org/docs/current/xfunc-volatility.html)).
- Reject multiple statements. Apply a per-session `statement_timeout`, a lock
  timeout, maximum returned rows, maximum cell/text size, maximum total result
  bytes, and connection-pool limits. PostgreSQL's `statement_timeout` aborts a
  statement exceeding the configured duration
  ([PostgreSQL client connection defaults](https://www.postgresql.org/docs/current/runtime-config-client.html#RUNTIME-CONFIG-CLIENT-STATEMENT)).
- Prefer a read replica or analytics database for production data. Where
  exposure is sensitive, expose curated views with row-level policy rather than
  broad base-table access.
- Keep DSNs and credentials outside extension source, prompts, schemas, tool
  results, and session JSONL. Load secret references from user configuration or
  environment, redact them from diagnostics, and never ask the model to supply
  credentials.
- Audit tool call ID, configured database/server alias, normalized statement or
  fingerprint, duration, row count, truncation, and outcome. Redact sensitive
  parameter values by default.
- Initially use sequential execution for database calls. It makes transaction,
  pool, timeout, cancellation, and audit semantics deterministic; concurrency
  can be enabled later with explicit pool limits.

Read-only mode is defense in depth, not a complete sandbox. The login's actual
privileges are the primary boundary, with transaction mode, allowlists, query
limits, output limits, and optional approval layered on top.

## MCP extension design

### Scope the first version to server tools

MCP servers may expose tools, resources, and prompts. The official model treats
tools as model-controlled, resources as application-controlled, and prompts as
user-controlled
([MCP server primitives](https://modelcontextprotocol.io/specification/2025-06-18/server/index)).
Tau should preserve those control semantics rather than converting everything
into model-callable tools:

- MCP tools -> Tau tools, or initially operations behind one `mcp` gateway tool.
- MCP resources -> explicit `mcp_read_resource` operation or a future context/UI
  picker; do not inject every resource into the system prompt.
- MCP prompts -> explicit slash command/picker; do not let the model silently
  invoke a user-controlled prompt.

The first version should support `tools/list` and `tools/call` only. The MCP
spec explicitly cites database queries as a tool use case and requires tool
input schemas
([MCP tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)).

### Why start with one gateway tool

Tau extension `setup()` is synchronous, while MCP connection and discovery are
asynchronous. More importantly, Tau composes its harness tool list when the
coding session is created; registering an arbitrary set of discovered tools
after `session_start` does not currently trigger an atomic tool-list/system-
prompt rebuild. A fixed gateway registered during `setup()` can connect lazily
inside its async executor and expose operations such as:

```text
mcp(operation="list_servers")
mcp(operation="list_tools", server="analytics")
mcp(operation="call", server="analytics", tool="query", arguments={...})
```

This is an incremental compatibility choice, not the desired final user
experience. It avoids changing Tau core, handles many servers without injecting
every remote schema into every provider request, and leaves dynamic discovery
inside the MCP client manager.

If direct MCP tools become a product requirement, first add a narrow
`tau_coding` seam that atomically replaces extension-contributed tools and
rebuilds the system prompt/harness registry after discovery or a tool-list
change. The MCP specification allows tool lists to change and provides a change
notification; clients must therefore have an intentional refresh policy
([MCP tools: capabilities and list changes](https://modelcontextprotocol.io/specification/2026-07-28/server/tools#capabilities)).
This seam belongs in `tau_coding`, not `tau_agent`.

### Client manager and lifecycle

Use the official Python SDK (`mcp`, current stable v2) behind a small Tau-owned
adapter. Its high-level client has one async lifecycle, supports in-process
servers, URL-based Streamable HTTP, and custom transports such as stdio, and
exposes typed list/call/read operations
([official Python client](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/client/index.md),
[client transports](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/client/transports.md),
[installation/versioning](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/get-started/installation.md)).
Pin a compatible major/minor range because the official docs identify v2 as a
breaking major release and the protocol is still evolving.

Proposed lifecycle:

1. `setup()` registers the gateway tool and event handlers without I/O.
2. `session_start` creates a manager, validates trusted configuration, and may
   warm configured connections; tool execution may also connect lazily.
3. The manager owns exactly one client per configured MCP server.
4. Calls propagate Tau cancellation and enforce connection/call timeouts.
5. `session_shutdown` closes clients and stdio child processes. `/reload` must
   fully close the outgoing generation before opening replacements, matching
   Tau's documented extension lifecycle.

For local servers, prefer stdio with an explicit executable/argument list,
fixed working directory, and allowlisted environment. For remote servers, use
Streamable HTTP with TLS and explicit authentication. The protocol defines
stdio and Streamable HTTP as its standard transports
([MCP transports](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports)).

### Mapping MCP to Tau

- Preserve the remote `inputSchema` as JSON Schema and validate it before
  dispatch.
- Namespace direct tool names with the configured server alias. MCP only
  guarantees name uniqueness within one server and recommends a client-side
  disambiguation strategy when aggregating servers
  ([MCP tool names](https://modelcontextprotocol.io/specification/2026-07-28/server/tools#tool-names)).
- Map MCP text and image blocks to Tau `TextContent`/`ImageContent`. Store bounded
  structured output in `AgentToolResult.details`. Tau currently has no audio or
  resource-link content block, so v1 should return a concise text placeholder or
  URI and a diagnostic rather than silently dropping content.
- Translate MCP `isError` and protocol errors into raised Tau tool errors so the
  existing loop records `ToolResultMessage.is_error=True`.
- Enforce result byte/item limits before content enters Tau's transcript or a
  model request.
- Do not enable MCP sampling, elicitation, roots, tasks, resources, or prompts
  until each has an explicit Tau UX and authorization policy. Capability
  negotiation should advertise only what Tau actually implements.

### Trust, consent, and configuration

Treat an MCP server as executable/integration code, not as passive data. MCP's
tool specification says there should be a human able to deny calls, clients
should show exposed tools and invocation indicators, and tool annotations are
untrusted unless the server is trusted
([MCP tools: interaction and trust](https://modelcontextprotocol.io/specification/2026-07-28/server/tools#user-interaction-model)).
The broader security guidance requires input validation, access controls,
timeouts, result validation, logging, and user control
([MCP security best practices](https://modelcontextprotocol.io/docs/2026-07-28/tutorials/security/security_best_practices)).

Therefore:

- User-level MCP configuration may load by default; project-level MCP config
  and stdio commands should require the same explicit project trust posture as
  project extensions. Tau already keeps project extensions off by default
  because they execute arbitrary Python
  ([Tau extension security](../website/content/guides/extensions.md#where-extensions-live)).
- Show server alias, transport, and exposed tools in `/tools` or a future `/mcp`
  command. Show every invocation in normal tool event rendering.
- Default sensitive or mutating servers/tools to confirmation. A trusted,
  explicitly read-only database server may be configured for automatic calls.
- Never derive executable stdio commands, environment variables, URLs, or auth
  headers from model output. They come only from trusted user/project config.
- Set per-server allowlists/denylists and stable aliases in configuration. Do
  not rely on server-reported `serverInfo.name` as an identity or collision key.
- Log server alias, remote tool name, call ID, duration, outcome, and bounded
  argument metadata, with secret/PII redaction.

## Choosing direct DB vs. database-over-MCP

| Need | Recommended route |
| --- | --- |
| Fastest safe capability for Tau only | Direct DB Tau extension |
| Same DB capability shared with editors/other agents | DB MCP server + Tau MCP extension |
| Existing trusted DB MCP server already available | Tau MCP extension only |
| Domain-specific known queries | Narrow named tools/views, direct or MCP; avoid arbitrary SQL |
| General ad-hoc analytics | Read-only query tool with DB-enforced role, limits, audit, and schema discovery |

A general MCP client does not make an unsafe database server safe. Whichever
component owns the DB connection must enforce the database role, read-only
transaction, parameter binding, schema/table policy, timeouts, result caps, and
audit trail.

## Suggested delivery phases

### Phase A: read-only database extension

- PostgreSQL first, behind a small typed adapter so other engines are not baked
  into the Tau tool contract.
- `db_schema` and `db_query` tools, fixed configuration aliases, no credentials
  in tool arguments.
- Fake adapter tests plus an opt-in real PostgreSQL integration suite.
- Tests for parameter binding, multi-statement rejection, timeouts, row/byte
  truncation, redaction, cancellation, and read-only failures.
- Beginner-facing `dev-notes` implementation journal and `website/content`
  guide, per the project instructions.

### Phase B: MCP tools gateway extension

- Official Python SDK v2; stdio and Streamable HTTP.
- Fixed gateway tool, explicit server aliases, lazy connections, tool list/call,
  shutdown/reload cleanup, and bounded result conversion.
- In-memory fake MCP servers for deterministic tests. The official SDK supports
  `Client(server)` specifically as an in-process test/embedding path
  ([Python SDK testing pattern](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/get-started/index.md)).
- No resources, prompts, sampling, elicitation, roots, or tasks in v1.

### Phase C: optional first-class MCP UX

- Add an atomic dynamic-tool refresh seam in `tau_coding` only.
- Direct namespaced MCP tools, change notifications, `/mcp` status/config UX,
  and confirmation policies.
- Add resources and prompts only with their proper application/user-controlled
  interaction models.
- Consider packaging/install UX after the runtime behavior is stable; packaging
  should distribute the extension rather than define a second plugin system.

## Decision

The architecture-aligned answer is **extension first**:

- Database capability is a direct, read-only custom tool extension unless
  interoperability justifies a database MCP server.
- MCP capability is an MCP client/host extension in `tau_coding`, initially
  behind one gateway tool.
- Skills may add query/domain guidance but never own connections or authority.
- No new plugin subsystem, and no database/MCP dependency in `tau_agent`.
