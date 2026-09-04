from __future__ import annotations

import importlib
import importlib.util
import json
import sqlite3
import sys
import threading
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "store_cognitive_retention", ROOT / "store_cognitive_retention.py"
)
assert SPEC is not None and SPEC.loader is not None
retention = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(retention)


TOPIC = "calculus.chain_rule"
HYPOTHESIS = "calculus.chain_rule:omit_inner_derivative"
MODEL = "cognitive-v2.1-1"
TRANSFER_AT = datetime(2026, 9, 1, 8, 30, tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


class _Store:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("CREATE TABLE topics (id TEXT PRIMARY KEY)")
        self.conn.execute(
            """
            CREATE TABLE cognitive_fact_roots (
                root_fact_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                fact_type TEXT NOT NULL,
                source_id TEXT NOT NULL,
                effective_at TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                UNIQUE(fact_type, source_id)
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE attempts (
                attempt_id TEXT PRIMARY KEY,
                topic_id TEXT NOT NULL REFERENCES topics(id)
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE cognitive_user_controls (
                control_id TEXT PRIMARY KEY,
                topic_id TEXT NOT NULL,
                hypothesis_code TEXT NOT NULL,
                action TEXT NOT NULL,
                expires_at TEXT NOT NULL DEFAULT '',
                root_fact_seq INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        retention.create_cognitive_retention_schema(self.conn)
        self.conn.execute("INSERT INTO topics (id) VALUES (?)", (TOPIC,))
        self.conn.commit()

    def _require_conn(self) -> sqlite3.Connection:
        return self.conn

    def _require_read_conn(self) -> sqlite3.Connection:
        return self.conn

    @staticmethod
    def _json_dumps(value: object) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    def add_attempt(self, attempt_id: str) -> None:
        self.conn.execute(
            "INSERT INTO attempts (attempt_id, topic_id) VALUES (?, ?)",
            (attempt_id, TOPIC),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


@pytest.fixture
def store() -> Iterator[_Store]:
    subject = _Store()
    yield subject
    subject.close()


def _transfer(
    store: _Store,
    attempt_id: str = "transfer-attempt-1",
    *,
    occurred_at: datetime = TRANSFER_AT,
) -> dict[str, object]:
    store.add_attempt(attempt_id)
    return {
        "hypothesis_id": HYPOTHESIS,
        "topic_id": TOPIC,
        "hypothesis_code": retention.ACTIVE_RETENTION_HYPOTHESIS,
        "model_version": MODEL,
        "source_attempt_id": attempt_id,
        "source_event_id": f"transfer-event:{attempt_id}",
        "question_family_id": f"transfer-family:{attempt_id}",
        "evaluation_verdict": "correct",
        "certified": True,
        "used_hint": False,
        "occurred_at": _iso(occurred_at),
    }


def _create(store: _Store, attempt_id: str = "transfer-attempt-1") -> dict[str, Any]:
    return retention.record_certified_transfer_success(store, _transfer(store, attempt_id))


def _claim(
    store: _Store,
    at: datetime,
    *,
    worker: str = "worker-a",
    lease_seconds: int = 300,
) -> dict[str, object]:
    items = retention.claim_cognitive_obligations(
        store,
        worker_id=worker,
        lease_seconds=lease_seconds,
        as_of=_iso(at),
        obligation_types=("retention",),
    )
    assert len(items) == 1
    return items[0]


def test_certified_transfer_atomically_creates_fixed_window_and_is_idempotent(
    store: _Store,
) -> None:
    transfer = _transfer(store)
    created = retention.record_certified_transfer_success(store, transfer)
    episode = created["episode"]
    obligation = created["obligation"]

    assert episode["status"] == "open"
    assert episode["not_before"] == _iso(TRANSFER_AT + timedelta(hours=24))
    assert episode["due_by"] == _iso(TRANSFER_AT + timedelta(hours=72))
    assert episode["eligibility_until"] == _iso(TRANSFER_AT + timedelta(days=7))
    assert obligation["status"] == "pending"
    assert obligation["obligation_type"] == "retention"
    assert obligation["not_before"] == episode["not_before"]

    repeated = retention.record_certified_transfer_success(store, transfer)
    assert repeated["episode"]["episode_id"] == episode["episode_id"]
    assert repeated["obligation"]["obligation_id"] == obligation["obligation_id"]
    assert len(retention.list_cognitive_monitoring_episodes(store)) == 1
    assert len(retention.list_cognitive_learning_obligations(store)) == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("hypothesis_code", "unreviewed_mechanism"),
        ("evaluation_verdict", "partial"),
        ("certified", False),
        ("used_hint", True),
    ],
)
def test_episode_creation_fails_closed_for_uncertified_transfer(
    store: _Store, field: str, value: object
) -> None:
    transfer = _transfer(store)
    transfer[field] = value
    with pytest.raises(ValueError):
        retention.record_certified_transfer_success(store, transfer)
    assert retention.list_cognitive_monitoring_episodes(store) == []


def test_claim_lease_takeover_fences_old_worker(store: _Store) -> None:
    _create(store)
    due = TRANSFER_AT + timedelta(hours=24)
    first = _claim(store, due, worker="old-worker", lease_seconds=30)
    assert retention.claim_cognitive_obligations(
        store,
        worker_id="new-worker",
        as_of=_iso(due + timedelta(seconds=29)),
        obligation_types=("retention",),
    ) == []

    second = _claim(
        store,
        due + timedelta(seconds=31),
        worker="new-worker",
        lease_seconds=300,
    )
    assert second["claim_id"] != first["claim_id"]
    with pytest.raises(ValueError, match="stale or not owned"):
        retention.release_cognitive_obligation_claim(
            store,
            obligation_id=str(first["obligation_id"]),
            claim_token=str(first["claim_token"]),
            worker_id="old-worker",
            released_at=_iso(due + timedelta(seconds=32)),
        )
    released = retention.release_cognitive_obligation_claim(
        store,
        obligation_id=str(second["obligation_id"]),
        claim_token=str(second["claim_token"]),
        worker_id="new-worker",
        released_at=_iso(due + timedelta(seconds=32)),
    )
    assert released["status"] == "pending"


def test_claim_can_be_fenced_to_the_exact_planner_selected_obligation(
    store: _Store,
) -> None:
    first = _create(store, "transfer-attempt-first")["obligation"]
    second_transfer = _transfer(store, "transfer-attempt-second")
    second_transfer["hypothesis_id"] = f"{HYPOTHESIS}:second"
    second = retention.record_certified_transfer_success(
        store,
        second_transfer,
    )["obligation"]
    due = TRANSFER_AT + timedelta(hours=24)

    claimed = retention.claim_cognitive_obligations(
        store,
        worker_id="planner-worker",
        as_of=_iso(due),
        obligation_types=("retention",),
        obligation_ids=(str(second["obligation_id"]),),
    )

    assert [item["obligation_id"] for item in claimed] == [
        second["obligation_id"]
    ]
    obligations = {
        item["obligation_id"]: item
        for item in retention.list_cognitive_learning_obligations(store)
    }
    assert obligations[first["obligation_id"]]["status"] == "pending"
    assert obligations[second["obligation_id"]]["status"] == "claimed"
    assert retention.claim_cognitive_obligations(
        store,
        worker_id="wrong-id-worker",
        as_of=_iso(due),
        obligation_types=("retention",),
        obligation_ids=("missing-obligation",),
    ) == []

    released = retention.release_cognitive_obligation_claim(
        store,
        obligation_id=str(claimed[0]["obligation_id"]),
        claim_token=str(claimed[0]["claim_token"]),
        worker_id="planner-worker",
        released_at=_iso(due + timedelta(seconds=1)),
    )
    assert released["status"] == "pending"


def test_resolved_completes_obligation_and_rebuild_does_not_duplicate(
    store: _Store,
) -> None:
    transfer = _transfer(store)
    created = retention.record_certified_transfer_success(store, transfer)
    claim = _claim(store, TRANSFER_AT + timedelta(hours=24))
    result = retention.apply_cognitive_retention_disposition(
        store,
        obligation_id=str(claim["obligation_id"]),
        claim_token=str(claim["claim_token"]),
        worker_id="worker-a",
        attempt_id="retention-attempt-1",
        disposition="resolved",
        occurred_at=_iso(TRANSFER_AT + timedelta(hours=24, minutes=1)),
        metadata={"evaluator_version": "evaluator-v1"},
    )
    assert result["episode"]["status"] == "resolved"
    assert result["obligation"]["status"] == "completed"

    rebuilt = retention.rebuild_cognitive_retention_from_transfers(store, [transfer])
    assert rebuilt[0]["episode"]["episode_id"] == created["episode"]["episode_id"]
    obligations = retention.list_cognitive_learning_obligations(store)
    assert len(obligations) == 1
    assert obligations[0]["status"] == "completed"


def test_relapse_closes_episode_and_schedules_transfer_after_24h_cooldown(
    store: _Store,
) -> None:
    _create(store)
    answered_at = TRANSFER_AT + timedelta(hours=24, minutes=5)
    claim = _claim(store, TRANSFER_AT + timedelta(hours=24))
    result = retention.apply_cognitive_retention_disposition(
        store,
        obligation_id=str(claim["obligation_id"]),
        claim_token=str(claim["claim_token"]),
        worker_id="worker-a",
        attempt_id="retention-relapse-1",
        disposition="relapse",
        occurred_at=_iso(answered_at),
    )

    assert result["episode"]["status"] == "relapsed"
    assert result["episode"]["relapse_count"] == 1
    assert result["obligation"]["status"] == "completed"
    assert result["next_obligation"]["obligation_type"] == "transfer_check"
    assert result["next_obligation"]["not_before"] == _iso(
        answered_at + timedelta(hours=24)
    )


@pytest.mark.parametrize("disposition", ["reschedule", "ordinary_evidence"])
def test_nonterminal_result_preserves_episode_and_enforces_rolling_24h(
    store: _Store, disposition: str
) -> None:
    _create(store)
    first_at = TRANSFER_AT + timedelta(hours=24)
    claim = _claim(store, first_at, lease_seconds=600)
    result = retention.apply_cognitive_retention_disposition(
        store,
        obligation_id=str(claim["obligation_id"]),
        claim_token=str(claim["claim_token"]),
        worker_id="worker-a",
        attempt_id=f"retention-{disposition}",
        disposition=disposition,
        occurred_at=_iso(first_at + timedelta(minutes=1)),
        metadata={
            "cognitive_question_family_id": "chain.exp-affine.retention",
            "cognitive_independence_group": "chain.exponential-affine",
        },
    )
    assert result["episode"]["status"] == "open"
    assert result["obligation"]["status"] == "pending"
    assert result["obligation"]["not_before"] == _iso(
        first_at + timedelta(hours=24, minutes=1)
    )
    listed = retention.list_cognitive_learning_obligations(store)
    assert listed[0]["previous_question_family_ids"] == (
        "chain.exp-affine.retention",
    )
    assert listed[0]["previous_independence_groups"] == (
        "chain.exponential-affine",
    )
    assert retention.claim_cognitive_obligations(
        store,
        worker_id="too-soon",
        as_of=_iso(first_at + timedelta(hours=23)),
        obligation_types=("retention",),
    ) == []
    later = retention.claim_cognitive_obligations(
        store,
        worker_id="later",
        as_of=_iso(first_at + timedelta(hours=24, minutes=2)),
        obligation_types=("retention",),
    )
    assert len(later) == 1


def test_window_expiry_is_idempotent_and_creates_transfer_check(store: _Store) -> None:
    created = _create(store)
    expired_at = TRANSFER_AT + timedelta(days=7, microseconds=1)
    first = retention.expire_cognitive_monitoring_episodes(
        store, as_of=_iso(expired_at)
    )
    second = retention.expire_cognitive_monitoring_episodes(
        store, as_of=_iso(expired_at + timedelta(minutes=1))
    )

    assert len(first) == 1
    assert second == []
    episodes = retention.list_cognitive_monitoring_episodes(store)
    assert episodes[0]["episode_id"] == created["episode"]["episode_id"]
    assert episodes[0]["status"] == "expired"
    obligations = retention.list_cognitive_learning_obligations(store)
    assert [item["obligation_type"] for item in obligations] == [
        "retention",
        "transfer_check",
    ]
    assert obligations[0]["status"] == "cancelled"
    assert obligations[1]["status"] == "pending"
    facts = store.conn.execute(
        "SELECT * FROM cognitive_monitoring_episode_facts"
    ).fetchall()
    assert len(facts) == 1
    assert facts[0]["fact_type"] == "expired"
    assert facts[0]["root_fact_seq"] > 0
    assert store.conn.execute(
        """SELECT COUNT(*) FROM cognitive_fact_roots
        WHERE fact_type = 'cognitive_episode'"""
    ).fetchone()[0] == 1


def test_window_expiry_skips_transfer_check_while_suppression_is_active(
    store: _Store,
) -> None:
    _create(store)
    expired_at = TRANSFER_AT + timedelta(days=7, microseconds=1)
    store.conn.execute(
        """
        INSERT INTO cognitive_user_controls (
            control_id, topic_id, hypothesis_code, action,
            expires_at, root_fact_seq
        ) VALUES ('active-suppress', ?, ?, 'suppress', ?, 1)
        """,
        (
            TOPIC,
            retention.ACTIVE_RETENTION_HYPOTHESIS,
            _iso(expired_at + timedelta(hours=1)),
        ),
    )
    store.conn.commit()
    retention.record_cognitive_obligation_control(
        store,
        topic_id=TOPIC,
        hypothesis_code=retention.ACTIVE_RETENTION_HYPOTHESIS,
        action="suppress",
        occurred_at=_iso(expired_at - timedelta(minutes=1)),
    )

    assert retention.expire_cognitive_monitoring_episodes(
        store, as_of=_iso(expired_at)
    ) == []
    assert retention.list_cognitive_monitoring_episodes(store)[0]["status"] == "expired"
    obligations = retention.list_cognitive_learning_obligations(store)
    assert [item["obligation_type"] for item in obligations] == ["retention"]
    assert obligations[0]["status"] == "cancelled"


def test_user_controls_pause_resume_and_cancel_claimed_work(store: _Store) -> None:
    _create(store)
    due = TRANSFER_AT + timedelta(hours=24)
    claim = _claim(store, due)
    suppressed = retention.record_cognitive_obligation_control(
        store,
        topic_id=TOPIC,
        hypothesis_code=retention.ACTIVE_RETENTION_HYPOTHESIS,
        action="suppress",
        occurred_at=_iso(due + timedelta(minutes=1)),
    )
    assert suppressed == {"episodes": 1, "obligations": 1, "claims": 1}
    assert retention.list_cognitive_monitoring_episodes(store)[0]["status"] == "paused"
    assert retention.claim_cognitive_obligations(
        store,
        worker_id="other-worker",
        as_of=_iso(due + timedelta(minutes=2)),
        obligation_types=("retention",),
    ) == []
    with pytest.raises(ValueError, match="stale or not owned"):
        retention.release_cognitive_obligation_claim(
            store,
            obligation_id=str(claim["obligation_id"]),
            claim_token=str(claim["claim_token"]),
            worker_id="worker-a",
            released_at=_iso(due + timedelta(minutes=2)),
        )

    restored = retention.record_cognitive_obligation_control(
        store,
        topic_id=TOPIC,
        hypothesis_code=retention.ACTIVE_RETENTION_HYPOTHESIS,
        action="restore",
        occurred_at=_iso(due + timedelta(minutes=3)),
    )
    assert restored == {"episodes": 1, "obligations": 1, "claims": 0}
    reclaimed = _claim(store, due + timedelta(minutes=4), worker="new-worker")
    assert reclaimed["worker_id"] == "new-worker"

    deleted = retention.record_cognitive_obligation_control(
        store,
        topic_id=TOPIC,
        hypothesis_code=retention.ACTIVE_RETENTION_HYPOTHESIS,
        action="delete",
        occurred_at=_iso(due + timedelta(minutes=5)),
    )
    assert deleted == {"episodes": 1, "obligations": 1, "claims": 1}
    assert retention.list_cognitive_monitoring_episodes(store)[0]["status"] == "cancelled"
    assert retention.list_cognitive_learning_obligations(store)[0]["status"] == "cancelled"


def test_expired_suppress_resumes_without_a_new_control_event(store: _Store) -> None:
    _create(store)
    due = TRANSFER_AT + timedelta(hours=24)
    expires_at = due + timedelta(hours=1)
    store.conn.execute(
        """
        INSERT INTO cognitive_user_controls (
            control_id, topic_id, hypothesis_code, action,
            expires_at, root_fact_seq
        ) VALUES ('suppress-1', ?, ?, 'suppress', ?, 1)
        """,
        (
            TOPIC,
            retention.ACTIVE_RETENTION_HYPOTHESIS,
            _iso(expires_at),
        ),
    )
    retention.apply_cognitive_obligation_control(
        store,
        store.conn,
        topic_id=TOPIC,
        hypothesis_code=retention.ACTIVE_RETENTION_HYPOTHESIS,
        action="suppress",
        occurred_at=_iso(due),
    )
    store.conn.commit()

    assert retention.claim_cognitive_obligations(
        store,
        worker_id="still-suppressed",
        as_of=_iso(expires_at - timedelta(microseconds=1)),
        obligation_types=("retention",),
    ) == []
    resumed = retention.claim_cognitive_obligations(
        store,
        worker_id="after-expiry",
        as_of=_iso(expires_at),
        obligation_types=("retention",),
    )
    assert len(resumed) == 1
    assert retention.list_cognitive_monitoring_episodes(store)[0]["status"] == "open"


def test_transfer_check_completion_uses_same_identity_fence(store: _Store) -> None:
    _create(store)
    expired_at = TRANSFER_AT + timedelta(days=7, seconds=1)
    retention.expire_cognitive_monitoring_episodes(store, as_of=_iso(expired_at))
    claims = retention.claim_cognitive_obligations(
        store,
        worker_id="transfer-worker",
        lease_seconds=60,
        as_of=_iso(expired_at),
        obligation_types=("transfer_check",),
    )
    assert len(claims) == 1
    completed = retention.complete_cognitive_obligation_claim(
        store,
        obligation_id=str(claims[0]["obligation_id"]),
        claim_token=str(claims[0]["claim_token"]),
        worker_id="transfer-worker",
        attempt_id="new-transfer-attempt",
        completed_at=_iso(expired_at + timedelta(seconds=10)),
    )
    assert completed["status"] == "completed"


def test_study_store_initializes_retention_schema_and_binds_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package_name = "_cognitive_retention_store_integration"
    package = ModuleType(package_name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package_name, package)
    mode_manager = ModuleType(f"{package_name}.mode_manager")
    setattr(mode_manager, "normalize_mode", lambda value: str(value or "companion"))
    monkeypatch.setitem(sys.modules, mode_manager.__name__, mode_manager)
    Store = importlib.import_module(f"{package_name}.store").StudyStore
    subject = Store(tmp_path / "study.db", tmp_path / "missing-seed.json", None)
    subject.open()
    try:
        tables = {
            str(row["name"])
            for row in subject._require_read_conn()
            .execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name LIKE 'cognitive_%'
                """
            )
            .fetchall()
        }
        assert {
            "cognitive_monitoring_episode_facts",
            "cognitive_monitoring_episodes",
            "cognitive_learning_obligations",
            "cognitive_obligation_claims",
            "cognitive_obligation_satisfactions",
            "cognitive_monitoring_episode_facts",
        } <= tables
        for method_name in (
            "record_certified_transfer_success",
            "insert_certified_transfer_episode",
            "rebuild_cognitive_retention_from_transfers",
            "claim_cognitive_obligations",
            "release_cognitive_obligation_claim",
            "complete_cognitive_obligation_claim",
            "apply_cognitive_retention_disposition",
            "expire_cognitive_monitoring_episodes",
            "record_cognitive_obligation_control",
            "apply_cognitive_obligation_control",
            "list_cognitive_monitoring_episodes",
            "list_cognitive_learning_obligations",
        ):
            assert callable(getattr(subject, method_name))
        purged_tables = subject.purge_all()
        assert {
            "cognitive_monitoring_episode_facts",
            "cognitive_monitoring_episodes",
            "cognitive_learning_obligations",
            "cognitive_obligation_claims",
            "cognitive_obligation_satisfactions",
            "cognitive_outbox",
            "cognitive_delete_cutoffs",
            "cognitive_fact_roots",
        } <= set(purged_tables)
    finally:
        subject.close()
