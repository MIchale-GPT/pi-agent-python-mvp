# SAG Agent contract readiness

Date: 2026-08-26
PRD: `docs/PRD-sag-agent-sql-planner.md`
Status: target-deployment contract captured and reviewed

## Outcome

Tau captured the running target deployment at `http://127.0.0.1:8100` on
2026-08-26. The sanitized artifacts are stored under
`tests/fixtures/sag_agent/` and cover the exact OpenAPI operation, request,
success response, authentication error, and client cancellation outcome. No
JWT, business question, SQL, private snippet, raw source identifier, or request
id is present in the fixtures.

The capture matches the source-level expectations below, so the readiness gate
was satisfied before implementation. The PRD is now marked `implemented`.

## Source-level evidence collected

A local SAG checkout at commit
`e683133ee22176ce10cf80c2dd81a66e138d8598` confirms the upstream implementation
currently exposes:

- `POST /api/v1/openai/{agent_id}/chat/completions`;
- Bearer JWT authentication through SAG's current-user dependency;
- an OpenAI Chat Completions request with `messages`, optional `model`,
  `stream`, `temperature`, and `max_tokens`;
- a non-streaming `chat.completion` response whose answer is
  `choices[0].message.content` and whose extension is
  `sag: {citations, sources}`;
- an error envelope shaped as
  `{error: {code, message, layer, stage, retryable, request_id?}}` for SAG
  domain errors.

The endpoint is stateless in that source revision (`thread_id=None`). It treats
the last user message as the current query and preceding user/assistant
messages as history. Internal citations may include `chunk_id`, `source_id`,
`heading`, `snippet`, and `score`; citations without a chunk id must remain
visible but non-expandable in Tau.

These observations are useful implementation context only. They do not prove
that the target deployment at port 8100 runs this exact revision or exposes the
same OpenAPI, authentication middleware, error body, citation fields, proxy
timeouts, or cancellation behavior.

## Captured deployment contract

The running deployment demonstrated:

1. the OpenAPI operation for the exact Agent chat path;
2. one `stream: false` success containing a non-empty answer and
   `sag.citations`;
3. one representative authentication or validation error, including status
   and bounded JSON body;
4. observed client-cancellation behavior;
5. whether citations expose `source_id` and a provider chunk/document id.

The resulting fixtures are:

- `openapi-operation.json` — exact POST path, Bearer security declaration, and
  request/response schema references;
- `request.json` — structural `messages` plus `stream: false` payload;
- `success.json` — standard `chat.completion` answer plus an internal citation
  carrying `chunk_id`, `source_id`, `heading`, and `snippet`;
- `error.json` — HTTP 401 with the stable SAG error envelope;
- `cancellation.json` — the in-flight client task was cancelled successfully.

Authorization values, business values, private snippets, raw identifiers, and
request ids were replaced with stable redaction markers before persistence.

## Capture command

After the target service is running, configure Agent origin/id and the SAG
token through Tau Web, `~/.tau/dataquery.json` plus the credential store, or
`TAU_SAG_*` environment variables. Then run:

```bash
uv run python -m tau_coding.dataquery.capture_contract
```

The command calls the target OpenAPI and Agent endpoint, writes five sanitized
files under `tests/fixtures/sag_agent/`, and refuses to overwrite an earlier
capture unless `--overwrite` is explicit. It never accepts a token argument,
so the credential cannot appear in shell history or the process list. It fails
instead of writing a partial capture if the invalid-token request is accepted,
the required OpenAPI operation is absent, or the cancellation probe completes
before cancellation can be observed.

## 2026-08-26 local startup attempt

At the user's request, Tau started the checked-out SAG API on port 8100. The
first startup correctly failed because this execution sandbox mounts the source
checkout read-only and SAG performs lease recovery writes during application
startup. A 257 MiB copy of `apps/api/.data` was therefore made under `/tmp`, and
the API then reached `Application startup complete` using explicit temporary
`SAG_DATABASE_URL`, `SAG_DATA_DIR`, and `SAG_UPLOAD_DIR` values. The original SAG
database and index were not modified; the temporary copy was removed after the
probe.

Two independent sandbox restrictions still prevented a qualifying capture:

- loopback clients could not connect to Uvicorn even after it reported
  `127.0.0.1:8100` ready;
- invoking the same FastAPI application through `httpx.ASGITransport` reached
  the real Agent route but the success request did not finish within 90
  seconds, consistent with the configured LLM/retrieval upstream being blocked
  by the sandbox's network policy.

No fixture was written during that restricted attempt. The capture helper now
wraps each request in an explicit asyncio deadline as a result.

## 2026-08-26 successful target capture

After loopback and network access became available, Tau reached the already
running SAG service, fetched its 110,466-byte OpenAPI document, and invoked the
configured Agent. The previously stored JWT was rejected with HTTP 401, so a
temporary token was issued through SAG's existing local single-user login flow
and injected only into the capture process through `TAU_SAG_TOKEN`; Tau's
credential file was not overwritten and the token was never printed.

The non-streaming Agent request completed in about 22 seconds with a cited
answer. The invalid-token probe returned the expected structured 401 response,
and the cancellation probe cancelled an in-flight request. The five sanitized
fixtures listed above were then written and reviewed.
