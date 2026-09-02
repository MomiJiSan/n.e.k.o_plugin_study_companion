from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_hosted_and_static_practice_status_contracts_match() -> None:
    hosted = (ROOT / "surfaces" / "study_panel.tsx").read_text(encoding="utf-8")
    static = "\n".join(
        (ROOT / "static" / name).read_text(encoding="utf-8")
        for name in ("main.js", "knowledge-map.js")
    )
    for source in (hosted, static):
        assert "attempt_status" in source
        assert "scope_status" in source
        assert "mastery_status" in source
        assert "ui.practice.attempt.correct" in source
        assert "ui.practice.mastery.mastered" in source
        assert "ui.error.question_validation_failed" in source
        assert "ui.error.evaluation_inconsistent" in source
        assert "practice_scope_status === 'completed'" not in source
        assert "data.mastery_status === 'mastered'" in source

    # A correct attempt has its own message; the mastered message remains gated
    # exclusively by the server-derived mastery status.
    assert "practiceAttemptMessage(data.attempt_status || data.verdict)" in hosted
    assert "practiceAttemptMessage(data)" in static


def test_both_practice_uis_guard_review_state_by_scope_key() -> None:
    hosted = (ROOT / "surfaces" / "study_panel.tsx").read_text(encoding="utf-8")
    static = (ROOT / "static" / "knowledge-map.js").read_text(encoding="utf-8")
    for source in (hosted, static):
        assert "responseScopeKey" in source
        assert "activeScopeKey" in source
        assert "responseScopeKey === activeScopeKey" in source
    assert "activePracticeScopeRef.current?.scope_key" in hosted
    assert "activePracticeScopeRef.current = scope" in hosted


def test_hosted_retryable_evaluation_keeps_answer_image() -> None:
    hosted = (ROOT / "surfaces" / "study_panel.tsx").read_text(encoding="utf-8")
    assert "shouldClearAnswerImage = !isRetryablePracticeError(error)" in hosted


def test_both_practice_uis_submit_attempt_signals_without_exposing_answers() -> None:
    hosted = (ROOT / "surfaces" / "study_panel.tsx").read_text(encoding="utf-8")
    static = (ROOT / "static" / "main.js").read_text(encoding="utf-8")
    for source in (hosted, static):
        assert "response_time_ms" in source
        assert "used_hint" in source
    assert "questionStartedAtRef" in hosted
    assert "hintRevealedRef" in hosted
    assert "questionStartedAt" in static
    assert "hintRevealed" in static


def test_both_practice_uis_render_next_step_without_automatic_generation() -> None:
    hosted = (ROOT / "surfaces" / "study_panel.tsx").read_text(encoding="utf-8")
    markup = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    static = (ROOT / "static" / "main.js").read_text(encoding="utf-8") + markup

    for source in (hosted, static):
        assert "learning_update" in source
        assert "next_step" in source
        assert "wrong_question_status" in source
        assert "next_review_at" in source
        assert "plan_progress" in source
        assert "SELECTION_CONTEXT_EXPIRED" in source
        assert "ui.button.continue_next_question" in source
        assert "ui.practice.next_step_reason_fmt" in source

    assert "onClick={() => void continueAdaptiveLoop()}" in hosted
    assert "bindButton(continueQuestionBtn, continueAdaptiveLoop)" in static
    assert 'id="continueQuestionBtn"' in markup
    # Evaluation only stores and renders the server suggestion. Generation is
    # reachable exclusively from the explicit Continue button handler.
    hosted_evaluation = hosted[hosted.index("async function evaluateAnswer") : hosted.index("async function continueAdaptiveLoop")]
    static_evaluation = static[static.index("async function evaluateAnswer") : static.index("async function continueAdaptiveLoop")]
    assert "study_generate_targeted_question" not in hosted_evaluation
    assert "study_generate_targeted_question" not in static_evaluation


