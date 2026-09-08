from dataclasses import FrozenInstanceError, replace

import pytest

from adaptive_learning.cognitive_catalog import COGNITIVE_CATALOG_V1
from adaptive_learning.cognitive_strategy_catalog import (
    COGNITIVE_STRATEGY_CATALOG_V1,
    COGNITIVE_STRATEGY_ENTRIES_V1,
    SHADOW_COLLECTION_DEFAULT_ENABLED,
    STRATEGY_CATALOG_VERSION_V1,
    CognitiveStrategyCatalog,
)


def _catalog(entries=COGNITIVE_STRATEGY_ENTRIES_V1) -> CognitiveStrategyCatalog:
    return CognitiveStrategyCatalog(
        STRATEGY_CATALOG_VERSION_V1,
        entries,
        reviewed_catalog=COGNITIVE_CATALOG_V1,
    )


def test_v1_catalog_is_default_off_and_closed_to_the_five_reviewed_families() -> None:
    assert SHADOW_COLLECTION_DEFAULT_ENABLED is False
    assert COGNITIVE_STRATEGY_CATALOG_V1.catalog_version == (
        "cognitive-strategy-catalog-v1"
    )
    assert {entry.strategy_family for entry in COGNITIVE_STRATEGY_ENTRIES_V1} == {
        "compare_correct_wrong_steps",
        "complete_steps",
        "minimal_change",
        "structure_discrimination",
        "alternate_representation",
    }
    assert {
        (entry.topic_id, entry.hypothesis_code)
        for entry in COGNITIVE_STRATEGY_ENTRIES_V1
        if entry.shadow_collection_eligible
    } == {("calculus.chain_rule", "omit_inner_derivative")}


def test_every_binding_matches_an_existing_reviewed_blueprint() -> None:
    for entry in COGNITIVE_STRATEGY_ENTRIES_V1:
        for blueprint_id in entry.blueprint_ids:
            blueprint = COGNITIVE_CATALOG_V1.get_blueprint(blueprint_id)
            assert blueprint is not None
            assert blueprint.topic_id == entry.topic_id
            assert blueprint.hypothesis_code == entry.hypothesis_code
            assert blueprint.learning_intent == entry.learning_intent
            assert blueprint.repair_strategy == entry.repair_strategy


def test_resolve_reviewed_requires_the_exact_server_owned_delivery_tuple() -> None:
    resolved = COGNITIVE_STRATEGY_CATALOG_V1.resolve_reviewed(
        topic_id="college_chain_rule",
        hypothesis_code="omit_inner_derivative",
        learning_intent="misconception_repair",
        repair_strategy="complete_inner_derivative",
        blueprint_id="chain.omit-inner.fill-factor.v1",
    )

    assert resolved is not None
    assert resolved.strategy_id == "chain.omit-inner.complete-steps"
    assert resolved.strategy_version == "v1"
    assert resolved.catalog_version == STRATEGY_CATALOG_VERSION_V1
    assert resolved.measurement_purpose == "repair"
    assert resolved.eligible_for_repair_attribution is True

    assert (
        COGNITIVE_STRATEGY_CATALOG_V1.resolve_reviewed(
            topic_id="calculus.chain_rule",
            hypothesis_code="omit_inner_derivative",
            learning_intent="misconception_probe",
            repair_strategy="complete_inner_derivative",
            blueprint_id="chain.omit-inner.fill-factor.v1",
        )
        is None
    )
    assert (
        COGNITIVE_STRATEGY_CATALOG_V1.resolve_reviewed(
            topic_id="calculus.chain_rule",
            hypothesis_code="omit_inner_derivative",
            learning_intent="misconception_repair",
            repair_strategy="complete_inner_derivative",
            blueprint_id="unreviewed.blueprint.v1",
        )
        is None
    )


def test_measurement_purpose_keeps_probe_and_transfer_out_of_repair_effects() -> None:
    entries = COGNITIVE_STRATEGY_CATALOG_V1.entries(
        topic_id="calculus.chain_rule",
        hypothesis_code="omit_inner_derivative",
        shadow_collection_eligible=True,
    )
    repair_entries = tuple(
        entry for entry in entries if entry.eligible_for_repair_attribution
    )

    assert {entry.strategy_family for entry in repair_entries} == {
        "complete_steps",
        "minimal_change",
    }
    assert {
        entry.measurement_purpose
        for entry in entries
        if not entry.eligible_for_repair_attribution
    } == {"probe", "transfer"}


def test_entries_and_baselines_have_stable_order_and_one_baseline_per_scope() -> None:
    forward = _catalog(COGNITIVE_STRATEGY_ENTRIES_V1).entries()
    reverse = _catalog(tuple(reversed(COGNITIVE_STRATEGY_ENTRIES_V1))).entries()

    assert forward == reverse
    scope_ids = {entry.comparison_scope_id for entry in forward}
    for scope_id in scope_ids:
        baseline = COGNITIVE_STRATEGY_CATALOG_V1.baseline_for(scope_id)
        assert baseline is not None
        assert baseline.baseline is True
        assert sum(entry.baseline for entry in forward if entry.comparison_scope_id == scope_id) == 1


def test_catalog_rejects_missing_or_duplicate_baselines() -> None:
    no_repair_baseline = tuple(
        replace(entry, baseline=False)
        if entry.strategy_id == "chain.omit-inner.complete-steps"
        else entry
        for entry in COGNITIVE_STRATEGY_ENTRIES_V1
    )
    two_repair_baselines = tuple(
        replace(entry, baseline=True)
        if entry.strategy_id == "chain.omit-inner.minimal-change"
        else entry
        for entry in COGNITIVE_STRATEGY_ENTRIES_V1
    )

    with pytest.raises(ValueError, match="exactly one baseline"):
        _catalog(no_repair_baseline)
    with pytest.raises(ValueError, match="exactly one baseline"):
        _catalog(two_repair_baselines)


def test_catalog_rejects_purpose_or_reviewed_blueprint_mismatches() -> None:
    wrong_purpose = tuple(
        replace(entry, measurement_purpose="probe")
        if entry.strategy_id == "chain.omit-inner.complete-steps"
        else entry
        for entry in COGNITIVE_STRATEGY_ENTRIES_V1
    )
    wrong_binding = tuple(
        replace(entry, repair_strategy="minimal_change")
        if entry.strategy_id == "chain.omit-inner.complete-steps"
        else entry
        for entry in COGNITIVE_STRATEGY_ENTRIES_V1
    )

    with pytest.raises(ValueError, match="purpose does not match"):
        _catalog(wrong_purpose)
    with pytest.raises(ValueError, match="does not match reviewed blueprint"):
        _catalog(wrong_binding)


def test_catalog_and_entries_do_not_expose_mutation() -> None:
    entry = COGNITIVE_STRATEGY_CATALOG_V1.entries()[0]

    with pytest.raises(FrozenInstanceError):
        entry.strategy_id = "changed"  # type: ignore[misc]
    with pytest.raises(AttributeError, match="immutable"):
        COGNITIVE_STRATEGY_CATALOG_V1._entries = ()  # type: ignore[attr-defined]
    assert isinstance(COGNITIVE_STRATEGY_CATALOG_V1.entries(), tuple)
