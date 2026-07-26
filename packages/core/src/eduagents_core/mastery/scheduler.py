"""FSRS 复习调度薄封装 (§5.6/F6/§9)。

py-fsrs 6.x 真实 API: fsrs.Scheduler().review_card(card, rating, review_datetime)
-> (Card, ReviewLog); Card.to_dict()/from_dict() 用 ISO 字符串序列化, 可直存 JSONB。

本模块只做纯函数封装, 所有时间参数显式传入 —— 调用方从 Clock 抽象 (API 层为
timectl 虚拟时钟) 取时间, 函数内禁止 datetime.now() (§14, time-travel 的根基)。

fsrs_card_json 里允许携带业务扩展键 (如连对计数 streak), review() 原样透传:
Card.from_dict 按键取值, 多余键不影响反序列化 (已对 py-fsrs 6.3.1 验证)。
"""

from __future__ import annotations

from datetime import UTC, datetime

from fsrs import Card, Rating, Scheduler

# enable_fuzzing=False: 间隔确定可复现 (演示脚本与 CI 断言依赖确定性, §9)
_scheduler = Scheduler(enable_fuzzing=False)

_FSRS_FIELDS = ("card_id", "state", "step", "stability", "difficulty", "due", "last_review")


def _ensure_aware(now: datetime) -> datetime:
    return now if now.tzinfo is not None else now.replace(tzinfo=UTC)


def _to_card(card_json: dict | None, now: datetime) -> Card:
    """反序列化 FSRS 卡; 空卡/缺字段 (历史行 fsrs_card_json={}) 视为此刻新建。"""
    if not card_json or any(field not in card_json for field in _FSRS_FIELDS):
        return Card(card_id=int(now.timestamp() * 1000), due=now)
    return Card.from_dict(card_json)


def new_card_json(now: datetime) -> dict:
    """新建 FSRS 卡并序列化: due=now, 即刻到期 (错题入本当天即可复习)。"""
    now = _ensure_aware(now)
    return Card(card_id=int(now.timestamp() * 1000), due=now).to_dict()


def review(card_json: dict | None, correct: bool, now: datetime) -> tuple[dict, datetime]:
    """一次复习评分: 对=Good / 错=Again, 返回 (新卡 json, 下次到期时间)。

    card_json 中的非 FSRS 扩展键 (如 streak) 原样带入新卡 json, 由调用方自行更新。
    """
    now = _ensure_aware(now)
    card = _to_card(card_json, now)
    rating = Rating.Good if correct else Rating.Again
    new_card, _log = _scheduler.review_card(card, rating, review_datetime=now)
    out = new_card.to_dict()
    for key, value in (card_json or {}).items():
        if key not in out:
            out[key] = value
    return out, new_card.due


def due_at(card_json: dict | None) -> datetime | None:
    """卡片到期时间; 空卡返回 None (调用方视为即刻到期)。"""
    if not card_json or not card_json.get("due"):
        return None
    return datetime.fromisoformat(card_json["due"])


def is_due(card_json: dict | None, now: datetime) -> bool:
    """是否到期。空卡视为到期 (F6-A4: 新错题当天进复习队列)。"""
    due = due_at(card_json)
    return True if due is None else due <= _ensure_aware(now)
