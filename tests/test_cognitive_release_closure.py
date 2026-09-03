from __future__ import annotations

import importlib
import sqlite3
import sys
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType

import pytest

# isort: off
from adaptive_learning.cognitive_projection import CognitiveProjector
# isort: on

ROOT = Path(__file__).resolve().parents[1]
TOPIC = "calculus.chain_rule"
CODE = "omit_inner_derivative"
V1_EXTRACTOR = "cognitive-extractor-v1"
V1_MODEL = "cognitive-v1"
V21_EXTRACTOR = "cognitive-extractor-v2"
V21_MODEL = "cognitive-v2.1-1"


class _Logger:
    def debug(self, *_args: object, **_kwargs: object) -> None:
        return None

    info = debug
    warning = debug
    error = debug
    exception = debug


class _UnusedExtractor:
    async def extract(self, _input: object) -> object:
        raise AssertionError("unknown versions must fail before extraction")


def _runtime(monkeypatch: pytest.MonkeyPatch, package_name: str):
    package = ModuleType(package_name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package_name, package)
    mode_manager = ModuleType(f"{package_name}.mode_manager")
    setattr(mode_manager, "normalize_mode", lambda value: str(value or "companion"))
    monkeypatch.setitem(sys.modules, mode_manager.__name__, mode_manager)
    return (
        importlib.import_module(f"{package_name}.models"),
        importlib.import_module(f"{package_name}.store"),
        importlib.import_module(f"{package_name}.store_cognitive_outbox"),
    )


def _open_store(Store: type, database: Path):
    store = Store(database, database.with_name("missing-seed.json"), _Logger())
    store.open()
    store.ensure_topic(topic_id=TOPIC, name="Chain rule")
    return store


def _write_answer(store: object, attempt_id: str, **overrides: object):
    payload: dict[str, object] = {
        "session_id": "release-session",
        "mode": "companion",
        "topic_id": TOPIC,
        "question": {
            "question_id": f"question-{attempt_id}",
            "question": "Differentiate sin(x^2).",
            "answer": "2x cos(x^2)",
            "question_type": "math_exact",
            "difficulty": 3,
        },
        "user_answer": "2x cos(x^2)",
        "eval_result": {"verdict": "correct", "score": 100},
        "response_time_ms": 100,
        "attempt_id": attempt_id,
    }
    payload.update(overrides)
    return store.batch_write_answer_data(**payload)  # type: ignore[attr-defined]


