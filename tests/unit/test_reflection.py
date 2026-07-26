"""Reflection 洞察匹配纯函数单测 (§5.5): 词表外/无证据的洞察必须如实丢弃。"""

from __future__ import annotations

from eduagents_worker.reflection import CHAT_EVIDENCE_WEIGHT, match_insights

VOCAB = [
    ("kc-1", "导数定义", ["导数的定义"]),
    ("kc-2", "极值判定", ["极值"]),
    ("kc-3", "泰勒公式", []),
]


def test_matches_canonical_alias_and_splits_by_signal():
    confused, understood, names = match_insights(
        [
            {"kc_name": "导数定义", "signal": "confused", "evidence": "为什么不能直接代入"},
            {"kc_name": "极值", "signal": "understood", "evidence": "我会判定了"},  # alias
        ],
        VOCAB,
    )
    assert confused == ["kc-1"]
    assert understood == ["kc-2"]
    assert names == {"kc-1": "导数定义", "kc-2": "极值判定"}


def test_drops_out_of_vocabulary_and_unsupported_insights():
    confused, understood, names = match_insights(
        [
            {"kc_name": "量子纠缠", "signal": "confused", "evidence": "不懂"},  # 词表外
            {"kc_name": "泰勒公式", "signal": "confused", "evidence": "  "},  # 无原话
            {"kc_name": "泰勒公式", "signal": "maybe", "evidence": "说不清"},  # 非法信号
            {"kc_name": "", "signal": "confused", "evidence": "x"},
            "not-a-dict",
        ],
        VOCAB,
    )
    assert (confused, understood, names) == ([], [], {})


def test_same_kc_counted_once_first_wins():
    confused, understood, _names = match_insights(
        [
            {"kc_name": "导数的定义", "signal": "confused", "evidence": "第一次"},
            {"kc_name": "导数定义", "signal": "understood", "evidence": "第二次"},
        ],
        VOCAB,
    )
    assert confused == ["kc-1"] and understood == []


def test_evidence_weight_below_tutor_and_quiz():
    # 对话证据必须弱于测评 (1.0) 与辅导小结/主观题 (0.5)
    assert CHAT_EVIDENCE_WEIGHT == 0.3
