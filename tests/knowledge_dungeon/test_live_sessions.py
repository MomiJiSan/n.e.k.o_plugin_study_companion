from copy import deepcopy

import pytest

# isort: split

from knowledge_dungeon.bridge_contracts import REQUIRED_DUNGEON_SCOPE, TrustedInvocationContext
from knowledge_dungeon.host_adapter import KnowledgeDungeonHostAdapter
from knowledge_dungeon.live_service import collection_from_snapshot
from knowledge_dungeon.persistence import DungeonRunStore

CONTEXT = TrustedInvocationContext("client-live", REQUIRED_DUNGEON_SCOPE)
STARTER = "neutral.momiji_mercy"
CARD = "math.calculus.limit_concept"


def snapshot(mastery=0.2, generation=1, dataset="dataset-one"):
    return dict(
        dataset_id=dataset,
        model_version="mastery-retention-trial-1",
        snapshot_version=1,
        as_of="2026-09-12T00:00:00+00:00",
        topics=[
            dict(
                topic_id="college_limit_concept",
                name="Limits",
                subject="math",
                mastery=mastery,
                owned=mastery > 0,
                generation=generation,
                status="active" if mastery > 0 else "forgotten",
            )
        ],
    )


async def invoke(adapter, op, **payload):
    result = await adapter.invoke(CONTEXT, op, dict(bridge_protocol_version=2, **payload))
    assert result.ok, result.error
    return result.value


async def start(adapter, sid="boot-one"):
    await invoke(adapter, "begin_game_session", game_session_id=sid)
    await invoke(
        adapter,
        "select_deck",
        game_session_id=sid,
        request_id="selection-one",
        expected_selection_version=0,
        card_ids=[STARTER, CARD],
    )
    return await invoke(
        adapter,
        "create_run",
        game_session_id=sid,
        request_id="create-one",
        subject_id="math",
        scenario_id="calculus_v0_1",
    )


async def act(adapter, run, action, request="action-one", sid="boot-one"):
    return await invoke(
        adapter,
        "perform_action",
        game_session_id=sid,
        run_id=run["run"]["run_id"],
        request_id=request,
        expected_state_version=run["state_version"],
        action_id=action,
    )


@pytest.mark.asyncio
async def test_snapshot_freezes_survives_restart_and_retired_session_cannot_return(tmp_path):
    calls = []
    current = snapshot()

    def provider():
        calls.append(1)
        return deepcopy(current)

    path = tmp_path / "runs.db"
    adapter = KnowledgeDungeonHostAdapter(path, learning_snapshot_provider=provider)
    first = await start(adapter)
    current["topics"][0]["mastery"] = 0.9
    restarted = KnowledgeDungeonHostAdapter(path, learning_snapshot_provider=provider)
    repeated = await invoke(restarted, "begin_game_session", game_session_id="boot-one")
    assert repeated["cards"][1]["mastery"] == 0.2
    assert len(calls) == 1
    replay = await invoke(
        restarted,
        "create_run",
        game_session_id="boot-one",
        request_id="create-one",
        subject_id="math",
        scenario_id="calculus_v0_1",
    )
    assert replay["state_hash"] == first["state_hash"]
    await invoke(restarted, "begin_game_session", game_session_id="boot-two")
    denied = await restarted.invoke(
        CONTEXT, "begin_game_session", dict(bridge_protocol_version=2, game_session_id="boot-one")
    )
    assert denied.error.code == "game_session_expired"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_fractional_damage_and_same_generation_refresh_preserve_battle(tmp_path):
    current = snapshot()
    adapter = KnowledgeDungeonHostAdapter(tmp_path / "runs.db", learning_snapshot_provider=lambda: deepcopy(current))
    run = await start(adapter)
    run = await act(adapter, run, "select_node:battle_1")
    run = await act(adapter, run, "enter_selected_node", "enter")
    run = await act(adapter, run, "play_card:" + CARD, "play")
    assert run["run"]["enemy"]["hp"] == 6.8
    assert next(e for e in run["events"] if e["type"] == "card_played")["damage"] == 1.2
    before = deepcopy(run["run"])
    current["topics"][0]["mastery"] = 0.5
    await invoke(adapter, "begin_game_session", game_session_id="boot-two")
    refreshed = await invoke(adapter, "get_run", game_session_id="boot-two", run_id=run["run"]["run_id"])
    for key in ("enemy", "player", "hand", "draw_pile_count", "discard_pile_count", "current_node_id"):
        assert refreshed["run"][key] == before[key]
    assert next(c for c in refreshed["run"]["cards"] if c["card_id"] == CARD)["mastery"] == 0.5


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["forgotten", "reacquired"])
async def test_new_session_removes_old_incarnation_without_draw_or_refund(tmp_path, change):
    current = snapshot()
    path = tmp_path / "runs.db"
    adapter = KnowledgeDungeonHostAdapter(path, learning_snapshot_provider=lambda: deepcopy(current))
    run = await start(adapter)
    run = await act(adapter, run, "select_node:battle_1")
    run = await act(adapter, run, "enter_selected_node", "enter")
    rid = run["run"]["run_id"]
    with DungeonRunStore(path) as store:
        before = store.load_run(rid)
    current = snapshot(
        0 if change == "forgotten" else 0.8,
        2 if change == "reacquired" else 1,
        "different" if change == "dataset" else "dataset-one",
    )
    await invoke(adapter, "begin_game_session", game_session_id="boot-two")
    await invoke(adapter, "get_run", game_session_id="boot-two", run_id=rid)
    with DungeonRunStore(path) as store:
        after = store.load_run(rid)
    assert list(after.cards) == [STARTER]
    for name in ("hand", "draw_pile", "discard_pile"):
        assert getattr(after, name) == [x for x in getattr(before, name) if x == STARTER]
    for name in (
        "player_hp",
        "energy",
        "rng_state",
        "rng_increment",
        "completed_node_ids",
        "applied_reward_ids",
        "enemy",
    ):
        assert getattr(after, name) == getattr(before, name)


