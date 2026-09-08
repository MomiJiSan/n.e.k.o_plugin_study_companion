from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest
from test_cognitive_active_question_entry import _active_subject, _generate


def test_active_delivery_freezes_reviewed_strategy_only_when_shadow_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disabled, _ = _active_subject(
        monkeypatch, "_cognitive_v3_shadow_entry_disabled"
    )
    asyncio.run(_generate(disabled))
    disabled_commit = disabled._store.events[-1]
    assert "strategy_exposure" not in disabled_commit.get("metadata", {})

    enabled, _ = _active_subject(
        monkeypatch, "_cognitive_v3_shadow_entry_enabled"
    )
    setattr(enabled._knowledge_tracker, "cognitive_strategy_shadow_enabled", True)
    setattr(
        enabled._knowledge_tracker,
        "_cognitive_version_set_id",
        "cognitive-v2.1-1",
    )
    asyncio.run(_generate(enabled))
    enabled_commit = enabled._store.events[-1]

    assert enabled_commit["event_type"] == "question_committed"
    assert enabled_commit["metadata"]["strategy_exposure"] == {
        "strategy_id": "chain.omit-inner.complete-steps",
        "strategy_version": "v1",
        "catalog_version": "cognitive-strategy-catalog-v1",
        "version_set_id": "cognitive-v2.1-1",
        "question_purpose": "repair",
        "difficulty_bucket": "3",
        "strategy_family": "complete_steps",
        "comparison_scope_id": "chain.omit-inner.repair",
        "baseline": True,
        "eligible_for_repair_attribution": True,
        "answer_window_expires_at": enabled_commit["metadata"][
            "strategy_exposure"
        ]["answer_window_expires_at"],
    }
    assert enabled_commit["metadata"]["strategy_exposure"][
        "answer_window_expires_at"
    ].endswith("Z")
    committed_at = datetime.fromisoformat(enabled_commit["created_at"].replace("Z", "+00:00"))
    expires_at = datetime.fromisoformat(
        enabled_commit["metadata"]["strategy_exposure"][
            "answer_window_expires_at"
        ].replace("Z", "+00:00")
    )
    assert expires_at - committed_at == timedelta(hours=24)
