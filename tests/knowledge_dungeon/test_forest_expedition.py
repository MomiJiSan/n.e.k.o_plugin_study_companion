from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier

import pytest

from knowledge_dungeon.application_service import ApplicationServiceError, KnowledgeDungeonApplicationService
from knowledge_dungeon.bridge_contracts import TrustedInvocationContext
from knowledge_dungeon.engine import KnowledgeDungeonEngine
from knowledge_dungeon.forest import NODES
from knowledge_dungeon.persistence import DungeonRunStore

CTX = TrustedInvocationContext("forest-tester", "study_companion:dungeon")


def create(service, request="create-forest"):
    return service.create_run(
        CTX, dict(bridge_protocol_version=1, request_id=request, subject_id="math", scenario_id="forest_v0_2")
    )


def act(service, state, action, request=None):
    return service.perform_action(
        CTX,
        state["run"]["run_id"],
        dict(
            bridge_protocol_version=1,
            request_id=request or f"action-{state['state_version']}",
            expected_state_version=state["state_version"],
            action_id=action,
        ),
    )


def visit(service, state, node):
    state = act(service, state, f"select_node:{node}")
    return act(service, state, "enter_selected_node")


def fight(service, state):
    for _ in range(100):
        if state["run"]["phase"] != "encounter":
            break
        playable = [a for a in state["available_actions"] if a["action_type"] == "play_card"]
        state = act(service, state, playable[0]["action_id"] if playable else "end_turn")
    assert state["run"]["status"] != "failed"
    if state["run"]["phase"] == "reward":
        state = act(service, state, "choose_reward:heal_6")
    return state


def route(service, dangerous=False):
    state = create(service)
    state = visit(service, state, "wisp_path" if dangerous else "old_camp")
    state = fight(service, state) if dangerous else act(service, state, "choose_event:read_journal")
    state = visit(service, state, "edge_spring")
    state = act(service, state, "choose_event:heal")
    state = visit(service, state, "mist_patrol" if dangerous else "convergence")
    state = fight(service, state) if dangerous else act(service, state, "choose_event:anchor")
    state = visit(service, state, "relic_grove" if dangerous else "broken_creek")
    state = act(service, state, "choose_event:take_relic" if dangerous else "choose_event:search")
    state = visit(service, state, "memory_spring")
    state = act(service, state, "choose_event:heal")
    state = visit(service, state, "guardian_seal")
    state = act(service, state, "choose_event:gather")
    return visit(service, state, "guardian")


@pytest.mark.parametrize("dangerous", [False, True])
def test_complete_routes_and_repair_and_persistence(tmp_path, dangerous):
    db = tmp_path / "save.sqlite"
    with DungeonRunStore(db) as store:
        service = KnowledgeDungeonApplicationService(KnowledgeDungeonEngine(store), seed_factory=lambda: 1)
        state = route(service, dangerous)
        assert state["run"]["enemy"]["max_hp"] == (30 if dangerous else 22)
        assert state["run"]["enemy"]["attack"] == (6 if dangerous else 4)
        state = fight(service, state)
        before = deepcopy(state)
        state = act(service, state, "finish_run", "finish")
        assert state["run"]["expedition"]["settlement"]["materials_kept"] > 6
        saved = deepcopy(state)
        assert act(service, before, "finish_run", "finish") == state
        state = act(service, state, "repair_camp", "repair")
        assert state["run"]["expedition"]["camp"]["watchtower_repaired"]
        assert state["run"]["expedition"]["camp"]["materials"] == saved["run"]["expedition"]["camp"]["materials"] - 6
        run_id = state["run"]["run_id"]
    with DungeonRunStore(db) as store:
        service = KnowledgeDungeonApplicationService(KnowledgeDungeonEngine(store))
        restored = service.get_run(CTX, run_id)
        assert restored["state_hash"] == state["state_hash"]
        assert (
            act(service, before, "finish_run", "finish")["run"]["expedition"]["camp"]
            == state["run"]["expedition"]["camp"]
        )
        fresh = create(service, "next-run")
        assert fresh["run"]["expedition"]["camp"] == state["run"]["expedition"]["camp"]
        assert not fresh["run"]["expedition"]["carried_relics"]
        assert fresh["run"]["cards"] == state["run"]["cards"]


@pytest.mark.parametrize("phase", ["map", "event", "encounter", "reward"])
def test_retreat_from_all_active_phases_keeps_materials(tmp_path, phase):
    service = KnowledgeDungeonApplicationService(seed_factory=lambda: 1)
    state = create(service)
    if phase in {"event"}:
        state = visit(service, state, "old_camp")
    if phase in {"encounter", "reward"}:
        state = visit(service, state, "wisp_path")
        if phase == "reward":
            while state["run"]["phase"] == "encounter":
                state = act(
                    service,
                    state,
                    next(
                        (a["action_id"] for a in state["available_actions"] if a["action_type"] == "play_card"),
                        "end_turn",
                    ),
                )
    before = state["run"]["expedition"]["carried_materials"]
    state = act(service, state, "abandon_run")
    assert state["run"]["status"] == "abandoned"
    assert state["run"]["expedition"]["settlement"]["materials_kept"] == before
    assert state["run"]["expedition"]["event"] is None


