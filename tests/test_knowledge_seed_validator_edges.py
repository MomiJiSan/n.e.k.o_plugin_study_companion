from __future__ import annotations

import json
from pathlib import Path

from knowledge_seed_validator import validate_knowledge_seed_manifest


def _topic(topic_id: str, **overrides: object) -> dict[str, object]:
    topic = {
        "id": topic_id,
        "name": topic_id,
        "chapter": "test",
        "depth": 1,
        "difficulty": 0.5,
        "prerequisites": [],
        "related": [],
        "skills": [],
        "question_types": [],
        "examples": [],
        "typical_misconceptions": [],
    }
    topic.update(overrides)
    return topic


def _validate(
    tmp_path: Path,
    topics: list[dict[str, object]],
    *,
    strict_taxonomy_coverage: bool = False,
):
    seed_path = tmp_path / "seed.json"
    manifest_path = tmp_path / "manifest.json"
    seed_path.write_text(
        json.dumps({"subject": "math", "stage": "junior", "topics": topics}),
        encoding="utf-8",
    )
    manifest_path.write_text(json.dumps({"files": ["seed.json"]}), encoding="utf-8")
    return validate_knowledge_seed_manifest(
        manifest_path,
        strict_taxonomy_coverage=strict_taxonomy_coverage,
    )


def test_taxonomy_coverage_is_report_only_until_strict_mode_is_requested(
    tmp_path: Path,
) -> None:
    topics = [_topic("unconnected")]

    report_only = _validate(tmp_path, topics)
    strict = _validate(tmp_path, topics, strict_taxonomy_coverage=True)

    assert report_only.is_valid
    assert "taxonomy_coverage_gap" not in {
        issue.code for issue in report_only.issues
    }
    assert "taxonomy_coverage_gap" in {issue.code for issue in strict.issues}


def test_validator_preserves_supported_distinct_relation_types(tmp_path: Path) -> None:
    result = _validate(
        tmp_path,
        [
            _topic(
                "source",
                related=[
                    {"id": "supports", "relation": "supports", "reason": "support"},
                    {"id": "next", "relation": "next", "reason": "next"},
                    {"id": "nearby", "relation": "nearby", "reason": "nearby"},
                ],
            ),
            _topic("supports"),
            _topic("next"),
            _topic("nearby"),
        ],
    )

    assert result.is_valid


def test_validator_rejects_invalid_edge_placements_aliases_and_keys(tmp_path: Path) -> None:
    result = _validate(
        tmp_path,
        [
            _topic(
                "source",
                prerequisites=[
                    {"id": "target", "relation": "application", "reason": "wrong field"}
                ],
                related=[
                    {"id": "target", "relation": "prerequisite", "reason": "wrong field"},
                    {"id": "target", "relation": "similar", "reason": "legacy alias"},
                    {"id": "source", "relation": "supports", "reason": "self"},
                    {
                        "id": "mastery",
                        "relation": "supports",
                        "reason": "out of range",
                        "required_mastery": 1.1,
                    },
                    {"id": "pair", "relation": "confusable", "reason": "first"},
                ],
            ),
            _topic(
                "target",
                related=[
                    {"id": "pair", "relation": "confusable", "reason": "duplicate"}
                ],
            ),
            _topic("mastery"),
            _topic(
                "pair",
                related=[
                    {"id": "source", "relation": "confusable", "reason": "mirror"}
                ],
            ),
        ],
    )

    codes = {issue.code for issue in result.issues}
    assert {
        "edge_relation_alias",
        "invalid_edge_relation_placement",
        "self_reference",
        "invalid_required_mastery",
        "duplicate_edge",
    } <= codes


def test_validator_detects_canonical_prerequisite_cycles(tmp_path: Path) -> None:
    result = _validate(
        tmp_path,
        [
            _topic("first", prerequisites=[{"id": "second"}]),
            _topic("second", prerequisites=[{"id": "first"}]),
        ],
    )

    assert "prerequisite_cycle" in {issue.code for issue in result.issues}


def test_validator_requires_prerequisite_mastery_threshold(tmp_path: Path) -> None:
    result = _validate(
        tmp_path,
        [
            _topic("target", prerequisites=[{"id": "prerequisite"}]),
            _topic("prerequisite"),
        ],
    )

    assert "missing_required_mastery" in {issue.code for issue in result.issues}


