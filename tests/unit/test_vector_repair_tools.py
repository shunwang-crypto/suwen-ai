"""Pure safety checks for the missing-vector repair planner."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import scripts.repair_missing_vectors as repair
from scripts.repair_missing_vectors import (
    RepairCandidate,
    RepairContractError,
    embedding_text,
    estimate_plan,
    select_missing_ids,
    validate_execute_budget,
)


def candidate(index: int) -> RepairCandidate:
    return RepairCandidate(
        chunk_id=f"00000000-0000-0000-0000-{index:012d}",
        tenant_id="11111111-1111-1111-1111-111111111111",
        kb_id="22222222-2222-2222-2222-222222222222",
        doc_id="33333333-3333-3333-3333-333333333333",
        text="正文",
        heading_path="章节",
        context_prefix="上下文",
        parent_id="44444444-4444-4444-4444-444444444444",
        page_start=1,
        page_end=1,
        kc_ids=[],
    )


def test_embedding_input_preserves_prefix_heading_and_text():
    assert embedding_text(candidate(1)) == "上下文\n章节\n正文"


def test_unknown_cost_rejects_large_batch():
    with pytest.raises(RepairContractError, match="UNKNOWN_COST"):
        estimate_plan(
            [candidate(index) for index in range(101)],
            batch_size=32,
            price_per_million_tokens=None,
            missing_point_count=101,
            skipped_missing_text=0,
            truncated=False,
        )


def test_execute_requires_cost_guard():
    plan = estimate_plan(
        [candidate(1)],
        batch_size=32,
        price_per_million_tokens=None,
        missing_point_count=1,
        skipped_missing_text=0,
        truncated=False,
    )
    with pytest.raises(RepairContractError, match="REQUIRES_KNOWN_COST"):
        validate_execute_budget(plan, None)
    with pytest.raises(RepairContractError, match="REQUIRES_KNOWN_COST"):
        validate_execute_budget(plan, 100.0)


def test_doc_scope_is_filtered_before_max_points_cap():
    ids = [candidate(index).chunk_id for index in range(3)]

    selected, scoped_count, truncated = select_missing_ids(
        ids,
        matching_ids={ids[2]},
        max_points=1,
    )

    assert selected == [ids[2]]
    assert scoped_count == 1
    assert truncated is False


def test_scope_matching_ids_are_normalised_before_statistics():
    item = candidate(1).chunk_id
    selected, scoped_count, truncated = select_missing_ids(
        [item.upper()],
        matching_ids={item.upper()},
        max_points=1,
    )
    assert selected == [item]
    assert scoped_count == 1
    assert truncated is False


def test_repair_point_and_batch_limits_are_hard_limits():
    with pytest.raises(RepairContractError, match="PLAN_POINT_LIMIT"):
        estimate_plan(
            [candidate(index) for index in range(101)],
            batch_size=32,
            price_per_million_tokens=1.0,
            missing_point_count=101,
            skipped_missing_text=0,
            truncated=True,
        )

    with pytest.raises(RepairContractError, match="batch-size"):
        estimate_plan(
            [candidate(1)],
            batch_size=33,
            price_per_million_tokens=1.0,
            missing_point_count=1,
            skipped_missing_text=0,
            truncated=False,
        )


def test_negative_and_nonfinite_cost_limits_are_rejected():
    with pytest.raises(RepairContractError, match="finite"):
        estimate_plan(
            [candidate(1)],
            batch_size=32,
            price_per_million_tokens=-0.1,
            missing_point_count=1,
            skipped_missing_text=0,
            truncated=False,
        )

    plan = estimate_plan(
        [candidate(1)],
        batch_size=32,
        price_per_million_tokens=1.0,
        missing_point_count=1,
        skipped_missing_text=0,
        truncated=False,
    )
    with pytest.raises(RepairContractError, match="finite"):
        validate_execute_budget(plan, float("nan"))
    with pytest.raises(RepairContractError, match="finite"):
        validate_execute_budget(plan, -1.0)


def test_optional_kb_and_doc_flags_are_not_validated_as_none(monkeypatch, capsys):
    plan = estimate_plan(
        [],
        batch_size=32,
        price_per_million_tokens=None,
        missing_point_count=0,
        skipped_missing_text=0,
        truncated=False,
    )

    async def fake_build_plan(_args):
        return plan, None

    monkeypatch.setattr(repair, "_build_plan", fake_build_plan)
    args = repair._parser().parse_args(
        [
            "--target-id",
            "maintenance-window-1",
            "--environment",
            "staging",
            "--database-url",
            "postgresql+asyncpg://readonly:pw@db.example/eduagents",
            "--qdrant-url",
            "https://qdrant.example",
        ]
    )

    assert asyncio.run(repair._async_main(args)) == 0
    assert "candidate_ids" not in capsys.readouterr().out


def test_post_reconciliation_is_aggregate_and_rejects_remaining_target():
    plan = estimate_plan(
        [candidate(1)],
        batch_size=32,
        price_per_million_tokens=1.0,
        missing_point_count=1,
        skipped_missing_text=0,
        truncated=False,
    )
    report = SimpleNamespace(
        exact=True,
        errors=[],
        status="drift",
        missing_points=[],
        extra_points=["extra-point"],
        wrong_tenant_points=[],
        malformed_points=[],
    )

    summary = repair._post_reconcile_summary(plan, report)

    assert summary == {
        "status": "drift",
        "verified_points": 1,
        "remaining_missing_points_in_scope": 0,
        "remaining_extra_points_in_scope": 1,
        "remaining_wrong_tenant_points_in_scope": 0,
        "remaining_malformed_points_in_scope": 0,
    }
    report.missing_points = [plan.candidates[0].chunk_id]
    with pytest.raises(RepairContractError, match="STILL_MISSING"):
        repair._post_reconcile_summary(plan, report)


def test_retrieval_smoke_uses_candidate_tenant_and_kb_filters():
    calls = []

    class FakeStore:
        async def search_dense(self, tenant_id, kb_id, vector, limit):
            calls.append(("dense", tenant_id, kb_id, limit))
            return [SimpleNamespace(id=plan.candidates[0].chunk_id)]

        async def search_sparse(self, tenant_id, kb_id, sparse, limit):
            calls.append(("sparse", tenant_id, kb_id, limit))
            return []

    plan = estimate_plan(
        [candidate(1)],
        batch_size=32,
        price_per_million_tokens=1.0,
        missing_point_count=1,
        skipped_missing_text=0,
        truncated=False,
    )
    smoke_kbs = asyncio.run(
        repair._run_retrieval_smoke(
            FakeStore(),
            plan.candidates,
            {plan.candidates[0].chunk_id: [1.0]},
            {plan.candidates[0].chunk_id: {1: 1.0}},
        )
    )

    assert smoke_kbs == 1
    assert calls == [
        ("dense", plan.candidates[0].tenant_id, plan.candidates[0].kb_id, 10),
        ("sparse", plan.candidates[0].tenant_id, plan.candidates[0].kb_id, 10),
    ]


def test_execute_rejects_invalid_batch_before_opening_provider():
    plan = estimate_plan(
        [],
        batch_size=32,
        price_per_million_tokens=None,
        missing_point_count=0,
        skipped_missing_text=0,
        truncated=False,
    )

    with pytest.raises(RepairContractError, match="batch-size"):
        asyncio.run(repair.execute_plan(plan, None, batch_size=33, concurrency=1))


def test_execute_plan_rejects_unknown_cost_before_provider_import_or_write():
    plan = estimate_plan(
        [candidate(1)],
        batch_size=32,
        price_per_million_tokens=None,
        missing_point_count=1,
        skipped_missing_text=0,
        truncated=False,
    )

    with pytest.raises(RepairContractError, match="REQUIRES_KNOWN_COST"):
        asyncio.run(
            repair.execute_plan(
                plan,
                None,
                batch_size=32,
                concurrency=1,
                max_cost_cny=100.0,
            )
        )


def test_cli_execute_rejects_unknown_cost_before_building_plan(monkeypatch):
    async def fail_if_called(_args):
        raise AssertionError("unknown-cost execute must fail before any plan read")

    monkeypatch.setattr(repair, "_build_plan", fail_if_called)
    args = repair._parser().parse_args(
        [
            "--target-id",
            "maintenance-window-1",
            "--environment",
            "staging",
            "--database-url",
            "postgresql+asyncpg://readonly@db.example/eduagents",
            "--qdrant-url",
            "https://qdrant.example",
            "--execute",
        ]
    )

    with pytest.raises(RepairContractError, match="REQUIRES_KNOWN_COST"):
        asyncio.run(repair._async_main(args))
