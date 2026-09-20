from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_static_study_contract_is_generated_from_versioned_hosted_contract() -> None:
    result = subprocess.run(
        ["uv", "run", "--locked", "python", "tools/sync_study_ui_contracts.py", "--check"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout

    source = (ROOT / "surfaces" / "study_ui_contracts.ts").read_text(encoding="utf-8")
    generated = (ROOT / "static" / "study-ui-contracts.js").read_text(encoding="utf-8")
    assert "STUDY_UI_CONTRACT_VERSION = 1" in source
    assert "contractVersion: exports.STUDY_UI_CONTRACT_VERSION" in generated