def test_failure_keeps_half_materials_and_permanent_clue_but_loses_new_relic(tmp_path):
    with DungeonRunStore(tmp_path / "save.sqlite") as store:
        service = KnowledgeDungeonApplicationService(KnowledgeDungeonEngine(store), seed_factory=lambda: 1)
        state = create(service)
        state = visit(service, state, "old_camp")
        state = act(service, state, "choose_event:read_journal")
        state = visit(service, state, "edge_spring")
        state = act(service, state, "choose_event:gather")
        state = visit(service, state, "mist_patrol")
        state = fight(service, state)
        state = visit(service, state, "relic_grove")
        state = act(service, state, "choose_event:take_relic")
        state = visit(service, state, "memory_spring")
        state = act(service, state, "choose_event:prepare")
        state = visit(service, state, "guardian_seal")
        state = act(service, state, "choose_event:gather")
        state = visit(service, state, "guardian")
        carried = state["run"]["expedition"]["carried_materials"]
        assert state["run"]["expedition"]["carried_relics"] == ["moss_charm"]
        while state["run"]["phase"] == "encounter":
            state = act(service, state, "end_turn")
        exp = state["run"]["expedition"]
        assert exp["settlement"]["materials_kept"] == carried // 2
        assert exp["settlement"]["materials_lost"] == carried - carried // 2
        assert exp["camp"]["relics"] == []
        assert exp["camp"]["clues"] == ["old_expedition_journal"]


def test_restart_mid_event_preserves_exact_state_and_rejects_illegal_branch(tmp_path):
    db = tmp_path / "save.sqlite"
    with DungeonRunStore(db) as store:
        service = KnowledgeDungeonApplicationService(KnowledgeDungeonEngine(store), seed_factory=lambda: 1)
        state = visit(service, create(service), "old_camp")
        run_id = state["run"]["run_id"]
    with DungeonRunStore(db) as store:
        service = KnowledgeDungeonApplicationService(KnowledgeDungeonEngine(store))
        restored = service.get_run(CTX, run_id)
        assert restored["run"] == state["run"]
        assert restored["state_hash"] == state["state_hash"]
        with pytest.raises(ApplicationServiceError) as error:
            act(service, restored, "choose_event:force")
        assert error.value.code == "action_unavailable"
        state = act(service, restored, "choose_event:read_journal")
        with pytest.raises(ApplicationServiceError):
            act(service, state, "select_node:guardian")


def test_cross_run_concurrent_repairs_charge_once(tmp_path):
    db = tmp_path / "save.sqlite"
    with DungeonRunStore(db) as store:
        service = KnowledgeDungeonApplicationService(KnowledgeDungeonEngine(store), seed_factory=lambda: 1)
        first = act(service, fight(service, route(service)), "finish_run")
        second = act(service, create(service, "second"), "abandon_run")
        material = second["run"]["expedition"]["camp"]["materials"]
    barrier = Barrier(2)

    def repair(snapshot):
        with DungeonRunStore(db) as store:
            engine = KnowledgeDungeonEngine(store)
            original = store.commit_transition

            def blocked(**kwargs):
                barrier.wait(timeout=10)
                return original(**kwargs)

            store.commit_transition = blocked
            service = KnowledgeDungeonApplicationService(engine)
            try:
                return act(service, snapshot, "repair_camp")
            except ApplicationServiceError as exc:
                return exc.code

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(repair, [first, second]))
    assert sum(isinstance(r, dict) for r in results) == 1
    with DungeonRunStore(db) as store:
        _, camp = store.load_camp(CTX.client_id)
        assert camp["materials"] == material - 6
        assert camp["watchtower_repaired"]


@pytest.mark.parametrize("hook", ["after_run_write", "after_receipt_write"])
def test_settlement_fault_rolls_back_camp_and_run_together(tmp_path, hook):
    with DungeonRunStore(tmp_path / "save.sqlite") as store:
        service = KnowledgeDungeonApplicationService(KnowledgeDungeonEngine(store), seed_factory=lambda: 1)
        state = fight(service, route(service))
        version, camp = store.load_camp(CTX.client_id)

        def fail(stage):
            if stage == hook:
                raise RuntimeError("injected")

        store._fault_hook = fail
        with pytest.raises(ApplicationServiceError):
            act(service, state, "finish_run")
        assert store.load_camp(CTX.client_id) == (version, camp)
        assert service.get_run(CTX, state["run"]["run_id"])["state_version"] == state["state_version"]
        store._fault_hook = None
        result = act(service, state, "finish_run")
        assert result["run"]["expedition"]["camp"]["materials"] > 0


def test_content_graph_has_two_branches_and_three_regions():
    assert 10 <= len(NODES) <= 12
    assert len({n["region_id"] for n in NODES.values()}) == 3
    assert len([n for n in NODES.values() if len(n["next"]) > 1]) == 2
    assert all(nxt in NODES for node in NODES.values() for nxt in node["next"])


