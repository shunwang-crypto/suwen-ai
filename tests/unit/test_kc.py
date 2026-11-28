"""KC 知识点体系单元测试 (§5.8): 全离线 — FakeEmbedder + FakeGateway, 零网络。"""

from __future__ import annotations

import json
import math

import pytest
from eduagents_core.gateway.base import ChatRequest, ChatResult, Tier
from eduagents_core.kc import (
    KcEntry,
    KcExtractionError,
    KcNormalizer,
    KcVocabulary,
    PrereqEdge,
    add_edge_safe,
    build_vocabulary,
    extract_chunk_kcs,
    normalize_kc_name,
    topological_order,
    validate_acyclic,
)

# --- fakes ---


class FakeEmbedder:
    """dict 驱动的确定性向量; 未登记文本给零向量 (余弦按 0 处理)。记录调用供断言。"""

    dim = 3

    def __init__(self, table: dict[str, list[float]]) -> None:
        self.table = table
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [list(self.table.get(t, [0.0] * self.dim)) for t in texts]


class FakeGateway:
    """预置响应文本队列, 逐次弹出; 记录收到的 ChatRequest 供断言。"""

    def __init__(self, texts: list[str], *, reasons: list[str] | None = None) -> None:
        self.texts = list(texts)
        self.reasons = list(reasons or [])
        self.requests: list[ChatRequest] = []

    async def chat(self, req: ChatRequest) -> ChatResult:
        self.requests.append(req)
        return ChatResult(
            text=self.texts.pop(0),
            model="fake",
            tier=req.tier.value,
            prompt_tokens=1,
            completion_tokens=1,
            cost_cny=0.0,
            latency_ms=0,
            mode="stub",
            finish_reason=self.reasons.pop(0) if self.reasons else None,
        )


def make_taylor_vocab() -> KcVocabulary:
    return KcVocabulary(
        entries=[
            KcEntry(
                kc_id="kc-taylor",
                canonical_name="泰勒公式",
                aliases=["Taylor公式"],
                description="",
                embedding=None,
            )
        ]
    )


def make_embedder() -> FakeEmbedder:
    # "泰勒展开" 与 "泰勒公式" 余弦 = 0.95 (>= 0.92 归并); "洛必达法则" 正交
    return FakeEmbedder(
        {
            "泰勒公式": [1.0, 0.0, 0.0],
            "泰勒展开": [0.95, math.sqrt(1.0 - 0.95**2), 0.0],
            "洛必达法则": [0.0, 0.0, 1.0],
        }
    )


# --- KC-A1 写死用例: 同一概念三种表述收敛为同一 kc_id ---


async def test_kc_a1_three_surface_forms_converge_to_one_kc_id():
    vocab = make_taylor_vocab()
    normalizer = KcNormalizer(vocab, make_embedder())

    id1, created1 = await normalizer.match_or_create("泰勒公式")  # canonical 命中
    id2, created2 = await normalizer.match_or_create("Taylor 公式")  # 别名 (含空格) 命中
    id3, created3 = await normalizer.match_or_create("泰勒展开")  # 余弦 0.95 归并

    assert id1 == id2 == id3 == "kc-taylor"
    assert (created1, created2, created3) == (False, False, False)
    assert len(vocab.entries) == 1  # 未新建

    new_id, created = await normalizer.match_or_create("洛必达法则", description="求极限法则")
    assert created is True
    assert new_id != "kc-taylor"
    assert len(vocab.entries) == 2
    new_entry = vocab.entries[1]
    assert new_entry.canonical_name == "洛必达法则"
    assert new_entry.description == "求极限法则"
    assert new_entry.embedding == [0.0, 0.0, 1.0]  # 新建复用刚算的向量


async def test_merge_appends_new_surface_form_to_aliases():
    vocab = make_taylor_vocab()
    embedder = make_embedder()
    normalizer = KcNormalizer(vocab, embedder)

    kc_id, created = await normalizer.match_or_create("泰勒展开")
    assert (kc_id, created) == ("kc-taylor", False)
    assert "泰勒展开" in vocab.entries[0].aliases  # 归并追加了新表述
    assert vocab.entries[0].embedding is not None  # 惰性补算缓存回 entry
    embed_calls_after_merge = len(embedder.calls)

    # 归并后同表述再来: 直接别名命中, 不再触发 embedding
    kc_id2, created2 = await normalizer.match_or_create("泰勒展开")
    assert (kc_id2, created2) == ("kc-taylor", False)
    assert len(embedder.calls) == embed_calls_after_merge