def test_all_cognitive_gates_off_leave_normal_answer_path_inert(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    models, store_module, _ = _runtime(
        monkeypatch, "_cognitive_release_default_off"
    )
    release_config = tomllib.loads(
        (ROOT / "plugin.toml").read_text(encoding="utf-8")
    )
    cognitive = models.build_config(release_config).cognitive
    assert (
        cognitive.projection_enabled,
        cognitive.read_mode,
        cognitive.intent_policy,
        cognitive.ui_enabled,
        cognitive.retention_enabled,
    ) == (False, "off", "off", False, False)
    assert cognitive.version_set == "cognitive-v2.1-1"

    store = _open_store(store_module.StudyStore, tmp_path / "study.db")
    try:
        result = _write_answer(store, "ordinary-attempt")

        assert result == {
            "ok": True,
            "wrong_question_id": "",
            "wrong_question_attempt": {},
        }
        assert store.get_attempt_fact("ordinary-attempt") is not None
        assert store.list_cognitive_outbox() == []
        conn = store._require_read_conn()
        for table in (
            "cognitive_projection_queue",
            "cognitive_extraction_queue",
            "cognitive_evidence",
            "cognitive_monitoring_episodes",
            "cognitive_learning_obligations",
        ):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    finally:
        store.close()


def test_unknown_version_set_disables_surfaces_and_projector_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models, _, _ = _runtime(monkeypatch, "_cognitive_release_unknown_version")
    cognitive = models.CognitiveConfig(
        projection_enabled=True,
        read_mode="active",
        intent_policy="on",
        ui_enabled=True,
        retention_enabled=True,
        version_set="unknown-release-combination",
    )

    assert (
        cognitive.projection_enabled,
        cognitive.read_mode,
        cognitive.intent_policy,
        cognitive.ui_enabled,
        cognitive.retention_enabled,
        cognitive.model_version,
    ) == (False, "off", "off", False, False, "")
    with pytest.raises(ValueError, match="unsupported cognitive version set"):
        CognitiveProjector(
            object(),  # type: ignore[arg-type]
            _UnusedExtractor(),  # type: ignore[arg-type]
            version_set="unknown-release-combination",
        )


def test_restart_persists_episode_claim_and_reference_only_outbox(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, store_module, outbox_module = _runtime(
        monkeypatch, "_cognitive_release_restart"
    )
    database = tmp_path / "study.db"
    store = _open_store(store_module.StudyStore, database)
    opened_at = datetime.now(timezone.utc) - timedelta(hours=24)
    _write_answer(store, "transfer-attempt")
    created = store.record_certified_transfer_success(
        {
            "hypothesis_id": f"{TOPIC}:{CODE}",
            "topic_id": TOPIC,
            "hypothesis_code": CODE,
            "model_version": V21_MODEL,
            "source_attempt_id": "transfer-attempt",
            "source_event_id": "transfer-event",
            "question_family_id": "chain.transfer.family",
            "evaluation_verdict": "correct",
            "certified": True,
            "used_hint": False,
            "occurred_at": opened_at.isoformat().replace("+00:00", "Z"),
        }
    )
    claim = store.claim_cognitive_obligations(
        worker_id="release-worker",
        lease_seconds=600,
        as_of=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        obligation_types=("retention",),
    )[0]
    with store.transaction() as conn:
        outbox_id = outbox_module.enqueue_cognitive_projection_outbox(
            store,
            conn,
            attempt_id="transfer-attempt",
            extractor_version=V21_EXTRACTOR,
            model_version=V21_MODEL,
        )
    store.close()

    reopened = _open_store(store_module.StudyStore, database)
    try:
        episode = reopened.list_cognitive_monitoring_episodes()[0]
        obligation = reopened.list_cognitive_learning_obligations()[0]
        outbox = reopened.list_cognitive_outbox()[0]
        persisted_claim = reopened._require_read_conn().execute(
            "SELECT * FROM cognitive_obligation_claims WHERE claim_id = ?",
            (claim["claim_id"],),
        ).fetchone()

        assert episode["episode_id"] == created["episode"]["episode_id"]
        assert obligation["status"] == "claimed"
        assert obligation["current_claim_id"] == claim["claim_id"]
        assert persisted_claim["status"] == "active"
        assert persisted_claim["claim_token"] == claim["claim_token"]
        assert outbox["outbox_id"] == outbox_id
        assert outbox["status"] == "pending"
        assert outbox["payload"]["attempt_id"] == "transfer-attempt"
        assert outbox["payload"]["extractor_version"] == V21_EXTRACTOR
        assert outbox["payload"]["model_version"] == V21_MODEL
        assert set(outbox["payload"]) == {
            "attempt_id",
            "event_id",
            "extractor_version",
            "model_version",
        }
        assert "user_answer" not in outbox["payload"]
        assert "answer" not in outbox["payload"]
    finally:
        reopened.close()


def test_v025_schema_migrates_additively_and_purge_knows_new_tables(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, store_module, _ = _runtime(monkeypatch, "_cognitive_release_v025_migration")
    database = tmp_path / "study.db"
    store = _open_store(store_module.StudyStore, database)
    _write_answer(
        store,
        "legacy-attempt",
        enqueue_cognitive_projection=True,
        cognitive_extractor_version=V1_EXTRACTOR,
        cognitive_model_version=V1_MODEL,
    )
    claim = store.claim_cognitive_projections(
        limit=1, extractor_version=V1_EXTRACTOR
    )[0]
    store.complete_cognitive_projection(
        attempt_id="legacy-attempt",
        extractor_version=V1_EXTRACTOR,
        model_version=V1_MODEL,
        lease_token=claim["lease_token"],
        evidence=[
            {
                "attempt_id": "legacy-attempt",
                "topic_id": TOPIC,
                "hypothesis_code": CODE,
                "direction": "support",
                "strength": 1.0,
                "extractor_confidence": 1.0,
                "diagnosticity": 0.8,
                "source_kind": "practice",
                "evidence_span": "missing inner derivative",
                "evidence_family_id": "legacy-family",
                "session_id": "release-session",
            }
        ],
    )
    store.close()

    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        objects = connection.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL"
        ).fetchall()
        for object_type, name, sql in objects:
            if object_type == "trigger" and (
                name.startswith("trg_cognitive_root_")
                or name == "trg_cognitive_control_expiry_validate"
            ):
                connection.execute(f'DROP TRIGGER "{name}"')
            if object_type == "index" and "root_fact_seq" in str(sql):
                connection.execute(f'DROP INDEX "{name}"')
        for table in (
            "cognitive_monitoring_episode_facts",
            "cognitive_obligation_satisfactions",
            "cognitive_obligation_claims",
            "cognitive_learning_obligations",
            "cognitive_monitoring_episodes",
            "cognitive_outbox",
            "cognitive_delete_cutoffs",
            "cognitive_fact_roots",
        ):
            connection.execute(f'DROP TABLE "{table}"')
        for table in (
            "question_instances",
            "attempts",
            "cognitive_evidence",
            "cognitive_user_controls",
            "cognitive_intervention_events",
        ):
            connection.execute(f'ALTER TABLE "{table}" DROP COLUMN root_fact_seq')
        connection.commit()
    finally:
        connection.close()

    reopened = _open_store(store_module.StudyStore, database)
    try:
        attempt = reopened.get_attempt_fact("legacy-attempt")
        evidence = reopened.list_cognitive_evidence(
            topic_id=TOPIC,
            hypothesis_code=CODE,
            extractor_version=V1_EXTRACTOR,
        )
        tables = {
            row["name"]
            for row in reopened._require_read_conn()
            .execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            .fetchall()
        }

        assert attempt is not None
        assert [row["attempt_id"] for row in evidence] == ["legacy-attempt"]
        attempt_root = reopened._require_read_conn().execute(
            "SELECT root_fact_seq FROM attempts WHERE attempt_id = 'legacy-attempt'"
        ).fetchone()[0]
        assert evidence[0]["root_fact_seq"] == attempt_root
        assert {
            "cognitive_fact_roots",
            "cognitive_delete_cutoffs",
            "cognitive_outbox",
            "cognitive_monitoring_episodes",
            "cognitive_learning_obligations",
            "cognitive_obligation_claims",
            "cognitive_obligation_satisfactions",
            "cognitive_monitoring_episode_facts",
        } <= tables
        current_columns = {
            row["name"]
            for row in reopened._require_read_conn()
            .execute("PRAGMA table_info(cognitive_hypothesis_current)")
            .fetchall()
        }
        assert {"status", "user_override"} <= current_columns

        purged = reopened.purge_all()
        assert {
            "cognitive_fact_roots",
            "cognitive_delete_cutoffs",
            "cognitive_outbox",
            "cognitive_monitoring_episodes",
            "cognitive_learning_obligations",
            "cognitive_obligation_claims",
            "cognitive_obligation_satisfactions",
            "cognitive_monitoring_episode_facts",
        } <= set(purged)
    finally:
        reopened.close()


def test_web_and_electron_share_one_platform_neutral_study_panel_source() -> None:
    panel_sources = sorted((ROOT / "surfaces").rglob("study_panel.*"))
    assert panel_sources == [ROOT / "surfaces" / "study_panel.tsx"]

    panel = panel_sources[0].read_text(encoding="utf-8")
    plugin_config = (ROOT / "plugin.toml").read_text(encoding="utf-8")
    assert "export default function StudyPanel(props: PluginSurfaceProps)" in panel
    assert "@neko/plugin-ui" in panel
    assert "electron" not in panel.lower()
    assert "expose_legacy_static_panel = false" in plugin_config