def test_next_step_i18n_contract_is_complete_in_all_locales() -> None:
    required = {
        "ui.practice.reason.blocked_diagnostic",
        "ui.practice.mastery_label",
        "ui.practice.wrong_status_label",
        "ui.practice.wrong_status.active",
        "ui.practice.wrong_status.retrying",
        "ui.practice.wrong_status.cooling",
        "ui.practice.wrong_status.resolved",
        "ui.practice.next_review_label",
        "ui.practice.plan_progress_fmt",
        "ui.practice.next_step.generate_question",
        "ui.practice.next_step.review_due",
        "ui.practice.next_step.wait_until",
        "ui.practice.next_step.summarize_plan",
        "ui.practice.next_step.choose_scope",
        "ui.practice.next_step.temporarily_unavailable",
        "ui.practice.next_step_reason_fmt",
        "ui.button.continue_next_question",
        "ui.button.view_plan_summary",
    }
    for locale_path in sorted((ROOT / "i18n").glob("*.json")):
        locale = json.loads(locale_path.read_text(encoding="utf-8"))
        assert not required - locale.keys(), locale_path.name
        assert all(str(locale[key]).strip() for key in required), locale_path.name


def test_both_uis_confirm_and_control_material_learning_plans() -> None:
    hosted = (ROOT / "surfaces" / "study_panel.tsx").read_text(encoding="utf-8")
    markup = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    static = (ROOT / "static" / "main.js").read_text(encoding="utf-8") + markup
    controller = (ROOT / "static" / "document-controller.js").read_text(encoding="utf-8")

    for source in (hosted, static):
        assert "learning_plan_draft" in source
        assert "learning_plan_mapping" in source
        assert "study_learning_plan_status" in source
        assert "study_learning_plan_activate" in source
        assert "accepted_topic_ids" in source
        assert "study_learning_plan_pause" in source
        assert "study_learning_plan_cancel" in source
        assert "ACTIVE_LEARNING_PLAN_EXISTS" in source
        assert "LEARNING_PLAN_CHANGED" in source
        assert "LEARNING_PLAN_TOPIC_REMOVED" in source
        assert "SELECTION_CONTEXT_EXPIRED" in source
        assert "ui.learning_plan.override_body" in source
        assert "ui.learning_plan.resume" in source

    assert "item.role === 'core'" in hosted
    assert "learningPlanHasSelectedCore()" in static
    assert "learningPlan.status !== 'paused'" in hosted
    assert "currentLearningPlan.status !== 'paused'" in static
    assert 'id="learningPlanActivateBtn"' in markup
    assert 'id="learningPlanResumeBtn"' in markup
    assert "await onAnalysisComplete(payload" in controller
    assert "await refreshAfterAnalysisComplete(data)" in controller


def test_learning_plan_i18n_contract_is_complete_in_all_locales() -> None:
    required = {
        "ui.learning_plan.draft_title",
        "ui.learning_plan.detected_topics",
        "ui.learning_plan.unmatched_warning",
        "ui.learning_plan.truncated_warning",
        "ui.learning_plan.confirm_start",
        "ui.learning_plan.role.core",
        "ui.learning_plan.role.prerequisite",
        "ui.learning_plan.confidence.high",
        "ui.learning_plan.status.active",
        "ui.learning_plan.status.paused",
        "ui.learning_plan.progress_summary",
        "ui.learning_plan.override_body",
        "ui.learning_plan.pause",
        "ui.learning_plan.resume",
        "ui.learning_plan.cancel",
        "ui.learning_plan.error.active_exists",
        "ui.learning_plan.error.changed",
        "ui.learning_plan.error.not_active",
        "ui.learning_plan.error.topic_removed",
        "ui.learning_plan.error.core_required",
        "ui.learning_plan.error.invalid_selection",
        "ui.learning_plan.error.context_expired",
    }
    for locale_path in sorted((ROOT / "i18n").glob("*.json")):
        locale = json.loads(locale_path.read_text(encoding="utf-8"))
        assert not required - locale.keys(), locale_path.name
        assert all(str(locale[key]).strip() for key in required), locale_path.name


