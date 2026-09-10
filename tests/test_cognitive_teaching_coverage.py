from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from test_cognitive_shadow_runtime import (
    _load_runtime,
    _Logger,
    _NeverCalledExtractor,
    _store,
)

# isort: split
from adaptive_learning.cognitive_catalog import (
    COGNITIVE_CATALOG_V1,
    COGNITIVE_CATALOG_V2,
)
from adaptive_learning.cognitive_delivery import (
    prepare_cognitive_intervention,
    reviewed_question_payload,
    validate_reviewed_question,
)
from adaptive_learning.cognitive_policy import CognitiveIntentPolicy
from adaptive_learning.cognitive_question_validation import DiagnosticQuestionValidator
from adaptive_learning.cognitive_state import (
    LearnerCognitiveHypothesis,
    LearnerCognitiveStateView,
)
from adaptive_learning.cognitive_versions import (
    TEACHING_COVERAGE_VERSION_SET,
    get_cognitive_version_set,
)
from adaptive_learning.contracts import (
    HypothesisRef,
    PracticeSelection,
    QuestionInstance,
    QuestionPlan,
    TopicRef,
)

ACTIVE_CODES = (
    "omit_inner_derivative",
    "differentiate_inner_incorrectly",
    "confuse_product_and_chain",
)


def _plan() -> QuestionPlan:
    topic = TopicRef(id="college_chain_rule", name="Chain rule", subject="math")
    return QuestionPlan(
        plan_id="coverage-plan",
        selection=PracticeSelection(
            reason="wrong_retry",
            target_topic=topic,
            eligible_topic_ids=(topic.id,),
            origin_wrong_question_id="wrong-coverage",
        ),
        difficulty=3,
        question_type="math_reasoning",
        scope_key="course:calculus",
        scope_revision=1,
        source_question_id="wrong-coverage",
    )


def _state(
    code: str,
    *,
    stage: str,
    last_intent: str = "",
    last_outcome: str = "",
) -> LearnerCognitiveStateView:
    hypothesis = LearnerCognitiveHypothesis(
        ref=HypothesisRef(
            hypothesis_id=f"calculus.chain_rule:{code}",
            topic_id="college_chain_rule",
            code=code,
            status="supported",
            probability=0.91,
            model_version=TEACHING_COVERAGE_VERSION_SET,
            source_snapshot_id=f"snapshot:{code}:4",
            source_attempt_id="attempt-source",
            projection_generation=4,
        ),
        evidence_status="supported",
        intervention_stage=stage,  # type: ignore[arg-type]
        last_intent=last_intent,
        last_outcome=last_outcome,
        support_count=2,
    )
    return LearnerCognitiveStateView(
        topic_id="college_chain_rule",
        model_version=TEACHING_COVERAGE_VERSION_SET,
        requested_generation=4,
        projected_generation=4,
        hypotheses=(hypothesis,),
        usable=True,
        reason="ready",
    )


def _question(decision) -> QuestionInstance:
    prepared = prepare_cognitive_intervention(
        decision,
        decision_id="coverage-decision",
        created_at="2026-09-10T08:00:00Z",
    )
    assert prepared is not None and prepared.blueprint is not None
    payload = reviewed_question_payload(prepared)
    plan = prepared.proposed_plan
    return QuestionInstance(
        question_id="coverage-question",
        plan_id=plan.plan_id,
        target_topic=plan.target_topic,
        question_type=plan.question_type,
        difficulty=plan.difficulty,
        public_payload=payload,
        mode=plan.mode,
        source_question_id=plan.source_question_id,
        target_binding=plan.target_binding,
        scope_key=plan.scope_key,
        scope_revision=plan.scope_revision,
        status="generated",
        learning_intent=plan.learning_intent,
        hypothesis_target=plan.hypothesis_target,
        repair_strategy=plan.repair_strategy,
        cognitive_decision_id=prepared.decision_id,
    )


def test_teaching_coverage_is_a_new_frozen_version() -> None:
    versions = get_cognitive_version_set(TEACHING_COVERAGE_VERSION_SET)

    assert versions is not None
    assert versions.catalog_version == "cognitive-catalog-v2"
    assert versions.projection_version == TEACHING_COVERAGE_VERSION_SET
    assert COGNITIVE_CATALOG_V1.active_codes("calculus.chain_rule") == (
        "omit_inner_derivative",
    )
    assert COGNITIVE_CATALOG_V2.active_codes("calculus.chain_rule") == ACTIVE_CODES