@pytest.mark.asyncio
async def test_request_conflicts_and_failed_snapshot_do_not_replace_session(tmp_path):
    current = snapshot()
    adapter = KnowledgeDungeonHostAdapter(tmp_path / "runs.db", learning_snapshot_provider=lambda: deepcopy(current))
    run = await start(adapter)
    conflict = await adapter.invoke(
        CONTEXT,
        "create_run",
        dict(
            bridge_protocol_version=2,
            game_session_id="boot-one",
            request_id="create-one",
            subject_id="math",
            scenario_id="forest_v0_2",
        ),
    )
    assert conflict.error.code == "command_id_conflict"
    changed = await act(adapter, run, "select_node:battle_1")
    conflict = await adapter.invoke(
        CONTEXT,
        "perform_action",
        dict(
            bridge_protocol_version=2,
            game_session_id="boot-one",
            run_id=run["run"]["run_id"],
            request_id="action-one",
            expected_state_version=run["state_version"],
            action_id="select_node:trap_1",
        ),
    )
    assert conflict.error.code == "command_id_conflict"
    current["topics"][0]["mastery"] = float("nan")
    invalid = await adapter.invoke(
        CONTEXT, "begin_game_session", dict(bridge_protocol_version=2, game_session_id="boot-two")
    )
    assert invalid.error.code == "learning_unavailable"
    retained = await invoke(adapter, "get_run", game_session_id="boot-one", run_id=run["run"]["run_id"])
    assert retained["state_hash"] == changed["state_hash"]


@pytest.mark.asyncio
async def test_lost_create_response_recovered_after_full_reopen(tmp_path):
    adapter = KnowledgeDungeonHostAdapter(tmp_path / "runs.db", learning_snapshot_provider=snapshot)
    first = await start(adapter)
    await invoke(adapter, "begin_game_session", game_session_id="boot-two")
    recovered = await invoke(
        adapter,
        "create_run",
        game_session_id="boot-two",
        request_id="create-one",
        subject_id="math",
        scenario_id="calculus_v0_1",
    )
    assert recovered["run"]["run_id"] == first["run"]["run_id"]
    assert recovered["game_session_id"] == "boot-two"
    assert {c["card_id"] for c in recovered["run"]["cards"]} == {STARTER, CARD}


@pytest.mark.asyncio
async def test_dataset_mismatch_preserves_saved_run(tmp_path):
    current = snapshot()
    path = tmp_path / "runs.db"
    adapter = KnowledgeDungeonHostAdapter(path, learning_snapshot_provider=lambda: deepcopy(current))
    run = await start(adapter)
    rid = run["run"]["run_id"]
    with DungeonRunStore(path) as store:
        before = store.load_run(rid).to_dict()
    current["dataset_id"] = "another-dataset"
    await invoke(adapter, "begin_game_session", game_session_id="boot-two")
    denied = await adapter.invoke(
        CONTEXT, "get_run", dict(bridge_protocol_version=2, game_session_id="boot-two", run_id=rid)
    )
    assert denied.error.code == "learning_dataset_mismatch"
    with DungeonRunStore(path) as store:
        assert store.load_run(rid).to_dict() == before


