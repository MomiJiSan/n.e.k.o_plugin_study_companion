from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_ruff_ci_workflow_is_a_full_pr_and_main_gate() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ruff.yml").read_text(
        encoding="utf-8"
    )

    assert "push:\n    branches:\n      - main" in workflow
    assert "pull_request:" in workflow
    assert "workflow_dispatch:" in workflow
    assert "paths:" not in workflow
    assert "permissions:\n  contents: read" in workflow
    assert "persist-credentials: false" in workflow
    assert "name: Ruff (0.12.4)" in workflow
    assert "timeout-minutes: 10" in workflow
    assert (
        "uvx ruff==0.12.4 check --ignore-noqa --config ruff.toml ." in workflow
    )
    assert "continue-on-error" not in workflow
