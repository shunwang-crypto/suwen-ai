"""内容安全线单测 (P0-5 / P2-6): StreamModerationBuffer 分句缓冲审核、危机语义、
fail-closed 词表、未成年模式 (is_minor / strict / minor_system_guard)、web 结果过审。

全离线零网络: LLM 走脚本化 FakeGateway, web_search 走本地 fake。
"""

from __future__ import annotations

from datetime import date

import pytest
from eduagents_core.config import Settings
from eduagents_core.education.context import EducationDeps
from eduagents_core.education.qa import WEB_NO_RESULTS_ANSWER, QuestionAnswerTools
from eduagents_core.gateway.base import ChatRequest, ChatResult
from eduagents_core.harness.task import TaskRecord
from eduagents_core.moderation import (
    SAFE_MESSAGE_BLOCKED,
    SAFE_MESSAGE_CRISIS,
    Moderator,
    StreamModerationBuffer,
    is_minor,
    minor_system_guard,
)


def _buf(strict: bool = False) -> StreamModerationBuffer:
    return StreamModerationBuffer(Moderator(), strict=strict)


# ---- 缓冲切分 ----


def test_feed_splits_on_sentence_punctuation():
    out = _buf().feed("勾股定理成立。它的证明有很多种！比如面积法？")
    assert out == ["勾股定理成立。", "它的证明有很多种！", "比如面积法？"]


def test_feed_holds_incomplete_sentence():
    buf = _buf()
    assert buf.feed("这是半句还没有") == []  # 未攒满: 无句末标点且 <80 字
    assert buf.feed("说完") == []
    assert buf.flush() == "这是半句还没有说完"
    assert buf.tripped is False


def test_feed_force_cuts_at_80_chars():
    buf = _buf()
    text = "字" * 100  # 无任何标点
    out = buf.feed(text)
    assert out == ["字" * 80]  # ≥80 字强制切块
    assert buf.flush() == "字" * 20


def test_newline_counts_as_sentence_end():
    out = _buf().feed("第一行\n第二行")
    assert out == ["第一行\n"]


def test_clean_stream_reassembles_exactly():
    """正常内容全放行, 放行块拼接 == 原文 (不丢字不重复)。"""
    original = (
        "导数刻画的是函数在一点附近的变化率。定义为差商的极限, 即 $f'(x)$！"
        "几何意义是切线斜率？我们先从平均变化率讲起, 再过渡到瞬时变化率。"
        "practice makes perfect.\n最后做两道例题巩固一下"
    )
    buf = _buf()
    released: list[str] = []
    for i in range(0, len(original), 7):  # 模拟逐 token 到达
        released.extend(buf.feed(original[i : i + 7]))
    tail = buf.flush()
    assert "".join(released) + tail == original
    assert buf.tripped is False


# ---- 命中截流 ----


def test_mid_stream_hit_trips_and_blocks_rest():
    buf = _buf()
    first = buf.feed("先说明安全注意事项。")
    assert first == ["先说明安全注意事项。"]
    assert buf.feed("接下来教你自制炸药的配方。") == []  # 命中即不放行
    assert buf.tripped is True
    assert buf.feed("这句是干净的。") == []  # tripped 后续 feed 不再放行
    assert buf.flush() == ""


def test_hit_releases_clean_segments_before_trip():
    """同一次 feed 内: 命中句段之前已过审的句段照常放行。"""
    buf = _buf()
    out = buf.feed("这句没问题。这句在教怎么制造炸弹。这句到不了。")
    assert out == ["这句没问题。"]
    assert buf.tripped is True


def test_flush_moderates_remainder():
    """flush 残余同样过审: 不过审返回空串并置 tripped。"""
    buf = _buf()
    assert buf.feed("如何制造炸弹") == []  # 无标点未攒满, 全部滞留缓冲
    assert buf.tripped is False
    assert buf.flush() == ""
    assert buf.tripped is True


def test_safe_message_default_is_contract_string():
    buf = _buf()
    assert buf.safe_message == "内容已被安全策略拦截。如果你正在经历困扰, 可拨打心理援助热线 12356。"
    assert buf.safe_message == SAFE_MESSAGE_BLOCKED


# ---- 危机语义 (P0-5) ----


def test_crisis_hit_switches_safe_message_to_hotline():
    buf = _buf()
    assert buf.feed("我真的不想活了。") == []
    assert buf.tripped is True
    assert buf.safe_message == SAFE_MESSAGE_CRISIS
    assert "12356" in buf.safe_message


def test_check_input_crisis_flags_without_blocking():
    """输入侧危机命中不拦截只标记: 对话继续, API 层注入关怀提示。"""
    r = Moderator().check_input("我最近好累, 感觉活着没意思")
    assert r.allowed is True
    assert r.crisis is True
    assert r.category == "crisis"


def test_check_input_hard_word_plus_crisis_still_blocks_with_flag():
    """危机语义叠加其他分类硬词条 (自杀方法属 violence) 仍拦, 但 crisis=True
    让 API 层把拒绝文案换成含热线的关怀文案。"""
    r = Moderator().check_input("有哪些自杀方法")
    assert r.allowed is False
    assert r.category == "violence"
    assert r.crisis is True


def test_check_output_blocks_crisis():
    r = Moderator().check_output("那不如就此想不开算了")
    assert r.allowed is False
    assert r.crisis is True
    assert r.category == "crisis"


