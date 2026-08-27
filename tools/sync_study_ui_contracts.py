"""Generate the legacy-browser study contracts from their TypeScript source.

The hosted surface owns ``surfaces/study_ui_contracts.ts``.  The static
workspace consumes an IIFE because it is loaded without a module bundler.  We
use the same pinned TypeScript compiler as the frontend contract tests so the
checked-in browser artifact cannot silently drift from the hosted source.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "surfaces" / "study_ui_contracts.ts"
OUTPUT = ROOT / "static" / "study-ui-contracts.js"
TYPESCRIPT = ROOT / "tests" / "frontend" / "node_modules" / "typescript" / "lib" / "typescript.js"

_TRANSPILE = r"""
const fs = require('fs');
const ts = require(process.argv[1]);
const source = fs.readFileSync(process.argv[2], 'utf8');
const result = ts.transpileModule(source, {
  compilerOptions: {
    target: ts.ScriptTarget.ES2022,
    module: ts.ModuleKind.CommonJS,
    newLine: ts.NewLineKind.LineFeed,
  },
  fileName: process.argv[2],
});
process.stdout.write(result.outputText);
"""


def _generated_contents() -> str:
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("Node.js is required; install frontend test dependencies first.")
    if not TYPESCRIPT.is_file():
        raise RuntimeError(
            "TypeScript is unavailable; run `npm ci` in tests/frontend before syncing UI contracts."
        )

    completed = subprocess.run(
        [node, "-e", _TRANSPILE, str(TYPESCRIPT), str(SOURCE)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip() or "TypeScript transpilation failed.")

    transpiled = completed.stdout.rstrip() + "\n"
    return (
        "/* GENERATED FROM surfaces/study_ui_contracts.ts. DO NOT EDIT BY HAND. */\n"
        "(function attachStudyUiContracts(global) {\n"
        "  var exports = {};\n"
        f"{transpiled}"
        "  global.StudyCompanionUiContracts = Object.freeze({\n"
        "    errorCode: exports.knowledgeMapErrorCode,\n"
        "    errorMessage: exports.knowledgeMapErrorMessage,\n"
        "    isCursorStale: exports.isKnowledgeMapCursorStale,\n"
        "    isScopeNode: exports.isScopeKnowledgeNode,\n"
        "    mergeKnowledgeMapPage: exports.mergeKnowledgeMapPage,\n"
        "  });\n"
        "})(window);\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail when the generated artifact is stale")
    args = parser.parse_args()

    try:
        generated = _generated_contents()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    existing = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
    if args.check:
        if existing != generated:
            print(
                "error: static/study-ui-contracts.js is stale; "
                "run `python tools/sync_study_ui_contracts.py`.",
                file=sys.stderr,
            )
            return 1
        print("study UI contracts are synchronized")
        return 0

    OUTPUT.write_text(generated, encoding="utf-8", newline="\n")
    print(f"updated {OUTPUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
