from types import SimpleNamespace

import pytest
from eduagents_api.routers.chat import _document_excerpts
from eduagents_api.routers.chat_attachments import _render_document, validate_attachment_file
from eduagents_core.gateway.base import message_text
from eduagents_core.parsing.base import Element, ParsedDocument
from fastapi import HTTPException


def test_validate_chat_document_and_image_signatures():
    assert validate_attachment_file("notes.txt", "泰勒公式".encode(), vision_enabled=True) == (
        "document",
        "text/plain",
    )
    assert validate_attachment_file(
        "question.png", b"\x89PNG\r\n\x1a\nrest", vision_enabled=True
    ) == ("image", "image/png")


def test_image_upload_requires_configured_vision_model():
    with pytest.raises(HTTPException) as exc:
        validate_attachment_file("question.jpg", b"\xff\xd8\xffrest", vision_enabled=False)
    assert exc.value.status_code == 503
    assert exc.value.detail == "VISION_MODEL_UNAVAILABLE"


def test_render_document_preserves_page_and_heading_structure():
    parsed = ParsedDocument(
        elements=[
            Element(type="heading", text="第一章", page=1, level=1),
            Element(type="paragraph", text="正文", page=1),
            Element(type="paragraph", text="下一页", page=2),
        ],
        page_count=2,
    )
    rendered = _render_document(parsed)
    assert "[第 1 页]" in rendered
    assert "# 第一章" in rendered
    assert "[第 2 页]" in rendered


def test_temporary_document_excerpts_prefer_query_matches_and_stay_bounded():
    text = "开头概览。" + ("无关内容。" * 500) + "拉格朗日中值定理的核心结论。"
    row = SimpleNamespace(filename="教材.txt", extracted_text=text)
    excerpts = _document_excerpts([row], "什么是拉格朗日中值定理")
    assert '<attachment filename="教材.txt">' in excerpts
    assert "拉格朗日中值定理" in excerpts
    assert len(excerpts) <= 60_500


def test_message_text_omits_image_data_urls():
    content = [
        {"type": "text", "text": "请分析图片"},
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,secret"},
        },
    ]
    assert message_text(content) == "请分析图片"
