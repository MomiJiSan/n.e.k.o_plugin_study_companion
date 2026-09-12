"""Versioned learning sessions. Only the companion provider grants knowledge cards."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from threading import RLock
from typing import Any

from .application_service import ApplicationServiceError, KnowledgeDungeonApplicationService
from .bridge_contracts import (
    BridgeContractError,
    CreateRunRequest,
    PerformActionRequest,
    require_identifier,
)
from .contracts import PROTOCOL_VERSION, VersionBundle, canonical_json, canonical_sha256
from .engine import KnowledgeDungeonEngine
from .public_projection import project_public_run
from .reducer import STARTER_CARD_ID, _starter_card

LIVE_VERSIONS = dict(
    bridge_protocol_version=2,
    engine_protocol_version=PROTOCOL_VERSION,
    public_projection_version=2,
    application_service_version="knowledge-dungeon-v0.3-learning",
)
# The private bridge holds the OS ownership lock. This lock also serializes adapters
# created separately in the same process; no await occurs inside a live operation.
LIVE_LOCK = RLock()
LEGACY_IDS = {
    "college_limit_concept": "math.calculus.limit_concept",
    "college_limit_rules": "math.calculus.limit_laws",
    "college_important_limits": "math.calculus.important_limits",
    "college_continuity": "math.calculus.continuity",
}


def parse_live_request(operation: str, payload: object) -> dict[str, Any]:
    fields = {
        "bootstrap": set(),
        "begin_game_session": {"game_session_id"},
        "select_deck": {"game_session_id", "request_id", "expected_selection_version", "card_ids"},
        "create_run": {"game_session_id", "request_id", "subject_id", "scenario_id"},
        "get_run": {"game_session_id", "run_id"},
        "perform_action": {"game_session_id", "run_id", "request_id", "expected_state_version", "action_id"},
    }
    if operation not in fields or not isinstance(payload, Mapping):
        raise BridgeContractError("invalid_request", "invalid live operation")
    if (
        set(payload) != fields[operation] | {"bridge_protocol_version"}
        or type(payload["bridge_protocol_version"]) is not int
        or payload["bridge_protocol_version"] != 2
    ):
        raise BridgeContractError("invalid_request", "invalid live request fields or version")
    result = dict(payload)
    for key in ("game_session_id", "run_id", "request_id"):
        if key in result:
            require_identifier(result[key], key)
    if operation == "create_run":
        CreateRunRequest(**{k: result[k] for k in ("request_id", "subject_id", "scenario_id")})
    if operation == "perform_action":
        PerformActionRequest(**{k: result[k] for k in ("request_id", "expected_state_version", "action_id")})
    if operation == "select_deck":
        ids = result["card_ids"]
        if (
            not isinstance(ids, list)
            or not 1 <= len(ids) <= 12
            or any(not isinstance(x, str) for x in ids)
            or len(set(ids)) != len(ids)
            or STARTER_CARD_ID not in ids
        ):
            raise BridgeContractError("invalid_request", "deck must contain starter and at most 12 distinct cards")
        if type(result["expected_selection_version"]) is not int or result["expected_selection_version"] < 0:
            raise BridgeContractError("invalid_request", "invalid selection version")
    return result


def collection_from_snapshot(raw: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Validate the complete snapshot; malformed/missing data never means forgotten."""
    if (
        not isinstance(raw, Mapping)
        or any(not isinstance(raw.get(k), str) or not raw[k] for k in ("dataset_id", "model_version", "as_of"))
        or type(raw.get("snapshot_version")) is not int
        or raw["snapshot_version"] < 0
        or not isinstance(raw.get("topics"), list)
    ):
        raise ApplicationServiceError("learning_unavailable", "invalid learning snapshot")
    try:
        if datetime.fromisoformat(raw["as_of"].replace("Z", "+00:00")).utcoffset() is None:
            raise ValueError("snapshot time must include a timezone")
    except ValueError as exc:
        raise ApplicationServiceError("learning_unavailable", "invalid snapshot time") from exc
    starter = asdict(_starter_card())
    starter.update(name="红葉的怜悯", available_in_run=True, topic_id=None, mastery=None, generation=0)
    cards = [starter]
    seen: set[str] = set()
    for topic in raw["topics"]:
        if not isinstance(topic, Mapping):
            raise ApplicationServiceError("learning_unavailable", "invalid learning topic")
        tid = require_identifier(topic.get("topic_id"), "topic_id")
        mastery, owned, generation = topic.get("mastery"), topic.get("owned"), topic.get("generation")
        valid_mastery = mastery is None or (
            type(mastery) in (int, float) and math.isfinite(mastery) and 0 <= mastery <= 1
        )
        if (
            tid in seen
            or not valid_mastery
            or type(owned) is not bool
            or type(generation) is not int
            or generation < 0
            or topic.get("status") not in {"unassessed", "active", "forgotten", "learning"}
        ):
            raise ApplicationServiceError("learning_unavailable", "invalid learning topic state")
        seen.add(tid)
        if (
            (owned and (mastery is None or mastery < 0.001 or generation < 1 or topic["status"] != "active"))
            or (not owned and topic["status"] == "active")
            or (not owned and mastery is not None and mastery >= 0.01)
            or (topic["status"] == "forgotten" and mastery != 0)
            or (topic["status"] == "unassessed" and mastery is not None)
            or (topic["status"] == "learning" and (owned or mastery is None or not 0.001 <= mastery < 0.01))
        ):
            raise ApplicationServiceError("learning_unavailable", "inconsistent acquired card")
        if not owned:
            continue
        name, subject = topic.get("name"), topic.get("subject")
        if not isinstance(name, str) or not name.strip():
            raise ApplicationServiceError("learning_unavailable", "missing topic name")
        require_identifier(subject, "subject")
        cards.append(
            dict(
                card_id=LEGACY_IDS.get(tid, "knowledge." + tid),
                name=name,
                subject_id=subject,
                base_damage=6,
                energy_cost=1,
                freshness_bps=10000,
                lifecycle_state="active",
                rules_text="",
                flavor_text="",
                starter=False,
                available_in_run=True,
                topic_id=tid,
                mastery=mastery,
                generation=generation,
            )
        )
    return cards


