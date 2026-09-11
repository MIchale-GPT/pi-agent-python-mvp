import httpx
import pytest

from tau_coding.dataquery.backends.sag_agent import SagAgentSqlPlanner
from tau_coding.dataquery.service import KnowledgeError
from test_dataquery_sag_agent import _fixture

pytestmark = pytest.mark.anyio


async def test_context_rejection_is_not_retried():
    calls = []

    async def handler(request):
        calls.append(request)
        return httpx.Response(
            502,
            json={
                "error": {
                    "code": "llm_bad_request",
                    "retryable": False,
                }
            },
        )

    planner = SagAgentSqlPlanner(
        origin="http://test", agent_id="agent", token="test", transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(KnowledgeError, match="llm_bad_request"):
            await planner.plan([{"role": "user", "content": "question"}])
        assert len(calls) == 1
    finally:
        await planner.close()


@pytest.mark.parametrize("failure", [502, "timeout"])
async def test_transient_failure_retries_same_planning_request(failure):
    requests = []

    async def handler(request):
        requests.append(request.content)
        if len(requests) == 1:
            if failure == "timeout":
                raise httpx.ReadTimeout("temporary", request=request)
            return httpx.Response(failure)
        return httpx.Response(200, json=_fixture("success.json")["body"])

    planner = SagAgentSqlPlanner(
        origin="http://test", agent_id="agent", token="test", transport=httpx.MockTransport(handler)
    )
    try:
        assert (await planner.plan([{"role": "user", "content": "question"}])).answer
        assert len(requests) == 2
        assert requests[0] == requests[1]
    finally:
        await planner.close()


@pytest.mark.parametrize("status,retries,count", [(403, 1, 1), (502, 0, 1), (502, 1, 2)])
async def test_retry_limit_and_nonretryable_failures(status, retries, count):
    calls = []

    async def handler(request):
        calls.append(True)
        return httpx.Response(status)

    planner = SagAgentSqlPlanner(
        origin="http://test",
        agent_id="agent",
        token="test",
        retry_count=retries,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(KnowledgeError, match=f"HTTP {status}"):
            await planner.plan([{"role": "user", "content": "question"}])
        assert len(calls) == count
    finally:
        await planner.close()
