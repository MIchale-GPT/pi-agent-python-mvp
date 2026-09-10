---
title: "Data queries (read-only DWS)"
description: "Ask Tau natural-language questions over Huawei Cloud DWS with evidence-backed, read-only SQL."
---

Tau can answer data questions through an explicit SAG planning mode, generate a
parameterized read-only SQL statement, and execute it against Huawei Cloud DWS.
The whole flow is evidence-bound: the SQL is frozen before execution and cannot
be swapped between approval and run.

Planning has three states:

- `unconfigured` (the default): data-query tools are not registered and Tau
  reports a named configuration diagnostic;
- `legacy`: Tau searches one fixed SAG MCP source and preserves the original
  ranked-evidence result contract;
- `agent`: Tau sends each requested result slice to a configured SAG Agent,
  receives an answer plus citations in an independent evidence bundle, and
  keeps corrections in that slice's run-scoped planner conversation. Agent
  errors never fall back to MCP.

The Agent HTTP adapter follows the reviewed target-deployment contract captured
under `tests/fixtures/sag_agent/`. Agent request errors fail explicitly and
never fall back to legacy MCP.

## Prerequisites

- An accessible DWS read-only account and its credentials.
- A SAG token plus either a legacy MCP endpoint/source or a reviewed SAG Agent
  origin/id, according to the explicit planning mode.
- A database administrator-approved object allowlist (schema or `schema.table`
  entries).

The four data tools (`data_knowledge_search`, `data_knowledge_read`,
`data_query_prepare`, `data_query_execute`) only appear once the connection is
configured; start a new session or run `/reload` after saving.

## Configure the connection

In Tau Web, open the session menu and choose **数据库连接**:

- DWS host, port, database, account, SSL mode, query timeout and max rows.
- DWS password and SAG token are **write-only**: save to set or replace them,
  and the interface never reads them back.
- Select `legacy` or `agent`; old configuration files without a mode remain
  `unconfigured` until an operator makes this migration choice.
- Agent origin contains only scheme, host and optional port. Tau constructs
  `/api/v1/openai/{agent_id}/chat/completions`; the Agent id cannot come from a
  model tool argument.
- Fields controlled by environment variables are shown as read-only
  (`TAU_DWS_*`, `TAU_SAG_*`); deployment overrides win over saved values.
- **测试连接** reports DWS, the selected planner, and optional MCP citation
  expansion separately, with sanitized errors.

Environment variable overrides follow `env → user config → default`. In legacy
mode Tau pins the configured source id. In Agent mode Tau trusts the fixed SAG
origin and Agent id to select across its financial knowledge sources. Tau still
requires usable citations, but does not maintain a second per-source allowlist.

## Ask a data question

For each requested result slice, the agent will:

1. rewrite that slice into one SAG request containing its entity, normalized
   report period, indicator and requested comparison/caliber, then ask SAG to
   use its SQL templates and resolve entity codes and reporting caliber itself;
2. prepare a parameterized `SELECT` with schema-qualified tables and `%s`
   placeholders (values are never inlined into the displayed SQL);
3. ask for confirmation once, showing the frozen SQL, evidence count, timeout
   and row limit;
4. run the query in a read-only transaction and return up to 1000 rows with a
   clear truncation status (`row_limit`, `byte_limit`, `cell_limit`).

Tau does not split entity resolution, reporting caliber, table structure and
field lookup into separate SAG questions. In Agent mode, SAG's complete answer
is untrusted generated evidence—not executable authority. Tau's model still
writes the final parameterized SQL, and prepare still validates its AST,
allowlist, placeholders and current-run citation ids.

When a request names multiple reporting calibers, periods, or entities, Tau may
issue one planning search per requested value. Each returned bundle is prepared
and executed independently; Tau does not mix evidence or SQL across bundles and
aggregates all results into one final answer.