class LiveLearningService:
    def __init__(self, store: Any, provider: Callable[[], Mapping[str, Any]] | None) -> None:
        self.store, self.provider = store, provider
        self.engine = KnowledgeDungeonEngine(store)
        self.db = store._connection
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS dungeon_learning_sessions (
                client_id TEXT NOT NULL, session_id TEXT NOT NULL, active INTEGER NOT NULL,
                data TEXT NOT NULL, data_hash TEXT NOT NULL, PRIMARY KEY(client_id,session_id));
            CREATE UNIQUE INDEX IF NOT EXISTS dungeon_one_active_session
                ON dungeon_learning_sessions(client_id) WHERE active=1;
            CREATE TABLE IF NOT EXISTS dungeon_selection_receipts (
                client_id TEXT NOT NULL, session_id TEXT NOT NULL, request_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL, response TEXT NOT NULL,
                PRIMARY KEY(client_id,session_id,request_id));
        """)

    def invoke(self, context: Any, operation: str, request: dict[str, Any]) -> dict[str, Any]:
        if operation == "bootstrap":
            result = KnowledgeDungeonApplicationService(self.engine).bootstrap(context)
            result.update(LIVE_VERSIONS)
            result["capabilities"]["real_learning_data"] = self.provider is not None
            return result
        if operation == "begin_game_session":
            return self.begin(context.client_id, request["game_session_id"])
        session = self.load(context.client_id, request["game_session_id"])
        if operation == "select_deck":
            return self.select(context.client_id, session, request)
        if operation == "create_run":
            return self.create(context, session, request)
        state = self.engine.get_state(request["run_id"])
        if state is None or state.owner_client_id != context.client_id:
            raise ApplicationServiceError("run_not_found", "run not found")
        state = self.reconcile(context, session, state)
        events = []
        if operation == "perform_action":
            # Session identity is part of command identity; previous-session retries
            # can never replay a receipt into the new session.
            action = PerformActionRequest(
                **{k: request[k] for k in ("request_id", "expected_state_version", "action_id")}
            )
            from .available_actions import command_for_action_id, resolve_available_action

            if (
                state.state_version == action.expected_state_version
                and resolve_available_action(state, action.action_id) is None
            ):
                raise ApplicationServiceError("action_unavailable", "action unavailable")
            intent, payload = command_for_action_id(action.action_id)
            response = self.dispatch(
                state.run_id,
                "action-"
                + canonical_sha256(
                    {
                        "client": context.client_id,
                        "session": request["game_session_id"],
                        "run": state.run_id,
                        "request": request["request_id"],
                    }
                )[:24],
                action.expected_state_version,
                intent,
                payload,
            )
            events = response["events"]
            state = self.engine.get_state(state.run_id)
        return self.project(state, session, events)

    def load(self, client: str, sid: str) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT active,data,data_hash FROM dungeon_learning_sessions WHERE client_id=? AND session_id=?",
            (client, sid),
        ).fetchone()
        if row is None or not row["active"]:
            raise ApplicationServiceError("game_session_expired", "game session is absent or retired")
        data = json.loads(row["data"])
        if canonical_sha256(data) != row["data_hash"]:
            raise ApplicationServiceError("learning_unavailable", "session integrity failure")
        return data

    def begin(self, client: str, sid: str) -> dict[str, Any]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            existing = self.db.execute(
                "SELECT 1 FROM dungeon_learning_sessions WHERE client_id=? AND session_id=?", (client, sid)
            ).fetchone()
            if existing:
                result = self.load(client, sid)
            else:
                if self.provider is None:
                    raise ApplicationServiceError("learning_unavailable", "learning provider unavailable")
                try:
                    raw = self.provider()
                    cards = collection_from_snapshot(raw)
                except Exception as exc:
                    raise ApplicationServiceError("learning_unavailable", "learning snapshot unavailable") from exc
                result = {
                    **LIVE_VERSIONS,
                    "game_session_id": sid,
                    "dataset_id": raw["dataset_id"],
                    "snapshot_id": canonical_sha256(raw),
                    "captured_at": raw["as_of"],
                    "policy_version": raw["model_version"],
                    "selection_version": 0,
                    "selected_card_ids": [STARTER_CARD_ID],
                    "cards": cards,
                }
                previous = self.db.execute(
                    "SELECT session_id FROM dungeon_learning_sessions WHERE client_id=? AND active=1", (client,)
                ).fetchone()
                if previous:
                    old = self.load(client, previous["session_id"])
                    if old["dataset_id"] == result["dataset_id"]:
                        old_cards = {c["card_id"]: c for c in old["cards"]}
                        new_cards = {c["card_id"]: c for c in cards}
                        result["selected_card_ids"] = [
                            cid
                            for cid in old["selected_card_ids"]
                            if cid in new_cards and old_cards[cid]["generation"] == new_cards[cid]["generation"]
                        ]
                self.db.execute("UPDATE dungeon_learning_sessions SET active=0 WHERE client_id=?", (client,))
                self.db.execute(
                    "INSERT INTO dungeon_learning_sessions VALUES(?,?,1,?,?)",
                    (client, sid, canonical_json(result), canonical_sha256(result)),
                )
            self.db.execute("COMMIT")
            return result
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def select(self, client: str, session: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
        sid = session["game_session_id"]
        fingerprint = canonical_sha256(request)
        row = self.db.execute(
            "SELECT fingerprint,response FROM dungeon_selection_receipts WHERE client_id=? AND session_id=? AND request_id=?",
            (client, sid, request["request_id"]),
        ).fetchone()
        if row:
            if row["fingerprint"] != fingerprint:
                raise ApplicationServiceError("command_id_conflict", "request reused")
            return json.loads(row["response"])
        if session["selection_version"] != request["expected_selection_version"]:
            raise ApplicationServiceError("stale_selection_version", "selection changed")
        if not set(request["card_ids"]) <= {c["card_id"] for c in session["cards"]}:
            raise ApplicationServiceError("card_unavailable", "card not in frozen collection")
        updated = deepcopy(session)
        updated.update(selection_version=session["selection_version"] + 1, selected_card_ids=request["card_ids"])
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.load(client, sid)
            self.db.execute(
                "UPDATE dungeon_learning_sessions SET data=?,data_hash=? WHERE client_id=? AND session_id=? AND active=1",
                (canonical_json(updated), canonical_sha256(updated), client, sid),
            )
            self.db.execute(
                "INSERT INTO dungeon_selection_receipts VALUES(?,?,?,?,?)",
                (client, sid, request["request_id"], fingerprint, canonical_json(updated)),
            )
        return updated

    def dispatch(
        self, run_id: str, command_id: str, version: int, intent: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        response = self.engine.dispatch(
            dict(
                protocol_version=PROTOCOL_VERSION,
                run_id=run_id,
                command_id=command_id,
                expected_state_version=version,
                intent=intent,
                payload=dict(payload),
            )
        )
        KnowledgeDungeonApplicationService._require_accepted(response)
        return response

    def create(self, context: Any, session: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
        rid = "run-" + canonical_sha256({"client": context.client_id, "request": request["request_id"]})[:24]
        request_fingerprint = canonical_sha256({k: v for k, v in request.items() if k != "game_session_id"})
        state = self.engine.get_state(rid)
        if state is not None:
            if state.versions.get("learning_create_request") != request_fingerprint:
                raise ApplicationServiceError("command_id_conflict", "create request reused")
            return self.project(self.reconcile(context, session, state), session)
        selected = set(session["selected_card_ids"])
        cards = [c for c in session["cards"] if c["card_id"] in selected]
        versions = VersionBundle().to_dict()
        versions["learning"] = self.metadata(session, cards)
        versions["learning_create_request"] = request_fingerprint
        from secrets import randbelow

        response = self.dispatch(
            rid,
            "create-" + canonical_sha256(request)[:24],
            0,
            "start_run",
            dict(
                seed=randbelow((1 << 64) - 1) + 1,
                owner_client_id=context.client_id,
                map_subject_id=request["subject_id"],
                scenario_id=request["scenario_id"],
                cards=cards,
                versions=versions,
            ),
        )
        return self.project(self.engine.get_state(rid), session, response["events"])

    @staticmethod
    def metadata(session: dict[str, Any], cards: list[dict[str, Any]]) -> dict[str, Any]:
        return dict(
            game_session_id=session["game_session_id"],
            dataset_id=session["dataset_id"],
            cards={c["card_id"]: {k: c[k] for k in ("topic_id", "mastery", "generation")} for c in cards},
        )

    def reconcile(self, context: Any, session: dict[str, Any], state: Any) -> Any:
        old = state.versions.get("learning", {})
        if old.get("dataset_id") is not None and old["dataset_id"] != session["dataset_id"]:
            raise ApplicationServiceError("learning_dataset_mismatch", "saved run belongs to another learning dataset")
        if old.get("game_session_id") == session["game_session_id"]:
            return state
        self.dispatch(
            state.run_id,
            "reconcile-" + canonical_sha256({"session": session["game_session_id"], "run": state.run_id})[:24],
            state.state_version,
            "reconcile_learning",
            {"learning": self.metadata(session, session["cards"])},
        )
        return self.engine.get_state(state.run_id)

    def project(self, state: Any, session: dict[str, Any], events: Any = ()) -> dict[str, Any]:
        # Learning internals are projected explicitly, not passed through legacy
        # public versions. Command/state hashes still cover the authoritative data.
        learning = state.versions.get("learning", {})
        public_state = deepcopy(state)
        public_state.versions.pop("learning", None)
        public_state.versions.pop("learning_create_request", None)
        result = project_public_run(
            public_state,
            scenario_id="forest_v0_2" if state.expedition else "calculus_v0_1",
            events=events,
            camp=self.engine.get_camp(state.owner_client_id)[1] if state.expedition else None,
        )
        from .serializer import state_hash

        result.update(LIVE_VERSIONS, game_session_id=session["game_session_id"], state_hash=state_hash(state))
        for card in result["run"]["cards"]:
            card.update(learning.get("cards", {}).get(card["card_id"], dict(topic_id=None, mastery=None, generation=0)))
            if card["starter"]:
                card["name"] = "红葉的怜悯"
        return result
