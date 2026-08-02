from __future__ import annotations

import asyncio
import base64

import pymupdf
import pytest
from eduagents_core.document_recognition import (
    PdfRecognitionPolicy,
    inspect_pdf,
    recognize_pdf,
    render_pdf_page,
)
from eduagents_core.gateway import ChatResult

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class FakeVisionGateway:
    def __init__(self, responses: list[str | Exception]):
        self.responses = responses
        self.requests = []

    async def chat(self, request):
        self.requests.append(request)
        response = self.responses[len(self.requests) - 1]
        if isinstance(response, Exception):
            raise response
        return ChatResult(
            text=response,
            model="fake-vision",
            tier="standard",
            prompt_tokens=1,
            completion_tokens=1,
            cost_cny=0,
            latency_ms=1,
            mode="stub",
        )


def _pdf(*kinds: str) -> bytes:
    document = pymupdf.open()
    for index, kind in enumerate(kinds, start=1):
        page = document.new_page(width=400, height=500)
        if kind == "native":
            page.insert_textbox(
                pymupdf.Rect(30, 90, 370, 450),
                (f"Page {index} complete native exercise text and formula x squared. " * 20),
                fontsize=11,
            )
        elif kind == "short":
            page.insert_textbox(
                pymupdf.Rect(30, 180, 370, 320),
                "Solve x + 2 = 5 and explain each algebraic step. " * 2,
                fontsize=11,
            )
        elif kind == "scanned":
            page.insert_image(page.rect, stream=_PNG)
        elif kind == "mixed":
            page.insert_text((30, 35), f"Page {index} title", fontsize=12)
            page.insert_image(pymupdf.Rect(20, 80, 380, 470), stream=_PNG)
        else:
            raise AssertionError(kind)
    data = document.tobytes()
    document.close()
    return data


def _text_pdf(text: str) -> bytes:
    document = pymupdf.open()
    page = document.new_page(width=400, height=500)
    page.insert_textbox(pymupdf.Rect(30, 90, 370, 450), text, fontsize=11)
    data = document.tobytes()
    document.close()
    return data


def _policy(**changes) -> PdfRecognitionPolicy:
    values = {
        "max_pages": 10,
        "max_vision_pages": 5,
        "vision_concurrency": 1,
        "page_timeout_seconds": 1,
        "document_timeout_seconds": 3,
    }
    return PdfRecognitionPolicy(**{**values, **changes})


def test_inspection_classifies_native_scanned_and_mixed_pages_independently():
    result = inspect_pdf(_pdf("native", "scanned", "mixed"), _policy())

    assert [page.extraction_method for page in result.pages] == [
        "native_text",
        "scanned",
        "mixed",
    ]
    assert [page.page_number for page in result.pages] == [1, 2, 3]
    assert result.pages[1].native_text_chars == 0
    assert result.pages[2].image_count == 1


@pytest.mark.asyncio
async def test_native_pdf_uses_text_layer_without_vision():
    gateway = FakeVisionGateway([])
    result = await recognize_pdf(
        _pdf("native"),
        policy=_policy(),
        gateway=gateway,
        vision_model="fake-vision",
        tenant_id="tenant",
        user_id="user",
    )

    assert gateway.requests == []
    assert result.pages[0].extraction_method == "native_text"
    assert result.pages[0].vision_used is False
    assert "<!-- page: 1 -->" in result.markdown
    assert "complete native exercise" in result.markdown


@pytest.mark.asyncio
async def test_trusted_short_text_without_visuals_uses_native_without_vision():
    gateway = FakeVisionGateway([])
    inspection = inspect_pdf(_pdf("short"), _policy())
    assert inspection.pages[0].native_text_chars < 180
    assert inspection.pages[0].extraction_method == "native_text"

    result = await recognize_pdf(
        _pdf("short"),
        policy=_policy(),
        gateway=gateway,
        vision_model="fake-vision",
        tenant_id="tenant",
        user_id="user",
    )

    assert gateway.requests == []
    assert result.pages[0].vision_used is False


@pytest.mark.parametrize("text", ["?" * 160, "\ufffd" * 160, "a" * 160])
def test_repeated_or_replacement_text_layer_is_not_trusted_as_native(text):
    result = inspect_pdf(_text_pdf(text), _policy())

    assert result.pages[0].extraction_method == "mixed"
    assert result.pages[0].warning_code == "PDF_NATIVE_TEXT_INCOMPLETE"


@pytest.mark.asyncio
async def test_scanned_and_mixed_pages_use_platform_vision_and_preserve_math():
    gateway = FakeVisionGateway(
        [
            "## 扫描题\n求 $x^2$。",
            "# Page 2 title\n\n计算：$$\\sum_{n=1}^{10} n$$",
        ]
    )
    result = await recognize_pdf(
        _pdf("scanned", "mixed"),
        policy=_policy(),
        gateway=gateway,
        vision_model="fake-vision",
        tenant_id="tenant",
        user_id="user",
    )

    assert [page.extraction_method for page in result.pages] == ["scanned", "mixed"]
    assert all(page.vision_used for page in result.pages)
    assert all(request.credential_policy == "platform" for request in gateway.requests)
    assert all(request.model_override == "fake-vision" for request in gateway.requests)
    assert all(request.request_id.startswith("pdfocr-") for request in gateway.requests)
    assert len({request.request_id for request in gateway.requests}) == 2
    assert "$x^2$" in result.markdown
    assert "$$\\sum_{n=1}^{10} n$$" in result.markdown
    assert "<!-- page: 1 -->" in result.markdown
    assert "<!-- page: 2 -->" in result.markdown
    # mixed uses the whole-page visual transcription as the primary body; the
    # native title is not blindly concatenated a second time.
    assert result.pages[1].markdown.count("Page 2 title") == 1


