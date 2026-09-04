"""Durable, isolated SQLite storage for Knowledge Dungeon runs."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any

from .commands import DungeonCommand, command_to_dict
from .contracts import canonical_json, canonical_sha256
from .serializer import state_hash
from .state import RunState

STORE_SCHEMA_VERSION = 1
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class DungeonStoreError(RuntimeError):
    """Base exception for fail-closed persistence failures."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class CorruptDungeonState(DungeonStoreError):
    def __init__(self, run_id: str, reason: str) -> None:
        super().__init__("corrupt_dungeon_state", f"run {run_id} is quarantined: {reason}")
        self.run_id = run_id
        self.reason = reason


class DungeonCommandConflict(DungeonStoreError):
    def __init__(self, command_id: str) -> None:
        super().__init__(
            "command_id_conflict",
            f"command_id was already used with a different request: {command_id}",
        )


class ConcurrentDungeonWrite(DungeonStoreError):
    def __init__(self, run_id: str) -> None:
        super().__init__("concurrent_state_change", f"run changed before commit: {run_id}")


@dataclass(frozen=True, slots=True)
class CommandReceipt:
    request_hash: str
    response: dict[str, Any]


class DuplicateDungeonCommand(DungeonStoreError):
    def __init__(self, receipt: CommandReceipt) -> None:
        super().__init__("duplicate_command", "command already committed")
        self.receipt = receipt


FaultHook = Callable[[str], None]


def command_fingerprint(command: DungeonCommand) -> str:
    return canonical_sha256(command_to_dict(command))


