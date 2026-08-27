"""Safe on-disk staging and installation for optional local model assets.

This module performs no network I/O and never loads a model.  It owns only a
private model root, validates staged bytes against a trusted catalog, then uses
atomic filesystem operations to publish an installed package.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Iterator

try:
    from .local_model_manifest import (
        LocalModelAssetError,
        LocalModelCatalog,
        ModelFile,
        ModelPackage,
        validate_relative_path,
    )
except ImportError:  # Direct imports in isolated tests.
    from local_model_manifest import (  # type: ignore[no-redef]
        LocalModelAssetError,
        LocalModelCatalog,
        ModelFile,
        ModelPackage,
        validate_relative_path,
    )


INSTALLED_SCHEMA_VERSION: Final = 1
MINIMUM_DISK_RESERVE_BYTES: Final = 256 * 1024 * 1024
STALE_STAGING_SECONDS: Final = 7 * 24 * 60 * 60
_ROOT_LOCKS: dict[str, threading.RLock] = {}
_ROOT_DOWNLOAD_LOCKS: dict[str, threading.Lock] = {}
_ROOT_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True, slots=True)
class InstalledModelPackage:
    package_id: str
    version: str
    role: str
    size_bytes: int
    installed_at: int

    def to_payload(self) -> dict[str, object]:
        return {
            "id": self.package_id,
            "version": self.version,
            "role": self.role,
            "size_bytes": self.size_bytes,
            "installed_at": self.installed_at,
        }


def default_local_models_root() -> Path:
    """Return a user-writable, platform-appropriate model directory."""

    if sys.platform.startswith("win"):
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "N.E.K.O" / "StudyCompanion" / "models"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "N.E.K.O" / "StudyCompanion" / "models"
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "N.E.K.O" / "StudyCompanion" / "models"


class _CrossInstanceLock:
    """Small advisory lock, compatible with Windows and POSIX Python runtimes."""

    def __init__(self, path: Path, *, blocking: bool = True) -> None:
        self._path = path
        self._blocking = blocking
        self._file = None

    def __enter__(self) -> "_CrossInstanceLock":
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._file = self._path.open("a+b")
            self._file.seek(0)
            self._file.write(b"0")
            self._file.flush()
            if os.name == "nt":
                import msvcrt  # noqa: PLC0415

                self._file.seek(0)
                mode = msvcrt.LK_LOCK if self._blocking else msvcrt.LK_NBLCK
                msvcrt.locking(self._file.fileno(), mode, 1)
            else:
                import fcntl  # type: ignore[import-not-found]  # noqa: PLC0415

                mode = fcntl.LOCK_EX if self._blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
                fcntl.flock(self._file.fileno(), mode)
        except OSError:
            if self._file is not None:
                self._file.close()
                self._file = None
            raise
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        if self._file is None:
            return
        try:
            if os.name == "nt":
                import msvcrt  # noqa: PLC0415

                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl  # type: ignore[import-not-found]  # noqa: PLC0415

                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None


class LocalModelDownloadLease:
    """An exclusive, releaseable lease for one root's download/install writer."""

    def __init__(self, thread_lock: threading.Lock, file_lock: _CrossInstanceLock) -> None:
        self._thread_lock = thread_lock
        self._file_lock = file_lock
        self._released = False

    def __enter__(self) -> "LocalModelDownloadLease":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.release()

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            self._file_lock.__exit__(None, None, None)
        finally:
            self._thread_lock.release()


