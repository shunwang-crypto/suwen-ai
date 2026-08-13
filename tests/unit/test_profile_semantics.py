"""画像面向用户的证据语义映射。"""

from eduagents_api.routers.profile import evidence_strength
from eduagents_core.mastery.thresholds import evidence_state, evidence_state_label


def test_evidence_state_is_event_count_not_mastery_prior() -> None:
    assert evidence_state(-1) == "unobserved"
    assert evidence_state(0) == "unobserved"
    assert evidence_state(1) == "estimated"
    assert evidence_state(2) == "estimated"
    assert evidence_state(3) == "observed"
    assert evidence_state_label("unobserved") == "未观测"
    assert evidence_state_label("estimated") == "估计中"
    assert evidence_state_label("observed") == "已观测"


def test_evidence_strength_is_qualitative_not_probability() -> None:
    assert evidence_strength(0.0) == "limited"
    assert evidence_strength(0.3999) == "limited"
    assert evidence_strength(0.4) == "moderate"
    assert evidence_strength(0.7999) == "moderate"
    assert evidence_strength(0.8) == "strong"
    assert evidence_strength(1.0) == "strong"
