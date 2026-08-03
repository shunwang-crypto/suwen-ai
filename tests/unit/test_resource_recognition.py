from __future__ import annotations

import pymupdf
import pytest
from eduagents_core.config import Settings
from eduagents_core.document_recognition import PdfPageResult, PdfRecognitionResult
from eduagents_core.gateway import ChatResult
from eduagents_worker import resources as resource_worker
from eduagents_worker.resources import (
    _PROMPT,
    _extract_pdf_questions,
    _failed_pages_after_empty_retry,
    _filter_questions_to_retry_pages,
    _parse_question_response,
    _pending_question_is_replaceable,
    _request_id,
)


def test_resource_prompt_requires_json_object_and_object_payload_is_supported():
    assert '"questions"' in _PROMPT
    assert "JSON 数组" not in _PROMPT
    assert "JSON 对象" in _PROMPT
    response = type("Response", (), {"text": '{"questions":[{"stem":"对象题"}]}'})()
    questions, parsed = _parse_question_response(response)
    assert parsed is True
    assert questions == [{"stem": "对象题"}]


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
    def __init__(self, responses: list[str], finish_reasons: list[str | None] | None = None):
        self.responses = responses
        self.finish_reasons = finish_reasons or []
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
            finish_reason=(
                self.finish_reasons[len(self.requests) - 1]
                if len(self.finish_reasons) >= len(self.requests)
                else None
            ),
        )


def test_resource_request_id_is_bounded_stable_and_page_batch_specific():
    first = _request_id("resource:one:sha256", [1, 2])
    assert len(first) <= 64
    assert first == _request_id("resource:one:sha256", [1, 2])
    assert first != _request_id("resource:one:sha256", [2, 3])


