"""Private loopback bridge owned by the Study Companion plugin.

This module deliberately exposes a Knowledge Dungeon domain protocol rather
than a generic local-application dispatch surface.  Pairing material is
published through a user-private rendezvous file while an OS advisory lock
provides the cross-process ownership fence for that file.
"""

from __future__ import annotations

import asyncio
import ctypes
import hmac
import json
import os
import re
import socket
import sys
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import import_module
from pathlib import Path
from secrets import token_hex, token_urlsafe
from typing import Any, BinaryIO, Callable, Iterator, Mapping

from .bridge_contracts import (
    BRIDGE_PROTOCOL_VERSION,
    REQUIRED_DUNGEON_SCOPE,
    TrustedInvocationContext,
    require_identifier,
)
from .host_adapter import KnowledgeDungeonHostAdapter
from .persistence import DungeonRunStore

_BIND_HOST = "127.0.0.1"
_RENDEZVOUS_FILENAME = "bridge-v1.json"
_LOCK_FILENAME = "bridge-v1.lock"
_RUNTIME_OVERRIDE = "NEKO_KNOWLEDGE_DUNGEON_RUNTIME_DIR"
_INSTANCE_PATTERN = re.compile(r"^bridge-[0-9a-f]{32}$")
_LAUNCH_CODE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
_ACCESS_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
_CLIENT_ID_PATTERN = re.compile(r"^client-[0-9a-f]{32}$")
_PAIR_FIELDS = frozenset(
    ("protocol_version", "bridge_instance_id", "generation", "client_id", "launch_code")
)
_RENDEZVOUS_FIELDS = frozenset(
    (
        "protocol_version",
        "bridge_instance_id",
        "generation",
        "port",
        "launch_code",
        "expires_at",
        "owner_pid",
    )
)
_DOMAIN_PATHS = {
    "/v1/bootstrap": "bootstrap",
    "/v1/runs/create": "create_run",
    "/v1/runs/get": "get_run",
    "/v1/runs/action": "perform_action",
}

ACCESS_TOKEN_EXPIRES_IN = 1800
SESSION_IDLE_TIMEOUT = 1800
SESSION_ABSOLUTE_TIMEOUT = 43200
LAUNCH_CODE_TTL = 300
LAUNCH_CODE_RENEWAL_MARGIN = 60
LAUNCH_CODE_RENEWAL_INTERVAL = 15.0
MAX_BODY_BYTES = 64 * 1024
MAX_HEADER_BYTES = 8 * 1024
MAX_HEADER_COUNT = 32
MAX_CONCURRENT_REQUESTS = 8
MAX_PAIR_FAILURES = 5
PAIR_FAILURE_WINDOW = 60.0
REQUEST_TIMEOUT = 5.0
SHUTDOWN_TIMEOUT = 6.0


class BridgeUnavailableError(RuntimeError):
    """The optional private bridge could not start or continue safely."""


class RendezvousOwnershipError(BridgeUnavailableError):
    """Another process owns the Knowledge Dungeon rendezvous directory."""


class RendezvousPublishError(BridgeUnavailableError):
    """Pairing material could not be atomically published."""


