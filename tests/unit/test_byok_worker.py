from __future__ import annotations

from eduagents_core.config import Settings
from eduagents_core.gateway import ChatResult


class _Gateway:
    def __init__(self, text: str):
        self.text = text
        self.requests = []

    async def chat(self, request):
        self.requests.append(request)
        return ChatResult(
            text=self.text,
            model="fake-model",
            tier=request.tier.value,
            prompt_tokens=1,
            completion_tokens=1,
            cost_cny=0.0,
            latency_ms=0,
            mode="live",
        )


def _settings(tmp_path):
    return Settings(
        _env_file=None,
        gateway_mode="live",
        gateway_cassette_dir=str(tmp_path),
    )


async def test_worker_text_jobs_forward_owner_to_the_resolver(tmp_path):
    from eduagents_worker.enrichment import _gen_context_prefix
    from eduagents_worker.study_guide import _generate

    gateway = _Gateway(
        '{"outline":[{"title":"基础","summary":"摘要","kc_names":[]}],'
        '"suggested_questions":["如何应用？"]}'
    )
    prefix = await _gen_context_prefix(
        gateway,
        heading_path="第一章",
        section_context="上下文",
        chunk_text="正文",
        tenant_id="tenant-id",
        user_id="owner-id",
    )
    guide = await _generate(
        gateway,
        kb_name="库",
        kb_subject="数学",
        headings=["第一章"],
        kc_names=[],
        sample="正文",
        tenant_id="tenant-id",
        user_id="owner-id",
    )

    assert prefix is not None
    assert guide is not None
    assert [request.user_id for request in gateway.requests] == ["owner-id", "owner-id"]
    assert all(request.credential_policy == "text" for request in gateway.requests)


async def test_worker_vision_jobs_explicitly_stay_on_platform_credentials(tmp_path):
    from eduagents_worker.resources import _recognize

    gateway = _Gateway('[{"stem":"题目","type":"short"}]')
    settings = _settings(tmp_path)
    await _recognize(
        settings,
        gateway,
        "worksheet.png",
        b"image-bytes",
        "",
        {1: ""},
        10,
        "tenant-id",
        "owner-id",
    )

    request = gateway.requests[0]
    assert request.user_id == "owner-id"
    assert request.credential_policy == "platform"
    assert request.model_override == settings.vision_model
