from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "_material_topic_mapper_test"
if PACKAGE_NAME not in sys.modules:
    package = ModuleType(PACKAGE_NAME)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    sys.modules[PACKAGE_NAME] = package

mapper_module = importlib.import_module(f"{PACKAGE_NAME}.material_topic_mapper")
MaterialMappingInput = mapper_module.MaterialMappingInput
MaterialTopicMapper = mapper_module.MaterialTopicMapper


class _Store:
    def __init__(
        self,
        topics: list[dict[str, Any]],
        *,
        mastery: dict[str, float] | None = None,
        missing_on_validation: set[str] | None = None,
    ) -> None:
        self._topics = {str(topic["id"]): dict(topic) for topic in topics}
        self._mastery = dict(mastery or {})
        self._missing_on_validation = set(missing_on_validation or ())

    def list_topics(self, limit: int | None = 100, **_kwargs: object):
        assert limit is None
        return list(self._topics.values())

    def get_topic(self, topic_id: str):
        if topic_id in self._missing_on_validation:
            return None
        return self._topics.get(topic_id)

    def get_latest_mastery(self, topic_id: str):
        if topic_id not in self._mastery:
            return None
        return {"topic_id": topic_id, "mastery": self._mastery[topic_id]}


def _topic(
    topic_id: str,
    name: str,
    *,
    aliases: list[str] | None = None,
    prerequisites: list[object] | None = None,
) -> dict[str, Any]:
    return {
        "id": topic_id,
        "name": name,
        "aliases": aliases or [],
        "prerequisites": prerequisites or [],
    }


def test_mapper_deduplicates_and_applies_material_confidence_thresholds() -> None:
    topics = [
        _topic("algebra", "Algebra"),
        _topic("geometry", "Geometry"),
        _topic("probability", "Probability"),
    ]

    def matcher(_topics, *, query: str, limit: int):
        assert limit == 5
        if query == "An Algebra overview":
            return [
                {"id": "algebra", "score": 50},
                {"id": "geometry", "score": 20},
            ]
        if query == "memo one":
            return [
                {"id": "algebra", "score": 60},
                {"id": "geometry", "score": 12},
                {"id": "probability", "score": 9},
            ]
        return [
            {"id": "geometry", "score": 15},
            {"id": "probability", "score": 9},
        ]

    result = MaterialTopicMapper(_Store(topics), matcher=matcher).map(
        MaterialMappingInput(
            source_kind="document",
            merged_summary="An Algebra overview",
            chunk_memos=("memo one", "memo two"),
        )
    )

    assert result.candidates == (
        {
            "topic_id": "algebra",
            "role": "core",
            "mapping_score": 60.0,
            "mapping_confidence": "high",
            "reason_code": "material_exact_match",
            "required": True,
        },
        {
            "topic_id": "geometry",
            "role": "core",
            "mapping_score": 20.0,
            "mapping_confidence": "medium",
            "reason_code": "material_repeated_match",
            "required": True,
        },
    )
    assert result.unmatched_count == 1
    assert result.truncated is False


def test_mapper_revalidates_ids_and_bounds_core_candidates() -> None:
    topics = [_topic(f"topic-{index}", f"Concept {index}") for index in range(5)]

    def matcher(_topics, *, query: str, limit: int):
        del query, limit
        return [
            {"id": f"topic-{index}", "score": 50 + index} for index in range(5)
        ]

    summary = " ".join(f"Concept {index}" for index in range(5))
    result = MaterialTopicMapper(
        _Store(topics, missing_on_validation={"topic-4"}),
        matcher=matcher,
        max_core_topics=3,
    ).map(
        MaterialMappingInput(source_kind="document", merged_summary=summary)
    )

    assert result.truncated is True
    # topic-4 ranks first but disappears during the mandatory store re-check.
    assert [item["topic_id"] for item in result.candidates] == [
        "topic-3",
        "topic-2",
        "topic-1",
    ]
    assert result.unmatched_count == 1


def test_mapper_adds_only_unmastered_bounded_prerequisites_to_depth_two() -> None:
    topics = [
        _topic(
            "core-a",
            "Core A",
            prerequisites=[{"id": "mastered"}, {"id": "pre-one"}],
        ),
        _topic("core-b", "Core B"),
        _topic("mastered", "Mastered"),
        _topic("pre-one", "Pre One", prerequisites=["pre-two"]),
        _topic("pre-two", "Pre Two", prerequisites=["too-deep"]),
        _topic("too-deep", "Too Deep"),
    ]

    def matcher(_topics, *, query: str, limit: int):
        del query, limit
        return [
            {"id": "core-a", "score": 80},
            {"id": "core-b", "score": 70},
        ]

    result = MaterialTopicMapper(
        _Store(topics, mastery={"mastered": 0.9}), matcher=matcher
    ).map(
        MaterialMappingInput(
            source_kind="document", merged_summary="Core A and Core B"
        )
    )

    prerequisites = [
        item for item in result.candidates if item["role"] == "prerequisite"
    ]
    assert [item["topic_id"] for item in prerequisites] == ["pre-one", "pre-two"]
    assert all(item["mapping_score"] == 0.0 for item in prerequisites)
    assert "mastered" not in {item["topic_id"] for item in result.candidates}
    assert "too-deep" not in {item["topic_id"] for item in result.candidates}


def test_mapper_does_not_retain_or_return_material_text() -> None:
    sentinel = "PRIVATE-MATERIAL-ZXQ-882"
    topic = _topic("privacy", "Privacy")

    def matcher(_topics, *, query: str, limit: int):
        assert sentinel in query
        assert limit == 5
        return [{"id": "privacy", "score": 50}]

    mapper = MaterialTopicMapper(_Store([topic]), matcher=matcher)
    result = mapper.map(
        MaterialMappingInput(
            source_kind="document",
            merged_summary=f"Privacy {sentinel}",
            chunk_memos=(f"memo {sentinel}",),
        )
    )

    assert sentinel not in repr(mapper.__dict__)
    assert sentinel not in repr(result.to_dict())
    assert set(result.candidates[0]) == {
        "topic_id",
        "role",
        "mapping_score",
        "mapping_confidence",
        "reason_code",
        "required",
    }


def test_real_matcher_rejects_a_single_generic_word() -> None:
    result = MaterialTopicMapper(
        _Store([_topic("study-plan", "Study Plan", aliases=["plan"])])
    ).map(
        MaterialMappingInput(source_kind="document", merged_summary="plan")
    )
    assert result.candidates == ()