def test_both_knowledge_maps_activate_explicit_topic_scope() -> None:
    hosted = (ROOT / "surfaces" / "knowledge_map.tsx").read_text(encoding="utf-8")
    static = (ROOT / "static" / "knowledge-map.js").read_text(encoding="utf-8")
    assert "activatePracticeScope('explicit_topic')" in hosted
    assert "knowledgeTopicPracticeScope(node)" in static
    assert "mode: 'explicit_topic'" in static
    assert "runKnowledgePracticeScopeAction(topicAction, topicScope)" in static


def test_both_knowledge_maps_offer_local_stage_quick_settings() -> None:
    hosted = (ROOT / "surfaces" / "knowledge_map.tsx").read_text(encoding="utf-8")
    static = (ROOT / "static" / "knowledge-map.js").read_text(encoding="utf-8")
    for source in (hosted, static):
        assert "ui.knowledge.set_default_stage" in source
        assert "ui.knowledge.return_default_stage" in source
    assert "setLearningProfileStage(activeStage)" in static
    assert "study_companion.learning_profile.v1" in hosted

    for locale_path in sorted((ROOT / "i18n").glob("*.json")):
        import json

        locale = json.loads(locale_path.read_text(encoding="utf-8"))
        assert "ui.knowledge.set_default_stage" in locale, locale_path.name
        assert "ui.knowledge.return_default_stage" in locale, locale_path.name


def test_both_knowledge_maps_render_boundary_prerequisites() -> None:
    hosted = (ROOT / "surfaces" / "knowledge_map.tsx").read_text(encoding="utf-8")
    static = (ROOT / "static" / "knowledge-map.js").read_text(encoding="utf-8")
    css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")

    for source in (hosted, static):
        assert "boundary" in source
        assert "in_scope" in source
        assert "ui.knowledge.boundary_prerequisite" in source
        assert "ui.knowledge.boundary_description" in source
        assert "knowledge-edge-graph__node--boundary" in source
        assert "boundary: true, in_scope: false" in source

    assert ".knowledge-node--boundary" in css
    assert "stroke-dasharray" in css

    import json

    for locale_path in sorted((ROOT / "i18n").glob("*.json")):
        locale = json.loads(locale_path.read_text(encoding="utf-8"))
        for key in (
            "ui.knowledge.boundary_prerequisite",
            "ui.knowledge.boundary_prerequisites",
            "ui.knowledge.boundary_description",
        ):
            assert locale.get(key, "").strip(), f"{locale_path.name}: {key}"


def test_static_knowledge_map_marks_local_one_hop_nodes_as_render_only_boundaries() -> None:
    import json
    import subprocess

    source_path = json.dumps(str(ROOT / "static" / "knowledge-map.js"))
    script = f"""
import fs from 'node:fs';
import vm from 'node:vm';

globalThis.document = {{ getElementById: () => null }};
globalThis.window = {{}};
const source = fs.readFileSync({source_path}, 'utf8');
vm.runInThisContext(`${{source}}\nglobalThis.__boundaryClosure = knowledgeNodesWithBoundaryClosure;`);
const payloadNodes = [
  {{ id: 'junior_linear', stage: 'junior_high', subject: 'math' }},
  {{ id: 'primary_number_sense', stage: 'primary', subject: 'math' }},
];
const closure = globalThis.__boundaryClosure(
  payloadNodes,
  [{{ from: 'primary_number_sense', to: 'junior_linear', relation: 'prerequisite' }}],
  [payloadNodes[0]],
);
if (closure.length !== 2) throw new Error(`expected 2 nodes, received ${{closure.length}}`);
if (closure[1] === payloadNodes[1]) throw new Error('boundary node must be a render-only copy');
if (closure[1].boundary !== true || closure[1].in_scope !== false) throw new Error('missing boundary flags');
if ('boundary' in payloadNodes[1] || 'in_scope' in payloadNodes[1]) throw new Error('payload was mutated');
"""
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_structured_practice_errors_are_preserved_and_localized() -> None:
    hosted_panel = (ROOT / "surfaces" / "study_panel.tsx").read_text(encoding="utf-8")
    hosted_bridge = (ROOT / "surfaces" / "study_surface_utils.ts").read_text(
        encoding="utf-8"
    )
    static = (ROOT / "static" / "main.js").read_text(encoding="utf-8")
    for source in (hosted_panel, static):
        assert "QUESTION_VALIDATION_FAILED" in source
        assert "EVALUATION_INCONSISTENT" in source
        assert "ui.error.question_validation_failed" in source
        assert "ui.error.evaluation_inconsistent" in source
    assert "structuredPluginError" in hosted_bridge
    assert "structured.code = code" in hosted_bridge
    assert "error.code = String(pluginError.code" in static


