from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_python_pytest_workflow_is_a_locked_main_gate() -> None:
    workflow = (ROOT / ".github" / "workflows" / "python-tests.yml").read_text(
        encoding="utf-8"
    )

    assert "name: Python Tests" in workflow
    assert "branches: [main]" in workflow
    assert "pull_request:" in workflow
    assert "workflow_dispatch:" in workflow
    assert "paths:" not in workflow
    assert "permissions:\n  contents: read" in workflow
    assert "name: Python tests (3.11)" in workflow
    assert "timeout-minutes: 15" in workflow
    assert "actions/setup-python@v5" in workflow
    assert 'python-version: "3.11"' in workflow
    assert "persist-credentials: false" in workflow
    assert "uv sync --locked --group dev" in workflow
    assert "uv run --locked python -m pytest -q" in workflow
    assert "continue-on-error" not in workflow
