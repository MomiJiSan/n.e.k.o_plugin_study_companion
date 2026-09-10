from __future__ import annotations

import asyncio
import importlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_cognitive_active_question_entry import _active_subject, _targeted_context
from test_cognitive_shadow_runtime import (
    _load_runtime,
    _Logger,
    _NeverCalledExtractor,
    _store,
)

# isort: split
from adaptive_learning.cognitive_catalog import (
    COGNITIVE_CATALOG_V2,
    KNOWLEDGE_GRAPH_HYPOTHESES,
    build_knowledge_graph_cognitive_catalog,
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
    COMPREHENSIVE_TEACHING_COVERAGE_VERSION_SET,
    get_cognitive_version_set,
)
from adaptive_learning.contracts import (
    HypothesisRef,
    PracticeSelection,
    QuestionInstance,
    QuestionPlan,
    TopicRef,
)
from tools.cognitive_teaching_coverage_acceptance import build_report, load_topics

ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = ROOT / "static"
GENERIC_CODES = frozenset(item.code for item in KNOWLEDGE_GRAPH_HYPOTHESES)


def _catalog():
    topics, _ = load_topics(STATIC_ROOT)
    return topics, build_knowledge_graph_cognitive_catalog(
        topics.get,
        base_catalog=COGNITIVE_CATALOG_V2,
        comprehensive_teaching=True,
    )


def _plan(topic_id: str, topic_name: str) -> QuestionPlan:
    topic = TopicRef(id=topic_id, name=topic_name)
    return QuestionPlan(
        plan_id=f"coverage:{topic_id}",
        selection=PracticeSelection(
            reason="weak_topic",
            target_topic=topic,
            eligible_topic_ids=(topic_id,),
        ),
        difficulty=3,
        question_type="short_answer",
        scope_key="coverage:all",
        scope_revision=1,
    )


def _state(topic_id: str, code: str, stage: str) -> LearnerCognitiveStateView:
    last_intent = ""
    last_outcome = ""
    if stage == "probing":
        last_intent = "misconception_probe"
        last_outcome = "confirmed"
    elif stage == "provisionally_resolved":
        last_intent = "misconception_repair"
        last_outcome = "correct"
    hypothesis = LearnerCognitiveHypothesis(
        ref=HypothesisRef(
            hypothesis_id=f"{topic_id}:{code}",
            topic_id=topic_id,
            code=code,
            status="supported",
            probability=0.91,
            model_version=COMPREHENSIVE_TEACHING_COVERAGE_VERSION_SET,
            source_snapshot_id=f"snapshot:{topic_id}:{code}",
            source_attempt_id="source-attempt",
            projection_generation=2,
        ),
        evidence_status="supported",
        intervention_stage=stage,  # type: ignore[arg-type]
        last_intent=last_intent,
        last_outcome=last_outcome,
        support_count=2,
    )
    return LearnerCognitiveStateView(
        topic_id=topic_id,
        model_version=COMPREHENSIVE_TEACHING_COVERAGE_VERSION_SET,
        requested_generation=2,
        projected_generation=2,
        hypotheses=(hypothesis,),
        usable=True,
        reason="ready",
    )


def test_comprehensive_report_covers_every_bundled_topic_and_subject() -> None:
    report = build_report(STATIC_ROOT)

    assert report["status"] == "PASS"
    assert report["subject_count"] == 11
    assert report["topic_count"] == 892
    assert report["covered_topic_count"] == 892
    assert report["fully_covered_topic_count"] == 785
    assert report["active_topic_hypothesis_count"] == 2569
    assert report["generic_topic_hypothesis_count"] == 2566
    assert report["reviewed_blueprint_count"] == 12
    assert report["deterministic_graph_blueprint_count"] == 7698
    assert report["blueprint_count"] == 7710
    assert report["default_release_unchanged"] is True
    assert report["retention_expanded"] is False
    assert report["personalization_expanded"] is False


