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
from uuid import UUID, uuid4

from fsrs import Card, Rating, ReviewLog, Scheduler

# enable_fuzzing=False: 间隔确定可复现 (演示脚本与 CI 断言依赖确定性, §9)
_scheduler = Scheduler(enable_fuzzing=False)
FSRS_PARAMS_VERSION = "fsrs-6-default"

_FSRS_FIELDS = ("card_id", "state", "step", "stability", "difficulty", "due", "last_review")
type BusinessCardId = UUID | str


def _ensure_aware(now: datetime) -> datetime:
    return now if now.tzinfo is not None else now.replace(tzinfo=UTC)


def stable_card_id(business_card_id: BusinessCardId) -> int:
    """Return a deterministic py-fsrs integer id for a business UUID.

    py-fsrs 6 keeps ``Card.card_id`` as an integer, while the application owns
    card identity through the ErrorItem UUID.  Deriving the integer from that
    UUID avoids py-fsrs' timestamp default and makes a card replayable across
    processes and time-travel runs.
    """

    return UUID(str(business_card_id)).int


def normalize_business_card_json(
    card_json: dict | None, business_card_id: BusinessCardId
) -> dict:
    """Attach the stable ErrorItem identity to a serialized card snapshot."""

    out = dict(card_json or {})
    business_id = str(UUID(str(business_card_id)))
    out["business_card_id"] = business_id
    if "card_id" in out:
        out["card_id"] = stable_card_id(business_id)
    return out


def _to_card(
    card_json: dict | None, now: datetime, business_card_id: BusinessCardId | None = None
) -> Card:
    """反序列化 FSRS 卡; 空卡/缺字段 (历史行 fsrs_card_json={}) 视为此刻新建。"""
    resolved_business_id = business_card_id or (card_json or {}).get("business_card_id")
    if resolved_business_id is not None:
        card_json = normalize_business_card_json(card_json, resolved_business_id)
    if not card_json or any(field not in card_json for field in _FSRS_FIELDS):
        return Card(
            card_id=(
                stable_card_id(resolved_business_id)
                if resolved_business_id is not None
                else stable_card_id(uuid4())
            ),
            due=now,
        )
    card = Card.from_dict(card_json)
    if resolved_business_id is not None:
        card.card_id = stable_card_id(resolved_business_id)
    return card


def new_card_json(
    now: datetime, business_card_id: BusinessCardId | None = None
) -> dict:
    """新建 FSRS 卡并序列化: due=now, 即刻到期 (错题入本当天即可复习)。"""
    now = _ensure_aware(now)
    resolved_business_id = business_card_id or uuid4()
    card = Card(card_id=stable_card_id(resolved_business_id), due=now).to_dict()
    card["business_card_id"] = str(UUID(str(resolved_business_id)))
    return card


def review_with_log(
    card_json: dict | None,
    correct: bool,
    now: datetime,
    *,
    business_card_id: BusinessCardId | None = None,
    review_duration_ms: int | None = None,
) -> tuple[dict, datetime, ReviewLog]:
    """Review a card and retain the py-fsrs ``ReviewLog`` for persistence.

    The legacy :func:`review` wrapper below deliberately keeps its two-value
    return contract for existing callers.  API write paths use this function
    so the exact rating, business time, and duration are not discarded.
    """

    now = _ensure_aware(now)
    resolved_business_id = business_card_id or (card_json or {}).get("business_card_id")
    card = _to_card(card_json, now, resolved_business_id)
    rating = Rating.Good if correct else Rating.Again
    new_card, review_log = _scheduler.review_card(
        card,
        rating,
        review_datetime=now,
        review_duration=review_duration_ms,
    )
    out = new_card.to_dict()
    for key, value in (card_json or {}).items():
        if key not in out:
            out[key] = value
    if resolved_business_id is not None:
        out["business_card_id"] = str(UUID(str(resolved_business_id)))
        out["card_id"] = stable_card_id(resolved_business_id)
    return out, new_card.due, review_log


def review(card_json: dict | None, correct: bool, now: datetime) -> tuple[dict, datetime]:
    """一次复习评分: 对=Good / 错=Again, 返回 (新卡 json, 下次到期时间)。

    card_json 中的非 FSRS 扩展键 (如 streak) 原样带入新卡 json, 由调用方自行更新。
    """
    out, due, _log = review_with_log(card_json, correct, now)
    return out, due


def predicted_retention(card_json: dict | None, at: datetime) -> float | None:
    """FSRS 保持率预测 (P2-7 遗忘快照): 该卡在 at 时刻仍能正确回忆的概率。

    口径 = fsrs 库 Scheduler.get_card_retrievability (天粒度确定性衰减),
    at 由调用方经 Clock 换算传入 —— 本函数无时间源 (学 bkt.py 的纯函数纪律)。
    返回 None 的情形 (前端据此不渲染, 不假装能预测):
    - 无卡 / 缺 FSRS 字段 (历史行 fsrs_card_json={});
    - 卡从未复习过 (last_review/stability 为空, 记忆模型尚未建立)。
    at 早于 last_review 时库内按 elapsed=0 处理, 返回 1.0 (不外推未来记忆)。
    """
    if not card_json or any(field not in card_json for field in _FSRS_FIELDS):
        return None
    card = Card.from_dict(card_json)
    if card.last_review is None or card.stability is None:
        return None
    return float(_scheduler.get_card_retrievability(card, _ensure_aware(at)))


def due_at(card_json: dict | None) -> datetime | None:
    """卡片到期时间; 空卡返回 None (调用方视为即刻到期)。"""
    if not card_json or not card_json.get("due"):
        return None
    return datetime.fromisoformat(card_json["due"])


def is_due(card_json: dict | None, now: datetime) -> bool:
    """是否到期。空卡视为到期 (F6-A4: 新错题当天进复习队列)。"""
    due = due_at(card_json)
    return True if due is None else due <= _ensure_aware(now)
