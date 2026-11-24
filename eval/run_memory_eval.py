"""Small synthetic recall/update/forgetting suite, inspired by LongMemEval tasks."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from eduagents_core.education.memory import MAX_CONTEXT_CHARS, recall


def evaluate(cases):
    results = []
    for case in cases:
        items = [dict(item) for item in case["memories"]]
        for item in items:
            for key in ("created_at", "observed_at", "updated_at", "expires_at"):
                if item.get(key):
                    item[key] = datetime.fromisoformat(item[key])
        result = recall(items, case["query"], case.get("kc_names", []), now=datetime.fromisoformat(case["now"]))
        actual = [item["id"] for item in result["memories"]]
        expected = case["expected_ids"]
        results.append({
            "id": case["id"], "category": case["category"],
            "passed": actual == expected and len(json.dumps(result, ensure_ascii=False)) <= MAX_CONTEXT_CHARS,
            "expected_ids": expected, "actual_ids": actual,
        })
    return {"synthetic": True, "live_model": False, "passed": sum(r["passed"] for r in results),
            "total": len(results), "cases": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=Path(__file__).with_name("memory_cases.json"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = evaluate(json.loads(args.cases.read_text()))
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(serialized + "\n")
    print(serialized)
    return int(report["passed"] != report["total"])


if __name__ == "__main__":
    raise SystemExit(main())