def test_exported_snapshots_reproduce_authority():
    import json
    from pathlib import Path

    from knowledge_dungeon.forest_fixture_exporter import build_forest_snapshots

    fixture = Path(__file__).parents[1] / "fixtures/knowledge_dungeon/forest_snapshots.json"
    assert build_forest_snapshots() == json.loads(fixture.read_text(encoding="utf-8"))


def test_camp_is_owner_isolated_and_corruption_fails_closed(tmp_path):
    from knowledge_dungeon.persistence import DungeonStoreError

    with DungeonRunStore(tmp_path / "save.sqlite") as store:
        engine = KnowledgeDungeonEngine(store)
        service = KnowledgeDungeonApplicationService(engine, seed_factory=lambda: 1)
        state = act(service, fight(service, route(service)), "finish_run")
        other = TrustedInvocationContext("other-owner", "study_companion:dungeon")
        other_state = service.create_run(
            other,
            dict(bridge_protocol_version=1, request_id="other-create", subject_id="math", scenario_id="forest_v0_2"),
        )
        assert other_state["run"]["expedition"]["camp"]["materials"] == 0
        with pytest.raises(ApplicationServiceError):
            service.get_run(other, state["run"]["run_id"])
        store._connection.execute("UPDATE dungeon_camps SET camp_json=? WHERE owner_client_id=?", ("{}", CTX.client_id))
        with pytest.raises(DungeonStoreError) as error:
            service.get_run(CTX, state["run"]["run_id"])
        assert error.value.code == "corrupt_dungeon_camp"


def test_legacy_sqlite_version_migrates_without_changing_saved_run(tmp_path):
    import sqlite3

    db = tmp_path / "legacy.sqlite"
    with DungeonRunStore(db) as store:
        service = KnowledgeDungeonApplicationService(KnowledgeDungeonEngine(store), seed_factory=lambda: 1)
        state = service.create_run(
            CTX, dict(bridge_protocol_version=1, request_id="legacy", subject_id="math", scenario_id="calculus_v0_1")
        )
    with sqlite3.connect(db) as connection:
        connection.execute("DROP TABLE dungeon_camps")
        connection.execute("PRAGMA user_version=1")
    with DungeonRunStore(db) as store:
        service = KnowledgeDungeonApplicationService(KnowledgeDungeonEngine(store))
        restored = service.get_run(CTX, state["run"]["run_id"])
        assert restored["state_hash"] == state["state_hash"]
        assert "expedition" not in restored["run"]
        assert create(service)["run"]["expedition"]["camp"]["materials"] == 0


def test_memory_snapshot_restore_keeps_camp_and_older_run_cannot_revert_it():
    source = KnowledgeDungeonEngine()
    service = KnowledgeDungeonApplicationService(source, seed_factory=lambda: 1)
    completed = act(service, fight(service, route(service)), "finish_run")
    older = source.get_state(completed["run"]["run_id"])
    next_run = act(service, create(service, "next-before-repair"), "abandon_run")
    repaired = act(service, next_run, "repair_camp")
    newer = source.get_state(repaired["run"]["run_id"])
    assert older is not None and newer is not None
    restored = KnowledgeDungeonEngine()
    restored.restore_state(newer)
    restored.restore_state(older)
    recovered = KnowledgeDungeonApplicationService(restored)
    state = recovered.get_run(CTX, older.run_id)
    assert state["run"]["expedition"]["camp"] == repaired["run"]["expedition"]["camp"]
    fresh = create(recovered, "after-restore")
    assert fresh["run"]["expedition"]["camp"] == repaired["run"]["expedition"]["camp"]


def test_concurrent_cross_run_settlements_preserve_both_rewards_after_retry(tmp_path):
    db = tmp_path / "parallel-settlement.sqlite"
    with DungeonRunStore(db) as store:
        service = KnowledgeDungeonApplicationService(KnowledgeDungeonEngine(store), seed_factory=lambda: 1)
        runs = []
        for request in ["first-run", "second-run"]:
            state = visit(service, create(service, request), "old_camp")
            runs.append(act(service, state, "choose_event:salvage"))
    barrier = Barrier(2)

    def settle_run(snapshot):
        with DungeonRunStore(db) as store:
            original = store.commit_transition

            def blocked(**kwargs):
                barrier.wait(timeout=10)
                return original(**kwargs)

            store.commit_transition = blocked
            service = KnowledgeDungeonApplicationService(KnowledgeDungeonEngine(store))
            try:
                return act(service, snapshot, "abandon_run")
            except ApplicationServiceError as exc:
                return exc.code

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(settle_run, runs))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert "stale_state_version" in results
    with DungeonRunStore(db) as store:
        service = KnowledgeDungeonApplicationService(KnowledgeDungeonEngine(store))
        for snapshot in runs:
            latest = service.get_run(CTX, snapshot["run"]["run_id"])
            if latest["run"]["status"] == "active":
                act(service, latest, "abandon_run")
        assert store.load_camp(CTX.client_id)[1]["materials"] == 4
