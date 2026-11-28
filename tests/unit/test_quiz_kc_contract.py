"""Wave 1B/C: KC readiness and evidence-boundary contracts."""

from eduagents_api.services import KcReadiness, assess_quiz_kc_readiness


def test_stub_readiness_uses_chapter_fallback_only_and_requires_child_coverage():
    ready = assess_quiz_kc_readiness(
        gateway_mode="stub",
        document_statuses=["ready"],
        kc_rows=[
            ("chapter-1", "chapter_fallback", None),
            ("semantic-1", "enrichment", None),
        ],
        child_kc_ids=[["chapter-1", "semantic-1"], ["chapter-1"]],
    )

    assert isinstance(ready, KcReadiness)
    assert ready.status == "ready"
    assert ready.code is None
    assert ready.eligible_kc_ids == ("chapter-1",)

    incomplete = assess_quiz_kc_readiness(
        gateway_mode="stub",
        document_statuses=["ready"],
        kc_rows=[("chapter-1", "chapter_fallback", None)],
        child_kc_ids=[["chapter-1"], []],
    )
    assert incomplete.status == "not_ready"
    assert incomplete.code == "QUIZ_KC_NOT_READY"


def test_live_readiness_waits_for_enrichment_and_rejects_fallback_only():
    preparing = assess_quiz_kc_readiness(
        gateway_mode="live",
        document_statuses=["ready"],
        kc_rows=[("chapter-1", "chapter_fallback", None)],
        child_kc_ids=[["chapter-1"]],
    )
    assert preparing.status == "preparing"
    assert preparing.code == "QUIZ_KC_PREPARING"

    not_ready = assess_quiz_kc_readiness(
        gateway_mode="replay",
        document_statuses=["enriched"],
        kc_rows=[("chapter-1", "chapter_fallback", None)],
        child_kc_ids=[["chapter-1"]],
    )
    assert not_ready.status == "not_ready"
    assert not_ready.code == "QUIZ_KC_NOT_READY"


def test_superseded_kc_is_resolved_only_inside_the_same_kc_set():
    ready = assess_quiz_kc_readiness(
        gateway_mode="live",
        document_statuses=["enriched"],
        kc_rows=[
            ("old", "enrichment", "current"),
            ("current", "enrichment", None),
        ],
        child_kc_ids=[["old"]],
    )
    assert ready.status == "ready"
    assert ready.eligible_kc_ids == ("current",)


def test_live_readiness_allows_historical_fallback_alongside_semantic_coverage():
    ready = assess_quiz_kc_readiness(
        gateway_mode="live",
        document_statuses=["enriched"],
        kc_rows=[
            ("fallback", "chapter_fallback", None),
            ("semantic", "enrichment", None),
        ],
        child_kc_ids=[["fallback", "semantic"]],
    )

    assert ready.status == "ready"
    assert ready.eligible_kc_ids == ("semantic",)


def test_no_document_is_not_applicable_for_legacy_quiz_paths():
    result = assess_quiz_kc_readiness(
        gateway_mode="live",
        document_statuses=[],
        kc_rows=[],
        child_kc_ids=[],
    )
    assert result.status == "not_applicable"
    assert result.code is None


def test_explicit_heading_policy_in_live_mode_selects_only_bound_chapters():
    result = assess_quiz_kc_readiness(
        gateway_mode="live", headings_only=True, document_statuses=["ready"],
        kc_rows=[("current", "chapter_fallback", None), ("old", "chapter_fallback", None)],
        child_kc_ids=[["current"]],
    )
    assert result.status == "ready"
    assert result.eligible_kc_ids == ("current",)
