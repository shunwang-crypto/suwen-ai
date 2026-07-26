"""predicted_retention (P2-7 遗忘快照): 纯函数纪律 — 固定时间点, 不用真实 now。

口径 = fsrs 库 Scheduler.get_card_retrievability (天粒度确定性衰减):
- 无卡 / 缺字段 / 从未复习 (无 last_review/stability) → None, 不假装能预测;
- 复习过的卡按距上次复习的天数单调衰减;
- at 早于 last_review 时按 elapsed=0 处理 → 1.0 (库内 max(0, ·) 边界)。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from eduagents_core.mastery.scheduler import new_card_json, predicted_retention, review

T0 = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)


def _reviewed_card() -> dict:
    """T0 时刻答对一次的卡: 有 last_review/stability, 可预测。"""
    card, _due = review(new_card_json(T0), correct=True, now=T0)
    return card


def test_no_card_returns_none():
    # 无卡/空卡 (历史行 fsrs_card_json={}) → null, 前端据此不渲染
    assert predicted_retention(None, T0) is None
    assert predicted_retention({}, T0) is None


def test_never_reviewed_card_returns_none():
    # 新建未复习: last_review/stability 为空, 记忆模型尚未建立 → None (不给假 0%)
    assert predicted_retention(new_card_json(T0), T0 + timedelta(days=3)) is None


def test_same_day_full_retention():
    # 刚复习完 (elapsed 0 天) → 1.0, 前端 <90% 门槛自然不显示
    assert predicted_retention(_reviewed_card(), T0) == 1.0


def test_monotonic_decay_over_days():
    card = _reviewed_card()
    values = [
        predicted_retention(card, T0 + timedelta(days=d)) for d in (0, 1, 3, 7, 30, 90)
    ]
    assert all(v is not None for v in values)
    assert all(0.0 < v <= 1.0 for v in values)
    # 严格单调衰减 (F6/P2-7 的核心断言): 距上次复习越久, 保持率越低
    assert all(a > b for a, b in zip(values, values[1:], strict=False))


def test_higher_stability_retains_more():
    # 多复习一次 → stability 更高 → 同样距上次复习 3 天, 保持率更高 (spacing 效应)
    once = _reviewed_card()
    twice, _due = review(once, correct=True, now=T0 + timedelta(days=1))
    r_once = predicted_retention(once, T0 + timedelta(days=3))
    r_twice = predicted_retention(twice, T0 + timedelta(days=1) + timedelta(days=3))
    assert r_twice > r_once


def test_past_time_clamped_to_full():
    # at 早于 last_review: 库内 elapsed = max(0, ·) → 1.0, 不外推"过去更记得"以外的值
    assert predicted_retention(_reviewed_card(), T0 - timedelta(days=5)) == 1.0


def test_naive_datetime_tolerated():
    # 防御性兜底: naive at 视为 UTC, 与 aware 同值 (调用方本应传 Clock 的 aware 时间)
    card = _reviewed_card()
    naive = datetime(2026, 1, 4, 8, 0)
    assert predicted_retention(card, naive) == predicted_retention(
        card, naive.replace(tzinfo=UTC)
    )


def test_extra_business_keys_do_not_break():
    # fsrs_card_json 携带业务扩展键 (streak) 不影响预测 (与 review 同一容忍口径)
    card = _reviewed_card()
    card["streak"] = 2
    assert predicted_retention(card, T0 + timedelta(days=3)) is not None


def test_deterministic_same_inputs_same_output():
    # 确定性: 同卡同时刻两次调用结果逐位相同 (演示脚本与 CI 断言依赖)
    card = _reviewed_card()
    at = T0 + timedelta(days=3)
    assert predicted_retention(card, at) == predicted_retention(card, at)


# ---------------- 画像侧按 KC 聚合 (routers/profile._kc_retention_snapshots, 纯函数) ----------------


def test_kc_snapshots_pick_most_at_risk_card():
    from eduagents_api.routers.profile import _kc_retention_snapshots

    fresh = _reviewed_card()  # T0 复习过: 到 T0+9 天已衰减
    later, _due = review(dict(fresh), correct=True, now=T0 + timedelta(days=7))  # 更近的复习
    now = T0 + timedelta(days=9)
    at_3d = now + timedelta(days=3)
    rows = [(["kc-a"], fresh), (["kc-a"], later)]
    result = _kc_retention_snapshots(rows, now, at_3d)
    # 同 KC 两张卡取 retention_now 最低的那张 (最危险的记忆), 两点同卡自洽
    assert result["kc-a"] == (
        predicted_retention(fresh, now),
        predicted_retention(fresh, at_3d),
    )


def test_kc_snapshots_skip_unreviewed_and_fan_out_multi_kc():
    from eduagents_api.routers.profile import _kc_retention_snapshots

    card = _reviewed_card()
    now = T0 + timedelta(days=2)
    at_3d = now + timedelta(days=3)
    rows = [
        (["kc-a", "kc-b"], card),  # 一张卡挂两个 KC: 各自都拿到快照
        (["kc-c"], new_card_json(T0)),  # 未复习卡: 跳过, kc-c 无快照
        (["kc-d"], {}),  # 历史空卡: 跳过
        (None, card),  # kc_ids 为空: 不炸
    ]
    result = _kc_retention_snapshots(rows, now, at_3d)
    expected = (predicted_retention(card, now), predicted_retention(card, at_3d))
    assert result["kc-a"] == expected
    assert result["kc-b"] == expected
    assert "kc-c" not in result
    assert "kc-d" not in result
