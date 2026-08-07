"""出题个性化单元测试 (P1-1 ZPD 难度自适应 + P1-2 画像注入): FakeGateway 脚本化, 全离线。

覆盖: ① difficulty_mix 按掌握度分档 (阈值同源 thresholds, 边界 0.3/0.5/0.8);
② 逐 KC 理由只用定性词 (P2-7: 不含掌握度小数); ③ generate 实收 prompt 的画像段 —
薄弱生注入、无画像不注入且无占位符残留; interrupt 负载携带 difficulty_reasons。
"""

from __future__ import annotations

import json
import re

from eduagents_api.profile_summary import difficulty_rationale
from eduagents_api.routers.quiz import adaptive_difficulty_mix
from eduagents_core.config import Settings
from eduagents_core.gateway.base import ChatRequest, ChatResult
from eduagents_core.graphs.quiz_graph import PROFILE_SECTION_TEMPLATE, build_quiz_graph
from eduagents_core.graphs.state import GraphDeps
from eduagents_core.mastery.thresholds import difficulty_for_mastery, zone_label
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

# --- fakes (同 test_quiz_graph 的 FakeGateway 模式) ---


class FakeGateway:
    """预置响应文本队列, 逐次弹出; 记录收到的 ChatRequest 供断言。"""

    def __init__(self, texts: list[str]) -> None:
        self.texts = list(texts)
        self.requests: list[ChatRequest] = []

    async def chat(self, req: ChatRequest) -> ChatResult:
        self.requests.append(req)
        text = (
            json.dumps({"issues": [], "summary": "题目质量合格"}, ensure_ascii=False)
            if req.feature == "quiz_verify"
            else self.texts.pop(0)
        )
        return ChatResult(
            text=text,
            model="fake",
            tier=req.tier.value,
            prompt_tokens=1,
            completion_tokens=1,
            cost_cny=0.0,
            latency_ms=0,
            mode="stub",
        )


BP_JSON = json.dumps(
    {"items": [{"kc_name": "导数定义", "type": "single", "difficulty": "easy"}], "summary": "测试"},
    ensure_ascii=False,
)
GEN_JSON = json.dumps(
    {
        "questions": [
            {
                "type": "single",
                "stem": "导数的定义是?",
                "options": ["极限", "积分", "级数", "微分方程"],
                "answer": "A",
                "explanation": "由定义即差商极限; 选 B 通常是混淆了微分与积分。",
                "kc_names": ["导数定义"],
                "difficulty": "easy",
                "source_hint": None,
            }
        ]
    },
    ensure_ascii=False,
)


def weak_item(name: str, p: float, recent: bool = False) -> dict:
    return {"kc_id": "k-" + name, "name": name, "p_mastery": p, "recent_error": recent}


def make_app(gateway: FakeGateway):
    deps = GraphDeps(settings=Settings(), gateway=gateway, retrieval=None)
    return build_quiz_graph(deps).compile(checkpointer=MemorySaver())


def base_state(**over) -> dict:
    state = {
        "tenant_id": "t1",
        "user_id": "u1",
        "request_id": "r1",
        "kb_id": "",
        "topic": "薄弱点强化练习",
        "num_questions": 1,
        "difficulty_mix": {"easy": 1},
        "kc_names": ["导数定义"],
    }
    state.update(over)
    return state