@pytest.mark.asyncio
async def test_single_page_failure_is_bounded_and_other_pages_continue():
    gateway = FakeVisionGateway(["第一页正文", TimeoutError()])
    result = await recognize_pdf(
        _pdf("scanned", "scanned"),
        policy=_policy(),
        gateway=gateway,
        vision_model="fake-vision",
        tenant_id="tenant",
        user_id="user",
    )

    assert [page.recognition_status for page in result.pages] == ["ready", "failed"]
    assert result.pages[1].error_code == "PDF_PAGE_RECOGNITION_TIMEOUT"
    assert result.warnings == ("PDF_PARTIAL_PAGE_FAILURE:2",)
    assert "第一页正文" in result.markdown


@pytest.mark.asyncio
async def test_all_visual_pages_failed_are_reported_without_partial_markdown():
    gateway = FakeVisionGateway([RuntimeError("first"), TimeoutError()])
    result = await recognize_pdf(
        _pdf("scanned", "scanned"),
        policy=_policy(),
        gateway=gateway,
        vision_model="fake-vision",
        tenant_id="tenant",
        user_id="user",
    )

    assert result.successful_pages == ()
    assert [page.page_number for page in result.failed_pages] == [1, 2]
    assert result.warnings == ("PDF_PARTIAL_PAGE_FAILURE:1,2",)
    assert result.markdown == ""


class _OutOfOrderVisionGateway(FakeVisionGateway):
    async def chat(self, request):
        prompt = request.messages[0].content[0]["text"]
        page_number = int(prompt.split("第 ", 1)[1].split(" 页", 1)[0])
        await asyncio.sleep((4 - page_number) * 0.005)
        self.responses = [f"第 {page_number} 页识别结果"] * 3
        return await super().chat(request)


@pytest.mark.asyncio
async def test_page_request_ids_are_stable_across_retries_and_completion_order():
    data = _pdf("scanned", "scanned", "scanned")
    first_gateway = _OutOfOrderVisionGateway([])
    second_gateway = _OutOfOrderVisionGateway([])
    first = await recognize_pdf(
        data,
        policy=_policy(vision_concurrency=3),
        gateway=first_gateway,
        vision_model="fake-vision",
        tenant_id="tenant",
        user_id="user",
        request_namespace="kb:doc:version:7",
    )
    second = await recognize_pdf(
        data,
        policy=_policy(vision_concurrency=3),
        gateway=second_gateway,
        vision_model="fake-vision",
        tenant_id="tenant",
        user_id="user",
        request_namespace="kb:doc:version:7",
    )

    def ids_by_page(gateway):
        return {
            int(request.messages[0].content[0]["text"].split("第 ", 1)[1].split(" 页", 1)[0]): request.request_id
            for request in gateway.requests
        }

    first_ids = ids_by_page(first_gateway)
    second_ids = ids_by_page(second_gateway)
    assert [page.page_number for page in first.pages] == [1, 2, 3]
    assert [page.page_number for page in second.pages] == [1, 2, 3]
    assert len(set(first_ids.values())) == 3
    assert set(first_ids.values()) == set(second_ids.values())
    assert all("user-key" not in request_id for request_id in first_ids.values())


def test_render_pixel_limit_is_enforced_before_vision():
    with pytest.raises(ValueError, match="PDF_RENDER_PIXEL_LIMIT_EXCEEDED"):
        render_pdf_page(
            _pdf("native"),
            1,
            _policy(render_dpi=72, max_render_pixels=1_000),
        )


@pytest.mark.asyncio
async def test_page_and_visual_limits_fail_before_unbounded_calls():
    data = _pdf("scanned", "scanned")
    with pytest.raises(ValueError, match="PDF_PAGE_LIMIT_EXCEEDED"):
        inspect_pdf(data, _policy(max_pages=1))

    gateway = FakeVisionGateway([])
    with pytest.raises(ValueError, match="PDF_VISION_PAGE_LIMIT_EXCEEDED"):
        await recognize_pdf(
            data,
            policy=_policy(max_vision_pages=1),
            gateway=gateway,
            vision_model="fake-vision",
            tenant_id="tenant",
            user_id="user",
        )
    assert gateway.requests == []


def test_disguised_and_damaged_pdf_are_rejected_with_stable_codes():
    with pytest.raises(ValueError, match="PDF_FILE_TYPE_MISMATCH"):
        inspect_pdf(b"not a pdf", _policy())
    with pytest.raises(ValueError, match="PDF_DAMAGED"):
        inspect_pdf(b"%PDF-broken", _policy())