async def test_merge_threshold_is_respected():
    vocab = make_taylor_vocab()
    normalizer = KcNormalizer(vocab, make_embedder(), merge_threshold=0.99)
    kc_id, created = await normalizer.match_or_create("泰勒展开")  # 0.95 < 0.99
    assert created is True
    assert kc_id != "kc-taylor"
    assert len(vocab.entries) == 2


# --- 名称归一化 ---


def test_by_name_hits_fullwidth_whitespace_case_and_middledot_variants():
    vocab = make_taylor_vocab()
    entry = vocab.entries[0]
    variants = [
        "泰勒公式",
        " 泰勒 公式 ",  # 空白
        "　泰勒公式　",  # 全角空格
        "ＴＡＹＬＯＲ公式",  # 全角字母 + 大写
        "taylor公式",  # 小写
        "Taylor·公式",  # 中间点
    ]
    for variant in variants:
        assert vocab.by_name(variant) is entry, variant
    assert vocab.by_name("洛必达法则") is None


def test_normalize_kc_name():
    assert normalize_kc_name("Ｔａｙｌｏｒ · 公 式") == "taylor公式"
    assert normalize_kc_name("  泰勒展开\n") == "泰勒展开"


def test_vocabulary_payload_roundtrip():
    vocab = KcVocabulary(
        entries=[
            KcEntry("id1", "泰勒公式", ["Taylor公式"], "多项式逼近", [0.1, 0.2]),
            KcEntry("id2", "导数", [], "", None),
        ]
    )
    restored = KcVocabulary.from_payload(vocab.to_payload())
    assert restored == vocab


# --- 先修 DAG ---


def test_add_edge_safe_rejects_cycle_and_self_loop():
    edges: list[PrereqEdge] = []
    assert add_edge_safe(edges, PrereqEdge("a", "b")) is True
    assert add_edge_safe(edges, PrereqEdge("b", "c")) is True
    assert add_edge_safe(edges, PrereqEdge("c", "a")) is False  # a->b->c 后 c->a 成环
    assert add_edge_safe(edges, PrereqEdge("a", "a")) is False  # 自环恒拒绝
    assert len(edges) == 2  # 被拒边未写入
    assert validate_acyclic(edges) is True


def test_validate_acyclic_detects_cycle():
    assert validate_acyclic([]) is True
    assert validate_acyclic([PrereqEdge("a", "b"), PrereqEdge("b", "c")]) is True
    assert (
        validate_acyclic([PrereqEdge("a", "b"), PrereqEdge("b", "c"), PrereqEdge("c", "a")])
        is False
    )


def test_topological_order_respects_prereqs():
    edges = [PrereqEdge("a", "b"), PrereqEdge("b", "c")]
    order = topological_order(["c", "a", "b"], edges)
    assert sorted(order) == ["a", "b", "c"]
    assert order.index("a") < order.index("b") < order.index("c")


def test_topological_order_is_stable_for_unconstrained_nodes():
    # 无边: 输出即输入顺序
    assert topological_order(["x", "y", "z"], []) == ["x", "y", "z"]
    # 只约束 a->b, 无约束节点 x/y 保持原相对顺序
    mixed = topological_order(["x", "b", "y", "a"], [PrereqEdge("a", "b")])
    assert mixed.index("a") < mixed.index("b")
    assert mixed.index("x") < mixed.index("y")
    # 边端点不在列表内: 完全不影响
    assert topological_order(["m", "n"], [PrereqEdge("a", "b")]) == ["m", "n"]


# --- LLM 抽取层 (extraction) ---

VOCAB_JSON = json.dumps(
    {
        "kcs": [
            {"canonical_name": "导数", "aliases": ["求导"], "description": "函数变化率"},
            {"canonical_name": "泰勒公式", "aliases": ["Taylor公式"], "description": "多项式逼近"},
        ],
        "prerequisite_edges": [{"from": "导数", "to": "泰勒公式"}],
    },
    ensure_ascii=False,
)

STUB_JSON = '{"stub": true, "tier": "strong", "digest": "deadbeef", "echo": "词表"}'