If DWS rejects the SQL statement, the tool error includes the frozen
parameterized SQL and sanitized database error as a copyable `retryContext`.
The extension—not the model—selects the unique pending conversation for the
exact original slice and reconstructs it as original request, prior SAG answer,
then SQL/error feedback. At most two correction turns are allowed per planner
conversation. Cancellation, authentication, connection and other infrastructure
failures do not open or consume a SQL-correction turn.

### Inspect the SAG exchange in the TUI

The `data_knowledge_search` row shows a compact planning result by default.
Press **Ctrl+O** to expand tool results. In Agent mode the expanded card
contains the attempt number, citations, and:

- **Tau → SAG（实际请求）** — the rewritten question or correction message
  actually sent to the configured planner;
- **SAG → Tau（原始回复）** — the bounded raw provider response.

This exchange is stored in the tool result's display-only `details`, so it is
available after resuming the session but is not duplicated into the model's
tool-result text. Configured SAG tokens and DWS passwords are redacted before
the details are recorded. The business question and SAG answer are still
session data and may appear in local session exports.

This view never exposes hidden SAG chain-of-thought. **Ctrl+T** only controls
thinking content actually returned by Tau's active model provider; Tau shows a
notice when no such output exists.

Only safe read-only SQL passes the policy check: no DDL/DML, multi-statement,
locking clauses (`FOR UPDATE`/`FOR SHARE`), `SELECT INTO`, unknown functions or
tables outside the allowlist. Every query is recorded in a bounded audit trail
(no passwords, tokens or parameter values).

## Notes and limits

- Evidence and plans are scoped to a single agent run. Re-asking a question in
  a follow-up deliberately starts a fresh search; this is expected security
  behavior, not a bug.
- Agent limits are UTF-8 byte based: 8 KiB rewritten request and repair/error
  context, 64 KiB answer/raw response, 5 citations, 1 KiB citation titles,
  8 KiB snippets, 64 KiB per MCP expansion, and 256 KiB per transcript/bundle.
- A citation needs a non-empty snippet to be usable. Its `source_id` remains
  provider metadata and is not an authorization boundary in Agent mode. A missing provider chunk
  id only makes it non-expandable; the snippet remains visible. Agent mode does
  not require MCP, and unavailable expansion returns
  `citation_expansion_unavailable` without starting another search.
- Business SQL requires current-run citation evidence. Only policy-recognized
  schema inspection limited to `pg_catalog`/`information_schema` may prepare
  with no evidence ids.
- The extension runs in CLI, TUI and Web. CLI/TUI prompt for confirmation
  before execution; non-interactive print mode refuses unless
  `TAU_DATA_AUTO_APPROVE_EXECUTE=true` is explicitly set (it still logs the
  execution to stderr).

## DWS 函数兼容

只读查询以华为云 DWS 9.1.0.x 文档为语法依据。策略中的 DWS 函数清单位于
`src/tau_coding/dataquery/dws_functions.py`，保留官方来源，扩展正则清洗、类型转换、
日期计算、数学和统计聚合函数。`~`、`~*`、`!~`、`!~*` 按正则匹配检查；
`TO_DATE`、`TO_CHAR`、`STRING_AGG` 等解析器内部别名按对应数据库函数检查。
原始 SQL 不会因此被改写。

允许函数不代表忽略参数类型或版本条件。当前仍使用 PostgreSQL 兼容解析器，
尚非 DWS 全量语法实现；清单外函数和未识别语法会被拒绝。写入、DDL、锁定查询、
系统管理、文件访问、跨库连接和未批准自定义函数仍禁止，业务表白名单继续生效。

SAG 后台的“查询语法支持”页是策略生成的静态说明。两个仓库一起发布时，在 Tau 目录执行：

```bash
uv run python ../SAG/scripts/export-query-sql-support.py
uv run python ../SAG/scripts/export-query-sql-support.py --check
```

生成页仅包含公开策略，不读取本地数据库地址、白名单内容或凭据。
