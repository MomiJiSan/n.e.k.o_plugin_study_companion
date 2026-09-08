from __future__ import annotations

from pathlib import Path

from tools import cognitive_strategy_acceptance as acceptance


def test_synthetic_acceptance_runs_full_ledger_and_report_chain(tmp_path: Path) -> None:
    result = acceptance.run_acceptance(report_dir=tmp_path)

    assert result["evidence_class"] == "synthetic_engineering_only"
    assert result["human_effectiveness_claim"] is False
    assert result["live_database_touched"] is False
    assert result["summary"] == {"status": "PASS"}
    assert result["checks"] == {
        "ledger_exposure_n": 40,
        "ledger_fact_n": 160,
        "independent_exposure_n": 40,
        "common_strata_n": 1,
        "all_endpoints_comparable": True,
    }
    for endpoint in ("immediate", "transfer", "retention"):
        assert result["endpoints"][endpoint] == {
            "status": "comparable",
            "effect": {"numerator": 1, "denominator": 4, "value": 0.25},
            "observed_n": 40,
        }
    assert (tmp_path / acceptance.REPORT_JSON).is_file()
    assert (tmp_path / acceptance.REPORT_MARKDOWN).is_file()


def test_synthetic_acceptance_reports_are_byte_stable(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"

    acceptance.run_acceptance(report_dir=first)
    acceptance.run_acceptance(report_dir=second)

    assert (first / acceptance.REPORT_JSON).read_bytes() == (
        second / acceptance.REPORT_JSON
    ).read_bytes()
    assert (first / acceptance.REPORT_MARKDOWN).read_bytes() == (
        second / acceptance.REPORT_MARKDOWN
    ).read_bytes()


def test_synthetic_acceptance_has_no_network_sleep_or_live_database_dependency() -> None:
    source = Path(acceptance.__file__).read_text(encoding="utf-8")

    assert "sleep(" not in source
    assert "requests" not in source
    assert "httpx" not in source
    assert "study_companion.db" not in source