class LocalModelStore:
    """Catalog-constrained storage for staging, recovery, and safe uninstall."""

    def __init__(
        self,
        catalog: LocalModelCatalog,
        *,
        root: Path | str | None = None,
        minimum_free_bytes: int | None = None,
    ) -> None:
        if minimum_free_bytes is not None and (isinstance(minimum_free_bytes, bool) or int(minimum_free_bytes) < 0):
            raise ValueError("minimum_free_bytes must be non-negative")
        self.catalog = catalog
        self.root = Path(root) if root is not None else default_local_models_root()
        self.minimum_free_bytes = int(minimum_free_bytes) if minimum_free_bytes is not None else None
        self._root_resolved = self.root.resolve(strict=False)
        self._ensure_root()
        with _ROOT_LOCKS_GUARD:
            self._thread_lock = _ROOT_LOCKS.setdefault(str(self._root_resolved), threading.RLock())
            self._download_thread_lock = _ROOT_DOWNLOAD_LOCKS.setdefault(str(self._root_resolved), threading.Lock())

    @property
    def installed_manifest_path(self) -> Path:
        return self._child("state", "installed.json")

    def get_status(self) -> dict[str, object]:
        installed = self.installed_packages()
        try:
            free_bytes = shutil.disk_usage(self.root).free
            disk_available = True
        except OSError:
            free_bytes = 0
            disk_available = False
        return {
            "root_ready": True,
            "disk_available": disk_available,
            "free_bytes": free_bytes,
            "installed_package_count": len(installed),
            "installed_bytes": sum(item.size_bytes for item in installed),
            "manual_or_invalid_package_count": self._manual_or_invalid_count(),
            "stale_staging_count": self._stale_staging_count(),
        }

    def ensure_disk_space(self, required_bytes: int) -> None:
        if isinstance(required_bytes, bool) or not isinstance(required_bytes, int) or required_bytes < 0:
            raise LocalModelAssetError("local_model_size_invalid")
        reserve = (
            self.minimum_free_bytes
            if self.minimum_free_bytes is not None
            else max((required_bytes + 9) // 10, MINIMUM_DISK_RESERVE_BYTES)
        )
        try:
            available = shutil.disk_usage(self.root).free
        except OSError as exc:
            raise LocalModelAssetError("local_model_store_unavailable") from exc
        if available < required_bytes + reserve:
            raise LocalModelAssetError("local_model_disk_insufficient")

    def acquire_download_lease(self) -> LocalModelDownloadLease:
        """Try to exclusively reserve streaming writes without blocking UI reads."""

        if not self._download_thread_lock.acquire(blocking=False):
            raise LocalModelAssetError("local_model_busy")
        file_lock = _CrossInstanceLock(self._child("locks", "download.lock"), blocking=False)
        try:
            file_lock.__enter__()
        except OSError as exc:
            self._download_thread_lock.release()
            raise LocalModelAssetError("local_model_busy") from exc
        return LocalModelDownloadLease(self._download_thread_lock, file_lock)

    def staging_partial_path(self, package_id: str, version: str, relative_path: str) -> Path:
        package, relative_path = self._package_and_path(package_id, version, relative_path)
        safe = self._child_from(self._staging_root(package), ".partial", relative_path)
        safe.parent.mkdir(parents=True, exist_ok=True)
        self.ensure_disk_space(next(item.size_bytes for item in package.files if item.path == relative_path))
        return safe

    def staging_path(self, package_id: str, version: str, relative_path: str) -> Path:
        package, relative_path = self._package_and_path(package_id, version, relative_path)
        return self._child_from(self._staging_root(package), relative_path)

    def staging_file_status(self, package_id: str, version: str, relative_path: str) -> dict[str, object]:
        """Return a catalog-validated, path-free status for one promoted staging file."""

        package, relative_path = self._package_and_path(package_id, version, relative_path)
        target = self._child_from(self._staging_root(package), relative_path)
        try:
            if target.is_symlink():
                return {"exists": True, "valid": False, "size_bytes": 0}
            if not target.exists():
                return {"exists": False, "valid": False, "size_bytes": 0}
            if not target.is_file():
                return {"exists": True, "valid": False, "size_bytes": 0}
            size = target.stat().st_size
            expected = next(item for item in package.files if item.path == relative_path)
            try:
                self._validate_file(target, expected)
            except LocalModelAssetError:
                return {"exists": True, "valid": False, "size_bytes": size}
            return {"exists": True, "valid": True, "size_bytes": size}
        except OSError:
            return {"exists": False, "valid": False, "size_bytes": 0}

    def cleanup_staging(self, package_id: str, version: str) -> bool:
        """Delete exactly one catalog-known staging root after an explicit cancel."""

        with self._locked():
            package = self.catalog.package(package_id, version)
            target = self._staging_root(package)
            if not (target.exists() or target.is_symlink()):
                return False
            self._safe_remove_tree(target)
            return True

    def staging_partial_existing_path(self, package_id: str, version: str, relative_path: str) -> Path:
        """Return a constrained partial path without creating files or checking disk."""

        package, relative_path = self._package_and_path(package_id, version, relative_path)
        return self._child_from(self._staging_root(package), ".partial", relative_path)

    def partial_status(self, package_id: str, version: str, relative_path: str) -> dict[str, object]:
        path = self.staging_partial_existing_path(package_id, version, relative_path)
        try:
            exists = path.is_file() and not path.is_symlink()
            size = path.stat().st_size if exists else 0
        except OSError:
            exists = False
            size = 0
        return {"exists": exists, "size_bytes": size}

    def cleanup_partial(self, package_id: str, version: str, relative_path: str) -> None:
        package, relative_path = self._package_and_path(package_id, version, relative_path)
        partial = self.staging_partial_existing_path(package_id, version, relative_path)
        try:
            if partial.is_symlink():
                raise LocalModelAssetError("local_model_path_invalid")
            partial.unlink(missing_ok=True)
            parent = partial.parent
            while parent != self._staging_root(package):
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent
        except OSError as exc:
            raise LocalModelAssetError("local_model_staging_cleanup_failed") from exc

    def promote_partial(self, package_id: str, version: str, relative_path: str) -> Path:
        package, relative_path = self._package_and_path(package_id, version, relative_path)
        partial = self._child_from(self._staging_root(package), ".partial", relative_path)
        target = self._child_from(self._staging_root(package), relative_path)
        expected = next(item for item in package.files if item.path == relative_path)
        self._validate_file(partial, expected)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            raise LocalModelAssetError("local_model_staging_conflict")
        os.replace(partial, target)
        parent = partial.parent
        while parent != self._staging_root(package):
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
        return target

    def validate_staging(self, package_id: str, version: str) -> ModelPackage:
        package = self.catalog.package(package_id, version)
        stage = self._staging_root(package)
        partial_root = self._child_from(stage, ".partial")
        if partial_root.exists():
            raise LocalModelAssetError("local_model_staging_incomplete")
        for item in package.files:
            self._validate_file(self._child_from(stage, item.path), item)
        if self._tree_files(stage) != {item.path for item in package.files}:
            raise LocalModelAssetError("local_model_staging_invalid")
        return package

    def install_from_staging(self, package_id: str, version: str) -> InstalledModelPackage:
        with self._locked():
            package = self.validate_staging(package_id, version)
            self.ensure_disk_space(0)
            target = self._installed_root(package)
            if target.exists() or target.is_symlink():
                current = self._record_for(package.package_id, package.version)
                if current is not None and self._verify_installed(package):
                    return current
                raise LocalModelAssetError("local_model_install_conflict")
            target.parent.mkdir(parents=True, exist_ok=True)
            stage = self._staging_root(package)
            try:
                self._write_package_metadata(stage, package)
                if self._tree_files(stage) != {item.path for item in package.files} | {"package.json"}:
                    raise LocalModelAssetError("local_model_staging_invalid")
                # Both directories are rooted under one user-selected store,
                # so this is a same-volume atomic directory publication.
                os.replace(stage, target)
            except OSError as exc:
                raise LocalModelAssetError("local_model_install_failed") from exc
            record = InstalledModelPackage(
                package_id=package.package_id,
                version=package.version,
                role=package.role,
                size_bytes=package.total_size_bytes,
                installed_at=int(time.time()),
            )
            records = [item for item in self._load_or_recover_locked() if item.package_id != record.package_id]
            self._write_installed_records([*records, record])
            self._cleanup_superseded_versions(package)
            return record

    def installed_packages(self) -> tuple[InstalledModelPackage, ...]:
        with self._locked():
            return tuple(self._load_or_recover_locked())

    def recover(self) -> tuple[InstalledModelPackage, ...]:
        """Rebuild installed.json by safely scanning catalog-known package trees."""

        with self._locked():
            return tuple(self._recover_locked())

    def uninstall(self, package_id: str, version: str | None = None) -> int:
        """Remove only catalog-shaped package directories beneath this store root."""

        with self._locked():
            if version is not None:
                packages = [self.catalog.package(package_id, version)]
            else:
                package_id = self._validate_package_id(package_id)
                packages = [item for item in self.catalog.packages if item.package_id == package_id]
            removed: set[tuple[str, str]] = set()
            for package in packages:
                target = self._installed_root(package)
                if target.exists() or target.is_symlink():
                    self._safe_remove_tree(target)
                    removed.add((package.package_id, package.version))
            records = [
                item for item in self._load_or_recover_locked() if (item.package_id, item.version) not in removed
            ]
            self._write_installed_records(records)
            return len(removed)

    def _package_and_path(self, package_id: str, version: str, relative_path: str) -> tuple[ModelPackage, str]:
        package = self.catalog.package(package_id, version)
        relative_path = validate_relative_path(relative_path)
        if relative_path not in {item.path for item in package.files}:
            raise LocalModelAssetError("local_model_file_not_found")
        return package, relative_path

    def _staging_root(self, package: ModelPackage) -> Path:
        return self._child("staging", package.package_id, package.version)

    def _installed_root(self, package: ModelPackage) -> Path:
        return self._child("packages", package.package_id, package.version)

    def _child(self, *parts: str) -> Path:
        return self._child_from(self.root, *parts)

    def _child_from(self, base: Path, *parts: str) -> Path:
        candidate = base.joinpath(*parts)
        try:
            resolved = candidate.resolve(strict=False)
            base_resolved = base.resolve(strict=False)
            resolved.relative_to(base_resolved)
            resolved.relative_to(self._root_resolved)
        except (OSError, ValueError) as exc:
            raise LocalModelAssetError("local_model_path_invalid") from exc
        return candidate

    def _ensure_root(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            if self.root.is_symlink() or not self.root.is_dir():
                raise LocalModelAssetError("local_model_path_invalid")
            self._root_resolved = self.root.resolve(strict=True)
        except LocalModelAssetError:
            raise
        except OSError as exc:
            raise LocalModelAssetError("local_model_store_unavailable") from exc

    @staticmethod
    def _validate_package_id(package_id: str) -> str:
        # Catalog lookup performs the canonical manifest validation without
        # leaking local filesystem details through an error message.
        if not isinstance(package_id, str) or not package_id:
            raise LocalModelAssetError("local_model_package_id_invalid")
        return package_id

    def _validate_file(self, path: Path, expected: ModelFile) -> None:
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size != expected.size_bytes:
                raise LocalModelAssetError("local_model_file_validation_failed")
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            if digest.hexdigest().lower() != expected.sha256:
                raise LocalModelAssetError("local_model_file_validation_failed")
        except OSError as exc:
            raise LocalModelAssetError("local_model_file_validation_failed") from exc

    def _verify_installed(self, package: ModelPackage) -> bool:
        try:
            target = self._installed_root(package)
            if target.is_symlink() or not target.is_dir():
                return False
            if self._tree_files(target) != {item.path for item in package.files} | {"package.json"}:
                return False
            if not self._package_metadata_matches(target, package):
                return False
            for item in package.files:
                self._validate_file(self._child_from(target, item.path), item)
        except LocalModelAssetError:
            return False
        return True

    def _tree_files(self, root: Path) -> set[str]:
        """Return a portable file list while rejecting links, junctions, and escapes."""

        try:
            root_resolved = root.resolve(strict=True)
            root_resolved.relative_to(self._root_resolved)
            if self._is_link_or_reparse(root):
                raise LocalModelAssetError("local_model_path_invalid")
            files: set[str] = set()

            def visit(directory: Path, prefix: str = "") -> None:
                for entry in os.scandir(directory):
                    item = Path(entry.path)
                    if self._is_link_or_reparse(item):
                        raise LocalModelAssetError("local_model_path_invalid")
                    relative = f"{prefix}/{entry.name}" if prefix else entry.name
                    validate_relative_path(relative)
                    resolved = item.resolve(strict=True)
                    resolved.relative_to(root_resolved)
                    if entry.is_dir(follow_symlinks=False):
                        visit(item, relative)
                    elif entry.is_file(follow_symlinks=False):
                        files.add(relative)
                    else:
                        raise LocalModelAssetError("local_model_path_invalid")

            visit(root)
            return files
        except (OSError, ValueError) as exc:
            raise LocalModelAssetError("local_model_path_invalid") from exc

    @staticmethod
    def _is_link_or_reparse(path: Path) -> bool:
        try:
            attributes = path.stat(follow_symlinks=False).st_file_attributes
        except AttributeError:
            attributes = 0
        except OSError:
            return True
        return path.is_symlink() or bool(attributes & 0x400)

    def _write_package_metadata(self, stage: Path, package: ModelPackage) -> None:
        metadata = self._package_metadata(package)
        target = self._child_from(stage, "package.json")
        if target.exists() or target.is_symlink():
            raise LocalModelAssetError("local_model_staging_invalid")
        try:
            with target.open("x", encoding="utf-8") as stream:
                json.dump(metadata, stream, separators=(",", ":"), sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            raise LocalModelAssetError("local_model_install_failed") from exc

    @staticmethod
    def _package_metadata(package: ModelPackage) -> dict[str, object]:
        return {
            "id": package.package_id,
            "version": package.version,
            "role": package.role,
            "runtime_protocol": package.runtime_protocol,
            "requires_acceptance": package.requires_acceptance,
            "files": [
                {"name": item.name, "sha256": item.sha256, "size_bytes": item.size_bytes} for item in package.files
            ],
        }

    def _package_metadata_matches(self, root: Path, package: ModelPackage) -> bool:
        target = self._child_from(root, "package.json")
        try:
            if target.is_symlink() or not target.is_file():
                return False
            return json.loads(target.read_text(encoding="utf-8")) == self._package_metadata(package)
        except (OSError, ValueError, UnicodeError):
            return False

    def _load_or_recover_locked(self) -> list[InstalledModelPackage]:
        try:
            return self._load_installed_records()
        except LocalModelAssetError:
            return self._recover_locked()

    def _recover_locked(self) -> list[InstalledModelPackage]:
        recovered: list[InstalledModelPackage] = []
        for package in self.catalog.packages:
            if self._verify_installed(package):
                recovered.append(
                    InstalledModelPackage(
                        package_id=package.package_id,
                        version=package.version,
                        role=package.role,
                        size_bytes=package.total_size_bytes,
                        installed_at=int(self._installed_root(package).stat().st_mtime),
                    )
                )
        self._write_installed_records(recovered)
        return recovered

    def _manual_or_invalid_count(self) -> int:
        """Boundedly count unknown/reparse/invalid trees without exposing paths."""

        packages_root = self._child("packages")
        if not packages_root.exists():
            return 0
        if self._is_link_or_reparse(packages_root) or not packages_root.is_dir():
            return 1
        known = {(item.package_id, item.version): item for item in self.catalog.packages}
        count = 0
        try:
            for id_entry in os.scandir(packages_root):
                id_path = Path(id_entry.path)
                if self._is_link_or_reparse(id_path) or not id_entry.is_dir(follow_symlinks=False):
                    count += 1
                    continue
                for version_entry in os.scandir(id_path):
                    version_path = Path(version_entry.path)
                    if self._is_link_or_reparse(version_path) or not version_entry.is_dir(follow_symlinks=False):
                        count += 1
                        continue
                    package = known.get((id_entry.name, version_entry.name))
                    if package is None or not self._verify_installed(package):
                        count += 1
        except OSError:
            return count + 1
        return count

    def _cleanup_superseded_versions(self, current: ModelPackage) -> None:
        """Remove only catalog-known old directories after the new record is durable."""

        for candidate in self.catalog.packages:
            if candidate.package_id != current.package_id or candidate.version == current.version:
                continue
            target = self._installed_root(candidate)
            if not (target.exists() or target.is_symlink()):
                continue
            try:
                self._safe_remove_tree(target)
            except LocalModelAssetError:
                # The new version is already committed.  An unexpected old
                # tree is retained and reported as manual/invalid rather than
                # risking a broader delete or rolling back the new package.
                continue

    def _stale_staging_count(self) -> int:
        """Count staging package roots older than seven days; never auto-delete."""

        staging_root = self._child("staging")
        if not staging_root.exists():
            return 0
        if self._is_link_or_reparse(staging_root) or not staging_root.is_dir():
            return 1
        deadline = time.time() - STALE_STAGING_SECONDS
        count = 0
        try:
            for id_entry in os.scandir(staging_root):
                id_path = Path(id_entry.path)
                if self._is_link_or_reparse(id_path) or not id_entry.is_dir(follow_symlinks=False):
                    count += 1
                    continue
                for version_entry in os.scandir(id_path):
                    version_path = Path(version_entry.path)
                    if self._is_link_or_reparse(version_path) or not version_entry.is_dir(follow_symlinks=False):
                        count += 1
                        continue
                    if version_path.stat().st_mtime < deadline:
                        count += 1
        except OSError:
            return count + 1
        return count

    def _record_for(self, package_id: str, version: str) -> InstalledModelPackage | None:
        return next(
            (
                item
                for item in self._load_or_recover_locked()
                if item.package_id == package_id and item.version == version
            ),
            None,
        )

    def _load_installed_records(self) -> list[InstalledModelPackage]:
        path = self.installed_manifest_path
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("schema_version") != INSTALLED_SCHEMA_VERSION:
                raise ValueError
            values = payload.get("packages")
            if not isinstance(values, list):
                raise ValueError
            records: list[InstalledModelPackage] = []
            for value in values:
                if not isinstance(value, dict):
                    raise ValueError
                package = self.catalog.package(str(value.get("id") or ""), str(value.get("version") or ""))
                size = value.get("size_bytes")
                installed_at = value.get("installed_at")
                if (
                    isinstance(size, bool)
                    or not isinstance(size, int)
                    or size != package.total_size_bytes
                    or isinstance(installed_at, bool)
                    or not isinstance(installed_at, int)
                ):
                    raise ValueError
                records.append(
                    InstalledModelPackage(
                        package_id=package.package_id,
                        version=package.version,
                        role=package.role,
                        size_bytes=size,
                        installed_at=installed_at,
                    )
                )
            return records
        except (OSError, ValueError, LocalModelAssetError) as exc:
            raise LocalModelAssetError("local_model_installed_manifest_invalid") from exc

    def _write_installed_records(self, records: list[InstalledModelPackage]) -> None:
        payload = {
            "schema_version": INSTALLED_SCHEMA_VERSION,
            "packages": [item.to_payload() for item in records],
        }
        target = self.installed_manifest_path
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._child("state", f".installed-{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump(payload, stream, separators=(",", ":"), sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise LocalModelAssetError("local_model_installed_manifest_write_failed") from exc

    def _safe_remove_tree(self, target: Path) -> None:
        try:
            resolved = target.resolve(strict=False)
            resolved.relative_to(self._root_resolved)
            if self._is_link_or_reparse(target):
                raise LocalModelAssetError("local_model_path_invalid")
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
        except (OSError, ValueError) as exc:
            raise LocalModelAssetError("local_model_uninstall_failed") from exc

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._thread_lock, _CrossInstanceLock(self._child("locks", "store.lock")):
            yield


__all__ = [
    "MINIMUM_DISK_RESERVE_BYTES",
    "STALE_STAGING_SECONDS",
    "INSTALLED_SCHEMA_VERSION",
    "InstalledModelPackage",
    "LocalModelDownloadLease",
    "LocalModelStore",
    "default_local_models_root",
]
