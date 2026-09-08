"""Human-reviewed strategy identities for V3 Shadow attribution.

This catalog describes only strategies that are already bound to reviewed
question blueprints.  It does not select a strategy, generate a question, or
permit a model to extend the catalog at runtime.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Mapping

from .cognitive_catalog import COGNITIVE_CATALOG_V1, CognitiveCatalog
from .contracts import LearningIntent, RepairStrategy

STRATEGY_CATALOG_VERSION_V1 = "cognitive-strategy-catalog-v1"
SHADOW_COLLECTION_DEFAULT_ENABLED = False

StrategyFamily = Literal[
    "compare_correct_wrong_steps",
    "complete_steps",
    "minimal_change",
    "structure_discrimination",
    "alternate_representation",
]
MeasurementPurpose = Literal["probe", "repair", "transfer", "retention"]

_STRATEGY_FAMILIES = frozenset(
    {
        "compare_correct_wrong_steps",
        "complete_steps",
        "minimal_change",
        "structure_discrimination",
        "alternate_representation",
    }
)
_PURPOSE_BY_INTENT: Mapping[LearningIntent, MeasurementPurpose] = MappingProxyType(
    {
        "misconception_probe": "probe",
        "misconception_repair": "repair",
        "transfer_check": "transfer",
        "retention_check": "retention",
    }
)
_IDENTIFIER = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
_VERSION = re.compile(r"^v[1-9][0-9]*$")


@dataclass(frozen=True, slots=True)
class CognitiveStrategyEntry:
    """One immutable strategy identity and its reviewed delivery bindings."""

    catalog_version: str
    strategy_id: str
    strategy_version: str
    strategy_family: StrategyFamily
    title: str
    description: str
    topic_id: str
    hypothesis_code: str
    learning_intent: LearningIntent
    measurement_purpose: MeasurementPurpose
    repair_strategy: RepairStrategy
    comparison_scope_id: str
    baseline: bool
    blueprint_ids: tuple[str, ...]
    review_provenance: str
    shadow_collection_eligible: bool = True

    @property
    def eligible_for_repair_attribution(self) -> bool:
        """Whether this delivery is a repair exposure rather than a measure."""

        return self.measurement_purpose == "repair"


class CognitiveStrategyCatalog:
    """Closed, immutable lookup over manually curated strategy entries."""

    __slots__ = (
        "_by_blueprint",
        "_by_identity",
        "_by_scope",
        "_catalog_version",
        "_entries",
        "_reviewed_catalog",
    )

    def __setattr__(self, name: str, value: object) -> None:
        if hasattr(self, name):
            raise AttributeError("cognitive strategy catalog is immutable")
        object.__setattr__(self, name, value)

    def __init__(
        self,
        catalog_version: str,
        entries: tuple[CognitiveStrategyEntry, ...],
        *,
        reviewed_catalog: CognitiveCatalog,
    ) -> None:
        if not _IDENTIFIER.fullmatch(catalog_version):
            raise ValueError("strategy catalog_version must be a stable identifier")
        if not isinstance(entries, tuple) or not entries:
            raise ValueError("strategy catalog entries must be a non-empty tuple")

        by_identity: dict[tuple[str, str], CognitiveStrategyEntry] = {}
        by_blueprint: dict[str, CognitiveStrategyEntry] = {}
        by_scope: dict[str, list[CognitiveStrategyEntry]] = {}

        for entry in entries:
            self._validate_entry(catalog_version, entry, reviewed_catalog)
            identity = (entry.strategy_id, entry.strategy_version)
            if identity in by_identity:
                raise ValueError(f"duplicate cognitive strategy identity: {identity!r}")
            by_identity[identity] = entry

            for blueprint_id in entry.blueprint_ids:
                if blueprint_id in by_blueprint:
                    raise ValueError(
                        f"reviewed blueprint is bound more than once: {blueprint_id}"
                    )
                by_blueprint[blueprint_id] = entry
            by_scope.setdefault(entry.comparison_scope_id, []).append(entry)

        present_families = {entry.strategy_family for entry in entries}
        if present_families != _STRATEGY_FAMILIES:
            raise ValueError("strategy catalog must contain all five reviewed families")

        for scope_id, scoped_entries in by_scope.items():
            if sum(entry.baseline for entry in scoped_entries) != 1:
                raise ValueError(
                    f"comparison scope must have exactly one baseline: {scope_id}"
                )
            scope_dimensions = {
                (
                    entry.topic_id,
                    entry.hypothesis_code,
                    entry.measurement_purpose,
                )
                for entry in scoped_entries
            }
            if len(scope_dimensions) != 1:
                raise ValueError(
                    f"comparison scope mixes incompatible dimensions: {scope_id}"
                )

        ordered = tuple(sorted(entries, key=self._sort_key))
        self._catalog_version = catalog_version
        self._reviewed_catalog = reviewed_catalog
        self._entries = ordered
        self._by_identity: Mapping[
            tuple[str, str], CognitiveStrategyEntry
        ] = MappingProxyType(by_identity)
        self._by_blueprint: Mapping[str, CognitiveStrategyEntry] = MappingProxyType(
            by_blueprint
        )
        self._by_scope: Mapping[str, tuple[CognitiveStrategyEntry, ...]] = (
            MappingProxyType(
                {
                    scope_id: tuple(sorted(items, key=self._sort_key))
                    for scope_id, items in by_scope.items()
                }
            )
        )

    @staticmethod
    def _sort_key(entry: CognitiveStrategyEntry) -> tuple[object, ...]:
        return (
            entry.topic_id,
            entry.hypothesis_code,
            entry.measurement_purpose,
            entry.comparison_scope_id,
            0 if entry.baseline else 1,
            entry.strategy_id,
            entry.strategy_version,
            entry.blueprint_ids,
        )

    @staticmethod
    def _validate_entry(
        catalog_version: str,
        entry: CognitiveStrategyEntry,
        reviewed_catalog: CognitiveCatalog,
    ) -> None:
        if entry.catalog_version != catalog_version:
            raise ValueError("strategy entry catalog_version does not match catalog")
        if not _IDENTIFIER.fullmatch(entry.strategy_id):
            raise ValueError("strategy_id must be a stable identifier")
        if not _VERSION.fullmatch(entry.strategy_version):
            raise ValueError("strategy_version must use the form vN")
        if entry.strategy_family not in _STRATEGY_FAMILIES:
            raise ValueError("unknown cognitive strategy family")
        if not entry.title.strip() or not entry.description.strip():
            raise ValueError("strategy title and description are required")
        if not _IDENTIFIER.fullmatch(entry.topic_id):
            raise ValueError("strategy topic_id must be canonical")
        if not _IDENTIFIER.fullmatch(entry.hypothesis_code):
            raise ValueError("strategy hypothesis_code must be a stable identifier")
        if not _IDENTIFIER.fullmatch(entry.comparison_scope_id):
            raise ValueError("comparison_scope_id must be a stable identifier")
        if not entry.review_provenance.strip():
            raise ValueError("manual review provenance is required")
        if not isinstance(entry.baseline, bool) or not isinstance(
            entry.shadow_collection_eligible, bool
        ):
            raise ValueError("strategy flags must be booleans")
        if not isinstance(entry.blueprint_ids, tuple) or not entry.blueprint_ids:
            raise ValueError("strategy blueprint_ids must be a non-empty tuple")
        if len(set(entry.blueprint_ids)) != len(entry.blueprint_ids):
            raise ValueError("strategy blueprint_ids must be unique")

        expected_purpose = _PURPOSE_BY_INTENT.get(entry.learning_intent)
        if expected_purpose is None or expected_purpose != entry.measurement_purpose:
            raise ValueError("measurement purpose does not match learning intent")

        canonical_topic = reviewed_catalog.canonical_topic_id(entry.topic_id)
        if canonical_topic != entry.topic_id:
            raise ValueError("strategy entry must use a canonical reviewed topic")

        for blueprint_id in entry.blueprint_ids:
            if not _IDENTIFIER.fullmatch(blueprint_id):
                raise ValueError("blueprint_id must be a stable identifier")
            blueprint = reviewed_catalog.get_blueprint(blueprint_id)
            if blueprint is None:
                raise ValueError(f"unknown reviewed strategy blueprint: {blueprint_id}")
            if (
                blueprint.topic_id != entry.topic_id
                or blueprint.hypothesis_code != entry.hypothesis_code
                or blueprint.learning_intent != entry.learning_intent
                or blueprint.repair_strategy != entry.repair_strategy
            ):
                raise ValueError(
                    f"strategy entry does not match reviewed blueprint: {blueprint_id}"
                )

    @property
    def catalog_version(self) -> str:
        return self._catalog_version

    def get(
        self, strategy_id: str, strategy_version: str
    ) -> CognitiveStrategyEntry | None:
        return self._by_identity.get(
            (str(strategy_id or "").strip(), str(strategy_version or "").strip())
        )

    def entries(
        self,
        *,
        topic_id: str = "",
        hypothesis_code: str = "",
        measurement_purpose: MeasurementPurpose | None = None,
        shadow_collection_eligible: bool | None = None,
    ) -> tuple[CognitiveStrategyEntry, ...]:
        canonical_topic = (
            self._reviewed_catalog.canonical_topic_id(topic_id) if topic_id else None
        )
        if topic_id and canonical_topic is None:
            return ()
        return tuple(
            entry
            for entry in self._entries
            if (not topic_id or entry.topic_id == canonical_topic)
            and (not hypothesis_code or entry.hypothesis_code == hypothesis_code)
            and (
                measurement_purpose is None
                or entry.measurement_purpose == measurement_purpose
            )
            and (
                shadow_collection_eligible is None
                or entry.shadow_collection_eligible == shadow_collection_eligible
            )
        )

    def resolve_reviewed(
        self,
        *,
        topic_id: str,
        hypothesis_code: str,
        learning_intent: LearningIntent,
        repair_strategy: RepairStrategy,
        blueprint_id: str,
    ) -> CognitiveStrategyEntry | None:
        """Resolve an exact delivered blueprint to its approved V3 identity."""

        normalized_blueprint = str(blueprint_id or "").strip()
        entry = self._by_blueprint.get(normalized_blueprint)
        if entry is None or not entry.shadow_collection_eligible:
            return None
        canonical_topic = self._reviewed_catalog.canonical_topic_id(topic_id)
        if (
            canonical_topic != entry.topic_id
            or str(hypothesis_code or "").strip() != entry.hypothesis_code
            or learning_intent != entry.learning_intent
            or repair_strategy != entry.repair_strategy
        ):
            return None
        return entry

    def baseline_for(self, comparison_scope_id: str) -> CognitiveStrategyEntry | None:
        scoped_entries = self._by_scope.get(str(comparison_scope_id or "").strip(), ())
        return next((entry for entry in scoped_entries if entry.baseline), None)


_PROVENANCE = "cognitive-v2.1-reviewed-question-blueprints"
_TOPIC = "calculus.chain_rule"
_HYPOTHESIS = "omit_inner_derivative"

COGNITIVE_STRATEGY_ENTRIES_V1 = (
    CognitiveStrategyEntry(
        catalog_version=STRATEGY_CATALOG_VERSION_V1,
        strategy_id="chain.omit-inner.compare-correct-wrong-steps",
        strategy_version="v1",
        strategy_family="compare_correct_wrong_steps",
        title="Compare correct and incorrect steps",
        description="Contrast two derivations and identify the omitted required factor.",
        topic_id=_TOPIC,
        hypothesis_code=_HYPOTHESIS,
        learning_intent="misconception_probe",
        measurement_purpose="probe",
        repair_strategy="compare_steps",
        comparison_scope_id="chain.omit-inner.probe",
        baseline=False,
        blueprint_ids=("chain.omit-inner.compare-steps.v1",),
        review_provenance=_PROVENANCE,
    ),
    CognitiveStrategyEntry(
        catalog_version=STRATEGY_CATALOG_VERSION_V1,
        strategy_id="chain.omit-inner.complete-steps",
        strategy_version="v1",
        strategy_family="complete_steps",
        title="Complete the missing step",
        description="Supply the missing inner-derivative factor in a fixed derivation.",
        topic_id=_TOPIC,
        hypothesis_code=_HYPOTHESIS,
        learning_intent="misconception_repair",
        measurement_purpose="repair",
        repair_strategy="complete_inner_derivative",
        comparison_scope_id="chain.omit-inner.repair",
        baseline=True,
        blueprint_ids=("chain.omit-inner.fill-factor.v1",),
        review_provenance=_PROVENANCE,
    ),
    CognitiveStrategyEntry(
        catalog_version=STRATEGY_CATALOG_VERSION_V1,
        strategy_id="chain.omit-inner.minimal-change",
        strategy_version="v1",
        strategy_family="minimal_change",
        title="Make one minimal change",
        description="Change one structural element and recompute only its consequences.",
        topic_id=_TOPIC,
        hypothesis_code=_HYPOTHESIS,
        learning_intent="misconception_repair",
        measurement_purpose="repair",
        repair_strategy="minimal_change",
        comparison_scope_id="chain.omit-inner.repair",
        baseline=False,
        blueprint_ids=("chain.omit-inner.minimal-change.v1",),
        review_provenance=_PROVENANCE,
    ),
    CognitiveStrategyEntry(
        catalog_version=STRATEGY_CATALOG_VERSION_V1,
        strategy_id="chain.omit-inner.structure-discrimination",
        strategy_version="v1",
        strategy_family="structure_discrimination",
        title="Discriminate the expression structure",
        description="Classify composition versus product before naming the inner factor.",
        topic_id=_TOPIC,
        hypothesis_code=_HYPOTHESIS,
        learning_intent="misconception_probe",
        measurement_purpose="probe",
        repair_strategy="structure_classification",
        comparison_scope_id="chain.omit-inner.probe",
        baseline=True,
        blueprint_ids=("chain.omit-inner.classify-structure.v1",),
        review_provenance=_PROVENANCE,
    ),
    CognitiveStrategyEntry(
        catalog_version=STRATEGY_CATALOG_VERSION_V1,
        strategy_id="chain.omit-inner.alternate-representation",
        strategy_version="v1",
        strategy_family="alternate_representation",
        title="Use an alternate representation",
        description="Measure transfer with a reviewed problem in a different expression form.",
        topic_id=_TOPIC,
        hypothesis_code=_HYPOTHESIS,
        learning_intent="transfer_check",
        measurement_purpose="transfer",
        repair_strategy="cross_form_transfer",
        comparison_scope_id="chain.omit-inner.transfer",
        baseline=True,
        blueprint_ids=(
            "chain.omit-inner.cross-form-transfer.v1",
            "chain.omit-inner.cross-form-transfer.v2-retest",
        ),
        review_provenance=_PROVENANCE,
    ),
)

COGNITIVE_STRATEGY_CATALOG_V1 = CognitiveStrategyCatalog(
    STRATEGY_CATALOG_VERSION_V1,
    COGNITIVE_STRATEGY_ENTRIES_V1,
    reviewed_catalog=COGNITIVE_CATALOG_V1,
)


__all__ = [
    "COGNITIVE_STRATEGY_CATALOG_V1",
    "COGNITIVE_STRATEGY_ENTRIES_V1",
    "SHADOW_COLLECTION_DEFAULT_ENABLED",
    "STRATEGY_CATALOG_VERSION_V1",
    "CognitiveStrategyCatalog",
    "CognitiveStrategyEntry",
    "MeasurementPurpose",
    "StrategyFamily",
]
