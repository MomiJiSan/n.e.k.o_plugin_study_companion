from __future__ import annotations

import hashlib
import importlib
import json
import os
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "_study_companion_model_store_test"
PACKAGE = ModuleType(PACKAGE_NAME)
PACKAGE.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
sys.modules[PACKAGE_NAME] = PACKAGE

manifest = importlib.import_module(
    f"{PACKAGE_NAME}.experimental.local_models.local_model_manifest"
)
store_module = importlib.import_module(
    f"{PACKAGE_NAME}.experimental.local_models.local_model_store"
)
protocol = importlib.import_module(
    f"{PACKAGE_NAME}.experimental.local_models.local_runtime_protocol"
)
LocalModelAssetError = manifest.LocalModelAssetError
LocalModelCatalog = manifest.LocalModelCatalog
LocalModelStore = store_module.LocalModelStore


def _catalog(content: bytes = b"local-model", *, versions: tuple[str, ...] = ("1.0.0",)):
    packages = []
    for version in versions:
        packages.append(
            {
                "id": "math_recognizer_demo",
                "version": version,
                "role": "recognizer",
                "runtime_protocol": protocol.PROTOCOL_VERSION,
                "license": {
                    "name": "Apache-2.0",
                    "url": "https://assets.example.test/license",
                    "requires_acceptance": False,
                },
                "files": [
                    {
                        "name": "weights/model.bin",
                        "url": "https://assets.example.test/model.bin",
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "size_bytes": len(content),
                    }
                ],
            }
        )
    return LocalModelCatalog.from_payload(
        {
            "catalog_version": 1,
            "allowed_hosts": ["assets.example.test"],
            "packages": packages,
        }
    )


def _stage(store: LocalModelStore, content: bytes) -> None:
    _stage_version(store, "1.0.0", content)


def _stage_version(store: LocalModelStore, version: str, content: bytes) -> None:
    partial = store.staging_partial_path("math_recognizer_demo", version, "weights/model.bin")
    partial.write_bytes(content)
    store.promote_partial("math_recognizer_demo", version, "weights/model.bin")


def test_linux_default_directory_matches_application_data_convention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_module.sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

    assert store_module.default_local_models_root() == (tmp_path / "N.E.K.O" / "StudyCompanion" / "models")


def test_atomic_install_recovery_and_safe_uninstall(tmp_path: Path) -> None:
    content = b"local-model"
    store = LocalModelStore(_catalog(content), root=tmp_path / "models", minimum_free_bytes=0)
    _stage(store, content)
    installed = store.install_from_staging("math_recognizer_demo", "1.0.0")
    target = tmp_path / "models" / "packages" / "math_recognizer_demo" / "1.0.0"
    assert installed.size_bytes == len(content)
    assert (target / "weights" / "model.bin").read_bytes() == content
    assert (target / "package.json").is_file()
    assert not (tmp_path / "models" / "staging" / "math_recognizer_demo" / "1.0.0").exists()
    assert (tmp_path / "models" / "state" / "installed.json").is_file()

    store.installed_manifest_path.write_text("not json", encoding="utf-8")
    recovered = store.installed_packages()
    assert [(item.package_id, item.version) for item in recovered] == [("math_recognizer_demo", "1.0.0")]
    assert store.uninstall("math_recognizer_demo", "1.0.0") == 1
    assert not target.exists()


def test_new_version_replaces_only_known_old_version_after_commit(tmp_path: Path) -> None:
    # A real catalog version has hashes per package; use matching content in
    # this narrow lifecycle test so both versions are catalog-valid.
    content = b"shared-model"
    root = tmp_path / "models"
    store = LocalModelStore(_catalog(content, versions=("1.0.0", "1.1.0")), root=root, minimum_free_bytes=0)
    _stage_version(store, "1.0.0", content)
    store.install_from_staging("math_recognizer_demo", "1.0.0")
    old_target = root / "packages" / "math_recognizer_demo" / "1.0.0"
    unknown = root / "packages" / "math_recognizer_demo" / "manual-version"
    unknown.mkdir(parents=True)

    _stage_version(store, "1.1.0", content)
    store.install_from_staging("math_recognizer_demo", "1.1.0")

    assert not old_target.exists()
    assert (root / "packages" / "math_recognizer_demo" / "1.1.0").is_dir()
    assert unknown.is_dir(), "unknown trees must never be removed by version cleanup"
    assert [(item.package_id, item.version) for item in store.installed_packages()] == [
        ("math_recognizer_demo", "1.1.0")
    ]


