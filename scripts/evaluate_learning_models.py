#!/usr/bin/env python3
"""Run B3/B4/B5/B7 on a redacted learning-event JSON export.

This CLI is intentionally offline-only.  It does not import the API database
session, contact Qdrant/object storage/Redis, call a model, or mutate KC edges.

Input shape::

    {
      "events": [{"id": "...", "user_id": "...", "occurred_at": "...", ...}],
      "questions": {"question-id": {"id": "...", "source_type": "manual", ...}},
      "kc_edges": [{"from_kc": "...", "to_kc": "..."}],
      "calibrated_items": [{"canonical_item_id": "...", ...}]
    }

Only identifiers, timestamps, labels, KC IDs, and outcomes are consumed.
Question text and student responses are ignored when present in an export.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from eduagents_core.learning_models.evaluate import run_offline_evaluation


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="redacted JSON export")
    parser.add_argument("--output", type=Path, help="optional local JSON result path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    with args.input.open("r", encoding="utf-8") as handle:
        payload: Any = json.load(handle)
    if not isinstance(payload, dict):
        raise SystemExit("input must be a JSON object")
    result = run_offline_evaluation(payload)
    encoded = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.write_text(encoded + "\n", encoding="utf-8")
    else:
        print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