async def test_build_vocabulary_parses_llm_json():
    gateway = FakeGateway([VOCAB_JSON])
    vocab, edges = await build_vocabulary(gateway, "目录: 第一章 导数; 第二章 泰勒公式")

    assert [e.canonical_name for e in vocab.entries] == ["导数", "泰勒公式"]
    assert vocab.entries[0].aliases == ["求导"]
    assert vocab.entries[1].description == "多项式逼近"
    assert len(edges) == 1
    assert edges[0].from_kc == vocab.entries[0].kc_id  # 边引用归一后的 kc_id
    assert edges[0].to_kc == vocab.entries[1].kc_id
    assert validate_acyclic(edges) is True

    req = gateway.requests[0]
    assert req.json_mode is True
    assert req.tier is Tier.strong
    assert req.max_tokens is not None
    assert req.response_schema["required"] == ["kcs", "prerequisite_edges"]
    assert req.response_schema["properties"]["kcs"]["minItems"] == 1
    assert "第一章 导数" in req.messages[-1].content  # 提示词已渲染进知识库内容


async def test_build_vocabulary_retries_once_on_bad_json_then_succeeds():
    gateway = FakeGateway(["模型跑飞了, 这不是 JSON {", VOCAB_JSON])
    vocab, _edges = await build_vocabulary(gateway, "x")
    assert len(gateway.requests) == 2
    assert len(vocab.entries) == 2
    before, retry = gateway.requests
    assert before.messages != retry.messages
    assert "<知识库内容>\nx\n</知识库内容>" in retry.messages[0].content
    assert retry.max_tokens < before.max_tokens
    for kind in ("kcs", "prerequisite_edges"):
        assert retry.response_schema["properties"][kind]["maxItems"] < (
            before.response_schema["properties"][kind]["maxItems"]
        )


async def test_build_vocabulary_raises_after_two_failures():
    gateway = FakeGateway(["坏输出一", "坏输出二"])
    with pytest.raises(KcExtractionError):
        await build_vocabulary(gateway, "x")
    assert len(gateway.requests) == 2  # 只重试一次


async def test_build_vocabulary_rejects_length_finish_and_empty_terms():
    gateway = FakeGateway([VOCAB_JSON, '{"kcs": [], "prerequisite_edges": []}'],
                          reasons=["length", "stop"])
    with pytest.raises(KcExtractionError, match=r"token_limit\(1\), invalid_vocabulary\(1\)"):
        await build_vocabulary(gateway, "目录和正文没有可验证词条")
    assert len(gateway.requests) == 2


async def test_stub_gateway_payload_treated_as_parse_failure():
    # stub 网关返回 {"stub": true, ...}: 是合法 JSON 但缺业务字段, 必须按解析失败处理
    gateway = FakeGateway([STUB_JSON, STUB_JSON])
    with pytest.raises(KcExtractionError):
        await build_vocabulary(gateway, "x")

    gateway2 = FakeGateway([STUB_JSON, STUB_JSON])
    with pytest.raises(KcExtractionError):
        await extract_chunk_kcs(gateway2, "文本块", ["导数"])


async def test_extract_chunk_kcs_ok_and_capped_at_three():
    payload = json.dumps({"kcs": ["泰勒公式", "导数", "极限", "连续", "微分"]}, ensure_ascii=False)
    gateway = FakeGateway([payload])
    names = await extract_chunk_kcs(gateway, "本节讲泰勒公式的推导……", ["导数", "泰勒公式"])
    assert names == ["泰勒公式", "导数", "极限"]  # ≤3, 保序截断

    req = gateway.requests[0]
    assert req.json_mode is True
    assert req.tier is Tier.cheap
    assert "本节讲泰勒公式的推导" in req.messages[-1].content
    assert "- 导数" in req.messages[-1].content  # 词表名称已渲染进提示词


async def test_extract_chunk_kcs_bad_json_then_fenced_json():
    good = '```json\n{"kcs": ["导数"]}\n```'  # 围栏包裹也要能解析
    gateway = FakeGateway(["not json at all", good])
    assert await extract_chunk_kcs(gateway, "t", []) == ["导数"]
    assert len(gateway.requests) == 2


async def test_extract_chunk_kcs_empty_list_is_valid():
    gateway = FakeGateway(['{"kcs": []}'])
    assert await extract_chunk_kcs(gateway, "版权页", ["导数"]) == []
    assert len(gateway.requests) == 1
