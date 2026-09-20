"""Command line release gate for cognitive rollout profiles.

The command only evaluates a supplied config/evidence document and writes an
auditable JSON decision.  It never talks to a store or performs deployment.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from adaptive_learning.cognitive_release_gates import (
    build_release_snapshot,
    evaluate_release_gates,
)


def _json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("A", "B", "C", "D"), required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--report-hash", required=True)
    parser.add_argument("--plugin-version", required=True)
    parser.add_argument("--model-version", required=True)
    parser.add_argument("--database-schema-version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = _json_object(args.config)
        evidence = _json_object(args.evidence)
        # Release automation must provide measured evidence; a missing metric
        # must never be interpreted as zero.  The pure policy remains usable
        # by callers that intentionally supply defaults for unit tests.
        required_metrics = ("ordinary_answer_failures", "duplicate_delivery", "error_rate_bps")
        missing = [name for name in required_metrics if name not in evidence]
        if missing:
            raise ValueError(f"evidence missing required metrics: {', '.join(missing)}")
        if not args.report_hash.strip() or not args.plugin_version.strip() or not args.model_version.strip():
            raise ValueError("report and version metadata must be non-empty")
        snapshot = build_release_snapshot(
            profile=args.profile,
            config=config,
            report_hash=args.report_hash,
            plugin_version=args.plugin_version,
            model_version=args.model_version,
            database_schema_version=args.database_schema_version,
        )
        decision = evaluate_release_gates(
            args.profile, config=config, evidence=evidence
        )
        payload = decision.to_dict()
        payload["snapshot"] = snapshot
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"release gate input error: {exc}", file=sys.stderr)
        return 20
    if decision.allowed:
        return 0
    return 10 if decision.rollback_to == "A" else 20


if __name__ == "__main__":
    raise SystemExit(main())
