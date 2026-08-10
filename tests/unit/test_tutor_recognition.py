from __future__ import annotations

import asyncio
import base64
from pathlib import Path

import pymupdf
import pytest
from docx import Document
from eduagents_core.config import Settings
from eduagents_core.gateway import ChatResult
from eduagents_worker.tutor_recognition import (
    _PROMPT,
    _document_text,
    _parse_question_response,
    _recognize_source,
)


class FakeGateway:
    def __init__(self, responses: list[str], *, delay: float = 0):
        self.responses = responses
        self.delay = delay
        self.requests = []

    async def chat(self, request):
        self.requests.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        text = self.responses[min(len(self.requests) - 1, len(self.responses) - 1)]
        return ChatResult(
            text=text,
            model="fake",
            tier="standard",
            prompt_tokens=1,
            completion_tokens=1,
            cost_cny=0,
            latency_ms=1,
            mode="stub",
        )


def _settings(**values) -> Settings:
    defaults = {
        "gateway_mode": "live",
        "model_routing_enabled": False,
        "vision_model": "fake-vision",
        "tutor_recognition_timeout_seconds": 2,
        "tutor_ocr_timeout_seconds": 1,
    }
    return Settings(**{**defaults, **values})


def _pdf(pages: list[str]) -> bytes:
    document = pymupdf.open()
    for text in pages:
        page = document.new_page(width=400, height=500)
        if text:
            page.insert_textbox(pymupdf.Rect(30, 30, 370, 470), text, fontsize=12)
    data = document.tobytes()
    document.close()
    return data


def _scanned_pdf(page_count: int) -> bytes:
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    document = pymupdf.open()
    for _ in range(page_count):
        page = document.new_page(width=400, height=500)
        page.insert_image(page.rect, stream=png)
    data = document.tobytes()
    document.close()
    return data


@pytest.mark.asyncio
async def test_normal_pdf_uses_selected_text_layer_without_vision():
    gateway = FakeGateway(['[{"question_no":"1","page_no":2,"type":"short","stem":"第二页题目","options":[],"confidence":0.9}]'])
    questions, page_count, warnings = await _recognize_source(
        _settings(),
        gateway,
        "paper.pdf",
        _pdf(["Page one exercise. " * 30, "Page two exercise. " * 30]),
        [2],
        "tenant",
        "user",
    )
    assert page_count == 2
    assert warnings == []
    assert questions[0]["page_no"] == 2
    request = gateway.requests[0]
    assert request.credential_policy == "text"
    assert request.model_override is None
    assert "Page two exercise" in request.messages[0].content
    assert "Page one exercise" not in request.messages[0].content
    assert request.json_mode is True
    assert '"questions"' in _PROMPT
    assert "JSON 数组" not in _PROMPT


def test_tutor_object_payload_parses_questions_and_historical_array_remains_compatible():
    questions, parsed = _parse_question_response('{"questions":[{"stem":"对象题"}]}')
    assert parsed is True
    assert questions == [{"stem": "对象题"}]
    historical, parsed_historical = _parse_question_response('[{"stem":"旧数组题"}]')
    assert parsed_historical is True
    assert historical == [{"stem": "旧数组题"}]


@pytest.mark.asyncio
async def test_tutor_pdf_structures_each_page_and_keeps_successful_pages_on_failure():
    gateway = FakeGateway(
        [
            '{"questions":[{"question_no":"1","page_no":1,"type":"short","stem":"第一页题目","options":[],"confidence":0.9}]}',
            '[{"question_no":"2","type":"short","stem":"截断',
            '{"questions":[{"question_no":"3","type":"short","stem":"第三页题目","options":[],"confidence":0.9}]}',
        ]
    )
    questions, page_count, warnings = await _recognize_source(
        _settings(),
        gateway,
        "paper.pdf",
        _pdf(["Page one exercise. " * 30, "Page two exercise. " * 30, "Page three exercise. " * 30]),
        [],
        "tenant",
        "user",
        request_namespace="draft:attempt:1",
    )

    assert page_count == 3
    assert [question["page_no"] for question in questions] == [1, 3]
    assert "TUTOR_PDF_TEXT_PAGE_FAILURE:2" in warnings
    assert len(gateway.requests) == 3
    assert all(request.max_tokens == 8000 for request in gateway.requests)
    labels = ["Page one", "Page two", "Page three"]
    for index, request in enumerate(gateway.requests, start=1):
        content = request.messages[0].content
        assert f"当前真实 PDF 页码：{index}" in content
        assert f"{labels[index - 1]} exercise" in content
        assert all(
            f"{label} exercise" not in content
            for other, label in enumerate(labels, start=1)
            if other != index
        )


@pytest.mark.asyncio
async def test_scanned_pdf_renders_only_selected_page_and_uses_platform_vision():
    gateway = FakeGateway([
        "## 第七题\n扫描题",
        '[{"question_no":"7","page_no":2,"type":"short","stem":"扫描题","options":[],"confidence":0.8}]',
    ])
    questions, page_count, _ = await _recognize_source(
        _settings(), gateway, "scan.pdf", _scanned_pdf(2), [2], "tenant", "user"
    )
    assert page_count == 2
    assert questions[0]["page_no"] == 2
    request = gateway.requests[0]
    assert request.credential_policy == "platform"
    assert request.model_override == "fake-vision"
    content = request.messages[0].content
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert "真实 PDF 页码是第 2 页" in content[0]["text"]
    assert gateway.requests[1].credential_policy == "text"


@pytest.mark.asyncio
async def test_tutor_visual_request_id_is_stable_per_attempt_and_changes_on_new_attempt():
    async def recognize(attempt: int) -> str:
        gateway = FakeGateway([
            "## 第七题\n扫描题",
            '[{"question_no":"7","page_no":2,"type":"short","stem":"扫描题","options":[],"confidence":0.8}]',
        ])
        await _recognize_source(
            _settings(),
            gateway,
            "scan.pdf",
            _scanned_pdf(2),
            [2],
            "tenant",
            "user",
            request_namespace=f"draft:attempt:{attempt}",
        )
        return gateway.requests[0].request_id

    first_retry = await recognize(3)
    second_retry = await recognize(3)
    new_attempt = await recognize(4)

    assert first_retry == second_retry
    assert first_retry != new_attempt
    assert "扫描题" not in first_retry
    assert "user-key" not in first_retry


@pytest.mark.asyncio
async def test_scan_vision_timeout_is_bounded():
    gateway = FakeGateway(["[]"], delay=0.05)
    with pytest.raises(ValueError, match="TUTOR_PDF_ALL_PAGES_FAILED"):
        await _recognize_source(
            _settings(tutor_ocr_timeout_seconds=0.01),
            gateway,
            "scan.pdf",
            _scanned_pdf(1),
            [1],
            "tenant",
            "user",
        )


@pytest.mark.asyncio
async def test_docx_embedded_image_warning_does_not_invent_text(tmp_path: Path):
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    image = tmp_path / "pixel.png"
    image.write_bytes(png)
    path = tmp_path / "image-only.docx"
    document = Document()
    document.add_picture(str(image))
    document.save(path)

    text, page_count, warnings = await _document_text(path.name, path.read_bytes())

    assert text == ""
    assert page_count == 0
    assert warnings == ["EMBEDDED_IMAGES_NOT_RECOGNIZED"]
