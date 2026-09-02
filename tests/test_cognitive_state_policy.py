from __future__ import annotations

from dataclasses import fields
from datetime import datetime, timezone
from typing import Any

from adaptive_learning.cognitive_policy import (
    CognitiveIntentPolicy,
    question_plan_ownership_fingerprint,
)
from adaptive_learning.cognitive_state import (
    CognitiveStateReader,
    LearnerCognitiveHypothesis,
    LearnerCognitiveStateView,
)
from adaptive_learning.contracts import HypothesisRef, PracticeSelection, QuestionPlan, TopicRef


class FakeStateStore:
    def __init__(
        self,
        *,
        projection: dict[str, Any] | None = None,
        current: list[dict[str, Any]] | None = None,
        controls: list[dict[str, Any]] | None = None,
        projection_reads: list[dict[str, Any] | None] | None = None,
    ) -> None:
        self.projection = projection
        self.current = current or []
        self.controls = controls or []
        self.projection_reads = list(projection_reads or [])
        self.current_calls = 0

    def get_cognitive_topic_projection_state(
        self, *, topic_id: str, model_version: str
    ) -> dict[str, Any] | None:
        if self.projection_reads:
            return self.projection_reads.pop(0)
        return self.projection

    def list_cognitive_hypothesis_current(self, **kwargs: object) -> list[dict[str, Any]]:
        self.current_calls += 1
        return list(self.current)

    def list_cognitive_user_controls(self, **kwargs: object) -> list[dict[str, Any]]:
        assert kwargs["active_only"] is True
        assert kwargs["as_of"] == "2026-03-01T12:00:00Z"
        return list(self.controls)


def _projection(*, requested: int = 3, projected: int = 3, status: str = "done") -> dict[str, Any]:
    return {
        "topic_id": "college_chain_rule",
        "model_version": "cognitive-v2",
        "status": status,
        "requested_generation": requested,
        "projected_generation": projected,
    }


def _current(
    *,
    code: str = "omit_inner_derivative",
    evidence_status: str = "supported",
    intervention_stage: str = "idle",
    generation: int = 3,
    probability: float = 0.83,
    user_override: str = "",
    last_intent: str = "",
    last_outcome: str = "",
) -> dict[str, Any]:
    return {
        "hypothesis_id": f"college_chain_rule:{code}",
        "source_snapshot_id": f"snapshot:{code}:3",
        "source_attempt_id": "attempt-3",
        "topic_id": "college_chain_rule",
        "hypothesis_code": code,
        "evidence_status": evidence_status,
        "intervention_stage": intervention_stage,
        "user_override": user_override,
        "probability": probability,
        "support_count": 2,
        "counter_count": 0,
        "diagnostic_support_count": 0,
        "relapse_count": 0,
        "model_version": "cognitive-v2",
        "projected_generation": generation,
        "last_intent": last_intent,
        "last_outcome": last_outcome,
        "computed_at": "2026-03-01T11:59:00Z",
    }


def _reader(store: FakeStateStore) -> CognitiveStateReader:
    return CognitiveStateReader(
        store,
        model_version="cognitive-v2",
        clock=lambda: datetime(2026, 3, 1, 12, tzinfo=timezone.utc),
    )


def _hypothesis(
    *,
    code: str = "omit_inner_derivative",
    evidence_status: str = "supported",
    intervention_stage: str = "idle",
    probability: float = 0.83,
    last_intent: str = "",
    last_outcome: str = "",
) -> LearnerCognitiveHypothesis:
    return LearnerCognitiveHypothesis(
        ref=HypothesisRef(
            hypothesis_id=f"college_chain_rule:{code}",
            topic_id="college_chain_rule",
            code=code,
            status=evidence_status,
            probability=probability,
            model_version="cognitive-v2",
            source_snapshot_id=f"snapshot:{code}:3",
            source_attempt_id="attempt-3",
            projection_generation=3,
        ),
        evidence_status=evidence_status,  # type: ignore[arg-type]
        intervention_stage=intervention_stage,  # type: ignore[arg-type]
        last_intent=last_intent,
        last_outcome=last_outcome,
        support_count=2,
    )


def _view(*hypotheses: LearnerCognitiveHypothesis, usable: bool = True) -> LearnerCognitiveStateView:
    return LearnerCognitiveStateView(
        topic_id="college_chain_rule",
        model_version="cognitive-v2",
        requested_generation=3,
        projected_generation=3,
        hypotheses=tuple(hypotheses),
        usable=usable,
        reason="ready" if usable else "stale_projection",
    )


