"""Ablation evidence is paired, keeps failures visible and never accesses history."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from eduagents_core.education.prompt_evolution import (
    EvaluationGateway,
    EvolutionCase,
    validate_cases,
)
from eduagents_core.education.prompts import load_prompt_bundle
from eduagents_core.gateway.base import ChatResult

from eval.plan_contracts import plan_contracts
from eval.run_agent_ablation import direct_case, run, summarize


class Model:
    def __init__(self, artifact):
        self.artifact, self.requests = artifact, []

    async def chat(self, req):
        self.requests.append(req)
        body = {"score": 3, "violations": [], "feedback": []} if req.feature == "prompt_evolution_judge" else self.artifact
        return ChatResult(json.dumps(body, ensure_ascii=False), "fixed", req.tier.value, 10, 10, 0.1, 2, "live")


def test_extended_synthetic_corpus_has_distinct_inputs_and_balanced_holdout():
    path = Path(__file__).resolve().parents[2] / "eval/golden_set/education-v2.jsonl"
    cases = [EvolutionCase.model_validate_json(line) for line in path.read_text().splitlines()]
    validate_cases(cases)
    assert len(cases) >= 100
    for task in ("qa", "quiz", "plan", "tutor"):
        assert len([c for c in cases if c.task == task and c.split == "holdout"]) >= 10


async def test_direct_judge_sees_anonymous_artifact_and_invalid_shape_is_not_completed():
    case = EvolutionCase(id="direct", split="holdout", task="qa", payload={"query": "导数定义"}, rubric=("变化率",))
    model = Model({"answer": "导数是瞬时变化率"})
    result = await direct_case(case, load_prompt_bundle("edu-v2"), EvaluationGateway(model), repetition=1, judge=True)
    assert result.completed and result.score == 3 and result.model_calls == 2
    body = json.loads(model.requests[-1].messages[-1].content)
    assert body["artifact"] == result.artifact
    assert "architecture" not in body and "policies" not in body and "version" not in body
    broken = await direct_case(case, load_prompt_bundle("edu-v2"), EvaluationGateway(Model({"answer": {"invalid": 1}})), repetition=1, judge=True)
    assert not broken.completed and broken.error_code == "EVAL_ARTIFACT_INVALID"


def test_summary_separates_completion_quality_contracts_and_cost():
    rows = [{"architecture": "team", "completed": True, "score": 4, "error_code": "",
             "violations": ["提前泄露答案"], "contract_errors": [], "tool_errors": [],
             "model_calls": 10, "cost_cny": 1.0, "wall_ms": 1000},
            {"architecture": "team", "completed": False, "score": None, "error_code": "HARNESS_TIMEOUT",
             "violations": [], "contract_errors": [], "tool_errors": ["拒绝"],
             "model_calls": 12, "cost_cny": 1.2, "wall_ms": 3000}]
    report = summarize(rows)["team"]
    assert report["samples"] == 2 and report["completed"] == 1 and report["clean"] == 0
    assert report["judge_scored"] == 1 and report["calls"] == 22 and report["mean_wall_ms"] == 2000
    assert report["generation_calls_per_clean"] is None
    rows[0].update(violations=[], generation_calls=8, generation_wall_ms=800)
    rows[1].update(generation_calls=12, generation_wall_ms=2500)
    report = summarize(rows)["team"]
    assert report["clean"] == 1
    assert report["generation_calls_per_clean"] == 20
    assert report["generation_wall_ms_per_clean"] == 3300


async def test_interrupted_ablation_records_selected_denominator(tmp_path, monkeypatch):
    from eduagents_core.gateway.base import ChatRequest, GatewayError

    from eval import run_agent_ablation as runner

    async def exhausted(_case, _bundle, gateway, **kwargs):
        await gateway.chat(ChatRequest(messages=(), feature="test_generation"))
        await gateway.chat(ChatRequest(messages=(), feature="prompt_evolution_judge"))
        raise GatewayError("BUDGET_EXCEEDED", "budget exhausted")

    monkeypatch.setattr(runner, "direct_case", exhausted)
    args = SimpleNamespace(smoke=True, ollama_model=None, prompt_version="edu-v2",
                           golden=runner.ROOT / "eval/golden_set/plan-constraints.jsonl",
                           case_id=["plan-simple-units"], split="holdout", repetitions=2,
                           max_calls=2, max_cost_cny=1., output=tmp_path / "run.json")
    with pytest.raises(GatewayError, match="BUDGET_EXCEEDED"):
        await run(args)
    report = json.loads(args.output.read_text())
    assert report["case_ids"] == ["plan-simple-units"]
    assert (report["recorded_runs"], report["expected_runs"]) == (0, 6)
    assert report["status"] == "interrupted"
    assert report["model_call_accounting"] == {
        "generation_calls": 1, "judge_calls": 1,
        "generation_calls_without_outcome": 1, "judge_calls_without_outcome": 1,
    }


def test_cross_document_relevance_requires_matching_book():
    from eduagents_core.retrieval.base import RetrievedChunk

    from eval.run_retrieval_eval import GoldenItem, is_relevant

    item = GoldenItem("q", "变化率", "微积分.md", ["导数"], "", relevant_chunk_hints=["瞬时变化率"])
    hit = RetrievedChunk("a", "瞬时变化率", "导数", "doc", 1, 1, 0.9, "dense")
    assert not is_relevant(hit, item, {"doc": "其他资料.md"})
    assert is_relevant(hit, item, {"doc": "微积分.md"})


def test_plan_contracts_require_observed_grounded_and_assessable_draft():
    case = EvolutionCase(
        id="grounded", split="holdout", task="plan",
        payload={"plan_mode": "course_bound", "kb_id": "course", "total_days": 1,
                 "budget_hours_per_day": 0.25, "kc_order": ["速度换算"]},
        sources=[{"id": "fixture-1", "title": "讲义", "text": "同时换算长度与时间。"}],
        rubric=["独立换算"],
    )
    assert {check["status"] for check in plan_contracts(case, None, []).values()} == {"unobserved"}
    tasks = [
        {"step_key": "start", "learning_phase": "baseline", "depends_on": [],
         "kc_names": ["速度换算"], "minutes": 3},
        {"step_key": "learn", "learning_phase": "learn", "depends_on": ["start"],
         "kc_names": ["速度换算"], "minutes": 5, "resource_ref": 0},
        {"step_key": "test", "learning_phase": "practice", "depends_on": ["learn"],
         "kc_names": ["速度换算"], "minutes": 7, "instructions": ["把 36 公里每小时换成米每秒"],
         "completion_criteria": "提交带单位的算式，结果为 10 米每秒"},
    ]
    plan = {"stages": [{"days": [{"day": 1, "tasks": tasks}]}]}
    resources = [{"type": "kb", "kb_id": "course", "chunk_id": "fixture-1"}]
    assert {check["status"] for check in plan_contracts(case, plan, resources).values()} == {"passed"}
    resources[0]["title"] = "伪造标题"
    assert "UNVERIFIED_RESOURCE_REFERENCE" in plan_contracts(case, plan, resources)["course_sources"]["errors"]
    resources[0]["title"] = "讲义"
    tasks.append({**tasks[-1], "step_key": "test-2", "completion_criteria": "完成练习。"})
    assert plan_contracts(case, plan, resources)["observable_outcome"]["errors"] == [
        "PRACTICE_WITHOUT_EXPLICIT_CRITERIA",
    ]
    tasks.pop()
    tasks[1]["resource_ref"] = True
    tasks[2].update(minutes=8, depends_on=["start"], completion_criteria="")
    checks = plan_contracts(case, plan, resources)
    assert {name for name, check in checks.items() if check["status"] == "failed"} == {
        "daily_budget", "course_sources", "learning_order", "observable_outcome",
    }


async def test_an_independent_judge_cannot_score_stub_output():
    from dataclasses import replace

    class StubJudge(Model):
        async def chat(self, req):
            result = await super().chat(req)
            return replace(result, mode="stub") if req.feature == "prompt_evolution_judge" else result

    case = EvolutionCase(id="judge", split="holdout", task="qa", payload={"query": "比率"}, rubric=["正确"])
    outcome = await direct_case(case, load_prompt_bundle("edu-v2"), EvaluationGateway(StubJudge({"answer": "比例"})),
                                repetition=0, judge=True)
    assert outcome.generation_live and outcome.completed
    assert outcome.score is None and outcome.judge_status == "EVAL_JUDGE_NON_LIVE"


def test_logged_journey_usage_keeps_cache_and_stub_out_of_real_call_counts():
    from eval.learning_journey_usage import _summary

    counted = _summary([
        ("request", "agent_plan", "live", 2, 20, 10),
        ("request", "agent_plan", "cache", 1, 5, 3),
        ("", "quiz_grade", "record", 1, 7, 3),
        ("other", "enrich_prefix", "stub", 1, 11, 5),
    ])
    assert counted["logged_rows"] == 5 and counted["real_calls"] == 3
    assert (counted["prompt_tokens"], counted["completion_tokens"]) == (27, 13)
    assert counted["by_feature"] == {"agent_plan": 2, "quiz_grade": 1}
    assert counted["by_request"] == {"(empty)": 1, "request": 2}