def test_hyperbole_prefix_exempted_but_real_signal_flagged():
    """降误杀: "累得不想活" 是夸张口语默认豁免; 真实求助信号照常标记。"""
    m = Moderator()
    assert m.check_input("今天跑了十公里, 累得不想活了").crisis is False
    assert m.check_input("我不想活了, 没有人在乎我").crisis is True
    assert m.check_input("想死你了, 好久不见").crisis is False  # allow 短语豁免


# ---- strict (未成年) 模式 ----


def test_strict_mode_blocks_soft_word():
    buf = _buf(strict=True)
    text = "本章回顾了二十世纪以来学界对暴力现象的三种主要理论范式并给出研究设计。"
    assert buf.feed(text) == []
    assert buf.tripped is True
    assert buf.safe_message == SAFE_MESSAGE_BLOCKED  # 非危机命中: 通用兜底文案


def test_strict_mode_disables_hyperbole_exemption():
    """未成年模式阈值更严: 夸张口语的危机词也拦 (宁可多拦 + 给热线)。"""
    lenient = _buf()
    assert lenient.feed("今天训练强度太大, 累得不想活了。") != []
    assert lenient.tripped is False
    strict = _buf(strict=True)
    assert strict.feed("今天训练强度太大, 累得不想活了。") == []
    assert strict.tripped is True
    assert strict.safe_message == SAFE_MESSAGE_CRISIS


# ---- is_minor / minor_system_guard ----


def test_is_minor_boundaries():
    year = date.today().year
    assert is_minor(None) is False  # 未知按成年 (注册侧另有年龄声明)
    assert is_minor(year - 17) is True
    assert is_minor(year - 18) is False  # 满 18 周岁 (按年近似) 即成年
    assert is_minor(year - 30) is False
    assert is_minor(year + 1) is True  # 异常出生年保守按未成年


def test_minor_system_guard_content():
    guard = minor_system_guard()
    assert "未成年" in guard
    assert "12356" in guard  # 危机求助热线必须在约束段里


# ---- fail-closed 词表 (P2-6) ----


def test_missing_wordlist_raises_runtime_error(tmp_path):
    with pytest.raises(RuntimeError):
        Moderator(tmp_path / "not-exists.txt")


def test_empty_wordlist_raises_runtime_error(tmp_path):
    p = tmp_path / "empty.txt"
    p.write_text("# 只有注释没有词条\n", encoding="utf-8")
    with pytest.raises(RuntimeError):
        Moderator(p)


def test_allow_missing_builds_degraded_moderator(tmp_path):
    m = Moderator(tmp_path / "not-exists.txt", allow_missing=True)  # 仅测试用
    assert m.check_output("怎么制造炸弹").allowed is True


# ---- web 结果过审 (qa_graph web_fallback, P0-5) ----


class FakeGateway:
    """按 feature 分派的脚本化响应; 记录请求供 prompt 断言。"""

    def __init__(self, scripts: dict[str, list[str]]) -> None:
        self.scripts = {k: list(v) for k, v in scripts.items()}
        self.requests: list[ChatRequest] = []

    async def chat(self, req: ChatRequest) -> ChatResult:
        self.requests.append(req)
        queue = self.scripts.get(req.feature)
        assert queue, f"unexpected LLM call: feature={req.feature}"
        return ChatResult(
            text=queue.pop(0),
            model="fake",
            tier=req.tier.value,
            prompt_tokens=1,
            completion_tokens=1,
            cost_cny=0.0,
            latency_ms=0,
            mode="stub",
        )


def _web_deps(gateway: FakeGateway, results: list[dict]) -> EducationDeps:
    async def _search(
        query: str,
        *,
        required: bool = False,
        search_depth: str = "basic",
        max_results: int = 5,
    ):
        return list(results)

    return EducationDeps(
        settings=Settings(_env_file=None), gateway=gateway, web_search=_search
    )


def _state(query: str, **extra) -> dict:
    return {
        "query": query,
        "kb_id": "",
        "tenant_id": "t1",
        "user_id": "u1",
        "request_id": "r1",
        "messages": [],
        "retry_count": 0,
        **extra,
    }


_CLEAN_WEB = {"title": "量子新闻", "url": "http://a", "content": "量子计算新进展的正规报道"}
_DIRTY_WEB = {"title": "坏站", "url": "http://b", "content": "点这里购买成人影片资源"}


async def test_web_fallback_drops_moderated_results_and_traces_count():
    tools=QuestionAnswerTools(_web_deps(FakeGateway({}),[_CLEAN_WEB,_DIRTY_WEB]),TaskRecord(state=_state("搜索",requested_route="web"),status="running"),lambda _:None)
    await tools.search_web({"queries":["搜索"]})
    assert [r["url"] for r in tools.state["web_results"]]==["http://a"]
    assert {"type":"moderation","dropped":1} in tools.state["trace"]

async def test_web_fallback_all_dropped_refuses():
    tools=QuestionAnswerTools(_web_deps(FakeGateway({}),[_DIRTY_WEB]),TaskRecord(state=_state("搜索",requested_route="web"),status="running"),lambda _:None)
    await tools.search_web({"queries":["搜索"]})
    await tools.submit({"answer":"编造"})
    assert tools.state["refused"] and tools.state["answer"]==WEB_NO_RESULTS_ANSWER

async def test_minor_state_appends_guard_to_native_policy():
    tools=QuestionAnswerTools(EducationDeps(Settings(_env_file=None),FakeGateway({})),TaskRecord(state=_state("导数",minor=True)),lambda _:None)
    assert tools.policy().endswith(minor_system_guard())