def _plan() -> QuestionPlan:
    topic = TopicRef(id="college_chain_rule", name="Chain rule", subject="math")
    return QuestionPlan(
        plan_id="plan-1",
        selection=PracticeSelection(
            reason="wrong_retry",
            target_topic=topic,
            eligible_topic_ids=("college_chain_rule",),
            origin_wrong_question_id="wrong-7",
            policy_version="coach-policy-4",
        ),
        difficulty=4,
        question_type="math_reasoning",
        learning_objective="Differentiate a composition",
        misconception_target="legacy description",
        scope_key="course:calculus",
        scope_revision=8,
        mode="companion",
        source_question_id="wrong-7",
        target_binding={"learning_plan_id": "lp-1", "learning_plan_revision": 12},
        policy_version="coach-policy-4",
    )


def test_reader_returns_generation_consistent_current_state() -> None:
    store = FakeStateStore(projection=_projection(), current=[_current()])

    view = _reader(store).read_topic("college_chain_rule")

    assert view.usable is True
    assert view.reason == "ready"
    assert view.requested_generation == view.projected_generation == 3
    assert len(view.hypotheses) == 1
    assert view.hypotheses[0].ref.source_snapshot_id == "snapshot:omit_inner_derivative:3"
    assert view.hypotheses[0].ref.projection_generation == 3


def test_reader_fails_closed_before_reading_current_when_projection_is_stale() -> None:
    store = FakeStateStore(projection=_projection(requested=4, projected=3), current=[_current()])

    view = _reader(store).read_topic("college_chain_rule")

    assert view.usable is False
    assert view.reason == "stale_projection"
    assert view.hypotheses == ()
    assert store.current_calls == 0


def test_reader_rejects_generation_change_during_read() -> None:
    store = FakeStateStore(
        current=[_current()],
        projection_reads=[_projection(), _projection(requested=4, projected=3, status="pending")],
    )

    view = _reader(store).read_topic("college_chain_rule")

    assert view.usable is False
    assert view.reason == "projection_not_ready"
    assert view.hypotheses == ()


def test_reader_filters_controls_overrides_and_wrong_versions() -> None:
    wrong_version = _current(code="differentiate_inner_incorrectly")
    wrong_version["model_version"] = "cognitive-v1"
    store = FakeStateStore(
        projection=_projection(),
        current=[
            _current(),
            _current(code="confuse_product_and_chain", user_override="deleted"),
            wrong_version,
        ],
        controls=[{"hypothesis_code": "omit_inner_derivative", "action": "dismiss"}],
    )

    view = _reader(store).read_topic("college_chain_rule")

    assert view.usable is True
    assert view.hypotheses == ()


def test_reader_returns_empty_state_when_store_fails() -> None:
    class BrokenStore(FakeStateStore):
        def list_cognitive_hypothesis_current(self, **kwargs: object) -> list[dict[str, Any]]:
            raise RuntimeError("database unavailable")

    view = _reader(BrokenStore(projection=_projection())).read_topic("college_chain_rule")

    assert view.usable is False
    assert view.reason == "read_failed"
    assert view.hypotheses == ()


def test_shadow_hypothesis_proposes_probe_but_keeps_effective_plan_exactly_original() -> None:
    plan = _plan()
    decision = CognitiveIntentPolicy(mode="shadow").decorate(
        plan, _view(_hypothesis(evidence_status="hypothesized"))
    )

    assert decision.applied is False
    assert decision.effective_plan is plan
    assert decision.proposed_plan is not None
    assert decision.proposed_plan.learning_intent == "misconception_probe"
    assert decision.proposed_plan.hypothesis_target is not None
    assert decision.original_fingerprint == decision.proposal_fingerprint


def test_active_supported_omit_inner_derivative_applies_topic_preserving_probe() -> None:
    plan = _plan()
    decision = CognitiveIntentPolicy(mode="on").decorate(plan, _view(_hypothesis()))

    assert decision.applied is True
    assert decision.effective_plan.learning_intent == "misconception_probe"
    assert decision.effective_plan.repair_strategy == "structure_classification"
    assert decision.effective_plan.hypothesis_target is not None
    assert decision.effective_plan.hypothesis_target.code == "omit_inner_derivative"
    assert decision.original_fingerprint == question_plan_ownership_fingerprint(
        decision.effective_plan
    )
    for item in fields(plan):
        if item.name not in {"learning_intent", "hypothesis_target", "repair_strategy"}:
            assert getattr(decision.effective_plan, item.name) == getattr(plan, item.name)