def test_failed_new_version_install_keeps_old_version_and_record(tmp_path: Path) -> None:
    content = b"shared-model"
    root = tmp_path / "models"
    store = LocalModelStore(_catalog(content, versions=("1.0.0", "1.1.0")), root=root, minimum_free_bytes=0)
    _stage_version(store, "1.0.0", content)
    store.install_from_staging("math_recognizer_demo", "1.0.0")
    old_target = root / "packages" / "math_recognizer_demo" / "1.0.0"

    partial = store.staging_partial_path("math_recognizer_demo", "1.1.0", "weights/model.bin")
    partial.write_bytes(b"corrupt")
    with pytest.raises(LocalModelAssetError):
        store.install_from_staging("math_recognizer_demo", "1.1.0")

    assert old_target.is_dir()
    assert [(item.package_id, item.version) for item in store.installed_packages()] == [
        ("math_recognizer_demo", "1.0.0")
    ]


def test_staging_rejects_wrong_hash_and_partial_files(tmp_path: Path) -> None:
    store = LocalModelStore(_catalog(), root=tmp_path / "models", minimum_free_bytes=0)
    partial = store.staging_partial_path("math_recognizer_demo", "1.0.0", "weights/model.bin")
    partial.write_bytes(b"wrong")
    with pytest.raises(LocalModelAssetError) as raised:
        store.promote_partial("math_recognizer_demo", "1.0.0", "weights/model.bin")
    assert raised.value.code == "local_model_file_validation_failed"


def test_staging_file_status_validates_promoted_and_corrupt_content(tmp_path: Path) -> None:
    content = b"local-model"
    store = LocalModelStore(_catalog(content), root=tmp_path / "models", minimum_free_bytes=0)
    assert store.staging_file_status("math_recognizer_demo", "1.0.0", "weights/model.bin") == {
        "exists": False,
        "valid": False,
        "size_bytes": 0,
    }
    _stage(store, content)
    assert store.staging_file_status("math_recognizer_demo", "1.0.0", "weights/model.bin") == {
        "exists": True,
        "valid": True,
        "size_bytes": len(content),
    }
    store.staging_path("math_recognizer_demo", "1.0.0", "weights/model.bin").write_bytes(b"corrupt")
    status = store.staging_file_status("math_recognizer_demo", "1.0.0", "weights/model.bin")
    assert status == {"exists": True, "valid": False, "size_bytes": len(b"corrupt")}


def test_cleanup_staging_is_exact_to_one_known_package_version(tmp_path: Path) -> None:
    content = b"shared-model"
    root = tmp_path / "models"
    store = LocalModelStore(_catalog(content, versions=("1.0.0", "1.1.0")), root=root, minimum_free_bytes=0)
    _stage_version(store, "1.0.0", content)
    _stage_version(store, "1.1.0", content)
    store.install_from_staging("math_recognizer_demo", "1.0.0")
    # Recreate v1 staging after installation consumed it.
    _stage_version(store, "1.0.0", content)

    assert store.cleanup_staging("math_recognizer_demo", "1.0.0") is True
    assert not (root / "staging" / "math_recognizer_demo" / "1.0.0").exists()
    assert (root / "staging" / "math_recognizer_demo" / "1.1.0").is_dir()
    assert (root / "packages" / "math_recognizer_demo" / "1.0.0").is_dir()
    assert store.cleanup_staging("math_recognizer_demo", "1.0.0") is False


def test_partial_status_and_cleanup_do_not_require_disk_guard(tmp_path: Path) -> None:
    store = LocalModelStore(_catalog(), root=tmp_path / "models", minimum_free_bytes=0)
    partial = store.staging_partial_path("math_recognizer_demo", "1.0.0", "weights/model.bin")
    partial.write_bytes(b"")
    assert store.partial_status("math_recognizer_demo", "1.0.0", "weights/model.bin") == {
        "exists": True,
        "size_bytes": 0,
    }
    store.cleanup_partial("math_recognizer_demo", "1.0.0", "weights/model.bin")
    assert store.partial_status("math_recognizer_demo", "1.0.0", "weights/model.bin")["exists"] is False


