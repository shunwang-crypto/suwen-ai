from __future__ import annotations

import uuid

import pytest
from eduagents_api.plan_adjust import (
    enrich_structure,
    plan_progress,
    set_task_status,
    task_status,
)


def _structure() -> dict:
    task_ids = [str(uuid.UUID(int=i + 1)) for i in range(3)]
    return {
        "stages": [
            {
                "days": [
                    {
                        "day": 1,
                        "tasks": [
                            {"task_id": task_ids[0], "title": "pending", "status": "pending"},
                            {"task_id": task_ids[1], "title": "done", "status": "done"},
                            {"task_id": task_ids[2], "title": "skipped", "status": "skipped"},
                        ],
                    }
                ]
            }
        ]
    }


def test_status_uses_only_explicit_status():
    assert task_status({"status": "pending"}) == "pending"
    assert task_status({"status": "done"}) == "done"
    assert task_status({"status": "skipped"}) == "skipped"
    assert task_status({"status": "invalid"}) == "pending"


def test_enrichment_and_progress_include_skip_aware_contract():
    enriched = enrich_structure(_structure())
    tasks = enriched["stages"][0]["days"][0]["tasks"]
    assert [task["status"] for task in tasks] == ["pending", "done", "skipped"]
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
    task_id = original["stages"][0]["days"][0]["tasks"][0]["task_id"]
    updated, status = set_task_status(original, task_id, "skipped")
    assert status == "skipped"
    assert task_status(updated["stages"][0]["days"][0]["tasks"][0]) == "skipped"
    assert original["stages"][0]["days"][0]["tasks"][0] == {
        "task_id": task_id,
        "title": "pending",
        "status": "pending",
    }
    repeated, status = set_task_status(updated, task_id, "skipped")
    assert status == "skipped"
    assert repeated == updated


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
        set_task_status(_structure(), _structure()["stages"][0]["days"][0]["tasks"][0]["task_id"], "ignored")
    with pytest.raises(KeyError):
        set_task_status(_structure(), str(uuid.UUID(int=99)), "done")
