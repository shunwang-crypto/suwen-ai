from __future__ import annotations

import asyncio
import base64
from pathlib import Path

import pymupdf
import pytest
from docx import Document
from eduagents_core.config import Settings
from eduagents_core.gateway import ChatResult
from eduagents_worker.tutor_recognition import _document_text, _recognize_source


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
        _settings(), gateway, "paper.pdf", _pdf(["第一页" * 30, "第二页题目" * 30]), [2], "tenant", "user"
    )
    assert page_count == 2
    assert warnings == []
    assert questions[0]["page_no"] == 2
    request = gateway.requests[0]
    assert request.credential_policy == "text"
    assert request.model_override is None
    assert "第 2 页" in request.messages[0].content
    assert "第 1 页" not in request.messages[0].content


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
