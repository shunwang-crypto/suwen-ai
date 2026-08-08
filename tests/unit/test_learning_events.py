"""B0 pure-function contracts: stable business cards and retained ReviewLog."""

from datetime import UTC, datetime
from uuid import UUID

from eduagents_api.routers.reviews import _new_review_idempotency_key
from eduagents_core.mastery.scheduler import (
    FSRS_PARAMS_VERSION,
    new_card_json,
    review_with_log,
    stable_card_id,
)
from fsrs import ReviewLog

T0 = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
ERROR_ITEM_ID = UUID("12345678-1234-5678-1234-567812345678")


def test_new_card_uses_stable_error_item_identity_not_time_milliseconds():
    card = new_card_json(T0, business_card_id=ERROR_ITEM_ID)

    assert card["business_card_id"] == str(ERROR_ITEM_ID)
    assert card["card_id"] == stable_card_id(ERROR_ITEM_ID)
    assert card["card_id"] != int(T0.timestamp() * 1000)


def test_review_with_log_is_replayable_and_preserves_duration():
    before = new_card_json(T0, business_card_id=ERROR_ITEM_ID)
    after, due, review_log = review_with_log(
        before,
        correct=False,
        now=T0,
        business_card_id=ERROR_ITEM_ID,
        review_duration_ms=321,
    )

    restored_log = ReviewLog.from_dict(review_log.to_dict())
    replayed, replay_due, replay_log = review_with_log(
        before,
        correct=False,
        now=T0,
        business_card_id=ERROR_ITEM_ID,
        review_duration_ms=321,
    )

    assert after == replayed
    assert due == replay_due
    assert review_log.to_dict() == replay_log.to_dict() == restored_log.to_dict()
    assert review_log.review_duration == 321
    assert review_log.card_id == stable_card_id(ERROR_ITEM_ID)
    assert FSRS_PARAMS_VERSION == "fsrs-6-default"


def test_legacy_review_requests_get_distinct_fallback_keys():
    first = _new_review_idempotency_key()
    second = _new_review_idempotency_key()

    assert first != second
    assert len(first) <= 128 and len(second) <= 128
