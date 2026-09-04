from __future__ import annotations

from pathlib import Path

from knowledge_dungeon.engine import KnowledgeDungeonEngine
from knowledge_dungeon.fixture_exporter import (
    build_calculus_demo_fixture,
    main,
    serialize_fixture,
    write_fixture,
)


def test_exported_fixture_is_a_complete_authority_engine_chain() -> None:
    fixture = build_calculus_demo_fixture()

    assert fixture["fixture_version"] == 2
    assert fixture["producer"] == "knowledge_dungeon.fixture_exporter"
    assert len(fixture["steps"]) == 16
    assert fixture["steps"][-1]["response"]["view"]["phase"] == "complete"
    assert fixture["steps"][-1]["response"]["view"]["status"] == "completed"
    assert fixture["final_state_hash"] == fixture["steps"][-1]["response"]["state_hash"]
    assert len(fixture["fixture_sha256"]) == 64

    replay = KnowledgeDungeonEngine()
    for index, step in enumerate(fixture["steps"]):
        assert step["request"]["expected_state_version"] == index
        assert step["response"]["state_version"] == index + 1
        assert replay.dispatch(step["request"]) == step["response"]


def test_exported_projection_and_terminal_events_preserve_product_boundaries() -> None:
    fixture = build_calculus_demo_fixture()
    cards = {card["card_id"]: card for card in fixture["projection"]["cards"]}

    assert cards["neutral.momiji_mercy"]["effective_damage"] == 1
    assert cards["math.calculus.limit_laws"]["effective_damage"] == 4
    assert cards["math.calculus.important_limits"]["effective_damage"] == 4
    assert cards["math.calculus.continuity"]["effective_damage"] == 0
    assert cards["math.calculus.continuity"]["playable"] is False

    terminal_events = fixture["steps"][-1]["response"]["events"]
    assert terminal_events == [
        {
            "type": "run_finished",
            "permanent_reward": None,
            "learning_fact_written": False,
        }
    ]


def test_export_is_stable_and_check_mode_detects_drift(tmp_path: Path) -> None:
    fixture = build_calculus_demo_fixture()
    output = tmp_path / "demo.json"
    write_fixture(output, fixture)

    assert output.read_text(encoding="utf-8") == serialize_fixture(fixture)
    assert main(["--output", str(output), "--check"]) == 0

    output.write_text("{}\n", encoding="utf-8")
    assert main(["--output", str(output), "--check"]) == 1
