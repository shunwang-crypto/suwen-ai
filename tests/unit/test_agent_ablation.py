"""Ablation evidence is paired, keeps failures visible and never accesses history."""

import json
from pathlib import Path

from eduagents_core.education.prompt_evolution import (
    EvaluationGateway,
    EvolutionCase,
    validate_cases,
)
from eduagents_core.education.prompts import load_prompt_bundle
from eduagents_core.gateway.base import ChatResult

from eval.run_agent_ablation import direct_case, summarize


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


def test_cross_document_relevance_requires_matching_book():
    from eduagents_core.retrieval.base import RetrievedChunk

    from eval.run_retrieval_eval import GoldenItem, is_relevant

    item = GoldenItem("q", "变化率", "微积分.md", ["导数"], "", relevant_chunk_hints=["瞬时变化率"])
    hit = RetrievedChunk("a", "瞬时变化率", "导数", "doc", 1, 1, 0.9, "dense")
    assert not is_relevant(hit, item, {"doc": "其他资料.md"})
    assert is_relevant(hit, item, {"doc": "微积分.md"})
