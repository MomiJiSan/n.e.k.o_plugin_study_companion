from __future__ import annotations

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


def test_both_knowledge_maps_activate_explicit_topic_scope() -> None:
    hosted = (ROOT / "surfaces" / "knowledge_map.tsx").read_text(encoding="utf-8")
    static = (ROOT / "static" / "knowledge-map.js").read_text(encoding="utf-8")
    assert "activatePracticeScope('explicit_topic')" in hosted
    assert "knowledgeTopicPracticeScope(node)" in static
    assert "mode: 'explicit_topic'" in static
    assert "runKnowledgePracticeScopeAction(topicAction, topicScope)" in static


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
    assert "./knowledge-map.js?v=study-mastery-status-20260824" in index
    assert "./main.js?v=study-settings-drawer-20260824" in index
