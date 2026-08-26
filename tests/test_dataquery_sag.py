"""SAG MCP adapter tests (PRD testing decision 4).

Runs an in-process fake Streamable HTTP MCP server and asserts initialize
handshake, Bearer header injection, search/read mapping, JSON and SSE response
handling, error conversion, result caps and shutdown.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from tau_coding.dataquery.backends.sag import SagMcpKnowledgeBackend, _bounded, _parse_text_hits
from tau_coding.dataquery.service import KnowledgeError

pytestmark = pytest.mark.anyio


class _FakeMcpServer:
    def __init__(self, *, response_style: str = "json") -> None:
        self.requests: list[dict[str, Any]] = []
        self.authorization_headers: list[str] = []
        self.response_style = response_style
        self.seen_initialize = False
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler_factory())
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def _handler_factory(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length).decode("utf-8")
                payload = json.loads(raw)
                server.requests.append(payload)
                server.authorization_headers.append(self.headers.get("Authorization", ""))

                if payload.get("method") == "initialize":
                    server.seen_initialize = True
                    result = {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fake-sag", "version": "1"},
                    }
                elif payload.get("method") == "tools/call":
                    name = payload["params"]["name"]
                    args = payload["params"]["arguments"]
                    if name == "search":
                        result = {
                            "structuredContent": [
                                {
                                    "id": "doc-1",
                                    "title": "Orders",
                                    "summary": "myschema.orders columns",
                                },
                                {"id": "doc-2", "title": "Rules", "summary": "region codes"},
                            ]
                        }
                    elif name == "read":
                        result = {
                            "structuredContent": {
                                "id": args["documentId"],
                                "text": "# Full content\n" + ("x" * (70 * 1024)),
                            }
                        }
                    else:
                        result = {"content": []}
                else:
                    result = {}
                body = json.dumps({"jsonrpc": "2.0", "id": payload.get("id"), "result": result})
                self.send_response(200)
                self.send_header("Content-Type", server.response_style)
                self.end_headers()
                if server.response_style == "text/event-stream":
                    self.wfile.write(f"data: {body}\n\n".encode())
                else:
                    self.wfile.write(body.encode("utf-8"))

            def log_message(self, *args: Any) -> None:
                del args

        return Handler

    def start(self) -> _FakeMcpServer:
        self.thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def make_backend(server: _FakeMcpServer) -> SagMcpKnowledgeBackend:
    return SagMcpKnowledgeBackend(
        endpoint=f"http://127.0.0.1:{server.port}/mcp",
        token="secret-token",
        source_id="src-123",
        protocol_version="2025-03-26",
        probe_query="__probe__",
        search_summary_max_bytes=8 * 1024,
    )


async def test_initialize_and_bearer_injection() -> None:
    server = _FakeMcpServer().start()
    try:
        backend = make_backend(server)
        await backend.search("orders")
        assert server.seen_initialize is True
        assert all(header == "Bearer secret-token" for header in server.authorization_headers)
        await backend.close()
    finally:
        server.stop()


async def test_search_maps_hits_with_bounds() -> None:
    server = _FakeMcpServer().start()
    try:
        backend = make_backend(server)
        hits = await backend.search("orders")
        assert [hit.evidence_id for hit in hits] == ["doc-1", "doc-2"]
        assert hits[0].title == "Orders"
        assert len(hits[0].summary.encode("utf-8")) <= 8 * 1024
        await backend.close()
    finally:
        server.stop()


async def test_read_truncates_to_max_bytes() -> None:
    server = _FakeMcpServer().start()
    try:
        backend = make_backend(server)
        content = await backend.read("doc-1", max_bytes=1024)
        assert len(content.content.encode("utf-8")) <= 1024
        assert content.evidence_id == "doc-1"
        await backend.close()
    finally:
        server.stop()


async def test_read_routes_agent_citation_to_its_source_id() -> None:
    server = _FakeMcpServer().start()
    try:
        backend = make_backend(server)
        await backend.read("chunk-from-agent", source_id="finance-source-b")

        call = server.requests[-1]
        assert call["method"] == "tools/call"
        assert call["params"]["arguments"] == {
            "chunk_id": "chunk-from-agent",
            "source_id": "finance-source-b",
        }
        await backend.close()
    finally:
        server.stop()


async def test_sse_response_style() -> None:
    server = _FakeMcpServer(response_style="text/event-stream").start()
    try:
        backend = make_backend(server)
        hits = await backend.search("orders")
        assert hits[0].evidence_id == "doc-1"
        await backend.close()
    finally:
        server.stop()


async def test_http_error_becomes_knowledge_error() -> None:
    class _FailingHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            self.send_response(500)
            self.end_headers()

        def log_message(self, *args: Any) -> None:
            del args

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FailingHandler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        backend = SagMcpKnowledgeBackend(
            endpoint=f"http://127.0.0.1:{port}/mcp", token="tok", source_id="s"
        )
        with pytest.raises(KnowledgeError, match="HTTP 500"):
            await backend.search("orders")
        await backend.close()
    finally:
        httpd.shutdown()
        httpd.server_close()


async def test_shutdown_releases_client() -> None:
    server = _FakeMcpServer().start()
    backend = make_backend(server)
    await backend.search("orders")
    await backend.close()
    await backend.close()  # idempotent
    server.stop()


async def test_search_parses_sag_text_blocks() -> None:
    text = (
        "[1] 数据源（数据库表结构）（chunk_id=39f66dc8-1d72-4b62-ae7b-c14f1a2d4d96）\n"
        "数据存放于 exchange_service schema。\n\n"
        "[2] 实体主体知识源（chunk_id=78d6eb0b-b2c7-4a46-938a-7376a1076d61）\n"
        "单体 vs 合并口径说明。"
    )
    hits = _parse_text_hits(text)
    assert [hit["chunk_id"] for hit in hits] == [
        "39f66dc8-1d72-4b62-ae7b-c14f1a2d4d96",
        "78d6eb0b-b2c7-4a46-938a-7376a1076d61",
    ]
    assert hits[0]["title"] == "数据源（数据库表结构）"
    assert "exchange_service" in str(hits[0]["summary"])


async def test_byte_bound_is_respected_for_chinese_text() -> None:
    bounded = _bounded("短期借款" * 100, 17)

    assert len(bounded.encode("utf-8")) <= 17


async def test_search_parses_text_content_response() -> None:
    class _TextSearchHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if payload.get("method") == "initialize":
                result = {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake-sag", "version": "1"},
                }
            else:
                result = {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "[1] Orders table（chunk_id=chunk-1）\n"
                                "myschema.orders columns\n\n"
                                "[2] Rules（chunk_id=chunk-2）\n"
                                "region codes"
                            ),
                        }
                    ]
                }
            body = json.dumps({"jsonrpc": "2.0", "id": payload.get("id"), "result": result})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))

        def log_message(self, *args: Any) -> None:
            del args

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _TextSearchHandler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        backend = SagMcpKnowledgeBackend(
            endpoint=f"http://127.0.0.1:{port}/mcp",
            token="tok",
            source_id="src-123",
        )
        hits = await backend.search("orders")
        assert [hit.evidence_id for hit in hits] == ["chunk-1", "chunk-2"]
        assert hits[0].title == "Orders table"
        assert "myschema.orders" in hits[0].summary
        await backend.close()
    finally:
        httpd.shutdown()
        httpd.server_close()


async def test_custom_tool_and_argument_names_are_used() -> None:
    server = _FakeMcpServer().start()
    try:
        backend = SagMcpKnowledgeBackend(
            endpoint=f"http://127.0.0.1:{server.port}/mcp",
            token="tok",
            source_id="src-custom",
            search_tool="find",
            read_tool="fetch",
            arg_query="q",
            arg_source="src",
            arg_document="doc",
            question_template="{question}",
        )
        await backend.search("orders")
        await backend.close()

        search_call = next(req for req in server.requests if req.get("method") == "tools/call")
        assert search_call["params"]["name"] == "find"
        assert search_call["params"]["arguments"] == {
            "q": "orders",
            "src": "src-custom",
            "top_k": 5,
        }
    finally:
        server.stop()


def _search_queries(server: _FakeMcpServer) -> list[str]:
    return [
        str(req["params"]["arguments"]["query"])
        for req in server.requests
        if req.get("method") == "tools/call"
    ]


async def test_default_question_template_rewrites_question() -> None:
    server = _FakeMcpServer().start()
    try:
        backend = make_backend(server)
        await backend.search("京能技术 2025年4月 短期借款")
        await backend.close()

        queries = _search_queries(server)
        assert len(queries) == 1
        rewritten = queries[0]
        assert "参考知识库中的SQL模板" in rewritten
        assert "只输出与用户提问相关的字段" in rewritten
        assert "一次问答中" in rewritten
        assert "不要只返回表结构" in rewritten
        assert "京能技术 2025年4月 短期借款" in rewritten
        assert rewritten != "京能技术 2025年4月 短期借款"
    finally:
        server.stop()


async def test_empty_template_passes_question_through() -> None:
    server = _FakeMcpServer().start()
    try:
        backend = SagMcpKnowledgeBackend(
            endpoint=f"http://127.0.0.1:{server.port}/mcp",
            token="tok",
            source_id="src-123",
            question_template="",
        )
        await backend.search("orders")
        await backend.close()

        assert _search_queries(server) == ["orders"]
    finally:
        server.stop()


async def test_retry_context_is_appended_to_rewritten_question() -> None:
    server = _FakeMcpServer().start()
    try:
        backend = make_backend(server)
        await backend.search(
            "京能技术 2025年4月 短期借款",
            retry_context="SELECT bad FROM t\nERROR: column bad does not exist",
        )
        await backend.close()

        queries = _search_queries(server)
        assert len(queries) == 1
        rewritten = queries[0]
        assert "参考知识库中的SQL模板" in rewritten
        assert "上一轮根据知识库生成的SQL执行失败" in rewritten
        assert "SELECT bad FROM t" in rewritten
        assert "column bad does not exist" in rewritten
    finally:
        server.stop()


async def test_direct_sag_answer_becomes_reusable_evidence() -> None:
    """SAG's Q&A tool may answer with SQL instead of ranked chunk blocks."""

    sql_answer = (
        "SELECT bpc_rs01_00470 FROM exchange_service.bpc_zbpc_con_s001 "
        "WHERE org_code = 'E101426' AND report_period = '202504'"
    )

    class _DirectAnswerHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if payload.get("method") == "initialize":
                result = {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake-sag", "version": "1"},
                }
            else:
                result = {"content": [{"type": "text", "text": sql_answer}]}
            body = json.dumps({"jsonrpc": "2.0", "id": payload.get("id"), "result": result})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))

        def log_message(self, *args: Any) -> None:
            del args

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _DirectAnswerHandler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        backend = SagMcpKnowledgeBackend(
            endpoint=f"http://127.0.0.1:{port}/mcp",
            token="tok",
            source_id="src-123",
        )
        exchanges = []
        hits = await backend.search(
            "京能技术 2025年4月 短期借款",
            on_exchange=exchanges.append,
        )

        assert len(hits) == 1
        assert hits[0].title == "SAG 问答结果"
        assert hits[0].summary == sql_answer
        content = await backend.read(hits[0].evidence_id)
        assert content.content == sql_answer
        assert len(exchanges) == 1
        assert "参考知识库中的SQL模板" in exchanges[0].request
        assert "京能技术 2025年4月 短期借款" in exchanges[0].request
        assert exchanges[0].response == sql_answer
        await backend.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