class DungeonRunStore:
    """Persist run snapshots and exact command receipts in one transaction."""

    def __init__(self, path: str | Path, *, fault_hook: FaultHook | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fault_hook = fault_hook
        self._lock = RLock()
        self._connection = sqlite3.connect(
            self.path,
            timeout=10,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._configure()
        self._initialize_schema()

    def _configure(self) -> None:
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = FULL")
        self._connection.execute("PRAGMA busy_timeout = 10000")

    def _initialize_schema(self) -> None:
        with self._lock:
            version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
            if version not in {0, STORE_SCHEMA_VERSION}:
                raise DungeonStoreError(
                    "unsupported_store_schema",
                    f"unsupported dungeon store schema: {version}",
                )
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS dungeon_runs (
                    run_id TEXT PRIMARY KEY,
                    state_version INTEGER NOT NULL CHECK (state_version >= 0),
                    state_hash TEXT NOT NULL CHECK (length(state_hash) = 64),
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS dungeon_command_receipts (
                    run_id TEXT NOT NULL,
                    command_id TEXT NOT NULL,
                    request_hash TEXT NOT NULL CHECK (length(request_hash) = 64),
                    request_json TEXT NOT NULL,
                    response_hash TEXT NOT NULL CHECK (length(response_hash) = 64),
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (run_id, command_id),
                    FOREIGN KEY (run_id) REFERENCES dungeon_runs(run_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS dungeon_quarantine (
                    quarantine_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL UNIQUE,
                    state_version INTEGER,
                    stored_hash TEXT,
                    state_json TEXT,
                    reason TEXT NOT NULL,
                    quarantined_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_dungeon_runs_updated_at
                    ON dungeon_runs(updated_at);
                """
            )
            self._connection.execute(f"PRAGMA user_version = {STORE_SCHEMA_VERSION}")

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "DungeonRunStore":
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()

    def _quarantine_reason(self, run_id: str) -> str | None:
        row = self._connection.execute(
            "SELECT reason FROM dungeon_quarantine WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return str(row["reason"]) if row is not None else None

    def _require_not_quarantined(self, run_id: str) -> None:
        reason = self._quarantine_reason(run_id)
        if reason is not None:
            raise CorruptDungeonState(run_id, reason)

    def _quarantine(self, row: sqlite3.Row, reason: str) -> None:
        run_id = str(row["run_id"])
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            self._connection.execute(
                """
                INSERT INTO dungeon_quarantine (
                    run_id, state_version, stored_hash, state_json, reason
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    state_version = excluded.state_version,
                    stored_hash = excluded.stored_hash,
                    state_json = excluded.state_json,
                    reason = excluded.reason,
                    quarantined_at = CURRENT_TIMESTAMP
                """,
                (
                    run_id,
                    row["state_version"],
                    row["state_hash"],
                    row["state_json"],
                    reason,
                ),
            )
            self._connection.execute("DELETE FROM dungeon_runs WHERE run_id = ?", (run_id,))
            self._connection.execute("COMMIT")
        except Exception:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise

    def load_run(self, run_id: str) -> RunState | None:
        with self._lock:
            self._require_not_quarantined(run_id)
            row = self._connection.execute(
                """
                SELECT run_id, state_version, state_hash, state_json
                FROM dungeon_runs
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            try:
                decoded = json.loads(str(row["state_json"]))
                if not isinstance(decoded, dict):
                    raise ValueError("state JSON is not an object")
                state = RunState.from_dict(decoded)
                if state.run_id != run_id:
                    raise ValueError("stored run_id does not match the lookup key")
                if state.state_version != int(row["state_version"]):
                    raise ValueError("stored state_version does not match the state JSON")
                calculated_hash = state_hash(state)
                if calculated_hash != str(row["state_hash"]):
                    raise ValueError("stored state hash does not match the state JSON")
            except Exception as exc:
                reason = f"invalid persisted state: {exc}"
                self._quarantine(row, reason)
                raise CorruptDungeonState(run_id, reason) from exc
            return state

    def load_receipt(self, run_id: str, command_id: str) -> CommandReceipt | None:
        with self._lock:
            self._require_not_quarantined(run_id)
            row = self._connection.execute(
                """
                SELECT request_hash, request_json, response_hash, response_json
                FROM dungeon_command_receipts
                WHERE run_id = ? AND command_id = ?
                """,
                (run_id, command_id),
            ).fetchone()
            if row is None:
                return None
            try:
                request_hash = str(row["request_hash"])
                if _SHA256_PATTERN.fullmatch(request_hash) is None:
                    raise ValueError("request hash is invalid")
                request = json.loads(str(row["request_json"]))
                if not isinstance(request, dict):
                    raise ValueError("request JSON is not an object")
                if canonical_sha256(request) != request_hash:
                    raise ValueError("request hash does not match the request JSON")
                response = json.loads(str(row["response_json"]))
                if not isinstance(response, dict):
                    raise ValueError("response JSON is not an object")
                response_hash = str(row["response_hash"])
                if _SHA256_PATTERN.fullmatch(response_hash) is None:
                    raise ValueError("response hash is invalid")
                if canonical_sha256(response) != response_hash:
                    raise ValueError("response hash does not match the response JSON")
                if response.get("accepted") is not True:
                    raise ValueError("receipt does not contain an accepted response")
                if _SHA256_PATTERN.fullmatch(str(response.get("state_hash") or "")) is None:
                    raise ValueError("response state hash is invalid")
            except Exception as exc:
                run_row = self._connection.execute(
                    """
                    SELECT run_id, state_version, state_hash, state_json
                    FROM dungeon_runs WHERE run_id = ?
                    """,
                    (run_id,),
                ).fetchone()
                reason = f"invalid command receipt {command_id}: {exc}"
                if run_row is not None:
                    self._quarantine(run_row, reason)
                raise CorruptDungeonState(run_id, reason) from exc
            return CommandReceipt(request_hash=request_hash, response=response)

    def commit_transition(
        self,
        *,
        previous_state_version: int,
        state: RunState,
        command: DungeonCommand,
        response: Mapping[str, Any],
    ) -> None:
        request = command_to_dict(command)
        request_hash = canonical_sha256(request)
        response_copy = deepcopy(dict(response))
        serialized_state = canonical_json(state.to_dict())
        serialized_request = canonical_json(request)
        serialized_response = canonical_json(response_copy)
        response_hash = canonical_sha256(response_copy)
        calculated_state_hash = state_hash(state)
        if response_copy.get("state_hash") != calculated_state_hash:
            raise DungeonStoreError(
                "persistence_contract_error",
                "response hash does not match the state being committed",
            )

        with self._lock:
            self._require_not_quarantined(state.run_id)
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                existing = self._connection.execute(
                    """
                    SELECT request_hash, response_hash, response_json
                    FROM dungeon_command_receipts
                    WHERE run_id = ? AND command_id = ?
                    """,
                    (state.run_id, command.command_id),
                ).fetchone()
                if existing is not None:
                    existing_hash = str(existing["request_hash"])
                    if existing_hash != request_hash:
                        raise DungeonCommandConflict(command.command_id)
                    existing_response = json.loads(str(existing["response_json"]))
                    if not isinstance(existing_response, dict):
                        raise CorruptDungeonState(
                            state.run_id,
                            f"invalid command receipt {command.command_id}",
                        )
                    if canonical_sha256(existing_response) != str(
                        existing["response_hash"]
                    ):
                        raise CorruptDungeonState(
                            state.run_id,
                            f"invalid command receipt {command.command_id}",
                        )
                    raise DuplicateDungeonCommand(
                        CommandReceipt(existing_hash, existing_response)
                    )

                cursor = self._connection.execute(
                    """
                    INSERT INTO dungeon_runs (
                        run_id, state_version, state_hash, state_json
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(run_id) DO UPDATE SET
                        state_version = excluded.state_version,
                        state_hash = excluded.state_hash,
                        state_json = excluded.state_json,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE dungeon_runs.state_version = ?
                    """,
                    (
                        state.run_id,
                        state.state_version,
                        calculated_state_hash,
                        serialized_state,
                        previous_state_version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ConcurrentDungeonWrite(state.run_id)
                if self._fault_hook is not None:
                    self._fault_hook("after_run_write")
                self._connection.execute(
                    """
                    INSERT INTO dungeon_command_receipts (
                        run_id, command_id, request_hash, request_json,
                        response_hash, response_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        state.run_id,
                        command.command_id,
                        request_hash,
                        serialized_request,
                        response_hash,
                        serialized_response,
                    ),
                )
                if self._fault_hook is not None:
                    self._fault_hook("after_receipt_write")
                self._connection.execute("COMMIT")
            except (DuplicateDungeonCommand, DungeonCommandConflict, ConcurrentDungeonWrite):
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise
            except CorruptDungeonState as exc:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                run_row = self._connection.execute(
                    """
                    SELECT run_id, state_version, state_hash, state_json
                    FROM dungeon_runs WHERE run_id = ?
                    """,
                    (state.run_id,),
                ).fetchone()
                if run_row is not None:
                    self._quarantine(run_row, exc.reason)
                raise
            except Exception as exc:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise DungeonStoreError(
                    "persistence_failure",
                    f"failed to commit dungeon transition: {exc}",
                ) from exc

    def list_active_run_ids(self) -> list[str]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT run_id FROM dungeon_runs ORDER BY run_id"
            ).fetchall()
            return [str(row["run_id"]) for row in rows]

    def list_quarantined_runs(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT run_id, state_version, stored_hash, reason, quarantined_at
                FROM dungeon_quarantine
                ORDER BY quarantine_id
                """
            ).fetchall()
            return [dict(row) for row in rows]
