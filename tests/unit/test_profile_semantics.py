"""画像面向用户的语义映射测试。"""

from eduagents_api.routers.profile import evidence_strength


def test_evidence_strength_is_qualitative_not_probability() -> None:
    assert evidence_strength(0.0) == "limited"
    assert evidence_strength(0.3999) == "limited"
    assert evidence_strength(0.4) == "moderate"
    assert evidence_strength(0.7999) == "moderate"
    assert evidence_strength(0.8) == "strong"
    assert evidence_strength(1.0) == "strong"
