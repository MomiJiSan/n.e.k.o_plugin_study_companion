"""Closed registry for compatible cognitive-engine component versions.

The engine never assembles extractor, catalog, reducer and validator versions
independently at runtime.  A named version set is the only supported unit of
compatibility; unknown names therefore disable cognitive behaviour rather than
silently falling back to a different projection.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

LEGACY_COGNITIVE_VERSION_SET = "cognitive-v1"
DEFAULT_COGNITIVE_VERSION_SET = "cognitive-v2.1-1"


@dataclass(frozen=True, slots=True)
class CognitiveVersionSet:
    name: str
    extractor_version: str
    catalog_version: str
    reducer_version: str
    projection_version: str
    validator_version: str


_VERSION_SETS: Mapping[str, CognitiveVersionSet] = MappingProxyType(
    {
        LEGACY_COGNITIVE_VERSION_SET: CognitiveVersionSet(
            name=LEGACY_COGNITIVE_VERSION_SET,
            extractor_version="cognitive-extractor-v1",
            catalog_version="cognitive-catalog-v1",
            reducer_version="cognitive-reducer-v1",
            projection_version="cognitive-v1",
            validator_version="cognitive-question-validator-v2",
        ),
        DEFAULT_COGNITIVE_VERSION_SET: CognitiveVersionSet(
            name=DEFAULT_COGNITIVE_VERSION_SET,
            extractor_version="cognitive-extractor-v1",
            catalog_version="cognitive-catalog-v1",
            reducer_version="cognitive-reducer-v2.1-1",
            projection_version="cognitive-v2.1-1",
            validator_version="cognitive-question-validator-v2.1-1",
        ),
    }
)


def get_cognitive_version_set(name: object) -> CognitiveVersionSet | None:
    """Return an exact supported combination; never coerce unknown input."""

    return _VERSION_SETS.get(str(name or "").strip())


def supported_cognitive_version_sets() -> tuple[str, ...]:
    return tuple(_VERSION_SETS)


__all__ = [
    "CognitiveVersionSet",
    "DEFAULT_COGNITIVE_VERSION_SET",
    "LEGACY_COGNITIVE_VERSION_SET",
    "get_cognitive_version_set",
    "supported_cognitive_version_sets",
]
