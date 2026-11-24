"""Run offline native-agent prompt comparisons; outputs stay outside production."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from importlib.metadata import version
from pathlib import Path

from eduagents_core.config import get_settings
from eduagents_core.education.prompt_evolution import (
    JUDGE_POLICY,
    EvaluationGateway,
    EvolutionCase,
    compare_outcomes,
    evaluate_bundle,
    propose_candidate,
    validate_cases,
)
from eduagents_core.education.prompts import PromptBundle, load_prompt_bundle, load_review_prompt
from eduagents_core.gateway import OpenAIGateway
from eduagents_core.gateway.base import GatewayError

ROOT = Path(__file__).resolve().parents[1]


async def run(args):
    settings = get_settings().model_copy(update={"gateway_mode": "stub" if args.smoke else "live"})
    baseline = load_prompt_bundle(args.baseline_version or settings.education_prompt_version)
    golden_text = args.golden.read_text("utf-8")
    cases = [EvolutionCase.model_validate_json(line) for line in golden_text.splitlines() if line.strip()]
    validate_cases(cases)
    tuning, holdout = [c for c in cases if c.split == "tune"], [c for c in cases if c.split == "holdout"]
    if not tuning or not holdout:
        raise ValueError("both tuning and holdout cases are required")
    if args.propose_version and (args.smoke or args.candidate is not None):
        raise ValueError("reflection requires --live and cannot be combined with --candidate")
    if args.propose_version and args.candidate_output is None:
        raise ValueError("reflection needs --candidate-output outside the deployed prompt directory")
    deployed = ROOT / "packages/core/src/eduagents_core/education/prompt_versions"
    for destination in (args.output, args.candidate_output):
        if destination and destination.resolve().is_relative_to(deployed.resolve()):
            raise ValueError("offline evaluation cannot write deployed prompt resources")
        if destination and not destination.parent.is_dir():
            raise ValueError("output directory must already exist")
        if destination and destination.resolve() in {args.golden.resolve(), args.candidate.resolve() if args.candidate else None}:
            raise ValueError("evaluation outputs must not overwrite their inputs")
    if args.candidate_output and args.candidate_output.resolve() == args.output.resolve():
        raise ValueError("candidate and report need separate output paths")
    candidate = None
    if args.candidate:
        candidate = PromptBundle.model_validate_json(args.candidate.read_text("utf-8")).validate_instructions()
    elif args.smoke:
        candidate = baseline.model_copy(update={"version": "smoke-candidate", "parent_version": baseline.version})
    elif args.propose_version:
        # Validate the requested version before invoking any paid model.
        PromptBundle.model_validate({**baseline.model_dump(), "version": args.propose_version})
        if args.propose_version == baseline.version:
            raise ValueError("reflection needs a new immutable version")
    else:
        raise ValueError("live comparison needs --candidate or --propose-version")
    if candidate and (candidate.version == baseline.version or candidate.parent_version != baseline.version):
        raise ValueError("candidate must declare a new version and the selected baseline parent")
    gateway = EvaluationGateway(OpenAIGateway(settings), max_calls=args.max_calls, max_cost_cny=args.max_cost_cny)
    before_tune = await evaluate_bundle(tuning, baseline, settings, gateway, repetitions=args.repetitions, judge=not args.smoke)
    rationale = ""
    if args.propose_version:
        candidate, rationale = await propose_candidate(baseline, before_tune, args.propose_version, gateway)
        args.candidate_output.write_text(candidate.model_dump_json(indent=2) + "\n", "utf-8")
    after_tune = await evaluate_bundle(tuning, candidate, settings, gateway, repetitions=args.repetitions, judge=not args.smoke)
    before_holdout, after_holdout = [], []
    # Alternate order so all baseline cases do not run before all candidate cases.
    for repetition in range(args.repetitions):
        for index, case in enumerate(holdout):
            order = ((baseline, before_holdout), (candidate, after_holdout))
            if (index + repetition) % 2:
                order = tuple(reversed(order))
            for bundle, destination in order:
                destination.extend(await evaluate_bundle([case], bundle, settings, gateway, repetitions=1, judge=not args.smoke))
                destination[-1].repetition = repetition
    before, after = before_tune + before_holdout, after_tune + after_holdout
    report = {
        "schema_version": 1, "mode": "smoke" if args.smoke else "live",
        "baseline": baseline.identity, "candidate": candidate.identity,
        "golden_sha256": hashlib.sha256(golden_text.encode()).hexdigest(),
        "judge_sha256": hashlib.sha256(JUDGE_POLICY.encode()).hexdigest(),
        "reviewer_sha256": {role: hashlib.sha256(load_review_prompt(role).encode()).hexdigest()
                            for role in ("qa", "quiz")},
        "harness_sdk": version("deepseek-harness-sdk"),
        "rationale": rationale,
        "limits": {"max_calls": args.max_calls, "max_cost_cny": args.max_cost_cny,
                   "repetitions": args.repetitions, "harness_calls_per_turn": settings.harness_max_model_calls,
                   "context_window": settings.harness_context_window_tokens},
        "total_model_calls": gateway.calls, "total_cost_cny": gateway.cost_cny,
        "comparison": compare_outcomes(before, after),
        "baseline_outcomes": [outcome.to_dict() for outcome in before],
        "candidate_outcomes": [outcome.to_dict() for outcome in after],
    }
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print(json.dumps({"report": str(args.output), "calls": gateway.calls,
                      "promotion_eligible": report["comparison"]["promotion_eligible"],
                      "reasons": report["comparison"]["reasons"]}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--smoke", action="store_true", help="real Harness with substitute model; no quality claim")
    mode.add_argument("--live", action="store_true", help="explicitly invoke configured paid model providers")
    parser.add_argument("--golden", type=Path, default=ROOT / "eval/golden_set/education-v1.jsonl")
    parser.add_argument("--baseline-version")
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--propose-version")
    parser.add_argument("--candidate-output", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, choices=range(1, 6), default=2)
    parser.add_argument("--max-calls", type=int, default=600)
    parser.add_argument("--max-cost-cny", type=float, default=10.0)
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except GatewayError as exc:
        parser.exit(2, f"prompt evaluation stopped: {exc.code}\n")
    except (ValueError, OSError) as exc:
        parser.exit(2, f"prompt evaluation failed: {exc}\n")


if __name__ == "__main__":
    main()