def test_static_assets_cache_bust_mastery_status_changes() -> None:
    index = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    assert "./knowledge-map.js?v=study-knowledge-mastery-status-pr1-20260825" in index
    assert "./local-models-controller.js" not in index
    assert "./main.js?v=study-scanned-pdf-hybrid-0.2.4" in index


def test_local_model_controller_normalizes_transfer_states_and_uses_canceled() -> None:
    controller = (ROOT / "static" / "local-models-controller.js").read_text(
        encoding="utf-8"
    )
    assert (
        "['checking', 'queued', 'downloading', 'verifying', 'installing', 'cancelling']"
        in controller
    )
    assert "? 'installing'" in controller
    assert "'canceled'" in controller
    assert "'cancelled'" not in controller
    assert "state === 'installed' ? 'ready'" in controller
    assert "['failed', 'canceled'].includes" in controller
    assert "stale_staging_count" in controller
    assert "manual_or_invalid_package_count" in controller


def test_local_model_controller_polls_queued_transfers_without_parallel_refreshes() -> None:
    controller = (ROOT / "static" / "local-models-controller.js").read_text(
        encoding="utf-8"
    )
    assert "const STATUS_POLL_MS = 750" in controller
    assert "let statusRefreshInFlight = null" in controller
    assert "if (statusRefreshInFlight) return statusRefreshInFlight" in controller
    assert "'queued', 'checking', 'downloading', 'paused', 'verifying', 'installing', 'cancelling'" in controller
    assert "callPlugin('study_local_models_status')" in controller
    assert "callPlugin(`study_local_model_${action}`, args)" in controller


def test_knowledge_map_mastery_protocol_keeps_unassessed_out_of_weak_topics() -> None:
    import json
    import subprocess

    main_path = json.dumps(str(ROOT / "static" / "main.js"))
    script = f"""
import fs from 'node:fs';
import vm from 'node:vm';
globalThis.t = (_key, fallback) => fallback;
const source = fs.readFileSync({main_path}, 'utf8');
const start = source.indexOf('function masteryIsAssessedForPanel');
const end = source.indexOf('function stageValueFromNode', start);
vm.runInThisContext(source.slice(start, end));
const unassessed = {{ assessed: false, mastery_status: 'progress', mastery: 0, weak: true }};
const legacyZero = {{ mastery: 0 }};
const assessedProgressWeak = {{ assessed: true, mastery_status: 'progress', mastery: 0.45, weak: true }};
const statusUnassessed = {{ mastery_status: 'unassessed', mastery: 0, weak: true }};
if (masteryLevelForPanel(unassessed) !== 'new' || weakTopicForPanel(unassessed)) throw new Error('unassessed node became weak');
if (masteryDisplayForPanel(unassessed).includes('0%')) throw new Error('unassessed node displayed zero percent');
if (masteryLevelForPanel(legacyZero) !== 'weak' || !weakTopicForPanel(legacyZero)) throw new Error('legacy numeric zero was treated as missing');
if (masteryLevelForPanel(assessedProgressWeak) !== 'progress' || !weakTopicForPanel(assessedProgressWeak)) throw new Error('progressing weak node was not retained as weak priority');
if (masteryLevelForPanel(statusUnassessed) !== 'new' || weakTopicForPanel(statusUnassessed)) throw new Error('mastery status unassessed became weak');
"""
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    hosted = (ROOT / "surfaces" / "knowledge_map.tsx").read_text(encoding="utf-8")
    static = (ROOT / "static" / "knowledge-map.js").read_text(encoding="utf-8")
    assert "nodeIsWeakTopic(node)" in hosted
    assert "masteryDisplayForPanel(node)" in static
    assert "weakTopicForPanel(node)" in static