@pytest.mark.parametrize(
    ("code", "stage", "last_intent", "last_outcome", "intent", "strategy"),
    [
        (
            "differentiate_inner_incorrectly",
            "idle",
            "",
            "",
            "misconception_probe",
            "compare_steps",
        ),
        (
            "differentiate_inner_incorrectly",
            "probing",
            "misconception_probe",
            "confirmed",
            "misconception_repair",
            "complete_inner_derivative",
        ),
        (
            "differentiate_inner_incorrectly",
            "provisionally_resolved",
            "misconception_repair",
            "correct",
            "transfer_check",
            "cross_form_transfer",
        ),
        (
            "confuse_product_and_chain",
            "idle",
            "",
            "",
            "misconception_probe",
            "structure_classification",
        ),
        (
            "confuse_product_and_chain",
            "probing",
            "misconception_probe",
            "confirmed",
            "misconception_repair",
            "compare_steps",
        ),
        (
            "confuse_product_and_chain",
            "provisionally_resolved",
            "misconception_repair",
            "correct",
            "transfer_check",
            "cross_form_transfer",
        ),
    ],
)
def test_new_active_mechanisms_have_reviewed_probe_repair_transfer_delivery(
    code: str,
    stage: str,
    last_intent: str,
    last_outcome: str,
    intent: str,
    strategy: str,
) -> None:
    policy = CognitiveIntentPolicy(
        mode="on",
        active_hypothesis_codes=frozenset(ACTIVE_CODES),
    )
    decision = policy.decorate(
        _plan(),
        _state(
            code,
            stage=stage,
            last_intent=last_intent,
            last_outcome=last_outcome,
        ),
    )

    assert decision.applied is True
    assert decision.proposed_intent == intent
    assert decision.repair_strategy == strategy
    prepared = prepare_cognitive_intervention(
        decision,
        decision_id="coverage-decision",
        created_at="2026-09-10T08:00:00Z",
    )
    assert prepared is not None and prepared.blueprint is not None
    assert prepared.blueprint.hypothesis_code == code
    assert prepared.blueprint.learning_intent == intent
    assert prepared.blueprint.repair_strategy == strategy

    versions = get_cognitive_version_set(TEACHING_COVERAGE_VERSION_SET)
    assert versions is not None
    validation = validate_reviewed_question(
        prepared,
        _question(decision),
        repair_question_family_id=(
            "coverage.prior-repair" if intent == "transfer_check" else ""
        ),
        validator=DiagnosticQuestionValidator(
            validator_version=versions.validator_version
        ),
    )
    assert validation.valid is True
    assert validation.errors == ()


def test_runtime_selects_coverage_catalog_only_for_the_new_version(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    Store, Tracker, _, _ = _load_runtime(
        monkeypatch,
        "_cognitive_teaching_coverage_runtime",
    )
    store = _store(tmp_path, Store)
    try:
        config = SimpleNamespace(
            projection_enabled=True,
            read_mode="active",
            intent_policy="on",
            ui_enabled=True,
            retention_enabled=False,
            strategy_shadow_enabled=False,
            strategy_rotation_enabled=False,
            knowledge_graph_enabled=True,
            version_set=TEACHING_COVERAGE_VERSION_SET,
            model_version=TEACHING_COVERAGE_VERSION_SET,
            supported_topics=("calculus.chain_rule",),
        )
        tracker = Tracker(
            store,
            logger=_Logger(),
            cognitive_config=config,
            cognitive_extractor=_NeverCalledExtractor(),
        )

        assert tracker.cognitive_catalog.active_codes("calculus.chain_rule") == ACTIVE_CODES
        assert tracker.cognitive_intent_policy_mode == "on"
    finally:
        store.close()


def test_old_policy_default_still_rejects_new_active_mechanisms() -> None:
    decision = CognitiveIntentPolicy(mode="on").decorate(
        _plan(),
        _state("differentiate_inner_incorrectly", stage="idle"),
    )

    assert decision.applied is False
    assert decision.fallback_reason == "no_eligible_hypothesis"
