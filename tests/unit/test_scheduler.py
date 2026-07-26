"""FSRS 调度薄封装 (mastery/scheduler.py): 纯函数, 时间全部显式传入, 零 IO。

py-fsrs 6.x: 对=Good 间隔拉长, 错=Again 回短间隔重学; enable_fuzzing=False 保证确定性。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from eduagents_core.mastery.scheduler import due_at, is_due, new_card_json, review

T0 = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)


def test_new_card_is_due_immediately_and_roundtrips_json():
    card = new_card_json(T0)
    assert is_due(card, T0)
    assert due_at(card) == T0
    # JSONB 往返: dumps/loads 后逐键相等, 且仍可继续 review
    restored = json.loads(json.dumps(card))
    assert restored == card
    new_card, due = review(restored, correct=True, now=T0)
    assert due > T0
    assert due_at(new_card) == due


def test_correct_answers_stretch_intervals():
    card = new_card_json(T0)
    card1, due1 = review(card, correct=True, now=T0)
    assert due1 > T0
    assert not is_due(card1, T0)
    assert is_due(card1, due1)
    card2, due2 = review(card1, correct=True, now=due1)
    card3, due3 = review(card2, correct=True, now=due2)
    # 对 → 间隔单调拉长 (F6-A4 的核心断言)
    assert (due2 - due1) > (due1 - T0)
    assert (due3 - due2) > (due2 - due1)


def test_wrong_answer_shrinks_interval():
    card = new_card_json(T0)
    card, due = review(card, correct=True, now=T0)
    card, due_prev = review(card, correct=True, now=due)
    interval_before = due_prev - due
    _card, due_after = review(card, correct=False, now=due_prev)
    # 错 → 间隔缩短/重置 (回到重学步)
    assert (due_after - due_prev) < interval_before


def test_extra_keys_survive_review():
    card = new_card_json(T0)
    card["streak"] = 1
    new_card, _due = review(card, correct=True, now=T0)
    assert new_card["streak"] == 1  # 业务扩展键 (连对计数) 原样透传


def test_empty_card_treated_as_due_and_reviewable():
    # 历史行 fsrs_card_json={} 视为到期新卡, review 不炸
    assert is_due({}, T0)
    assert is_due(None, T0)
    assert due_at({}) is None
    card, due = review({}, correct=True, now=T0)
    assert due > T0
    assert due_at(card) == due


def test_naive_datetime_tolerated():
    naive = datetime(2026, 1, 1, 8, 0)  # 故意 naive, 验证防御性兜底
    card = new_card_json(naive)
    assert is_due(card, naive)
    _card, due = review(card, correct=True, now=naive)
    assert due.tzinfo is not None
