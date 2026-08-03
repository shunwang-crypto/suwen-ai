from __future__ import annotations

import pymupdf
import pytest
from eduagents_core.config import Settings
from eduagents_core.gateway import ChatResult
from eduagents_worker.resources import _extract_pdf_questions


def _native_pdf(page_count: int = 4) -> bytes:
    document = pymupdf.open()
    for page_number in range(1, page_count + 1):
        page = document.new_page(width=420, height=560)
        page.insert_textbox(
            pymupdf.Rect(30, 40, 390, 520),
            f"{page_number}. Page {page_number} exercise statement. " + "Solve the expression carefully. " * 20,
            fontsize=11,
        )
    data = document.tobytes()
    document.close()
    return data


class FakeTextGateway:
    def __init__(self, responses: list[str]):
        self.responses = responses
        self.requests = []

    async def chat(self, request):
        self.requests.append(request)
        return ChatResult(
            text=self.responses[len(self.requests) - 1],
            model="fake-text",
            tier="standard",
            prompt_tokens=1,
            completion_tokens=1,
            cost_cny=0,
            latency_ms=1,
            mode="stub",
        )


@pytest.mark.asyncio
async def test_pdf_resource_extracts_small_ordered_batches_and_deduplicates():
    gateway = FakeTextGateway(
        [
            '[{"question_no":"1","page_no":1,"type":"short","stem":"同一道题","confidence":0.8},'
            '{"question_no":"2","page_no":1,"type":"short","stem":"同一道题","confidence":0.7}]',
            '[{"question_no":"4","page_no":4,"type":"short","stem":"第四页题目","confidence":0.9}]',
        ]
    )
    settings = Settings(
        _env_file=None,
        gateway_mode="live",
        vision_model="fake-vision",
        max_pdf_pages=10,
        pdf_vision_max_pages=10,
        pdf_native_min_chars=100,
        pdf_native_no_visual_min_chars=40,
    )

    questions, page_count, failed_pages, metadata = await _extract_pdf_questions(
        settings,
        gateway,
        _native_pdf(),
        tenant_id="tenant",
        user_id="owner",
        resource_namespace="resource:stable",
        max_questions=20,
    )

    assert page_count == 4
    assert failed_pages == []
    assert [question["page_no"] for question in questions] == [1, 4]
    assert [request.credential_policy for request in gateway.requests] == ["text", "text"]
    assert all(request.request_id.startswith("resource-extract-") for request in gateway.requests)
    assert len({request.request_id for request in gateway.requests}) == 2
    assert metadata["pages"][0]["vision_used"] is False
    assert all("image_url" not in str(request.messages[0].content) for request in gateway.requests)