def test_extra_or_linked_files_are_never_verified(tmp_path: Path) -> None:
    content = b"local-model"
    store = LocalModelStore(_catalog(content), root=tmp_path / "models", minimum_free_bytes=0)
    _stage(store, content)
    store.install_from_staging("math_recognizer_demo", "1.0.0")
    target = tmp_path / "models" / "packages" / "math_recognizer_demo" / "1.0.0"
    (target / "manual.bin").write_bytes(b"manual")
    assert store.recover() == ()
    assert store.get_status()["manual_or_invalid_package_count"] == 1


def test_disk_guard_uses_requested_plus_reserve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = LocalModelStore(_catalog(), root=tmp_path / "models")

    class _Usage:
        free = 100

    monkeypatch.setattr(store_module.shutil, "disk_usage", lambda _path: _Usage())
    with pytest.raises(LocalModelAssetError) as raised:
        store.ensure_disk_space(100)
    assert raised.value.code == "local_model_disk_insufficient"


def test_status_does_not_expose_model_root(tmp_path: Path) -> None:
    store = LocalModelStore(_catalog(), root=tmp_path / "very-private-root", minimum_free_bytes=0)
    status = store.get_status()
    encoded = json.dumps(status)
    assert status["disk_available"] is True
    assert isinstance(status["free_bytes"], int)
    assert status["free_bytes"] >= 0
    assert "very-private-root" not in encoded


def test_status_counts_unknown_and_stale_staging_without_paths(tmp_path: Path) -> None:
    root = tmp_path / "models"
    store = LocalModelStore(_catalog(), root=root, minimum_free_bytes=0)
    unknown = root / "packages" / "manual-package" / "0.0.1"
    unknown.mkdir(parents=True)
    stale = root / "staging" / "math_recognizer_demo" / "1.0.0"
    stale.mkdir(parents=True)
    old = time.time() - store_module.STALE_STAGING_SECONDS - 1
    os.utime(stale, (old, old))

    status = store.get_status()
    assert status["manual_or_invalid_package_count"] == 1
    assert status["stale_staging_count"] == 1
    assert "manual-package" not in json.dumps(status)


def test_invalid_root_error_never_exposes_requested_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    private_root = tmp_path / "very-private-root"

    def fail_mkdir(self, *_args, **_kwargs):
        raise OSError(f"cannot create {self}")

    monkeypatch.setattr(Path, "mkdir", fail_mkdir)
    with pytest.raises(LocalModelAssetError) as raised:
        LocalModelStore(_catalog(), root=private_root, minimum_free_bytes=0)
    assert raised.value.code == "local_model_store_unavailable"
    assert "very-private-root" not in str(raised.value)


def test_disk_usage_error_is_sanitized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = LocalModelStore(_catalog(), root=tmp_path / "models", minimum_free_bytes=0)

    def fail_disk_usage(path):
        raise OSError(f"cannot inspect {path}")

    monkeypatch.setattr(store_module.shutil, "disk_usage", fail_disk_usage)
    with pytest.raises(LocalModelAssetError) as raised:
        store.ensure_disk_space(0)
    assert raised.value.code == "local_model_store_unavailable"
    assert "models" not in str(raised.value)
    status = store.get_status()
    assert status["free_bytes"] == 0
    assert status["disk_available"] is False


def test_download_lease_is_nonblocking_per_store_root_and_reacquirable(tmp_path: Path) -> None:
    root = tmp_path / "models"
    first = LocalModelStore(_catalog(), root=root, minimum_free_bytes=0)
    second = LocalModelStore(_catalog(), root=root, minimum_free_bytes=0)

    lease = first.acquire_download_lease()
    with pytest.raises(LocalModelAssetError) as raised:
        second.acquire_download_lease()
    assert raised.value.code == "local_model_busy"

    lease.release()
    with second.acquire_download_lease() as replacement:
        assert replacement is not None
