"""Generate a deterministic V3 cognitive strategy report from a JSON snapshot."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from adaptive_learning.cognitive_strategy_report import (
    CognitiveStrategyReportError,
    build_cognitive_strategy_report,
    canonical_report_json,
    render_report_markdown,
)
from store_cognitive_strategy import build_cognitive_strategy_report_snapshot


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="python -m tools.cognitive_strategy_report",
        description="Build a read-only attribution report from a frozen JSON snapshot.",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path, help="Frozen snapshot JSON path")
    source.add_argument(
        "--database", type=Path, help="Read a Study Companion SQLite database"
    )
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument("--output", type=Path, help="Write output to this path")
    parser.add_argument("--as-of-root-fact-seq", type=int)
    parser.add_argument("--catalog-version")
    parser.add_argument("--version-set-id")
    return parser


def _error(code: str) -> None:
    print(json.dumps({"error": {"code": code}}, sort_keys=True), file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    connection: sqlite3.Connection | None = None
    try:
        args = _parser().parse_args(argv)
        if args.database is not None:
            database = args.database.resolve()
            connection = sqlite3.connect(
                f"file:{database.as_posix()}?mode=ro", uri=True
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")

            class _ReadOnlyStore:
                def _require_conn(self) -> sqlite3.Connection:
                    assert connection is not None
                    return connection

            snapshot: Any = build_cognitive_strategy_report_snapshot(
                _ReadOnlyStore(),
                as_of_root_fact_seq=args.as_of_root_fact_seq,
                catalog_version=args.catalog_version or "",
                version_set_id=args.version_set_id or "",
            )
        else:
            snapshot = json.loads(args.input.read_text(encoding="utf-8"))
        report = build_cognitive_strategy_report(
            snapshot,
            as_of_root_fact_seq=args.as_of_root_fact_seq,
            catalog_version=args.catalog_version,
            version_set_id=args.version_set_id,
        )
        rendered = (
            canonical_report_json(report)
            if args.format == "json"
            else render_report_markdown(report)
        )
        if args.output is None:
            sys.stdout.write(rendered)
            if not rendered.endswith("\n"):
                sys.stdout.write("\n")
        else:
            args.output.write_bytes(rendered.encode("utf-8"))
    except (ValueError, argparse.ArgumentError):
        _error("invalid_arguments")
        return 2
    except (OSError, json.JSONDecodeError, CognitiveStrategyReportError):
        _error("report_failed")
        return 2
    except sqlite3.DatabaseError:
        _error("report_failed")
        return 2
    finally:
        if connection is not None:
            connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
