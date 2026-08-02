from __future__ import annotations

import re

_FENCE = re.compile(r"^```(?:markdown|md)?\s*|\s*```$", re.IGNORECASE)
_PAGE_COMMENT = re.compile(r"<!--\s*(?:page|extraction)\s*:[^>]*-->", re.IGNORECASE)


def normalize_page_markdown(text: str, page_number: int, extraction_method: str) -> str:
    """Normalize a page without combining native and vision text blindly."""
    value = text.replace("\x00", "").replace("\r\n", "\n").strip()
    value = _FENCE.sub("", value).strip()
    value = _PAGE_COMMENT.sub("", value).strip()
    value = re.sub(r"\n{3,}", "\n\n", value)
    if not value:
        return ""
    return (
        f"<!-- page: {page_number} -->\n"
        f"<!-- extraction: {extraction_method} -->\n\n{value}"
    )
