"""Command-line interface for the read-only cognitive health snapshot."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any

from .cognitive_observability import CognitiveHealthError, collect_cognitive_health

_EXIT_BY_STATUS = {"healthy": 0, "degraded": 1, "blocked": 2}


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="python -m tools.cognitive_health",
        description="Inspect a cognitive SQLite database without modifying it.",
    )
    parser.add_argument("--db", required=True, help="Path to an existing SQLite file")
    parser.add_argument(
        "--format", choices=("json", "text"), default="text", dest="output_format"
    )
    parser.add_argument(
        "--as-of",
        default=None,
        help="Optional ISO timestamp used for deterministic lease/deadline checks",
    )
    return parser


def _text_report(payload: dict[str, Any]) -> str:
    lines = [
        "Cognitive engine health",
        f"runtime.mode: {payload['runtime']['mode']}",
        f"health.status: {payload['health']['status']}",
        f"as_of: {payload['as_of']}",
    ]
    reasons = payload["health"]["reasons"]
    if not reasons:
        lines.append("reasons: none")
    else:
        lines.append("reasons:")
        for reason in reasons:
            resources = reason.get("resources") or []
            suffix = f" [{', '.join(resources)}]" if resources else ""
            lines.append(
                f"- {reason['severity']}:{reason['code']}={reason['count']}{suffix}"
            )
    extraction = payload["queues"]["extraction"]
    projection = payload["queues"]["topic_projection"]
    outbox = payload["outbox"]
    retention = payload["retention"]
    lines.extend(
        (
            f"extraction: {json.dumps(extraction['counts'], sort_keys=True)}",
            "topic_projection: "
            f"stale={projection['stale']} "
            f"counts={json.dumps(projection['counts'], sort_keys=True)}",
            f"outbox: {json.dumps(outbox['counts'], sort_keys=True)}",
            "retention: "
            f"episodes={json.dumps(retention['episodes']['counts'], sort_keys=True)} "
            f"obligations={json.dumps(retention['obligations']['counts'], sort_keys=True)} "
            f"claims={json.dumps(retention['claims']['counts'], sort_keys=True)}",
        )
    )
    return "\n".join(lines)


def _tool_error(output_format: str, code: str) -> None:
    if output_format == "json":
        print(json.dumps({"error": {"code": code}}, sort_keys=True), file=sys.stderr)
    else:
        print(f"cognitive health error: {code}", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except (ValueError, argparse.ArgumentError):
        _tool_error("text", "invalid_arguments")
        return 3

    try:
        snapshot = collect_cognitive_health(args.db, as_of=args.as_of)
    except CognitiveHealthError:
        _tool_error(args.output_format, "inspection_failed")
        return 3

    payload = snapshot.to_dict()
    if args.output_format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(_text_report(payload))
    return _EXIT_BY_STATUS[str(payload["health"]["status"])]


if __name__ == "__main__":
    raise SystemExit(main())
