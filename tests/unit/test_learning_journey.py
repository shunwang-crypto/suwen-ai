import json
import uuid

import httpx
import pytest

from eval.learning_journey_http import CheckFailed, JourneyApi, save_report, terminal
from eval.run_learning_journey import match_topic


def test_topic_mapping_uses_one_observed_course_label():
    matching = {"kc_id": "1", "kc_name": "月食形成条件"}
    unrelated = {"kc_id": "2", "kc_name": "月相变化规律"}
    assert match_topic("月食条件", [], [matching, unrelated]) == (matching, "unique_similarity")
    assert match_topic("月食条件", ["月食形成条件"], [matching, unrelated]) == (matching, "exact")
    with pytest.raises(CheckFailed, match="ambiguous KC"):
        match_topic("月食条件", [], [matching, {"kc_id": "3", "kc_name": "月食发生条件"}])


async def test_http_receipt_keeps_request_ids_and_reads_sse_frames():
    observed = []

    def respond(request):
        observed.append(request.headers["X-Request-ID"])
        if request.method == "GET":
            return httpx.Response(200, json={"ready": True})
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream; charset=utf-8"},
            text=(
                ": heartbeat\r\n\r\n"
                'event: token\r\ndata: {"text":"一"}\r\n\r\n'
                'event: done\r\ndata: {\r\ndata: "result": {"id": "real-receipt"}}\r\n\r\n'
            ),
        )

    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(respond)
    ) as client:
        api = JourneyApi(client)
        assert (await api.request("GET", "/ready"))["ready"]
        events = await api.stream("/stream", {"message": "学"})
    assert events["token"] == [{"text": "一"}]
    assert terminal(events, "done") == {"result": {"id": "real-receipt"}}
    assert observed == api.request_ids and len(set(observed)) == 2
    assert all(uuid.UUID(request_id).hex == request_id for request_id in observed)


async def test_http_report_errors_omit_response_details_and_credentials():
    def respond(_request):
        return httpx.Response(401, json={"detail": "private credential text"})

    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(respond)
    ) as client:
        api = JourneyApi(client)
        with pytest.raises(CheckFailed, match=r"^POST /api/auth/login: HTTP_401 \(401\)$"):
            await api.request("POST", "/api/auth/login", json={"password": "test input"})


def test_serialization_failure_preserves_previous_atomic_report(tmp_path):
    path = tmp_path / "journey.json"
    save_report(path, {"status": "running"})
    with pytest.raises(TypeError):
        save_report(path, {"status": object()})
    assert json.loads(path.read_text()) == {"status": "running"}
    assert list(tmp_path.iterdir()) == [path]