@pytest.mark.asyncio
async def test_pdf_resource_structures_each_page_and_deduplicates():
    gateway = FakeTextGateway(
        [
            '{"questions":[{"question_no":"1","page_no":1,"type":"short","stem":"同一道题","confidence":0.8},'
            '{"question_no":"2","page_no":1,"type":"short","stem":"同一道题","confidence":0.7}]}',
            '{"questions":[{"question_no":"2","page_no":2,"type":"short","stem":"第二页题目","confidence":0.9}]}',
            '{"questions":[{"question_no":"3","page_no":3,"type":"short","stem":"第三页题目","confidence":0.9}]}',
            '{"questions":[{"question_no":"4","page_no":4,"type":"short","stem":"第四页题目","confidence":0.9}]}',
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
    assert [question["page_no"] for question in questions] == [1, 2, 3, 4]
    assert len(gateway.requests) == 4
    assert all(request.json_mode is True for request in gateway.requests)
    assert [request.credential_policy for request in gateway.requests] == ["text"] * 4
    assert all(request.request_id.startswith("resx-") for request in gateway.requests)
    assert len({request.request_id for request in gateway.requests}) == 4
    assert all(len(request.request_id) <= 64 for request in gateway.requests)
    for index, request in enumerate(gateway.requests, start=1):
        content = request.messages[0].content
        assert f"当前真实 PDF 页码：{index}" in content
        assert f"Page {index} exercise" in content
        assert all(
            f"Page {other} exercise" not in content
            for other in range(1, 5)
            if other != index
        )
    assert metadata["pages"][0]["vision_used"] is False
    assert all("image_url" not in str(request.messages[0].content) for request in gateway.requests)


@pytest.mark.asyncio
async def test_pdf_resource_keeps_other_pages_when_one_structuring_response_is_truncated():
    gateway = FakeTextGateway(
        [
            '[{"question_no":"1","type":"short","stem":"第一页题目"}]',
            '[{"question_no":"2","type":"short","stem":"截断',
            '[{"question_no":"3","type":"short","stem":"第三页题目"}]',
        ]
    )
    settings = Settings(
        _env_file=None,
        gateway_mode="live",
        vision_model="fake-vision",
        max_pdf_pages=10,
        pdf_native_min_chars=100,
        pdf_native_no_visual_min_chars=40,
    )

    questions, _page_count, failed_pages, _metadata = await _extract_pdf_questions(
        settings,
        gateway,
        _native_pdf(3),
        tenant_id="tenant",
        user_id="owner",
        resource_namespace="resource:truncated",
        max_questions=20,
    )

    assert [question["page_no"] for question in questions] == [1, 3]
    assert failed_pages == [2]
    assert len(gateway.requests) == 3
    assert all(request.max_tokens == 8000 for request in gateway.requests)


@pytest.mark.asyncio
async def test_pdf_resource_marks_finish_reason_length_as_page_failure():
    gateway = FakeTextGateway(
        [
            '{"questions":[{"question_no":"1","type":"short","stem":"第一页题目"}]}',
            '{"questions":[{"question_no":"2","type":"short","stem":"第二页题目"}]}',
        ],
        finish_reasons=["length", "stop"],
    )
    settings = Settings(
        _env_file=None,
        gateway_mode="live",
        vision_model="fake-vision",
        max_pdf_pages=10,
        pdf_native_min_chars=100,
        pdf_native_no_visual_min_chars=40,
    )

    questions, _page_count, failed_pages, _metadata = await _extract_pdf_questions(
        settings,
        gateway,
        _native_pdf(2),
        tenant_id="tenant",
        user_id="owner",
        resource_namespace="resource:finish-reason",
        max_questions=20,
    )

    assert [question["page_no"] for question in questions] == [2]
    assert failed_pages == [1]


@pytest.mark.asyncio
async def test_pdf_resource_fills_missing_page_number_from_current_page():
    gateway = FakeTextGateway(
        [
            '[{"question_no":"1","type":"short","stem":"第一页题目"}]',
            '[{"question_no":"2","type":"short","stem":"第二页题目"}]',
        ]
    )
    settings = Settings(
        _env_file=None,
        gateway_mode="live",
        vision_model="fake-vision",
        max_pdf_pages=10,
        pdf_native_min_chars=100,
        pdf_native_no_visual_min_chars=40,
    )

    questions, _page_count, failed_pages, _metadata = await _extract_pdf_questions(
        settings,
        gateway,
        _native_pdf(2),
        tenant_id="tenant",
        user_id="owner",
        resource_namespace="resource:missing-page",
        max_questions=20,
    )

    assert [question["page_no"] for question in questions] == [1, 2]
    assert failed_pages == []


def test_partial_retry_only_replaces_pending_questions_on_failed_pages():
    page_one = type("ResourceQuestion", (), {"verification_status": "pending", "page_no": 1})()
    page_two = type("ResourceQuestion", (), {"verification_status": "pending", "page_no": 2})()
    verified = type("ResourceQuestion", (), {"verification_status": "verified", "page_no": 2})()

    assert not _pending_question_is_replaceable(page_one, has_evidence=False, retry_pages={2})
    assert _pending_question_is_replaceable(page_two, has_evidence=False, retry_pages={2})
    assert not _pending_question_is_replaceable(page_two, has_evidence=True, retry_pages={2})
    assert not _pending_question_is_replaceable(verified, has_evidence=False, retry_pages={2})


def test_full_retry_can_replace_all_pending_questions_without_evidence():
    page_one = type("ResourceQuestion", (), {"verification_status": "pending", "page_no": 1})()
    page_two = type("ResourceQuestion", (), {"verification_status": "pending", "page_no": 2})()

    assert _pending_question_is_replaceable(page_one, has_evidence=False, retry_pages=set())
    assert _pending_question_is_replaceable(page_two, has_evidence=False, retry_pages=set())
    assert not _pending_question_is_replaceable(page_one, has_evidence=True, retry_pages=set())


@pytest.mark.asyncio
async def test_successful_partial_retry_clears_failed_pages(monkeypatch):
    async def recognize_retry(*_args, selected_pages=None, **_kwargs):
        assert selected_pages == [2]
        return PdfRecognitionResult(
            page_count=2,
            pages=(
                PdfPageResult(
                    page_number=2,
                    extraction_method="native_text",
                    recognition_status="ready",
                    markdown="<!-- page: 2 -->\n2. Retry question",
                ),
            ),
        )

    monkeypatch.setattr(resource_worker, "recognize_pdf", recognize_retry)
    settings = Settings(
        _env_file=None,
        gateway_mode="live",
        vision_model="fake-vision",
        max_pdf_pages=10,
        pdf_native_min_chars=100,
        pdf_native_no_visual_min_chars=40,
    )
    gateway = FakeTextGateway(
        ['[{"question_no":"2","page_no":2,"type":"short","stem":"Retry question"}]']
    )

    questions, _page_count, failed_pages, metadata = await _extract_pdf_questions(
        settings,
        gateway,
        _native_pdf(2),
        tenant_id="tenant",
        user_id="owner",
        resource_namespace="resource:retry",
        max_questions=20,
        selected_pages=[2],
    )

    assert [question["page_no"] for question in questions] == [2]
    assert failed_pages == []
    assert metadata["failed_pages"] == []


def test_partial_retry_with_only_out_of_scope_questions_keeps_failed_pages():
    wrong_page = [{"page_no": 1, "stem": "Wrong page"}]
    assert _filter_questions_to_retry_pages(wrong_page, [2]) == []
    assert _failed_pages_after_empty_retry([], [2], []) == [2]
