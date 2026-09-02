from __future__ import annotations

import importlib
import inspect
import sys
import tomllib
from dataclasses import asdict
from pathlib import Path
from types import ModuleType

import pytest

from adaptive_learning import (
    CognitiveIntentPolicy,
    CognitiveInterventionEvent,
    CognitivePolicyDecision,
    CognitiveQuestionValidationResult,
    CognitiveStateReader,
    DiagnosticQuestionValidator,
    HypothesisRef,
    LearnerCognitiveStateView,
    LearningActionCandidate,
    PracticeSelection,
    PreparedCognitiveIntervention,
    QuestionPlan,
    TopicRef,
)
from adaptive_learning.ports import CognitiveStatePort

ROOT = Path(__file__).resolve().parents[1]


def _load_models(monkeypatch: pytest.MonkeyPatch, name: str):
    package = ModuleType(name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, name, package)
    mode_manager = ModuleType(f"{name}.mode_manager")
    setattr(mode_manager, "normalize_mode", lambda value: str(value or "companion"))
    monkeypatch.setitem(sys.modules, mode_manager.__name__, mode_manager)
    return importlib.import_module(f"{name}.models")


def _legacy_plan_arguments() -> tuple[object, ...]:
    topic = TopicRef(id="calculus.chain_rule", name="Chain rule")
    selection = PracticeSelection(reason="wrong_retry", target_topic=topic)
    return (
        "plan-legacy",
        selection,
        3,
        "math_reasoning",
        "Differentiate a composite function",
        "Legacy model-facing description",
        (),
        "scope-a",
        4,
        "companion",
        "wrong-1",
        {"topic_id": topic.id},
        "practice-planner-v1",
        1,
    )


def test_question_plan_preserves_legacy_positional_construction() -> None:
    plan = QuestionPlan(*_legacy_plan_arguments())

    assert plan.learning_intent == "practice"
    assert plan.hypothesis_target is None
    assert plan.misconception_target == "Legacy model-facing description"
    assert plan.schema_version == 1


def test_v2_cognitive_types_have_stable_package_exports() -> None:
    exported = {
        CognitiveIntentPolicy,
        CognitiveInterventionEvent,
        CognitivePolicyDecision,
        CognitiveQuestionValidationResult,
        CognitiveStateReader,
        DiagnosticQuestionValidator,
        LearnerCognitiveStateView,
        PreparedCognitiveIntervention,
    }

    assert len(exported) == 8
    assert all(item.__module__.startswith("adaptive_learning.") for item in exported)


def test_question_plan_serializes_versioned_cognitive_target() -> None:
    hypothesis = HypothesisRef(
        hypothesis_id="hypothesis-1",
        topic_id="calculus.chain_rule",
        code="omit_inner_derivative",
        status="supported",
        probability=0.81,
        model_version="cognitive-v1",
    )
    plan = QuestionPlan(
        *_legacy_plan_arguments(),
        learning_intent="misconception_repair",
        hypothesis_target=hypothesis,
    )

    payload = asdict(plan)

    assert payload["learning_intent"] == "misconception_repair"
    assert payload["hypothesis_target"] == asdict(hypothesis)
    assert plan.target_topic.id == hypothesis.topic_id


def test_learning_action_candidate_is_shadow_proposal_only() -> None:
    candidate = LearningActionCandidate(
        source="cognitive_evidence",
        topic_id="calculus.chain_rule",
        intent="transfer_check",
        urgency=0.5,
        expected_learning_gain=0.7,
        information_gain=0.8,
        evidence_refs=("evidence-1",),
        satisfies=("cognitive_transfer",),
    )

    assert candidate.not_before == ""
    assert candidate.expires_at == ""
    assert "select" not in " ".join(CognitiveStatePort.__dict__).lower()
    assert inspect.signature(CognitiveStatePort.list_hypotheses).parameters.keys() == {
        "self",
        "topic_id",
    }


