from __future__ import annotations

import pytest
from knowledge_dungeon.application_service import KnowledgeDungeonApplicationService
from knowledge_dungeon.bridge_contracts import TrustedInvocationContext
from knowledge_dungeon.public_projection import PublicProjectionError, filter_public_events

CONTEXT = TrustedInvocationContext("electron-client-test", "study_companion:dungeon")


def test_seed_is_removed_from_public_events() -> None:
    assert filter_public_events(
        [{"type": "run_started", "run_id": "run-example", "seed": 123, "command_id": "secret"}]
    ) == [{"type": "run_started", "run_id": "run-example"}]


def test_unknown_event_type_fails_closed() -> None:
    with pytest.raises(PublicProjectionError, match="unsupported public event"):
        filter_public_events([{"type": "future_internal_event", "seed": 123}])


def test_get_run_is_event_free_and_recomputes_available_actions() -> None:
    service = KnowledgeDungeonApplicationService()
    created = service.create_run(
        CONTEXT,
        {
            "bridge_protocol_version": 1,
            "request_id": "public-projection-001",
            "subject_id": "math",
            "scenario_id": "calculus_v0_1",
        },
    )
    current = service.get_run(CONTEXT, created["run"]["run_id"])

    assert current["events"] == []
    assert current["state_hash"] == created["state_hash"]
    assert current["available_actions"] == created["available_actions"]
    assert isinstance(current["run"]["cards"], list)
    assert "seed" not in repr(current)