@pytest.mark.asyncio
async def test_concurrent_begin_reads_provider_once_and_selection_is_cas(tmp_path):
    import asyncio

    count = 0

    def provider():
        nonlocal count
        count += 1
        return snapshot()

    path = tmp_path / "runs.db"
    adapters = [KnowledgeDungeonHostAdapter(path, learning_snapshot_provider=provider) for _ in range(2)]
    sessions = await asyncio.gather(*(invoke(a, "begin_game_session", game_session_id="boot-one") for a in adapters))
    assert count == 1
    assert sessions[0] == sessions[1]
    responses = await asyncio.gather(
        *(
            a.invoke(
                CONTEXT,
                "select_deck",
                dict(
                    bridge_protocol_version=2,
                    game_session_id="boot-one",
                    request_id="select-" + str(i),
                    expected_selection_version=0,
                    card_ids=[STARTER, CARD],
                ),
            )
            for i, a in enumerate(adapters)
        )
    )
    assert sum(r.ok for r in responses) == 1
    assert next(r.error.code for r in responses if not r.ok) == "stale_selection_version"


@pytest.mark.asyncio
async def test_cancelled_begin_retry_keeps_original_capture(tmp_path):
    import asyncio
    from threading import Event

    entered, release = Event(), Event()
    calls = []

    def provider():
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return snapshot()

    adapter = KnowledgeDungeonHostAdapter(tmp_path / "runs.db", learning_snapshot_provider=provider)
    task = asyncio.create_task(invoke(adapter, "begin_game_session", game_session_id="boot-one"))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    session = await invoke(adapter, "begin_game_session", game_session_id="boot-one")
    assert session["cards"][1]["mastery"] == 0.2
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_low_mastery_floor_and_starter_ignore_encounter_buff(tmp_path):
    adapter = KnowledgeDungeonHostAdapter(tmp_path / "runs.db", learning_snapshot_provider=lambda: snapshot(0.001))
    run = await start(adapter)
    run = await act(adapter, run, "select_node:battle_1")
    run = await act(adapter, run, "enter_selected_node", "enter")
    run = await act(adapter, run, "play_card:" + CARD, "play")
    assert run["run"]["enemy"]["hp"] == 7
    from knowledge_dungeon.commands import DungeonCommand
    from knowledge_dungeon.reducer import reduce_command

    with DungeonRunStore(tmp_path / "runs.db") as store:
        boosted = store.load_run(run["run"]["run_id"])
    boosted.encounter_damage_bps = 20_000
    transition = reduce_command(
        boosted,
        DungeonCommand(
            command_id="mercy-boost",
            run_id=boosted.run_id,
            expected_state_version=boosted.state_version,
            intent="play_card",
            payload={"card_id": STARTER},
        ),
    )
    assert transition.state.enemy.hp == 6
    run = await act(adapter, run, "play_card:" + STARTER, "mercy")
    assert run["run"]["enemy"]["hp"] == 6


def test_collection_is_not_limited_to_combat_deck_and_keeps_unassessed_distinct():
    raw = snapshot()
    raw["topics"] += [
        dict(
            topic_id=f"topic-{i}",
            name=f"Topic {i}",
            subject="physics",
            mastery=0.01,
            owned=True,
            generation=1,
            status="active",
        )
        for i in range(20)
    ]
    raw["topics"] += [
        dict(
            topic_id="unknown",
            name="Unknown",
            subject="math",
            mastery=None,
            owned=False,
            generation=0,
            status="unassessed",
        ),
        dict(
            topic_id="partial",
            name="Partial",
            subject="math",
            mastery=0.005,
            owned=False,
            generation=0,
            status="learning",
        ),
    ]
    cards = collection_from_snapshot(raw)
    assert len(cards) == 22
    assert cards[1]["card_id"] == CARD
    assert cards[-1]["subject_id"] == "physics"


@pytest.mark.parametrize(
    "changes",
    [
        dict(mastery=None),
        dict(mastery=-0.1),
        dict(mastery=float("inf")),
        dict(owned=False),
        dict(status="forgotten"),
        dict(generation=0),
    ],
)
def test_bad_owned_topic_never_becomes_empty_collection(changes):
    from knowledge_dungeon.application_service import ApplicationServiceError

    raw = snapshot()
    raw["topics"][0].update(changes)
    with pytest.raises(ApplicationServiceError):
        collection_from_snapshot(raw)

