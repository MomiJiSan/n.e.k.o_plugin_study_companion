from __future__ import annotations

from tools.cognitive_personalization_acceptance import (
    REPORT_JSON,
    REPORT_MARKDOWN,
    REQUIRED_CASES,
    ROOT,
    run_acceptance,
)


def test_personalization_acceptance_is_deterministic_and_writes_both_reports(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"

    result = run_acceptance(report_dir=first)
    repeated = run_acceptance(report_dir=second)

    assert result == repeated
    assert result["summary"]["status"] == "PASS"
    assert set(result["checks"]["cases"]) == REQUIRED_CASES
    assert result["checks"]["production_defaults_closed"] is True
    assert result["checks"]["canonical_question_recorded"] is True
    assert result["checks"]["canonical_answer_recorded"] is True
    assert result["checks"]["outcome_projection_updated"] is True
    assert result["checks"]["concurrent_delivery_rejected"] is True
    assert (first / REPORT_JSON).read_bytes() == (second / REPORT_JSON).read_bytes()
    assert (first / REPORT_MARKDOWN).read_bytes() == (second / REPORT_MARKDOWN).read_bytes()
    committed = ROOT / "docs" / "reports"
    assert (first / REPORT_JSON).read_bytes() == (committed / REPORT_JSON).read_bytes()
    assert (first / REPORT_MARKDOWN).read_bytes() == (
        committed / REPORT_MARKDOWN
    ).read_bytes()