def test_validator_rejects_prerequisite_from_higher_stage(tmp_path: Path) -> None:
    result = _validate(
        tmp_path,
        [
            _topic(
                "primary-target",
                stage="primary",
                prerequisites=[
                    {
                        "id": "junior-prerequisite",
                        "relation": "prerequisite",
                        "reason": "wrong stage direction",
                        "required_mastery": 0.55,
                    }
                ],
            ),
            _topic("junior-prerequisite", stage="junior_high"),
        ],
    )

    assert "reverse_stage_prerequisite" in {
        issue.code for issue in result.issues
    }


def test_validator_allows_prerequisite_from_lower_stage(tmp_path: Path) -> None:
    result = _validate(
        tmp_path,
        [
            _topic("primary-prerequisite", stage="primary"),
            _topic(
                "junior-target",
                stage="junior_high",
                prerequisites=[
                    {
                        "id": "primary-prerequisite",
                        "relation": "prerequisite",
                        "reason": "correct stage direction",
                        "required_mastery": 0.55,
                    }
                ],
            ),
        ],
    )

    assert result.is_valid
    assert "reverse_stage_prerequisite" not in {
        issue.code for issue in result.issues
    }


def test_validator_requires_sortable_depth_and_difficulty(tmp_path: Path) -> None:
    missing_depth_topic = _topic("missing-depth")
    missing_depth_topic.pop("depth")
    missing_difficulty_topic = _topic("missing-difficulty")
    missing_difficulty_topic.pop("difficulty")
    missing_depth = _validate(tmp_path, [missing_depth_topic])
    missing_difficulty = _validate(tmp_path, [missing_difficulty_topic])
    invalid_depth = _validate(
        tmp_path,
        [_topic("invalid-depth", depth=True), _topic("fractional-depth", depth=2.5)],
    )
    invalid_difficulty = _validate(
        tmp_path,
        [
            _topic("boolean-difficulty", difficulty=True),
            _topic("nan-difficulty", difficulty=float("nan")),
            _topic("out-of-range-difficulty", difficulty=1.1),
        ],
    )

    assert "missing_required_field" in {issue.code for issue in missing_depth.issues}
    assert "missing_required_field" in {issue.code for issue in missing_difficulty.issues}
    assert "invalid_depth" in {issue.code for issue in invalid_depth.issues}
    assert "invalid_difficulty" in {issue.code for issue in invalid_difficulty.issues}
    assert missing_depth.report is not None
    assert missing_difficulty.report is not None
    assert invalid_depth.report is not None
    assert invalid_difficulty.report is not None
    assert missing_depth.report["schema_ready_topics"] == 0
    assert missing_difficulty.report["schema_ready_topics"] == 0
    assert invalid_depth.report["schema_ready_topics"] == 0
    assert invalid_difficulty.report["schema_ready_topics"] == 0


def test_bundled_seed_has_no_invalid_or_duplicate_edges() -> None:
    manifest = Path(__file__).resolve().parents[1] / "static" / "knowledge_graph_seed.json"
    result = validate_knowledge_seed_manifest(manifest)

    assert result.is_valid
    assert len(result.topics) == 892
    assert result.report is not None
    assert result.report["cycles_in_prerequisites"] == 0
    assert result.report["isolated_nodes"] == 0
    assert result.report["prerequisite_stage_reverse_count"] == 0
    assert result.report["edge_count"] == 4790
    assert result.report["relation_counts"]["prerequisite"] == 1019
    assert result.report["relation_counts"]["supports"] == 13


def test_bundled_seed_target_context_audit_baseline() -> None:
    manifest = Path(__file__).resolve().parents[1] / "static" / "knowledge_graph_seed.json"
    result = validate_knowledge_seed_manifest(manifest)

    assert result.report is not None
    report = result.report
    assert report["root_topic_counts"] == 106
    assert report["depth_gt1_root_topic_counts"] == 100
    assert report["missing_required_mastery_count"] == 0
    assert report["prerequisite_depth_reverse_count"] == 46
    assert report["prerequisite_difficulty_reverse_count"] == 77
    assert sum(report["missing_required_mastery_by_subject"].values()) == 0
    assert sum(report["subject_target_context_ready_counts"].values()) + sum(
        report["subject_target_context_gap_counts"].values()
    ) == report["topic_count"]