class _BridgeRequestError(ValueError):
    def __init__(self, status: HTTPStatus, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass(frozen=True, slots=True)
class RendezvousRecord:
    protocol_version: int
    bridge_instance_id: str
    generation: int
    port: int
    launch_code: str
    expires_at: int
    owner_pid: int

    def __post_init__(self) -> None:
        if self.protocol_version != BRIDGE_PROTOCOL_VERSION:
            raise ValueError("protocol_version is unsupported")
        if _INSTANCE_PATTERN.fullmatch(self.bridge_instance_id) is None:
            raise ValueError("bridge_instance_id is invalid")
        if isinstance(self.generation, bool) or not isinstance(self.generation, int) or self.generation < 1:
            raise ValueError("generation must be a positive integer")
        if isinstance(self.port, bool) or not isinstance(self.port, int) or not 1 <= self.port <= 65535:
            raise ValueError("port is invalid")
        if _LAUNCH_CODE_PATTERN.fullmatch(self.launch_code) is None:
            raise ValueError("launch_code is invalid")
        if isinstance(self.expires_at, bool) or not isinstance(self.expires_at, int) or self.expires_at < 1:
            raise ValueError("expires_at is invalid")
        if isinstance(self.owner_pid, bool) or not isinstance(self.owner_pid, int) or self.owner_pid < 1:
            raise ValueError("owner_pid is invalid")

    def to_dict(self) -> dict[str, object]:
        result = {
            "protocol_version": self.protocol_version,
            "bridge_instance_id": self.bridge_instance_id,
            "generation": self.generation,
            "port": self.port,
            "launch_code": self.launch_code,
            "expires_at": self.expires_at,
            "owner_pid": self.owner_pid,
        }
        if frozenset(result) != _RENDEZVOUS_FIELDS:
            raise AssertionError("rendezvous schema drifted")
        return result


@dataclass(slots=True)
class _Session:
    client_id: str
    created_at: float
    last_used_at: float


class RuntimeOwnershipLock:
    """Hold one cross-process advisory lock for the entire bridge lifetime."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._file: BinaryIO | None = None
        self._held = False
        self._guard = threading.RLock()

    @property
    def held(self) -> bool:
        with self._guard:
            return self._held

    def acquire(self) -> None:
        with self._guard:
            if self._held:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            file = self.path.open("a+b")
            try:
                file.seek(0, os.SEEK_END)
                if file.tell() == 0:
                    file.write(b"\0")
                    file.flush()
                    os.fsync(file.fileno())
                file.seek(0)
                if os.name == "nt":
                    msvcrt = import_module("msvcrt")
                    msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl = import_module("fcntl")
                    fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except Exception as exc:
                file.close()
                raise RendezvousOwnershipError(
                    "another Knowledge Dungeon bridge owns the runtime directory"
                ) from exc
            self._file = file
            self._held = True

    def release(self) -> None:
        with self._guard:
            file = self._file
            if file is None or not self._held:
                return
            try:
                file.seek(0)
                if os.name == "nt":
                    msvcrt = import_module("msvcrt")
                    msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl = import_module("fcntl")
                    fcntl.flock(file.fileno(), fcntl.LOCK_UN)
            finally:
                self._held = False
                self._file = None
                file.close()

    @contextmanager
    def commit_guard(self) -> Iterator[None]:
        """Fence the final filesystem mutation against ownership release."""

        with self._guard:
            if not self._held:
                raise RendezvousOwnershipError("rendezvous ownership is not held")
            yield

    def __enter__(self) -> RuntimeOwnershipLock:
        self.acquire()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.release()


class RendezvousPublisher:
    """Publish and remove rendezvous state only while ownership is held."""

    def __init__(self, runtime_dir: str | Path, ownership: RuntimeOwnershipLock) -> None:
        self.runtime_dir = Path(runtime_dir)
        self.path = self.runtime_dir / _RENDEZVOUS_FILENAME
        self._ownership = ownership
        self._accepting_commits = False

    def begin(self) -> None:
        with self._ownership.commit_guard():
            self._accepting_commits = True

    def publish(self, record: RendezvousRecord) -> None:
        self._require_ownership()
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(self.runtime_dir, 0o700)
        temporary_path = self.runtime_dir / f".{_RENDEZVOUS_FILENAME}.{token_hex(8)}.tmp"
        payload = json.dumps(record.to_dict(), separators=(",", ":"), sort_keys=True) + "\n"
        descriptor: int | None = None
        try:
            descriptor = os.open(temporary_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                descriptor = None
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            with self._ownership.commit_guard():
                if not self._accepting_commits:
                    raise RendezvousOwnershipError(
                        "rendezvous publisher is no longer active"
                    )
                os.replace(temporary_path, self.path)
                if os.name != "nt":
                    os.chmod(self.path, 0o600)
        except Exception as exc:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise RendezvousPublishError("failed to publish Knowledge Dungeon rendezvous") from exc

    def remove(self, expected: RendezvousRecord) -> bool:
        """Remove only the rendezvous generation still owned by this bridge.

        The advisory lock prevents a cooperating second bridge from publishing,
        while this identity check prevents cleanup from deleting a file that was
        replaced independently after this process published it.
        """

        self._require_ownership()
        try:
            with self._ownership.commit_guard():
                self._accepting_commits = False
                current = self._read_strict_record()
                if current is None or (
                    not hmac.compare_digest(
                        current.bridge_instance_id, expected.bridge_instance_id
                    )
                    or current.generation != expected.generation
                ):
                    return False
                self.path.unlink(missing_ok=True)
                return True
        except OSError as exc:
            raise RendezvousPublishError("failed to remove Knowledge Dungeon rendezvous") from exc

    def _read_strict_record(self) -> RendezvousRecord | None:
        try:
            raw = self.path.read_text(encoding="utf-8")
            payload = json.loads(
                raw,
                object_pairs_hook=self._strict_object,
                parse_constant=self._reject_json_constant,
            )
            if not isinstance(payload, dict) or frozenset(payload) != _RENDEZVOUS_FIELDS:
                return None
            return RendezvousRecord(**payload)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    @staticmethod
    def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate rendezvous key: {key}")
            result[key] = value
        return result

    @staticmethod
    def _reject_json_constant(value: str) -> object:
        raise ValueError(f"non-finite rendezvous value: {value}")

    def _require_ownership(self) -> None:
        if not self._ownership.held:
            raise RendezvousOwnershipError("rendezvous ownership is not held")


def default_runtime_directory() -> Path:
    override = os.environ.get(_RUNTIME_OVERRIDE)
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        return root / "Project-N-E-K-O" / "KnowledgeDungeon" / "runtime"
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "Project-N-E-K-O"
            / "KnowledgeDungeon"
            / "runtime"
        )
    runtime_root = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_root:
        return Path(runtime_root) / "project-n-e-k-o" / "knowledge-dungeon"
    state_root = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))
    return state_root / "project-n-e-k-o" / "knowledge-dungeon"


def harden_runtime_directory(path: str | Path) -> None:
    """Restrict runtime material to this user and Windows system operators."""

    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(directory, 0o700)
        return
    try:
        from ctypes import wintypes

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class SidAndAttributes(ctypes.Structure):
            _fields_ = [("sid", ctypes.c_void_p), ("attributes", wintypes.DWORD)]

        class TokenUser(ctypes.Structure):
            _fields_ = [("user", SidAndAttributes)]

        class Trustee(ctypes.Structure):
            _fields_ = [
                ("multiple_trustee", ctypes.c_void_p),
                ("multiple_trustee_operation", ctypes.c_int),
                ("trustee_form", ctypes.c_int),
                ("trustee_type", ctypes.c_int),
                ("name", ctypes.c_void_p),
            ]

        class ExplicitAccess(ctypes.Structure):
            _fields_ = [
                ("access_permissions", wintypes.DWORD),
                ("access_mode", ctypes.c_int),
                ("inheritance", wintypes.DWORD),
                ("trustee", Trustee),
            ]

        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        advapi32.ConvertStringSidToSidW.argtypes = [
            wintypes.LPCWSTR,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
        advapi32.SetEntriesInAclW.argtypes = [
            wintypes.ULONG,
            ctypes.POINTER(ExplicitAccess),
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        advapi32.SetEntriesInAclW.restype = wintypes.DWORD
        advapi32.SetNamedSecurityInfoW.argtypes = [
            wintypes.LPWSTR,
            ctypes.c_int,
            wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD

        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error())
        allocated_sids: list[ctypes.c_void_p] = []
        acl = ctypes.c_void_p()
        try:
            needed = wintypes.DWORD()
            advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
            if needed.value == 0:
                raise ctypes.WinError(ctypes.get_last_error())
            token_buffer = ctypes.create_string_buffer(needed.value)
            if not advapi32.GetTokenInformation(
                token, 1, token_buffer, needed, ctypes.byref(needed)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            current_sid = ctypes.cast(token_buffer, ctypes.POINTER(TokenUser)).contents.user.sid

            for sid_text in ("S-1-5-18", "S-1-5-32-544"):
                sid = ctypes.c_void_p()
                if not advapi32.ConvertStringSidToSidW(sid_text, ctypes.byref(sid)):
                    raise ctypes.WinError(ctypes.get_last_error())
                allocated_sids.append(sid)

            sid_entries = (current_sid, allocated_sids[0].value, allocated_sids[1].value)
            trustee_types = (1, 5, 5)  # user, well-known group, well-known group
            entries = (ExplicitAccess * 3)()
            for index, (sid, trustee_type) in enumerate(zip(sid_entries, trustee_types, strict=True)):
                entries[index].access_permissions = 0x1F01FF  # FILE_ALL_ACCESS
                entries[index].access_mode = 2  # SET_ACCESS
                entries[index].inheritance = 0x3  # object and container inheritance
                entries[index].trustee = Trustee(None, 0, 0, trustee_type, sid)
            result = advapi32.SetEntriesInAclW(3, entries, None, ctypes.byref(acl))
            if result != 0:
                raise OSError(result, "SetEntriesInAclW failed")
            result = advapi32.SetNamedSecurityInfoW(
                str(directory),
                1,  # SE_FILE_OBJECT
                0x00000004 | 0x80000000,  # DACL + protected DACL
                None,
                None,
                acl,
                None,
            )
            if result != 0:
                raise OSError(result, "SetNamedSecurityInfoW failed")
        finally:
            if acl.value:
                kernel32.LocalFree(acl)
            for sid in allocated_sids:
                kernel32.LocalFree(sid)
            kernel32.CloseHandle(token)
    except Exception as exc:
        raise BridgeUnavailableError("failed to secure Knowledge Dungeon runtime directory") from exc


class _BridgeState:
    def __init__(
        self,
        *,
        adapter: KnowledgeDungeonHostAdapter,
        publisher: RendezvousPublisher,
        bridge_instance_id: str,
        port: int,
        clock: Any = time.time,
        monotonic: Any = time.monotonic,
    ) -> None:
        self.adapter = adapter
        self.publisher = publisher
        self.bridge_instance_id = bridge_instance_id
        self.port = port
        self._clock = clock
        self._monotonic = monotonic
        self._lock = threading.RLock()
        self._request_condition = threading.Condition(threading.Lock())
        self._request_slots = threading.BoundedSemaphore(MAX_CONCURRENT_REQUESTS)
        self._failed_pair_attempts: deque[float] = deque()
        self._session_lock = threading.RLock()
        self._sessions: dict[str, _Session] = {}
        self._client_tokens: dict[str, str] = {}
        self._record: RendezvousRecord | None = None
        self._draining = threading.Event()
        self._active_requests = 0

    @property
    def record(self) -> RendezvousRecord:
        with self._lock:
            if self._record is None:
                raise BridgeUnavailableError("bridge rendezvous is not ready")
            return self._record

    def initialize(self) -> RendezvousRecord:
        with self._lock:
            record = self._new_record(1)
            self.publisher.publish(record)
            self._record = record
            return record

    def begin_request(self) -> bool:
        if not self._request_slots.acquire(blocking=False):
            return False
        with self._request_condition:
            if self._draining.is_set():
                self._request_slots.release()
                return False
            self._active_requests += 1
            return True

    def end_request(self) -> None:
        with self._request_condition:
            if self._draining.is_set():
                self.clear_sessions()
            self._active_requests -= 1
            self._request_condition.notify_all()
        self._request_slots.release()

    def begin_draining(self) -> None:
        self._draining.set()

    def wait_for_requests(self, timeout: float) -> bool:
        deadline = self._monotonic() + timeout
        with self._request_condition:
            while self._active_requests:
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    return False
                self._request_condition.wait(remaining)
            return True

    def clear_sessions(self) -> None:
        with self._session_lock:
            self._sessions.clear()
            self._client_tokens.clear()

    def renew_launch_code_if_needed(self) -> bool:
        """Refresh pairing material before expiry without disturbing a session."""

        with self._lock:
            if self._draining.is_set():
                return False
            current = self.record
            if int(self._clock()) < current.expires_at - LAUNCH_CODE_RENEWAL_MARGIN:
                return False
            self._rotate_record(current)
            return True

    def pair(self, payload: object) -> dict[str, object]:
        self._require_not_draining()
        raw = self._require_exact_mapping(payload, _PAIR_FIELDS, "pair request")
        if raw["protocol_version"] != BRIDGE_PROTOCOL_VERSION:
            self._pair_failure()
            raise _BridgeRequestError(
                HTTPStatus.BAD_REQUEST, "unsupported_protocol", "protocol_version is unsupported"
            )
        instance_id = raw["bridge_instance_id"]
        generation = raw["generation"]
        launch_code = raw["launch_code"]
        try:
            client_id = require_identifier(raw["client_id"], "client_id")
            if _CLIENT_ID_PATTERN.fullmatch(client_id) is None:
                raise ValueError("client_id is invalid")
        except (TypeError, ValueError) as exc:
            self._pair_failure()
            raise _BridgeRequestError(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc)) from exc
        if not isinstance(instance_id, str) or _INSTANCE_PATTERN.fullmatch(instance_id) is None:
            self._pair_failure()
            raise _BridgeRequestError(
                HTTPStatus.BAD_REQUEST, "invalid_request", "bridge_instance_id is invalid"
            )
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            self._pair_failure()
            raise _BridgeRequestError(HTTPStatus.BAD_REQUEST, "invalid_request", "generation is invalid")
        if not isinstance(launch_code, str) or _LAUNCH_CODE_PATTERN.fullmatch(launch_code) is None:
            self._pair_failure()
            raise _BridgeRequestError(HTTPStatus.BAD_REQUEST, "invalid_request", "launch_code is invalid")

        with self._lock:
            self._require_not_draining()
            self._require_pair_attempt_available()
            record = self.record
            if int(self._clock()) >= record.expires_at:
                self._rotate_record(record)
                self._record_pair_failure_locked()
                raise _BridgeRequestError(
                    HTTPStatus.UNAUTHORIZED, "launch_code_expired", "launch code has expired"
                )
            if (
                not hmac.compare_digest(instance_id, record.bridge_instance_id)
                or generation != record.generation
                or not hmac.compare_digest(launch_code, record.launch_code)
            ):
                self._record_pair_failure_locked()
                raise _BridgeRequestError(
                    HTTPStatus.UNAUTHORIZED, "invalid_launch_code", "pairing material is invalid"
                )

            access_token = token_urlsafe(32)
            if _ACCESS_TOKEN_PATTERN.fullmatch(access_token) is None:
                raise BridgeUnavailableError("generated access token is invalid")
            consumed_generation = record.generation
            # Publishing the replacement is the commit point.  Until it
            # succeeds, the old one-time code remains valid and retryable.
            self._rotate_record(record)
            now = self._monotonic()
            with self._session_lock:
                self._require_not_draining()
                self._sessions.clear()
                self._client_tokens.clear()
                self._sessions[access_token] = _Session(client_id, now, now)
                self._client_tokens[client_id] = access_token
            self._failed_pair_attempts.clear()
            return {
                "ok": True,
                "protocol_version": BRIDGE_PROTOCOL_VERSION,
                "bridge_instance_id": self.bridge_instance_id,
                "generation": consumed_generation,
                "client_id": client_id,
                "access_token": access_token,
                "access_token_expires_in": ACCESS_TOKEN_EXPIRES_IN,
                "session_idle_timeout": SESSION_IDLE_TIMEOUT,
                "session_absolute_timeout": SESSION_ABSOLUTE_TIMEOUT,
            }

    def authorize(self, authorization: str | None) -> TrustedInvocationContext:
        self._require_not_draining()
        if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
            raise _BridgeRequestError(
                HTTPStatus.UNAUTHORIZED, "authentication_required", "Bearer access token is required"
            )
        token = authorization.removeprefix("Bearer ")
        if _ACCESS_TOKEN_PATTERN.fullmatch(token) is None:
            raise _BridgeRequestError(
                HTTPStatus.UNAUTHORIZED, "invalid_access_token", "access token is invalid"
            )
        with self._session_lock:
            self._require_not_draining()
            session = self._sessions.get(token)
            now = self._monotonic()
            if session is None:
                raise _BridgeRequestError(
                    HTTPStatus.UNAUTHORIZED, "invalid_access_token", "access token is invalid"
                )
            if (
                now - session.created_at >= ACCESS_TOKEN_EXPIRES_IN
                or
                now - session.last_used_at >= SESSION_IDLE_TIMEOUT
                or now - session.created_at >= SESSION_ABSOLUTE_TIMEOUT
            ):
                self._sessions.pop(token, None)
                if self._client_tokens.get(session.client_id) == token:
                    self._client_tokens.pop(session.client_id, None)
                raise _BridgeRequestError(
                    HTTPStatus.UNAUTHORIZED, "access_token_expired", "access token has expired"
                )
            session.last_used_at = now
            return TrustedInvocationContext(
                client_id=session.client_id,
                scope=REQUIRED_DUNGEON_SCOPE,
            )

    def _rotate_record(self, current: RendezvousRecord) -> RendezvousRecord:
        self._require_not_draining()
        replacement = self._new_record(current.generation + 1)
        try:
            self.publisher.publish(replacement)
        except RendezvousPublishError as exc:
            raise _BridgeRequestError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "rendezvous_unavailable",
                "pairing material could not be refreshed",
            ) from exc
        self._record = replacement
        self._require_not_draining()
        return replacement

    def _require_not_draining(self) -> None:
        if self._draining.is_set():
            raise _BridgeRequestError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "bridge_unavailable",
                "Knowledge Dungeon bridge is unavailable",
            )

    def _new_record(self, generation: int) -> RendezvousRecord:
        return RendezvousRecord(
            protocol_version=BRIDGE_PROTOCOL_VERSION,
            bridge_instance_id=self.bridge_instance_id,
            generation=generation,
            port=self.port,
            launch_code=token_urlsafe(32),
            expires_at=int(self._clock()) + LAUNCH_CODE_TTL,
            owner_pid=os.getpid(),
        )

    def _pair_failure(self) -> None:
        with self._lock:
            self._require_pair_attempt_available()
            self._record_pair_failure_locked()

    def _require_pair_attempt_available(self) -> None:
        now = self._monotonic()
        while self._failed_pair_attempts and now - self._failed_pair_attempts[0] >= PAIR_FAILURE_WINDOW:
            self._failed_pair_attempts.popleft()
        if len(self._failed_pair_attempts) >= MAX_PAIR_FAILURES:
            raise _BridgeRequestError(
                HTTPStatus.TOO_MANY_REQUESTS, "pairing_rate_limited", "too many pairing attempts"
            )

    def _record_pair_failure_locked(self) -> None:
        self._failed_pair_attempts.append(self._monotonic())

    @staticmethod
    def _require_exact_mapping(
        payload: object, expected: frozenset[str], name: str
    ) -> Mapping[str, Any]:
        if not isinstance(payload, Mapping) or not all(isinstance(key, str) for key in payload):
            raise _BridgeRequestError(HTTPStatus.BAD_REQUEST, "invalid_request", f"{name} must be an object")
        if frozenset(payload) != expected:
            raise _BridgeRequestError(HTTPStatus.BAD_REQUEST, "invalid_request", f"{name} fields are invalid")
        return payload


class _PrivateBridgeHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 16
    allow_reuse_address = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.bridge_state: _BridgeState | None = None
        self._worker_slots = threading.BoundedSemaphore(MAX_CONCURRENT_REQUESTS)
        self._worker_count_lock = threading.Lock()
        self._worker_condition = threading.Condition(self._worker_count_lock)
        self._active_workers = 0
        super().__init__(*args, **kwargs)

    @property
    def active_worker_count(self) -> int:
        with self._worker_count_lock:
            return self._active_workers

    def process_request(self, request: socket.socket, client_address: Any) -> None:
        if not self._worker_slots.acquire(blocking=False):
            self._reject_overloaded_connection(request)
            self.shutdown_request(request)
            return
        with self._worker_count_lock:
            self._active_workers += 1
        try:
            super().process_request(request, client_address)
        except Exception:
            self._release_worker_slot()
            raise

    def process_request_thread(self, request: socket.socket, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release_worker_slot()

    def _release_worker_slot(self) -> None:
        with self._worker_condition:
            self._active_workers -= 1
            self._worker_condition.notify_all()
        self._worker_slots.release()

    def wait_for_workers(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self._worker_condition:
            while self._active_workers:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._worker_condition.wait(remaining)
            return True

    @staticmethod
    def _reject_overloaded_connection(request: socket.socket) -> None:
        body = b'{"ok":false,"error":{"code":"bridge_busy","message":"too many concurrent requests"}}'
        response = (
            b"HTTP/1.1 503 Service Unavailable\r\n"
            b"Content-Type: application/json; charset=utf-8\r\n"
            + f"Content-Length: {len(body)}\r\n".encode()
            + b"Cache-Control: no-store\r\nConnection: close\r\n\r\n"
            + body
        )
        try:
            request.settimeout(1.0)
            request.sendall(response)
        except OSError:
            pass


class KnowledgeDungeonPrivateBridge:
    """Bounded-lifecycle private HTTP bridge for one plugin process."""

    def __init__(
        self,
        store_path: str | Path,
        *,
        runtime_dir: str | Path | None = None,
        logger: Any | None = None,
        runtime_hardener: Callable[[str | Path], None] | None = None,
    ) -> None:
        self._store_path = Path(store_path)
        self.runtime_dir = Path(runtime_dir) if runtime_dir is not None else default_runtime_directory()
        self._logger = logger
        self._runtime_hardener = runtime_hardener or harden_runtime_directory
        self._ownership = RuntimeOwnershipLock(self.runtime_dir / _LOCK_FILENAME)
        self._publisher = RendezvousPublisher(self.runtime_dir, self._ownership)
        self._server: _PrivateBridgeHttpServer | None = None
        self._thread: threading.Thread | None = None
        self._renewal_thread: threading.Thread | None = None
        self._renewal_stop = threading.Event()
        self._state: _BridgeState | None = None

    @property
    def rendezvous_path(self) -> Path:
        return self._publisher.path

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def record(self) -> RendezvousRecord:
        state = self._state
        if state is None:
            raise BridgeUnavailableError("bridge is not running")
        return state.record

    def start(self) -> RendezvousRecord:
        if self.running:
            return self.record
        self._runtime_hardener(self.runtime_dir)
        try:
            with DungeonRunStore(self._store_path):
                pass
        except Exception as exc:
            raise BridgeUnavailableError(
                "Knowledge Dungeon storage preflight failed"
            ) from exc
        self._ownership.acquire()
        self._publisher.begin()
        server: _PrivateBridgeHttpServer | None = None
        state: _BridgeState | None = None
        thread: threading.Thread | None = None
        renewal_thread: threading.Thread | None = None
        try:
            server = _PrivateBridgeHttpServer((_BIND_HOST, 0), self._handler_type())
            server.timeout = REQUEST_TIMEOUT
            port = int(server.server_address[1])
            state = _BridgeState(
                adapter=KnowledgeDungeonHostAdapter(self._store_path),
                publisher=self._publisher,
                bridge_instance_id=f"bridge-{token_hex(16)}",
                port=port,
            )
            record = state.initialize()
            server.bridge_state = state
            self._server = server
            self._state = state
            thread = threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.1},
                name="study-companion-knowledge-dungeon-bridge",
                daemon=True,
            )
            self._renewal_stop.clear()
            renewal_thread = threading.Thread(
                target=self._run_launch_code_renewal,
                args=(state,),
                name="study-companion-knowledge-dungeon-rendezvous-renewal",
                daemon=True,
            )
            thread.start()
            self._thread = thread
            if not thread.is_alive():
                raise BridgeUnavailableError("Knowledge Dungeon bridge listener did not start")
            renewal_thread.start()
            self._renewal_thread = renewal_thread
            if not renewal_thread.is_alive():
                raise BridgeUnavailableError("Knowledge Dungeon rendezvous renewal did not start")
            return record
        except Exception:
            self._cleanup_failed_start(
                server=server,
                state=state,
                listener_thread=thread,
                renewal_thread=renewal_thread,
            )
            raise

    def stop(self, timeout: float = SHUTDOWN_TIMEOUT) -> bool:
        state = self._state
        server = self._server
        thread = self._thread
        renewal_thread = self._renewal_thread
        self._state = None
        self._server = None
        self._thread = None
        self._renewal_thread = None
        deadline = time.monotonic() + max(0.0, timeout)
        drained = True
        try:
            if state is not None:
                state.begin_draining()
                state.clear_sessions()
            self._renewal_stop.set()
            if renewal_thread is not None:
                drained = self._join_if_started(
                    renewal_thread,
                    max(0.0, deadline - time.monotonic()),
                ) and drained
            if server is not None and thread is not None and thread.is_alive():
                server.shutdown()
            if state is not None:
                drained = state.wait_for_requests(max(0.0, deadline - time.monotonic()))
                state.clear_sessions()
            if server is not None:
                drained = server.wait_for_workers(
                    max(0.0, deadline - time.monotonic())
                ) and drained
            if server is not None:
                server.server_close()
            if thread is not None:
                drained = self._join_if_started(
                    thread,
                    max(0.0, deadline - time.monotonic()),
                ) and drained
            if self._ownership.held:
                try:
                    if state is not None:
                        self._publisher.remove(state.record)
                except RendezvousPublishError as exc:
                    self._warn("knowledge dungeon rendezvous cleanup failed: {}", exc)
            return drained
        finally:
            self._ownership.release()

    def _cleanup_failed_start(
        self,
        *,
        server: _PrivateBridgeHttpServer | None,
        state: _BridgeState | None,
        listener_thread: threading.Thread | None,
        renewal_thread: threading.Thread | None,
    ) -> None:
        """Rollback every acquired resource without one failure hiding the next."""

        self._renewal_stop.set()
        try:
            if state is not None:
                state.begin_draining()
                state.clear_sessions()
        except Exception as exc:
            self._warn("knowledge dungeon state rollback failed: {}", exc)
        try:
            self._join_if_started(renewal_thread, 1.0)
        except Exception as exc:
            self._warn("knowledge dungeon renewal cleanup failed: {}", exc)
        try:
            if (
                server is not None
                and listener_thread is not None
                and listener_thread.is_alive()
            ):
                server.shutdown()
        except Exception as exc:
            self._warn("knowledge dungeon listener shutdown failed: {}", exc)
        try:
            if server is not None:
                server.server_close()
        except Exception as exc:
            self._warn("knowledge dungeon listener close failed: {}", exc)
        try:
            self._join_if_started(listener_thread, 1.0)
        except Exception as exc:
            self._warn("knowledge dungeon listener join failed: {}", exc)
        try:
            if self._ownership.held and state is not None:
                self._publisher.remove(state.record)
        except Exception as exc:
            self._warn("knowledge dungeon rendezvous rollback failed: {}", exc)
        try:
            self._ownership.release()
        except Exception as exc:
            self._warn("knowledge dungeon ownership release failed: {}", exc)
        finally:
            self._server = None
            self._thread = None
            self._renewal_thread = None
            self._state = None

    @staticmethod
    def _join_if_started(thread: threading.Thread | None, timeout: float) -> bool:
        if thread is None or thread.ident is None:
            return True
        thread.join(max(0.0, timeout))
        return not thread.is_alive()

    def _run_launch_code_renewal(self, state: _BridgeState) -> None:
        while not self._renewal_stop.wait(LAUNCH_CODE_RENEWAL_INTERVAL):
            try:
                state.renew_launch_code_if_needed()
            except _BridgeRequestError:
                if state._draining.is_set():  # noqa: SLF001 - bridge owns its state
                    return
                self._warn("knowledge dungeon rendezvous renewal was deferred")
            except Exception as exc:
                self._warn("knowledge dungeon rendezvous renewal failed: {}", exc)

    def _handler_type(self) -> type[BaseHTTPRequestHandler]:
        class Handler(BaseHTTPRequestHandler):
            server_version = "KnowledgeDungeonBridge/1"
            sys_version = ""

            def setup(self) -> None:
                super().setup()
                self.connection.settimeout(REQUEST_TIMEOUT)

            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
                server = self.server
                state = (
                    server.bridge_state
                    if isinstance(server, _PrivateBridgeHttpServer)
                    else None
                )
                if state is None or not state.begin_request():
                    self._send_error(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        "bridge_unavailable",
                        "Knowledge Dungeon bridge is unavailable",
                    )
                    return
                try:
                    self._validate_transport(state.port)
                    payload = self._read_json_body()
                    if self.path == "/v1/pair":
                        if self.headers.get_all("Authorization", []):
                            raise _BridgeRequestError(
                                HTTPStatus.BAD_REQUEST,
                                "unexpected_authorization",
                                "Authorization is not accepted by the pairing endpoint",
                            )
                        self._send_json(HTTPStatus.OK, state.pair(payload))
                        return
                    operation = _DOMAIN_PATHS.get(self.path)
                    if operation is None:
                        raise _BridgeRequestError(
                            HTTPStatus.NOT_FOUND, "endpoint_not_found", "endpoint was not found"
                        )
                    authorization = self._require_single_header(
                        "Authorization",
                        missing_status=HTTPStatus.UNAUTHORIZED,
                        missing_code="authentication_required",
                        missing_message="Bearer access token is required",
                    )
                    context = state.authorize(authorization)
                    outcome = asyncio.run(state.adapter.invoke(context, operation, payload))
                    self._send_json(
                        HTTPStatus.OK,
                        {
                            "ok": True,
                            "protocol_version": BRIDGE_PROTOCOL_VERSION,
                            "result": outcome.to_dict(),
                        },
                    )
                except _BridgeRequestError as exc:
                    self._send_error(exc.status, exc.code, str(exc))
                except (TimeoutError, socket.timeout):
                    self._send_error(
                        HTTPStatus.REQUEST_TIMEOUT, "request_timeout", "request timed out"
                    )
                except Exception:
                    self._send_error(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        "bridge_unavailable",
                        "Knowledge Dungeon bridge is temporarily unavailable",
                    )
                finally:
                    state.end_request()

            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
                self._send_error(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", "POST is required")

            def do_PUT(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
                self.do_GET()

            def do_DELETE(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
                self.do_GET()

            def do_OPTIONS(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
                self.do_GET()

            def do_PATCH(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
                self.do_GET()

            def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
                self._send_error(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    "method_not_allowed",
                    "POST is required",
                    include_body=False,
                )

            def _validate_transport(self, port: int) -> None:
                if "Origin" in self.headers:
                    raise _BridgeRequestError(
                        HTTPStatus.FORBIDDEN, "browser_origin_rejected", "browser origins are not allowed"
                    )
                if self.headers.get_all("Transfer-Encoding", []):
                    raise _BridgeRequestError(
                        HTTPStatus.BAD_REQUEST,
                        "transfer_encoding_rejected",
                        "Transfer-Encoding is not accepted",
                    )
                host = self._require_single_header("Host")
                if host != f"{_BIND_HOST}:{port}":
                    raise _BridgeRequestError(
                        HTTPStatus.FORBIDDEN, "invalid_host", "Host header is invalid"
                    )
                if len(self.headers) > MAX_HEADER_COUNT:
                    raise _BridgeRequestError(
                        HTTPStatus.REQUEST_HEADER_FIELDS_TOO_LARGE,
                        "headers_too_large",
                        "too many request headers",
                    )
                header_bytes = sum(len(key) + len(value) + 4 for key, value in self.headers.items())
                if header_bytes > MAX_HEADER_BYTES:
                    raise _BridgeRequestError(
                        HTTPStatus.REQUEST_HEADER_FIELDS_TOO_LARGE,
                        "headers_too_large",
                        "request headers are too large",
                    )
                content_type = self._require_single_header("Content-Type")
                self._require_single_header(
                    "Content-Length",
                    missing_status=HTTPStatus.LENGTH_REQUIRED,
                    missing_code="content_length_required",
                    missing_message="Content-Length is required",
                )
                media_type, separator, parameter = content_type.partition(";")
                if media_type.strip().lower() != "application/json" or (
                    separator and parameter.strip().lower() != "charset=utf-8"
                ):
                    raise _BridgeRequestError(
                        HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                        "invalid_content_type",
                        "Content-Type must be application/json",
                    )

            def _read_json_body(self) -> object:
                raw_length = self.headers.get("Content-Length")
                if raw_length is None or not raw_length.isascii() or not raw_length.isdigit():
                    raise _BridgeRequestError(
                        HTTPStatus.LENGTH_REQUIRED, "content_length_required", "Content-Length is required"
                    )
                length = int(raw_length)
                if length < 2 or length > MAX_BODY_BYTES:
                    raise _BridgeRequestError(
                        HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body_too_large", "request body size is invalid"
                    )
                body = self.rfile.read(length)
                if len(body) != length:
                    raise _BridgeRequestError(
                        HTTPStatus.BAD_REQUEST, "incomplete_body", "request body is incomplete"
                    )
                try:
                    return json.loads(
                        body.decode("utf-8"),
                        object_pairs_hook=self._strict_object,
                        parse_constant=self._reject_json_constant,
                    )
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                    raise _BridgeRequestError(
                        HTTPStatus.BAD_REQUEST, "invalid_json", "request body must be valid UTF-8 JSON"
                    ) from exc

            def _require_single_header(
                self,
                name: str,
                *,
                missing_status: HTTPStatus = HTTPStatus.BAD_REQUEST,
                missing_code: str = "required_header_missing",
                missing_message: str | None = None,
            ) -> str:
                values = self.headers.get_all(name, [])
                if len(values) > 1:
                    raise _BridgeRequestError(
                        HTTPStatus.BAD_REQUEST,
                        "ambiguous_header",
                        f"{name} must appear exactly once",
                    )
                if not values:
                    raise _BridgeRequestError(
                        missing_status,
                        missing_code,
                        missing_message or f"{name} is required",
                    )
                return values[0]

            @staticmethod
            def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
                result: dict[str, object] = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError(f"duplicate JSON key: {key}")
                    result[key] = value
                return result

            @staticmethod
            def _reject_json_constant(value: str) -> object:
                raise ValueError(f"non-finite JSON constant: {value}")

            def _send_error(
                self,
                status: HTTPStatus,
                code: str,
                message: str,
                *,
                include_body: bool = True,
            ) -> None:
                self._send_json(
                    status,
                    {"ok": False, "error": {"code": code, "message": message}},
                    include_body=include_body,
                )

            def _send_json(
                self,
                status: HTTPStatus,
                payload: Mapping[str, object],
                *,
                include_body: bool = True,
            ) -> None:
                encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
                try:
                    self.send_response(status.value)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(encoded)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.end_headers()
                    if include_body:
                        self.wfile.write(encoded)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass

            def log_message(self, _format: str, *_args: object) -> None:
                return

        return Handler

    def _warn(self, message: str, *args: object) -> None:
        warning = getattr(self._logger, "warning", None)
        if callable(warning):
            try:
                warning(message, *args)
            except Exception:
                pass


__all__ = [
    "ACCESS_TOKEN_EXPIRES_IN",
    "BridgeUnavailableError",
    "KnowledgeDungeonPrivateBridge",
    "LAUNCH_CODE_TTL",
    "MAX_CONCURRENT_REQUESTS",
    "RendezvousOwnershipError",
    "RendezvousPublishError",
    "RendezvousPublisher",
    "RendezvousRecord",
    "RuntimeOwnershipLock",
    "SESSION_ABSOLUTE_TIMEOUT",
    "SESSION_IDLE_TIMEOUT",
    "default_runtime_directory",
    "harden_runtime_directory",
]
