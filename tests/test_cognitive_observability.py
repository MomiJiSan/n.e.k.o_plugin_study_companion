from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from tools.cognitive_health import main
from tools.cognitive_observability import collect_cognitive_health

NOW = "2026-09-04T12:00:00.000000Z"


def _create_database(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE cognitive_extraction_queue (
            attempt_id TEXT NOT NULL,
            extractor_version TEXT NOT NULL,
            status TEXT NOT NULL,
            retry_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            lease_token TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(attempt_id, extractor_version)
        );
        CREATE TABLE cognitive_topic_projection_queue (
            topic_id TEXT NOT NULL,
            model_version TEXT NOT NULL,
            status TEXT NOT NULL,
            requested_generation INTEGER NOT NULL,
            claimed_generation INTEGER NOT NULL,
            projected_generation INTEGER NOT NULL,
            retry_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            lease_token TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(topic_id, model_version)
        );
        CREATE TABLE cognitive_outbox (
            outbox_id TEXT PRIMARY KEY,
            operation TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL,
            retry_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            lease_token TEXT NOT NULL DEFAULT '',
            lease_expires_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE cognitive_monitoring_episodes (
            episode_id TEXT PRIMARY KEY,
            model_version TEXT NOT NULL,
            status TEXT NOT NULL,
            due_by TEXT NOT NULL,
            eligibility_until TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE cognitive_learning_obligations (
            obligation_id TEXT PRIMARY KEY,
            episode_id TEXT NOT NULL,
            obligation_type TEXT NOT NULL,
            status TEXT NOT NULL,
            due_by TEXT NOT NULL,
            eligibility_until TEXT NOT NULL,
            current_claim_id TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );
        CREATE TABLE cognitive_obligation_claims (
            claim_id TEXT PRIMARY KEY,
            obligation_id TEXT NOT NULL,
            claim_token TEXT NOT NULL,
            worker_id TEXT NOT NULL,
            status TEXT NOT NULL,
            lease_expires_at TEXT NOT NULL
        );
        CREATE TABLE cognitive_user_controls (
            control_id TEXT PRIMARY KEY,
            topic_id TEXT NOT NULL,
            hypothesis_code TEXT NOT NULL,
            action TEXT NOT NULL,
            expires_at TEXT NOT NULL DEFAULT '',
            root_fact_seq INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.commit()
    return conn


@pytest.fixture
def database(tmp_path: Path) -> Path:
    path = tmp_path / "health.sqlite3"
    conn = _create_database(path)
    conn.close()
    return path


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _reason_codes(payload: dict[str, object]) -> set[str]:
    health = payload["health"]
    assert isinstance(health, dict)
    reasons = health["reasons"]
    assert isinstance(reasons, list)
    return {str(item["code"]) for item in reasons if isinstance(item, dict)}


def test_empty_current_schema_is_healthy_and_runtime_is_unknown(database: Path) -> None:
    payload = collect_cognitive_health(database, as_of=NOW).to_dict()

    assert payload["runtime"] == {
        "mode": "unknown",
        "source": "offline_database",
    }
    assert payload["health"] == {"status": "healthy", "reasons": []}
    assert payload["queues"]["extraction"]["counts"]["pending"] == 0
    assert payload["versions"]["supported_version_sets"] == [
        "cognitive-v1",
        "cognitive-v2.1-1",
    ]


def test_health_collection_does_not_change_database_or_create_sidecars(
    database: Path,
) -> None:
    before_hash = _digest(database)
    before_files = sorted(item.name for item in database.parent.iterdir())

    collect_cognitive_health(database, as_of=NOW)

    assert _digest(database) == before_hash
    assert sorted(item.name for item in database.parent.iterdir()) == before_files


def test_missing_schema_is_blocked_without_attempting_migration(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE cognitive_extraction_queue (attempt_id TEXT)")
    conn.commit()
    conn.close()
    before = _digest(path)

    payload = collect_cognitive_health(path, as_of=NOW).to_dict()

    assert payload["health"]["status"] == "blocked"
    assert _reason_codes(payload) == {
        "schema_missing_column",
        "schema_missing_table",
    }
    assert _digest(path) == before


def test_recoverable_backlog_is_degraded_and_errors_are_redacted(
    database: Path,
) -> None:
    conn = sqlite3.connect(database)
    conn.execute(
        """INSERT INTO cognitive_extraction_queue VALUES
        ('attempt-secret', 'cognitive-extractor-v1', 'processing', 2,
         'private answer leaked here', 'secret-extraction-token',
         '2026-09-04T10:00:00Z', '2026-09-04T10:00:00Z')"""
    )
    conn.execute(
        """INSERT INTO cognitive_topic_projection_queue VALUES
        ('topic-1', 'cognitive-v2.1-1', 'pending', 4, 0, 2, 0, '', '',
         '2026-09-04T11:00:00Z', '2026-09-04T11:00:00Z')"""
    )
    conn.execute(
        """INSERT INTO cognitive_outbox VALUES
        ('outbox-secret', 'projection_enqueue', '{"prompt":"private"}',
         'failed', 5, 'network timeout carrying private answer',
         'secret-outbox-token', '',
         '2026-09-04T11:00:00Z', '2026-09-04T11:00:00Z')"""
    )
    conn.execute(
        """INSERT INTO cognitive_monitoring_episodes VALUES
        ('episode-1', 'cognitive-v2.1-1', 'open',
         '2026-09-04T11:00:00Z', '2026-09-10T00:00:00Z', ? )""",
        (NOW,),
    )
    conn.execute(
        """INSERT INTO cognitive_learning_obligations VALUES
        ('obligation-1', 'episode-1', 'retention', 'pending',
         '2026-09-04T11:00:00Z', '2026-09-10T00:00:00Z', '', ?)""",
        (NOW,),
    )
    conn.commit()
    conn.close()

    payload = collect_cognitive_health(database, as_of=NOW).to_dict()
    serialized = json.dumps(payload, sort_keys=True)

    assert payload["health"]["status"] == "degraded"
    assert {
        "extraction_lease_expired",
        "projection_stale",
        "outbox_failed",
        "obligation_overdue",
    }.issubset(_reason_codes(payload))
    assert payload["outbox"]["error_categories"] == {"timeout": 1}
    assert payload["queues"]["topic_projection"]["generation_gap_total"] == 2
    for secret in (
        "private answer",
        "private",
        "secret-extraction-token",
        "secret-outbox-token",
        "attempt-secret",
        "outbox-secret",
    ):
        assert secret not in serialized


def test_invalid_versions_and_claim_ownership_are_blocked(database: Path) -> None:
    conn = sqlite3.connect(database)
    conn.execute(
        """INSERT INTO cognitive_topic_projection_queue VALUES
        ('topic-1', 'unknown-version', 'done', 1, 1, 1, 0, '', '', ?, ?)""",
        (NOW, NOW),
    )
    conn.execute(
        """INSERT INTO cognitive_monitoring_episodes VALUES
        ('episode-1', 'cognitive-v2.1-1', 'open', ?, ?, ?)""",
        (NOW, "2026-09-10T00:00:00Z", NOW),
    )
    conn.execute(
        """INSERT INTO cognitive_learning_obligations VALUES
        ('obligation-1', 'episode-1', 'retention', 'pending', ?, ?, '', ?)""",
        (NOW, "2026-09-10T00:00:00Z", NOW),
    )
    conn.execute(
        """INSERT INTO cognitive_obligation_claims VALUES
        ('claim-secret', 'obligation-1', 'secret-claim-token', 'worker-secret',
         'active', '2026-09-05T00:00:00Z')"""
    )
    conn.commit()
    conn.close()

    payload = collect_cognitive_health(database, as_of=NOW).to_dict()
    serialized = json.dumps(payload, sort_keys=True)

    assert payload["health"]["status"] == "blocked"
    assert {"unsupported_version", "claim_ownership_mismatch"}.issubset(
        _reason_codes(payload)
    )
    assert "secret-claim-token" not in serialized
    assert "worker-secret" not in serialized
    assert "claim-secret" not in serialized


def test_controls_count_only_latest_current_action(database: Path) -> None:
    conn = sqlite3.connect(database)
    rows = (
        ("c1", "t1", "h1", "dismiss", "", 1, "2026-09-01T00:00:00Z"),
        ("c2", "t1", "h1", "restore", "", 2, "2026-09-02T00:00:00Z"),
        ("c3", "t2", "h2", "suppress", "2026-09-04T11:00:00Z", 3, NOW),
        ("c4", "t3", "h3", "suppress", "2026-09-05T00:00:00Z", 4, NOW),
        ("c5", "t4", "h4", "delete", "", 5, NOW),
    )
    conn.executemany(
        "INSERT INTO cognitive_user_controls VALUES (?, ?, ?, ?, ?, ?, ?)", rows
    )
    conn.commit()
    conn.close()

    payload = collect_cognitive_health(database, as_of=NOW).to_dict()

    assert payload["controls"] == {"dismiss": 0, "suppress": 1, "delete": 1}


@pytest.mark.parametrize(
    ("setup", "expected"),
    (("healthy", 0), ("degraded", 1), ("blocked", 2), ("missing", 3)),
)
def test_cli_exit_codes_and_json_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    setup: str,
    expected: int,
) -> None:
    path = tmp_path / f"{setup}.sqlite3"
    if setup != "missing":
        conn = _create_database(path)
        if setup == "degraded":
            conn.execute(
                """INSERT INTO cognitive_outbox VALUES
                ('o1', 'projection_enqueue', '{}', 'discarded', 5,
                 'timeout', '', '', ?, ?)""",
                (NOW, NOW),
            )
        elif setup == "blocked":
            conn.execute("DROP TABLE cognitive_obligation_claims")
        conn.commit()
        conn.close()

    result = main(["--db", str(path), "--format", "json", "--as-of", NOW])
    captured = capsys.readouterr()

    assert result == expected
    if expected == 3:
        assert json.loads(captured.err) == {"error": {"code": "inspection_failed"}}
    else:
        assert json.loads(captured.out)["health"]["status"] == setup