def test_cognitive_config_defaults_are_fully_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = _load_models(monkeypatch, "_cognitive_config_defaults")

    config = models.build_config({})

    assert config.cognitive.to_dict() == {
        "projection_enabled": False,
        "read_mode": "off",
        "intent_policy": "off",
        "ui_enabled": False,
        "model_version": "cognitive-v1",
        "supported_topics": ("calculus.chain_rule", "college_chain_rule"),
    }


def test_cognitive_config_is_strict_versioned_and_topic_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = _load_models(monkeypatch, "_cognitive_config_strict")

    enabled = models.build_config(
        {
            "cognitive": {
                "projection_enabled": True,
                "read_mode": "shadow",
                "intent_policy": "shadow",
                "ui_enabled": True,
                "model_version": "cognitive-v1",
                "supported_topics": [
                    "calculus.chain_rule",
                    "college_chain_rule",
                    "algebra.linear_equation",
                    "calculus.chain_rule",
                ],
            }
        }
    ).cognitive
    invalid = models.build_config(
        {
            "cognitive": {
                "projection_enabled": "true",
                "read_mode": "future",
                "intent_policy": "enabled",
                "ui_enabled": 1,
                "model_version": "unversioned",
                "supported_topics": "calculus.chain_rule",
            }
        }
    ).cognitive

    assert enabled.to_dict() == {
        "projection_enabled": True,
        "read_mode": "shadow",
        "intent_policy": "shadow",
        "ui_enabled": True,
        "model_version": "cognitive-v1",
        "supported_topics": ("calculus.chain_rule", "college_chain_rule"),
    }
    assert invalid.to_dict() == {
        "projection_enabled": False,
        "read_mode": "off",
        "intent_policy": "off",
        "ui_enabled": False,
        "model_version": "cognitive-v1",
        "supported_topics": (),
    }


def test_study_config_cognitive_round_trip_preserves_existing_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = _load_models(monkeypatch, "_cognitive_config_round_trip")
    baseline = models.StudyConfig()
    payload = baseline.to_dict()

    reloaded = models.build_config(payload)

    assert reloaded.cognitive.to_dict() == baseline.cognitive.to_dict()
    assert reloaded.mode == baseline.mode
    assert reloaded.mastery.to_dict() == baseline.mastery.to_dict()
    assert reloaded.adaptive_loop.to_dict() == baseline.adaptive_loop.to_dict()


def test_example_config_keeps_every_cognitive_surface_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = _load_models(monkeypatch, "_cognitive_example_config")
    raw = tomllib.loads((ROOT / "config.example.toml").read_text(encoding="utf-8"))

    cognitive = models.build_config(raw).cognitive

    assert cognitive.projection_enabled is False
    assert cognitive.read_mode == "off"
    assert cognitive.intent_policy == "off"
    assert cognitive.ui_enabled is False


def test_plugin_manifest_keeps_every_cognitive_surface_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = _load_models(monkeypatch, "_cognitive_plugin_config")
    raw = tomllib.loads((ROOT / "plugin.toml").read_text(encoding="utf-8"))

    cognitive = models.build_config(raw).cognitive

    assert cognitive.projection_enabled is False
    assert cognitive.read_mode == "off"
    assert cognitive.intent_policy == "off"
    assert cognitive.ui_enabled is False


def test_public_question_never_exposes_cognitive_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = _load_models(monkeypatch, "_cognitive_private_question")

    public = models.public_current_question_payload(
        {
            "question": "Differentiate sin(x^2).",
            "learning_intent": "misconception_probe",
            "hypothesis_target": {"probability": 0.91},
            "repair_strategy": "compare_steps",
            "cognitive_decision_id": "decision-1",
            "cognitive_validator_version": "validator-v2",
            "diagnostic_validation_id": "validation-1",
            "cognitive_blueprint_id": "blueprint-1",
            "cognitive_question_family_id": "family-1",
            "competing_hypothesis_codes": ["differentiate_inner_incorrectly"],
            "diagnostic_signature": "private-signature",
        }
    )

    assert public == {"question": "Differentiate sin(x^2)."}
