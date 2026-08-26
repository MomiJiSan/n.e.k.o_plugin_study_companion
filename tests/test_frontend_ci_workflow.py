from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_frontend_workflow_runs_every_executable_contract_without_paths_filters() -> None:
    workflow = (ROOT / ".github" / "workflows" / "frontend-tests.yml").read_text(
        encoding="utf-8"
    )

    assert "branches: [main]" in workflow
    assert "pull_request:" in workflow
    assert "workflow_dispatch:" in workflow
    assert "paths:" not in workflow
    assert "permissions:\n  contents: read" in workflow
    assert "persist-credentials: false" in workflow
    assert "npm ci" in workflow
    assert "tests/test_workspace_frontend.py" in workflow
    assert "tests/test_notebook_frontend.py" in workflow
    assert "tests/test_scanned_pdf_frontend.py" in workflow
    assert "tests/test_scanned_pdf_surface.py" in workflow