def test_old_graph_catalog_remains_shadow_only() -> None:
    topics, _ = load_topics(STATIC_ROOT)
    old_catalog = build_knowledge_graph_cognitive_catalog(
        topics.get,
        base_catalog=COGNITIVE_CATALOG_V2,
    )

    assert old_catalog.allowed_codes("physics_junior_speed")
    assert old_catalog.active_codes("physics_junior_speed") == ()
    assert old_catalog.blueprints("physics_junior_speed") == ()


def test_old_policy_does_not_propose_generic_graph_interventions() -> None:
    decision = CognitiveIntentPolicy(mode="shadow").decorate(
        _plan("physics_junior_speed", "Speed"),
        _state("physics_junior_speed", "concept_misunderstanding", "idle"),
    )

    assert decision.proposed_plan is None
    assert decision.fallback_reason == "no_eligible_hypothesis"


@pytest.mark.parametrize(
    ("topic_id", "code", "stage", "intent"),
    [
        (
            "physics_junior_speed",
            "concept_misunderstanding",
            "idle",
            "misconception_probe",
        ),
        (
            "history_junior_qin_unification",
            "procedure_or_representation_error",
            "probing",
            "misconception_repair",
        ),
        (
            "college_c_basic_program_structure",
            "prerequisite_gap",
            "provisionally_resolved",
            "transfer_check",
        ),
    ],
)
def test_generic_coverage_delivers_and_validates_each_intent_path(
    topic_id: str,
    code: str,
    stage: str,
    intent: str,
) -> None:
    topics, catalog = _catalog()
    plan = _plan(topic_id, str(topics[topic_id]["name"]))
    decision = CognitiveIntentPolicy(
        mode="on",
        active_hypothesis_codes=GENERIC_CODES,
        known_hypothesis_codes=GENERIC_CODES,
    ).decorate(plan, _state(topic_id, code, stage))

    assert decision.applied is True
    assert decision.proposed_intent == intent
    prepared = prepare_cognitive_intervention(
        decision,
        catalog=catalog,
        decision_id="comprehensive-decision",
        created_at="2026-09-10T09:00:00Z",
    )
    assert prepared is not None and prepared.blueprint is not None
    assert prepared.blueprint.learning_intent == intent
    payload = reviewed_question_payload(prepared)
    proposed = prepared.proposed_plan
    question = QuestionInstance(
        question_id="comprehensive-question",
        plan_id=proposed.plan_id,
        target_topic=proposed.target_topic,
        question_type=proposed.question_type,
        difficulty=proposed.difficulty,
        public_payload=payload,
        scope_key=proposed.scope_key,
        scope_revision=proposed.scope_revision,
        status="generated",
        learning_intent=proposed.learning_intent,
        hypothesis_target=proposed.hypothesis_target,
        repair_strategy=proposed.repair_strategy,
        cognitive_decision_id=prepared.decision_id,
    )
    versions = get_cognitive_version_set(
        COMPREHENSIVE_TEACHING_COVERAGE_VERSION_SET
    )
    assert versions is not None
    validation = validate_reviewed_question(
        prepared,
        question,
        repair_question_family_id=("prior-repair" if intent == "transfer_check" else ""),
        validator=DiagnosticQuestionValidator(
            catalog=catalog,
            validator_version=versions.validator_version,
        ),
    )

    assert validation.valid is True
    assert validation.errors == ()


def test_incomplete_or_unknown_graph_topic_fails_closed() -> None:
    incomplete = {
        "id": "incomplete_topic",
        "name": "Incomplete",
        "typical_misconceptions": ["missing metadata"],
    }
    catalog = build_knowledge_graph_cognitive_catalog(
        lambda topic_id: incomplete if topic_id == incomplete["id"] else None,
        base_catalog=COGNITIVE_CATALOG_V2,
        comprehensive_teaching=True,
    )

    assert set(catalog.allowed_codes("incomplete_topic")) == GENERIC_CODES
    assert catalog.active_codes("incomplete_topic") == ()
    assert catalog.blueprints("incomplete_topic") == ()
    assert catalog.active_codes("unknown_topic") == ()


