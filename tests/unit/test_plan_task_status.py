from __future__ import annotations

import pytest
from eduagents_api.plan_adjust import (
    enrich_structure,
    plan_progress,
    set_task_done,
    set_task_status,
    task_status,
)


def _structure() -> dict:
    return {
        "stages": [
            {
                "days": [
                    {
                        "day": 1,
                        "tasks": [
                            {"title": "legacy pending", "done": False},
                            {"title": "legacy done", "done": True},
                            {"title": "skipped", "status": "skipped", "done": False},
                        ],
                    }
                ]
            }
        ]
    }


def test_status_normalizes_legacy_and_invalid_values():
    assert task_status({"done": False}) == "pending"
    assert task_status({"done": True}) == "done"
    assert task_status({"status": "skipped", "done": True}) == "skipped"
    assert task_status({"status": "invalid", "done": True}) == "done"


def test_enrichment_and_progress_include_skip_aware_contract():
    enriched = enrich_structure(_structure())
    tasks = enriched["stages"][0]["days"][0]["tasks"]
    assert [task["status"] for task in tasks] == ["pending", "done", "skipped"]
    assert [task["done"] for task in tasks] == [False, True, False]
    assert plan_progress(enriched) == {
        "total": 3,
        "done": 1,
        "skipped": 1,
        "pending": 1,
        "ratio": 0.3333,
        "effective_ratio": 0.5,
    }


def test_explicit_status_is_idempotent_and_does_not_mutate_input():
    original = _structure()
    updated, status = set_task_status(original, "stage0.day0.task0", "skipped")
    assert status == "skipped"
    assert task_status(updated["stages"][0]["days"][0]["tasks"][0]) == "skipped"
    assert original["stages"][0]["days"][0]["tasks"][0] == {
        "title": "legacy pending",
        "done": False,
    }
    repeated, status = set_task_status(updated, "stage0.day0.task0", "skipped")
    assert status == "skipped"
    assert repeated == updated


def test_legacy_toggle_recovers_skipped_to_pending():
    updated, done = set_task_done(_structure(), "stage0.day0.task2", None)
    assert done is False
    assert task_status(updated["stages"][0]["days"][0]["tasks"][2]) == "pending"


def test_all_skipped_is_fully_processed_but_not_reported_done():
    structure = {
        "stages": [{"days": [{"tasks": [{"status": "skipped"}]}]}]
    }
    assert plan_progress(structure) == {
        "total": 1,
        "done": 0,
        "skipped": 1,
        "pending": 0,
        "ratio": 0.0,
        "effective_ratio": 1.0,
    }


def test_invalid_or_missing_task_is_rejected():
    with pytest.raises(ValueError):
        set_task_status(_structure(), "stage0.day0.task0", "ignored")
    with pytest.raises(KeyError):
        set_task_status(_structure(), "stage9.day0.task0", "done")