def cfg(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


# --- ① 难度分档与整卷 mix (阈值同源 thresholds) ---


def test_difficulty_tier_boundaries():
    """边界值语义: 0.5 之下 easy, [0.5, 0.8) mid, 0.8 起 hard; 0.3 只影响定性区间词。"""
    assert difficulty_for_mastery(0.29) == "easy"
    assert difficulty_for_mastery(0.3) == "easy"
    assert difficulty_for_mastery(0.49) == "easy"
    assert difficulty_for_mastery(0.5) == "mid"
    assert difficulty_for_mastery(0.79) == "mid"
    assert difficulty_for_mastery(0.8) == "hard"
    # 0.3 是"较弱/待巩固"定性分界 (ZPD 下界), 不改变难度档
    assert zone_label(0.29) == "较弱"
    assert zone_label(0.3) == "待巩固"


def test_adaptive_mix_counts_by_kc_mastery():
    weak = [weak_item("导数定义", 0.2), weak_item("极值判定", 0.55)]
    # 题目按薄弱点轮转: easy, mid, easy, mid
    assert adaptive_difficulty_mix(weak, 4) == {"easy": 2, "mid": 2, "hard": 0}
    # 题数少于 KC 数时只取前面的 (recent_error/掌握度升序已由 weak_point_kcs 排好)
    assert adaptive_difficulty_mix(weak, 1) == {"easy": 1, "mid": 0, "hard": 0}


def test_adaptive_mix_sum_equals_num_questions():
    weak = [weak_item("a", 0.1), weak_item("b", 0.5), weak_item("c", 0.85)]
    for n in (1, 3, 5, 7):
        mix = adaptive_difficulty_mix(weak, n)
        assert sum(mix.values()) == n
    # p=0.85 的 KC 派 hard 档 (掌握达标线之上)
    assert adaptive_difficulty_mix(weak, 3)["hard"] == 1


# --- ② 理由文本 P2-7 脱敏: 定性词, 无掌握度小数 ---


def test_rationale_uses_qualitative_zone_words_without_decimals():
    cases = [(0.12, "较弱"), (0.3, "待巩固"), (0.55, "待巩固"), (0.7, "接近掌握")]
    for p, expected_zone in cases:
        r = difficulty_rationale("导数定义", p, recent_error=False)
        assert r["kc_name"] == "导数定义"
        assert r["zone"] == expected_zone
        assert r["difficulty"] == difficulty_for_mastery(p)
        assert expected_zone in r["reason"]
        # P2-7: 理由出境 (蓝图卡/预览), 禁止携带掌握度小数
        assert re.search(r"\d\.\d", r["reason"]) is None
        assert str(p) not in r["reason"]


def test_rationale_mentions_recent_error_priority():
    with_err = difficulty_rationale("极值判定", 0.4, recent_error=True)
    without = difficulty_rationale("极值判定", 0.4, recent_error=False)
    assert "近 7 天答错过" in with_err["reason"]
    assert "近 7 天答错过" not in without["reason"]


# --- ③ 画像注入 generate prompt (fake gateway 断言实收内容) ---


async def test_generate_prompt_contains_profile_section_for_weak_student():
    summary = "薄弱点: 导数定义(较弱); 近期错因偏向: 概念混淆。"
    gateway = FakeGateway([BP_JSON, GEN_JSON])
    app = make_app(gateway)
    config = cfg("weak-student")

    reasons = [difficulty_rationale("导数定义", 0.24, recent_error=True)]
    first = await app.ainvoke(
        base_state(profile_summary=summary, difficulty_reasons=reasons), config
    )
    # 理由随 blueprint interrupt 下发 (前端蓝图确认卡渲染"为什么这套题")
    payload = first["__interrupt__"][0].value
    assert payload["kind"] == "blueprint_confirm"
    assert payload["difficulty_reasons"] == reasons

    final = await app.ainvoke(Command(resume={"approved": True}), config)
    assert final["validation_errors"] == []

    gen_prompt = gateway.requests[1].messages[0].content
    assert gateway.requests[1].feature == "quiz_generate"
    # 画像段整段进 prompt: 段标题 + 摘要原文 + 干扰项贴合错因的个性化指令
    assert "## 学生画像" in gen_prompt
    assert summary in gen_prompt
    assert "错因分布" in gen_prompt
    assert "不得写进题干" in gen_prompt
    # 蓝图请求不注入画像 (画像只调整命题, 蓝图纯结构规划)
    assert "学生画像" not in gateway.requests[0].messages[0].content
    # 画像注入可观测
    gen_events = [e for e in final["trace"] if e.get("node") == "generate"]
    assert gen_events and gen_events[-1]["profile_injected"] is True


async def test_generate_prompt_clean_without_profile():
    gateway = FakeGateway([BP_JSON, GEN_JSON])
    app = make_app(gateway)
    config = cfg("no-profile")

    first = await app.ainvoke(base_state(), config)
    # 普通出题: interrupt 负载理由为空数组 (前端据此不渲染理由块)
    assert first["__interrupt__"][0].value["difficulty_reasons"] == []

    final = await app.ainvoke(Command(resume={"approved": True}), config)
    gen_prompt = gateway.requests[1].messages[0].content
    assert "学生画像" not in gen_prompt
    assert "{{PROFILE_SECTION}}" not in gen_prompt  # 占位符不残留
    gen_events = [e for e in final["trace"] if e.get("node") == "generate"]
    assert gen_events and gen_events[-1]["profile_injected"] is False


def test_profile_section_template_discipline():
    """措辞纪律 (同 qa 的 PROFILE_SECTION_TEMPLATE): 画像仅调整出题, 不进题干不罗列原文。"""
    rendered = PROFILE_SECTION_TEMPLATE.replace("{{PROFILE}}", "摘要")
    assert "仅用于调整出题" in rendered
    assert "不得写进题干" in rendered
    assert "不得罗列画像原文" in rendered
    assert "错因分布" in rendered


# ---------- 审查修复轮: 难度分布是约束不是建议 (_enforce_difficulty_mix) ----------


def _bp_items(*difficulties: str, kc: str = "导数定义") -> dict:
    return {"items": [{"kc_name": kc, "type": "single", "difficulty": d} for d in difficulties]}


def test_blueprint_difficulty_pinned_by_kc_reasons():
    """蓝图 LLM 给薄弱 KC 配错难度: 按定向分档表钉回, 展示与实际必须一致。"""
    from eduagents_core.graphs.quiz_graph import _fix_blueprint

    blueprint, _, enforced = _fix_blueprint(
        _bp_items("hard", "hard"),
        num_questions=2,
        difficulty_mix={"easy": 2, "mid": 0, "hard": 0},
        kc_names=["导数定义"],
        topic="导数",
        difficulty_by_kc={"导数定义": "easy"},
    )
    assert [it["difficulty"] for it in blueprint["items"]] == ["easy", "easy"]
    assert enforced == 2
    assert all("_pinned" not in it for it in blueprint["items"])


def test_blueprint_mix_enforced_when_llm_ignores_it():
    """题数够但难度全错 (审查 P2 场景): mix 程序性强制, 不再静默失效。"""
    from eduagents_core.graphs.quiz_graph import _fix_blueprint

    blueprint, corrected, enforced = _fix_blueprint(
        _bp_items("mid", "mid", "mid"),
        num_questions=3,
        difficulty_mix={"easy": 2, "mid": 1, "hard": 0},
        kc_names=[],
        topic="导数",
    )
    counts = [it["difficulty"] for it in blueprint["items"]]
    assert counts.count("easy") == 2 and counts.count("mid") == 1
    assert corrected is False  # 数量本来就对
    assert enforced == 2


def test_blueprint_mix_not_enforced_for_user_edit():
    """用户改过的蓝图: 学习者控制权, 难度不被系统改回 (enforce_mix=False)。"""
    from eduagents_core.graphs.quiz_graph import _fix_blueprint

    blueprint, _, enforced = _fix_blueprint(
        _bp_items("hard", "hard", "hard"),
        num_questions=3,
        difficulty_mix={"easy": 2, "mid": 1, "hard": 0},
        kc_names=[],
        topic="导数",
        enforce_mix=False,
    )
    assert [it["difficulty"] for it in blueprint["items"]] == ["hard", "hard", "hard"]
    assert enforced == 0


def test_blueprint_mix_skipped_when_sum_mismatch():
    """mix 目标数之和 != 题数时不做配比改标 (无法整除的意图不猜), 钉住仍生效。"""
    from eduagents_core.graphs.quiz_graph import _fix_blueprint

    blueprint, _, enforced = _fix_blueprint(
        _bp_items("mid", "mid", kc="极值判定"),
        num_questions=2,
        difficulty_mix={"easy": 5, "mid": 0, "hard": 0},
        kc_names=[],
        topic="导数",
        difficulty_by_kc={"极值判定": "mid"},
    )
    assert [it["difficulty"] for it in blueprint["items"]] == ["mid", "mid"]
    assert enforced == 0


# ---------- 回归: generate 的检索调用必须匹配 RetrievalService 真实签名 ----------


async def test_generate_calls_retrieval_with_real_signature():
    """曾因 kb_id= 关键字误用在真检索路径 TypeError (None/fake 检索盖不住, record 冒烟抓出)。

    fake 的 retrieve 签名逐字复刻 RetrievalService.retrieve —— 关键字误用在这里必炸。
    """
    from eduagents_core.retrieval.base import RetrievalResult, RetrievedChunk

    calls: list[tuple] = []

    class _RealSignatureRetrieval:
        async def retrieve(
            self,
            tenant_id,
            kb_ids,
            query,
            *,
            debug=False,
        ) -> RetrievalResult:
            calls.append((tenant_id, kb_ids, query))
            return RetrievalResult(
                chunks=[
                    RetrievedChunk(
                        chunk_id="c1", text="泰勒公式素材", heading_path="第四章",
                        doc_id="d1", page_start=1, page_end=1, score=0.9, source="dense",
                    )
                ]
            )

    gateway = FakeGateway([BP_JSON, GEN_JSON])
    settings = Settings()
    deps = GraphDeps(
        settings=settings, gateway=gateway,
        retrieval=_RealSignatureRetrieval(), web_search=None,
    )
    app = build_quiz_graph(deps).compile(checkpointer=MemorySaver())
    config = cfg("retrieval-signature")
    state = base_state()
    state["kb_id"] = "kb-1"
    await app.ainvoke(state, config)
    final = await app.ainvoke(Command(resume={"approved": True}), config)
    assert final["validation_errors"] == []
    # 生成和独立 critic 各检索一次；素材正文不进入 Graph checkpoint。
    assert calls == [
        ("t1", "kb-1", "薄弱点强化练习"),
        ("t1", "kb-1", "薄弱点强化练习"),
    ]
    # 素材真实进了出题 prompt
    gen_prompt = gateway.requests[1].messages[0].content
    assert "泰勒公式素材" in gen_prompt