def test_active_rejects_hypothesized_and_non_enabled_hypotheses() -> None:
    plan = _plan()
    hypothesized = CognitiveIntentPolicy(mode="on").decorate(
        plan, _view(_hypothesis(evidence_status="hypothesized"))
    )
    other_supported = CognitiveIntentPolicy(mode="on").decorate(
        plan, _view(_hypothesis(code="confuse_product_and_chain"))
    )

    assert hypothesized.effective_plan is plan
    assert hypothesized.applied is False
    assert other_supported.effective_plan is plan
    assert other_supported.applied is False


def test_policy_selects_only_one_and_prioritizes_an_in_progress_hypothesis() -> None:
    plan = _plan()
    in_progress = _hypothesis(
        code="omit_inner_derivative",
        intervention_stage="probing",
        probability=0.76,
        last_intent="misconception_probe",
        last_outcome="confirmed",
    )
    higher_probability = _hypothesis(
        code="differentiate_inner_incorrectly", probability=0.96
    )

    decision = CognitiveIntentPolicy(mode="shadow").decorate(
        plan, _view(higher_probability, in_progress)
    )

    assert decision.proposed_plan is not None
    assert decision.selected_hypothesis is not None
    assert decision.selected_hypothesis.code == "omit_inner_derivative"
    assert decision.proposed_intent == "misconception_repair"
    assert decision.repair_strategy == "complete_inner_derivative"


def test_policy_moves_provisionally_resolved_to_transfer() -> None:
    decision = CognitiveIntentPolicy(mode="on").decorate(
        _plan(), _view(_hypothesis(intervention_stage="provisionally_resolved"))
    )

    assert decision.applied is True
    assert decision.proposed_intent == "transfer_check"
    assert decision.repair_strategy == "cross_form_transfer"


def test_policy_allows_one_repair_retry_before_session_stop() -> None:
    item = _hypothesis(intervention_stage="remediating")
    item = LearnerCognitiveHypothesis(
        ref=item.ref,
        evidence_status=item.evidence_status,
        intervention_stage=item.intervention_stage,
        last_intent="misconception_repair",
        last_outcome="wrong",
        support_count=2,
        consecutive_repair_failures=1,
    )

    decision = CognitiveIntentPolicy(mode="on").decorate(_plan(), _view(item))

    assert decision.applied is True
    assert decision.proposed_intent == "misconception_repair"
    assert decision.repair_strategy == "complete_inner_derivative"


def test_policy_stops_after_two_consecutive_repair_failures() -> None:
    item = _hypothesis(intervention_stage="remediating")
    item = LearnerCognitiveHypothesis(
        ref=item.ref,
        evidence_status=item.evidence_status,
        intervention_stage=item.intervention_stage,
        last_intent="misconception_repair",
        last_outcome="wrong",
        support_count=2,
        consecutive_repair_failures=2,
    )

    decision = CognitiveIntentPolicy(mode="on").decorate(_plan(), _view(item))

    assert decision.applied is False
    assert decision.effective_plan is decision.original_plan
    assert decision.fallback_reason == "no_cognitive_action"


def test_policy_fails_closed_for_unusable_or_wrong_topic_state() -> None:
    plan = _plan()
    stale = CognitiveIntentPolicy(mode="on").decorate(plan, _view(usable=False))
    wrong_topic_view = LearnerCognitiveStateView(
        topic_id="other_topic",
        model_version="cognitive-v2",
        requested_generation=3,
        projected_generation=3,
        hypotheses=(_hypothesis(),),
        usable=True,
        reason="ready",
    )
    wrong_topic = CognitiveIntentPolicy(mode="on").decorate(plan, wrong_topic_view)

    assert stale.effective_plan is plan
    assert stale.fallback_reason == "state_unavailable"
    assert wrong_topic.effective_plan is plan
    assert wrong_topic.fallback_reason == "topic_mismatch"


def test_active_requires_exact_projection_provenance() -> None:
    item = _hypothesis()
    ref = HypothesisRef(
        hypothesis_id=item.ref.hypothesis_id,
        topic_id=item.ref.topic_id,
        code=item.ref.code,
        status=item.ref.status,
        probability=item.ref.probability,
        model_version=item.ref.model_version,
        source_snapshot_id="",
        source_attempt_id=item.ref.source_attempt_id,
        projection_generation=3,
    )
    missing_provenance = LearnerCognitiveHypothesis(
        ref=ref,
        evidence_status="supported",
        support_count=2,
    )

    decision = CognitiveIntentPolicy(mode="on").decorate(
        _plan(), _view(missing_provenance)
    )

    assert decision.applied is False
    assert decision.effective_plan is decision.original_plan
    assert decision.fallback_reason == "active_hypothesis_not_eligible"