def test_runtime_selects_comprehensive_catalog_only_for_coverage_v2(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    Store, Tracker, _, _ = _load_runtime(
        monkeypatch,
        "_cognitive_comprehensive_coverage_runtime",
    )
    store = _store(tmp_path, Store)
    try:
        topics, _ = load_topics(STATIC_ROOT)
        store.upsert_topic(topics["physics_junior_speed"])
        config = SimpleNamespace(
            projection_enabled=True,
            read_mode="active",
            intent_policy="on",
            ui_enabled=True,
            retention_enabled=False,
            strategy_shadow_enabled=False,
            strategy_rotation_enabled=False,
            knowledge_graph_enabled=True,
            version_set=COMPREHENSIVE_TEACHING_COVERAGE_VERSION_SET,
            model_version=COMPREHENSIVE_TEACHING_COVERAGE_VERSION_SET,
            supported_topics=(),
        )
        tracker = Tracker(
            store,
            logger=_Logger(),
            cognitive_config=config,
            cognitive_extractor=_NeverCalledExtractor(),
        )

        assert (
            set(tracker.cognitive_catalog.active_codes("physics_junior_speed"))
            == GENERIC_CODES
        )
        assert tracker.cognitive_intent_policy_mode == "on"
    finally:
        store.close()


def test_question_entry_uses_runtime_catalog_for_generic_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = "_cognitive_comprehensive_entry_delivery"
    subject, _ = _active_subject(
        monkeypatch,
        package,
    )
    contracts = importlib.import_module(f"{package}.adaptive_learning.contracts")
    policy = importlib.import_module(f"{package}.adaptive_learning.cognitive_policy")
    topics, catalog = _catalog()
    topic_id = "physics_junior_speed"
    code = "concept_misunderstanding"

    class Tracker:
        cognitive_catalog = catalog

        def propose_cognitive_intent(self, original_plan):
            hypothesis = contracts.HypothesisRef(
                hypothesis_id=f"{topic_id}:{code}",
                topic_id=topic_id,
                code=code,
                status="supported",
                probability=0.91,
                model_version=COMPREHENSIVE_TEACHING_COVERAGE_VERSION_SET,
                source_snapshot_id="snapshot-generic",
                source_attempt_id="attempt-generic",
                projection_generation=2,
            )
            proposed = replace(
                original_plan,
                learning_intent="misconception_repair",
                hypothesis_target=hypothesis,
                repair_strategy="compare_steps",
            )
            return policy.CognitivePolicyDecision(
                mode="on",
                original_plan=original_plan,
                proposed_plan=proposed,
                effective_plan=proposed,
                proposed_intent="misconception_repair",
                selected_hypothesis=hypothesis,
                repair_strategy="compare_steps",
                applied=True,
            )

        def record_prompt_usage_for_question_params(self, _params):
            return None

    subject._knowledge_tracker = Tracker()
    context = _targeted_context()
    context.update(
        {
            "selected_topic_id": topic_id,
            "selected_topic_name": str(topics[topic_id]["name"]),
            "eligible_topic_ids": [topic_id],
        }
    )
    context["question_params"] = {
        **context["question_params"],
        "target_topic_id": topic_id,
        "target_topic": {"id": topic_id, "name": str(topics[topic_id]["name"])},
        "retry_wrong_question": {},
    }

    payload = asyncio.run(
        subject._generate_question_payload(
            source_text="Generate comprehensive cognitive question",
            source="targeted_question",
            targeted_context=context,
        )
    )

    assert subject._agent.generated == 0
    expected = catalog.blueprints(
        topic_id,
        hypothesis_code=code,
        learning_intent="misconception_repair",
    )[0]
    assert payload["question"] == expected.question_text
    assert [event["event_type"] for event in subject._store.events] == [
        "intent_proposed",
        "question_committed",
    ]
    assert subject.private_payload["target_binding"]["cognitive_hypothesis_target"][
        "code"
    ] == code
