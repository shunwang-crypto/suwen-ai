"""Paired direct-prompt/single-DSH/team-DSH experiments without application history."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from importlib.metadata import version
from pathlib import Path

import httpx
from eduagents_core.config import get_settings
from eduagents_core.education.plan import _coerce_plan
from eduagents_core.education.plan_checks import plan_issues
from eduagents_core.education.prompt_evolution import (
    JUDGE_POLICY,
    CaseOutcome,
    EvaluationGateway,
    EvolutionCase,
    Judgment,
    case_digest,
    evaluate_case,
    validate_cases,
)
from eduagents_core.education.prompts import load_prompt_bundle
from eduagents_core.gateway import OpenAIGateway
from eduagents_core.gateway.base import ChatMessage, ChatRequest, GatewayError, Tier
from eduagents_core.json_utils import parse_json_object

if __package__:
    from .local_model import prepare_ollama
else:
    from local_model import prepare_ollama

ROOT = Path(__file__).resolve().parents[1]
MODES = ("direct", "single", "team")
ARTIFACT_KEYS = {"qa": "answer", "quiz": "questions", "plan": "plan", "tutor": "reply"}
PLAN_FORMAT = """plan 是 {stages:[{name,days:[{day,tasks:[{title,minutes,kc_names,action_type,
objective,instructions,completion_criteria,resource_ref,step_key,learning_phase,depends_on}]}]}]}。
step_key 为本计划内唯一英文引用键；learning_phase 为 baseline/learn/practice/review/assessment；
depends_on 为先前步骤引用键列表。未观测主题先基线再教学再练习；复习依赖先前练习。
没有可用资源时 resource_ref 为 null；每天都有任务，分钟总和接近给定预算。"""


async def direct_case(case, bundle, gateway, *, repetition, judge):
    start = len(gateway.results)
    request_start = len(gateway.requests)
    judgment, artifact, error = None, None, ""
    valid = False
    key = ARTIFACT_KEYS[case.task]
    policy = bundle.text(case.task)
    if case.task == "plan":
        policy += "\n" + PLAN_FORMAT
    if case.task == "tutor":
        hint = min(int(case.payload.get("hint_level", 0)), 3)
        if hint >= 2 and case.payload.get("answer_requests", 0) >= 1:
            hint = 3
        policy += "\n" + bundle.text(f"tutor_hint{hint}")
        if hint < 3:
            policy += "\n禁止完整解答和最终答案。"
    policy += f"\n本次只进行一次直接生成。输出 JSON：{{\"{key}\":...}}；无需调用工具或执行确认流程。"
    try:
        response = await gateway.chat(ChatRequest(
            messages=(ChatMessage("system", policy), ChatMessage("user", json.dumps({
                "task": case.task, "input": case.payload, "sources": [s.model_dump() for s in case.sources],
            }, ensure_ascii=False))),
            tier=Tier.plan if case.task == "plan" else Tier.standard,
            temperature=0.2, max_tokens=8000 if case.task == "plan" else 4000,
            json_mode=True, feature="agent_ablation_direct",
        ))
        artifact = (parse_json_object(response.text) or {}).get(key)
        if case.task == "plan":
            artifact = _coerce_plan(artifact)
        if case.task in {"qa", "tutor"}:
            valid = isinstance(artifact, str) and bool(artifact.strip())
        elif case.task == "quiz":
            valid = isinstance(artifact, list) and bool(artifact) and all(isinstance(q, dict) for q in artifact)
        else:
            valid = isinstance(artifact, dict) and bool(artifact)
        if not valid:
            error = "EVAL_ARTIFACT_INVALID"
        elif judge:
            result = await gateway.chat(ChatRequest(
                messages=(ChatMessage("system", JUDGE_POLICY), ChatMessage("user", json.dumps({
                    "task": case.task, "input": case.payload, "sources": [s.model_dump() for s in case.sources],
                    "rubric": case.rubric, "artifact": artifact,
                    "hint_level": case.payload.get("hint_level", 0) if case.task == "tutor" else None,
                }, ensure_ascii=False))),
                tier=Tier.strong, temperature=0, max_tokens=1000, json_mode=True,
                feature="prompt_evolution_judge",
            ))
            judgment = Judgment.model_validate(parse_json_object(result.text))
    except GatewayError as exc:
        if exc.code == "BUDGET_EXCEEDED":
            raise
        error = exc.code
    except ValueError:
        error = "EVAL_JUDGE_OR_ARTIFACT_INVALID"
    results = gateway.results[start:]
    return CaseOutcome(
        case.id, case_digest(case), case.split, case.task, repetition, bundle.identity,
        valid, artifact,
        judgment.score if judgment else None, list(judgment.violations) if judgment else [],
        list(judgment.feedback) if judgment else [], error, [], len(gateway.requests) - request_start,
        sum(r.cost_cny for r in results), sum(r.latency_ms for r in results),
        sum(r.prompt_tokens + r.completion_tokens for r in results),
        sorted({f"{r.provider}:{r.model}:{r.tier}" for r in results}),
        bool(results) and all(r.mode in {"live", "record"} and not r.degraded for r in results),
    )


def summarize(rows):
    summary = {}
    for mode in MODES:
        selected = [r for r in rows if r["architecture"] == mode]
        scores = [r["score"] for r in selected if r["score"] is not None]
        summary[mode] = {
            "samples": len(selected), "completed": sum(r["completed"] for r in selected),
            "clean": sum(r["completed"] and not r["error_code"] and not r["violations"] and not r["contract_errors"] for r in selected),
            "judge_mean": sum(scores) / len(scores) if scores else None,
            "judge_scored": len(scores), "quality_violations": sum(bool(r["violations"]) for r in selected),
            "tool_error_cases": sum(bool(r["tool_errors"]) for r in selected),
            "generation_calls": sum(r.get("generation_calls", 0) for r in selected),
            "generation_cost_cny": sum(r.get("generation_cost_cny", 0.) for r in selected),
            "generation_mean_wall_ms": sum(r.get("generation_wall_ms", 0) for r in selected) / len(selected) if selected else None,
            "judge_cost_cny": sum(r.get("judge_cost_cny", 0.) for r in selected),
            "calls": sum(r["model_calls"] for r in selected),
            "cost_cny": sum(r["cost_cny"] for r in selected),
            "mean_wall_ms": sum(r["wall_ms"] for r in selected) / len(selected) if selected else None,
        }
    return summary


async def run(args):
    settings = get_settings().model_copy(update={"gateway_mode": "stub" if args.smoke else "live"})
    bundle = load_prompt_bundle(args.prompt_version)
    source = args.golden.read_text("utf-8")
    cases = [EvolutionCase.model_validate_json(line) for line in source.splitlines() if line.strip()]
    validate_cases(cases)
    if args.case_id:
        missing = set(args.case_id) - {c.id for c in cases}
        if missing:
            raise ValueError(f"unknown case ids: {sorted(missing)}")
        cases = [c for c in cases if c.id in args.case_id]
    if args.split != "all":
        cases = [c for c in cases if c.split == args.split]
    if not cases:
        raise ValueError("no selected cases")
    if not args.output.is_absolute() or args.output.resolve().is_relative_to(ROOT.resolve()):
        raise ValueError("reports must use an absolute output path outside the repository")
    if args.output.resolve() == args.golden.resolve():
        raise ValueError("report cannot overwrite the golden input")
    runtime_info = {}
    if args.ollama_model:
        settings, runtime_info = await prepare_ollama(settings, args.ollama_model)
    gateway = EvaluationGateway(OpenAIGateway(settings), max_calls=args.max_calls, max_cost_cny=args.max_cost_cny)
    report = {
        "schema_version": 2, "mode": "smoke" if args.smoke else ("ollama" if args.ollama_model else "live"), "prompt": bundle.identity,
        "golden_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "judge_sha256": hashlib.sha256(JUDGE_POLICY.encode()).hexdigest(),
        "harness_sdk": version("deepseek-harness-sdk"), "repetitions": args.repetitions,
        "limitations": ["Synthetic developer-authored cases, not a public benchmark or measured learning gains.",
                        "The same configured judge scores anonymized artifacts; model-judge bias remains.",
                        "Team delegation currently changes plans only; QA, quiz and tutor retain specialized single sessions.",
                        "Smoke models prove wiring only. Logical call/cost limits include judges; provider retries can add HTTP attempts."],
        "outcomes": [],
    }
    report.update(runtime_info)
    try:
        for repetition in range(args.repetitions):
            for index, case in enumerate(cases):
                offset = (index + repetition) % len(MODES)
                for mode in MODES[offset:] + MODES[:offset]:
                    prior_judge_calls = gateway.judge_calls
                    prior_judge_cost = gateway.judge_cost_cny
                    prior_judge_seconds = gateway.judge_seconds
                    started = time.monotonic()
                    if mode == "direct":
                        outcome = await direct_case(case, bundle, gateway, repetition=repetition, judge=not args.smoke)
                    else:
                        configured = settings.model_copy(update={"agent_collaboration_enabled": mode == "team"})
                        outcome = await evaluate_case(case, bundle, configured, gateway, repetition=repetition, judge=not args.smoke)
                    gateway.clear_trace()
                    row = outcome.to_dict()
                    row.update(architecture=mode, wall_ms=round((time.monotonic() - started) * 1000), contract_errors=[])
                    judge_ms = round(1000 * (gateway.judge_seconds - prior_judge_seconds))
                    row.update(judge_calls=gateway.judge_calls - prior_judge_calls,
                               judge_cost_cny=gateway.judge_cost_cny - prior_judge_cost, judge_wall_ms=judge_ms)
                    row.update(generation_calls=outcome.model_calls - row["judge_calls"],
                               generation_cost_cny=max(0.0, outcome.cost_cny - row["judge_cost_cny"]),
                               generation_wall_ms=max(0, row["wall_ms"] - judge_ms))
                    if case.task == "plan" and isinstance(outcome.artifact, dict):
                        row["contract_errors"] = [v["code"] for v in plan_issues(outcome.artifact, case.payload)]
                    report["outcomes"].append(row)
                    report.update(summary=summarize(report["outcomes"]), calls=gateway.calls, cost_cny=gateway.cost_cny,
                                  reasoning_response_count=gateway.reasoning_response_count)
                    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8")
                    print(json.dumps({"case": case.id, "architecture": mode, "completed": outcome.completed,
                                      "score": outcome.score, "calls": outcome.model_calls, "error": outcome.error_code}, ensure_ascii=False), flush=True)
                    if outcome.error_code in {"MODEL_BALANCE_INSUFFICIENT", "BYOK_BALANCE_INSUFFICIENT"}:
                        raise GatewayError(outcome.error_code, "供应商余额不足，已保存部分报告并停止评测")
    finally:
        report.update(summary=summarize(report["outcomes"]), calls=gateway.calls, cost_cny=gateway.cost_cny,
                      reasoning_response_count=gateway.reasoning_response_count)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--smoke", action="store_true")
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--ollama-model", help="evaluate all model tiers locally with this installed tool-capable model")
    parser.add_argument("--golden", type=Path, default=ROOT / "eval/golden_set/education-v2.jsonl")
    parser.add_argument("--prompt-version", default="edu-v2")
    parser.add_argument("--case-id", action="append")
    parser.add_argument("--split", choices=("all", "tune", "holdout"), default="holdout")
    parser.add_argument("--repetitions", type=int, choices=range(1, 6), default=2)
    parser.add_argument("--max-calls", type=int, default=1600)
    parser.add_argument("--max-cost-cny", type=float, default=15.0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except GatewayError as exc:
        parser.exit(2, f"agent evaluation stopped: {exc.code}\n")
    except (ValueError, OSError, httpx.HTTPError) as exc:
        parser.exit(2, f"agent evaluation failed: {exc}\n")


if __name__ == "__main__":
    main()
