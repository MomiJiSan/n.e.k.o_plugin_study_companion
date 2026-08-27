from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest


def _module(monkeypatch: pytest.MonkeyPatch) -> Any:
    root = Path(__file__).resolve().parents[1]
    package_name = "_local_model_download_manager_test"
    package = ModuleType(package_name)
    package.__path__ = [str(root)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package_name, package)
    return importlib.import_module(
        f"{package_name}.experimental.local_models.local_model_download_manager"
    )


def _catalog(
    module: Any,
    payload: bytes,
    *,
    url: str = "https://models.example/model.bin",
    requires_acceptance: bool = False,
) -> Any:
    return module.LocalModelCatalog.from_payload(
        {
            "catalog_version": 1,
            "allowed_hosts": ["models.example", "cdn.models.example"],
            "packages": [
                {
                    "id": "tiny-tutor",
                    "version": "1.0.0",
                    "role": "reasoner",
                    "runtime_protocol": 1,
                    "requires_acceptance": requires_acceptance,
                    "license": {
                        "name": "MIT License",
                        "spdx": "MIT",
                        "url": "https://models.example/license",
                        "requires_acceptance": requires_acceptance,
                    },
                    "files": [
                        {
                            "name": "model.bin",
                            "url": url,
                            "sha256": hashlib.sha256(payload).hexdigest(),
                            "size_bytes": len(payload),
                        }
                    ],
                }
            ],
        }
    )


def _two_file_catalog(module: Any, first: bytes, second: bytes) -> Any:
    return module.LocalModelCatalog.from_payload(
        {
            "catalog_version": 1,
            "allowed_hosts": ["models.example"],
            "packages": [
                {
                    "id": "tiny-tutor",
                    "version": "1.0.0",
                    "role": "reasoner",
                    "runtime_protocol": 1,
                    "license": {
                        "name": "MIT License",
                        "spdx": "MIT",
                        "url": "https://models.example/license",
                        "requires_acceptance": False,
                    },
                    "files": [
                        {
                            "name": "first.bin",
                            "url": "https://models.example/first.bin",
                            "sha256": hashlib.sha256(first).hexdigest(),
                            "size_bytes": len(first),
                        },
                        {
                            "name": "second.bin",
                            "url": "https://models.example/second.bin",
                            "sha256": hashlib.sha256(second).hexdigest(),
                            "size_bytes": len(second),
                        },
                    ],
                }
            ],
        }
    )


def _manager(module: Any, tmp_path: Path, catalog: Any, handler: Any) -> tuple[Any, Any]:
    store = module.LocalModelStore(catalog, root=tmp_path, minimum_free_bytes=0)
    manager = module.LocalModelDownloadManager(
        logger=None,
        catalog=catalog,
        store=store,
        transport=httpx.MockTransport(handler),
        timeout_seconds=2,
    )
    return manager, store


async def _wait_for_terminal(manager: Any) -> dict[str, object]:
    for _ in range(200):
        status = await manager.status()
        if not status["downloads"]:
            return status
        await asyncio.sleep(0)
    raise AssertionError("background download did not finish")


async def _install_and_wait(
    manager: Any, package_id: str = "tiny-tutor", version: str = "1.0.0", **kwargs: Any
) -> dict[str, object]:
    queued = await manager.install(package_id, version, **kwargs)
    assert queued["state"] == "queued"
    return await _wait_for_terminal(manager)


@pytest.mark.asyncio
async def test_install_is_explicit_and_performs_atomic_validated_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _module(monkeypatch)
    payload = b"small model bytes"
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(200, content=payload)

    catalog = _catalog(module, payload)
    manager, store = _manager(module, tmp_path, catalog, handler)

    assert (await manager.catalog())["packages"][0]["id"] == "tiny-tutor"
    assert (await manager.status())["downloads"] == []
    assert calls == []

    result = await manager.install("tiny-tutor", "1.0.0")

    assert result == {"state": "queued", "package_id": "tiny-tutor", "version": "1.0.0"}
    assert (await _wait_for_terminal(manager))["last"]["state"] == "installed"
    assert len(calls) == 1
    installed = store.installed_packages()
    assert len(installed) == 1
    assert (tmp_path / "packages" / "tiny-tutor" / "1.0.0" / "model.bin").read_bytes() == payload


@pytest.mark.asyncio
async def test_resume_uses_range_and_etag(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = _module(monkeypatch)
    payload = b"0123456789"
    seen_headers: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(dict(request.headers))
        assert request.headers["range"] == "bytes=4-"
        assert request.headers["if-range"] == '"old-tag"'
        return httpx.Response(206, headers={"Content-Range": "bytes 4-9/10", "ETag": '"old-tag"'}, content=payload[4:])

    catalog = _catalog(module, payload)
    manager, store = _manager(module, tmp_path, catalog, handler)
    partial = store.staging_partial_path("tiny-tutor", "1.0.0", "model.bin")
    partial.write_bytes(payload[:4])
    partial.with_name("model.bin.download.json").write_text(
        json.dumps(
            {
                "catalog_version": catalog.version,
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "url": "https://models.example/model.bin",
                "etag": '"old-tag"',
            }
        ),
        encoding="utf-8",
    )

    result = await manager.install("tiny-tutor", "1.0.0")

    assert result["state"] == "queued"
    assert (await _wait_for_terminal(manager))["last"]["state"] == "installed"
    assert len(seen_headers) == 1


@pytest.mark.asyncio
async def test_stale_partial_metadata_is_deleted_and_restarted_without_range(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _module(monkeypatch)
    payload = b"0123456789"
    ranges: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        ranges.append(request.headers.get("range", ""))
        return httpx.Response(200, headers={"ETag": '"fresh"'}, content=payload)

    catalog = _catalog(module, payload)
    manager, store = _manager(module, tmp_path, catalog, handler)
    partial = store.staging_partial_path("tiny-tutor", "1.0.0", "model.bin")
    partial.write_bytes(payload[:4])
    partial.with_name("model.bin.download.json").write_text(
        json.dumps(
            {
                "catalog_version": 0,
                "size_bytes": len(payload),
                "sha256": "0" * 64,
                "url": "https://models.example/old.bin",
                "etag": '"old"',
            }
        ),
        encoding="utf-8",
    )

    assert (await manager.status())["recoverable_partials"] == []
    assert not partial.exists()
    assert (await _install_and_wait(manager))["last"]["state"] == "installed"
    assert ranges == [""]


@pytest.mark.asyncio
async def test_source_tree_cannot_be_model_directory_but_injected_store_is_untouched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _module(monkeypatch)
    catalog = _catalog(module, b"source-dir")
    source_root = Path(module.__file__).resolve().parent

    with pytest.raises(module.LocalModelDownloadError) as rejected:
        module.LocalModelDownloadManager(directory=source_root, catalog=catalog)
    assert rejected.value.code == "local_model_path_invalid"
    assert str(source_root) not in str(rejected.value)

    manager, _store = _manager(module, tmp_path, catalog, lambda _request: httpx.Response(200, content=b"source-dir"))
    assert manager._directory is None
    with pytest.raises(module.LocalModelDownloadError) as switched:
        await manager.set_directory(source_root / "models")
    assert switched.value.code == "local_model_path_invalid"


@pytest.mark.asyncio
async def test_range_unsupported_restarts_cleanly(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = _module(monkeypatch)
    payload = b"0123456789"
    ranges: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        ranges.append(request.headers.get("range", ""))
        return httpx.Response(200, headers={"ETag": '"new-tag"'}, content=payload)

    catalog = _catalog(module, payload)
    manager, store = _manager(module, tmp_path, catalog, handler)
    partial = store.staging_partial_path("tiny-tutor", "1.0.0", "model.bin")
    partial.write_bytes(payload[:4])
    partial.with_name("model.bin.download.json").write_text(
        json.dumps(
            {
                "catalog_version": catalog.version,
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "url": "https://models.example/model.bin",
                "etag": '"old-tag"',
            }
        ),
        encoding="utf-8",
    )

    assert (await _install_and_wait(manager))["last"]["state"] == "installed"
    assert ranges == ["bytes=4-", ""]


@pytest.mark.asyncio
async def test_hash_failure_keeps_partial_for_diagnostics_and_never_installs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _module(monkeypatch)
    expected = b"correct"
    received = b"wrong!!"

    catalog = _catalog(module, expected)
    manager, store = _manager(module, tmp_path, catalog, lambda _request: httpx.Response(200, content=received))

    assert (await _install_and_wait(manager))["last"]["error_code"] == module.LOCAL_MODEL_HASH_MISMATCH
    partial = store.staging_partial_path("tiny-tutor", "1.0.0", "model.bin")
    assert partial.exists()
    assert store.installed_packages() == ()
    status = await manager.status()
    assert status["last"] == {
        "state": "failed",
        "package_id": "tiny-tutor",
        "version": "1.0.0",
        "error_code": module.LOCAL_MODEL_HASH_MISMATCH,
    }
    assert status["recoverable_partials"] == [
        {"package_id": "tiny-tutor", "version": "1.0.0", "file_count": 1, "bytes_present": len(received)}
    ]


@pytest.mark.asyncio
async def test_required_license_must_be_explicitly_accepted_before_any_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _module(monkeypatch)
    payload = b"accepted"
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, content=payload)

    manager, _store = _manager(module, tmp_path, _catalog(module, payload, requires_acceptance=True), handler)
    with pytest.raises(module.LocalModelDownloadError) as declined:
        await manager.install("tiny-tutor", "1.0.0")
    assert declined.value.code == module.LOCAL_MODEL_LICENSE_NOT_ACCEPTED
    assert requests == 0
    assert (await _install_and_wait(manager, license_accepted=True))["last"]["state"] == "installed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("store_code", "public_code"),
    [
        ("local_model_disk_insufficient", "local_model_disk_insufficient"),
        ("local_model_staging_invalid", "local_model_install_failed"),
    ],
)
async def test_store_errors_are_normalized_and_retained_in_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    store_code: str,
    public_code: str,
) -> None:
    module = _module(monkeypatch)
    payload = b"store-failure"
    catalog = _catalog(module, payload)
    manager, store = _manager(module, tmp_path, catalog, lambda _request: httpx.Response(200, content=payload))

    def fail_install(*_args: object) -> object:
        raise module.LocalModelAssetError(store_code, "C:/private/path?token=secret")

    monkeypatch.setattr(store, "install_from_staging", fail_install)
    assert (await _install_and_wait(manager))["last"] == {
        "state": "failed",
        "package_id": "tiny-tutor",
        "version": "1.0.0",
        "error_code": public_code,
    }


@pytest.mark.asyncio
async def test_total_disk_space_is_checked_before_queueing_or_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _module(monkeypatch)
    payload = b"disk-check"
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=payload)

    manager, store = _manager(module, tmp_path, _catalog(module, payload), handler)

    def no_space(*_args: object) -> None:
        raise module.LocalModelAssetError("local_model_disk_insufficient")

    monkeypatch.setattr(store, "ensure_disk_space", no_space)
    with pytest.raises(module.LocalModelDownloadError) as raised:
        await manager.install("tiny-tutor", "1.0.0")
    assert raised.value.code == "local_model_disk_insufficient"
    assert calls == 0
    assert (await manager.status())["downloads"] == []


@pytest.mark.asyncio
async def test_length_network_and_timeout_failures_have_fixed_safe_codes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _module(monkeypatch)
    payload = b"012345"
    catalog = _catalog(module, payload)

    manager, _store = _manager(
        module, tmp_path / "length", catalog, lambda _request: httpx.Response(200, content=b"short")
    )
    assert (await _install_and_wait(manager))["last"]["error_code"] == module.LOCAL_MODEL_SIZE_MISMATCH

    manager, _store = _manager(
        module,
        tmp_path / "network",
        catalog,
        lambda request: (_ for _ in ()).throw(httpx.ConnectError("secret?token=x", request=request)),
    )
    assert (await _install_and_wait(manager))["last"]["error_code"] == module.LOCAL_MODEL_DOWNLOAD_FAILED

    manager, _store = _manager(
        module,
        tmp_path / "timeout",
        catalog,
        lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("sensitive", request=request)),
    )
    assert (await _install_and_wait(manager))["last"]["error_code"] == module.LOCAL_MODEL_DOWNLOAD_FAILED


@pytest.mark.asyncio
async def test_redirects_are_revalidated_against_https_allowlist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _module(monkeypatch)
    payload = b"redirect!"
    catalog = _catalog(module, payload)

    def allowed(request: httpx.Request) -> httpx.Response:
        if request.url.host == "models.example":
            return httpx.Response(302, headers={"Location": "https://cdn.models.example/model.bin"})
        return httpx.Response(200, content=payload)

    manager, _store = _manager(module, tmp_path / "allowed", catalog, allowed)
    assert (await _install_and_wait(manager))["last"]["state"] == "installed"

    manager, _store = _manager(
        module,
        tmp_path / "blocked",
        catalog,
        lambda _request: httpx.Response(302, headers={"Location": "https://evil.example/model.bin?secret=x"}),
    )
    assert (await _install_and_wait(manager))["last"]["error_code"] == module.LOCAL_MODEL_REDIRECT_REJECTED


class _GateStream(httpx.AsyncByteStream):
    def __init__(self, first: bytes, rest: bytes, started: asyncio.Event, gate: asyncio.Event) -> None:
        self._first = first
        self._rest = rest
        self._started = started
        self._gate = gate

    async def __aiter__(self):
        self._started.set()
        yield self._first
        await self._gate.wait()
        yield self._rest

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_same_package_deduplicates_and_other_package_is_globally_serialized(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _module(monkeypatch)
    payload = b"abcdefgh"
    catalog = _catalog(module, payload)
    catalog = replace(
        catalog,
        packages=(catalog.packages[0], replace(catalog.packages[0], package_id="other-model")),
    )
    started, gate = asyncio.Event(), asyncio.Event()
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, stream=_GateStream(payload[:4], payload[4:], started, gate))

    manager, _store = _manager(module, tmp_path, catalog, handler)
    first = await manager.install("tiny-tutor", "1.0.0")
    await started.wait()
    second = await manager.install("tiny-tutor", "1.0.0")
    with pytest.raises(module.LocalModelDownloadError) as busy:
        await manager.install("other-model", "1.0.0")
    assert busy.value.code == module.LOCAL_MODEL_BUSY
    gate.set()
    assert first["state"] == "queued"
    assert second["state"] in {"checking", "downloading"}
    assert (await _wait_for_terminal(manager))["last"]["state"] == "installed"
    assert calls == 1


@pytest.mark.asyncio
async def test_two_managers_share_one_store_write_lease(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = _module(monkeypatch)
    payload = b"lease-test"
    catalog = _catalog(module, payload)
    started, gate = asyncio.Event(), asyncio.Event()
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, stream=_GateStream(payload[:4], payload[4:], started, gate))

    first, _store = _manager(module, tmp_path, catalog, handler)
    second_store = module.LocalModelStore(catalog, root=tmp_path, minimum_free_bytes=0)
    second = module.LocalModelDownloadManager(
        catalog=catalog,
        store=second_store,
        transport=httpx.MockTransport(handler),
    )
    assert (await first.install("tiny-tutor", "1.0.0"))["state"] == "queued"
    await started.wait()
    with pytest.raises(module.LocalModelDownloadError) as busy:
        await second.install("tiny-tutor", "1.0.0")
    assert busy.value.code == module.LOCAL_MODEL_BUSY
    assert calls == 1
    gate.set()
    assert (await _wait_for_terminal(first))["last"]["state"] == "installed"


@pytest.mark.asyncio
async def test_pause_resume_and_cancel_clean_partial(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = _module(monkeypatch)
    payload = b"abcdefgh"
    catalog = _catalog(module, payload)
    started, gate = asyncio.Event(), asyncio.Event()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_GateStream(payload[:4], payload[4:], started, gate))

    manager, store = _manager(module, tmp_path / "pause", catalog, handler)
    queued = await manager.install("tiny-tutor", "1.0.0")
    assert queued["state"] == "queued"
    await started.wait()
    assert (await manager.pause("tiny-tutor", "1.0.0"))["state"] == "paused"
    with pytest.raises(module.LocalModelDownloadError) as paused:
        await manager.pause("tiny-tutor", "1.0.0")
    assert paused.value.code == module.LOCAL_MODEL_DOWNLOAD_PAUSED
    gate.set()
    await asyncio.sleep(0.05)
    assert (await manager.status())["state"] == "paused"
    assert (await manager.resume("tiny-tutor", "1.0.0"))["state"] == "downloading"
    assert (await _wait_for_terminal(manager))["last"]["state"] == "installed"

    started, gate = asyncio.Event(), asyncio.Event()
    manager, store = _manager(module, tmp_path / "cancel", catalog, handler)
    await manager.install("tiny-tutor", "1.0.0")
    await started.wait()
    await manager.pause("tiny-tutor", "1.0.0")
    await manager.cancel("tiny-tutor", "1.0.0")
    gate.set()
    assert (await _wait_for_terminal(manager))["last"]["error_code"] == module.LOCAL_MODEL_DOWNLOAD_CANCELED
    partial = store.staging_partial_path("tiny-tutor", "1.0.0", "model.bin")
    assert not partial.exists()


@pytest.mark.asyncio
async def test_paused_queued_install_does_not_open_http_until_resumed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _module(monkeypatch)
    payload = b"pause-before-http"
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=payload)

    manager, _store = _manager(module, tmp_path, _catalog(module, payload), handler)
    assert (await manager.install("tiny-tutor", "1.0.0"))["state"] == "queued"
    assert (await manager.pause("tiny-tutor", "1.0.0"))["state"] == "paused"
    await asyncio.sleep(0)
    assert calls == 0
    await manager.resume("tiny-tutor", "1.0.0")
    assert (await _wait_for_terminal(manager))["last"]["state"] == "installed"
    assert calls == 1


@pytest.mark.asyncio
async def test_retry_reuses_valid_promoted_files_after_later_file_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _module(monkeypatch)
    first, second = b"first-file", b"second-file"
    catalog = _two_file_catalog(module, first, second)
    requests: list[str] = []
    second_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal second_attempts
        name = request.url.path.rsplit("/", 1)[-1]
        requests.append(name)
        if name == "first.bin":
            return httpx.Response(200, content=first)
        second_attempts += 1
        if second_attempts == 1:
            raise httpx.ConnectError("temporary disconnect", request=request)
        return httpx.Response(200, content=second)

    manager, store = _manager(module, tmp_path, catalog, handler)
    first_run = await _install_and_wait(manager)
    assert first_run["last"]["error_code"] == module.LOCAL_MODEL_DOWNLOAD_FAILED
    assert store.staging_file_status("tiny-tutor", "1.0.0", "first.bin") == {
        "exists": True,
        "valid": True,
        "size_bytes": len(first),
    }

    request_count_before_retry = len(requests)
    assert (await _install_and_wait(manager))["last"]["state"] == "installed"
    assert requests[request_count_before_retry:] == ["second.bin"]
    assert (tmp_path / "packages" / "tiny-tutor" / "1.0.0" / "first.bin").read_bytes() == first
    assert (tmp_path / "packages" / "tiny-tutor" / "1.0.0" / "second.bin").read_bytes() == second


@pytest.mark.asyncio
async def test_corrupt_promoted_staging_is_rebuilt_without_blocking_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _module(monkeypatch)
    first, second = b"first-file", b"second-file"
    catalog = _two_file_catalog(module, first, second)
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        requests.append(name)
        return httpx.Response(200, content=first if name == "first.bin" else second)

    manager, store = _manager(module, tmp_path, catalog, handler)
    corrupt = store.staging_path("tiny-tutor", "1.0.0", "first.bin")
    corrupt.parent.mkdir(parents=True, exist_ok=True)
    corrupt.write_bytes(b"corrupt")

    terminal = await _install_and_wait(manager)

    assert terminal["last"]["state"] == "installed"
    assert requests == ["first.bin", "second.bin"]


@pytest.mark.asyncio
async def test_explicit_cancel_cleans_all_staged_files_for_package(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _module(monkeypatch)
    first, second = b"first-file", b"second-file"
    catalog = _two_file_catalog(module, first, second)
    second_started, gate = asyncio.Event(), asyncio.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("first.bin"):
            return httpx.Response(200, content=first)
        return httpx.Response(200, stream=_GateStream(second[:2], second[2:], second_started, gate))

    manager, store = _manager(module, tmp_path, catalog, handler)
    assert (await manager.install("tiny-tutor", "1.0.0"))["state"] == "queued"
    await second_started.wait()
    progress = await manager.status()
    assert progress["downloads"][0]["downloaded_bytes"] >= len(first)

    assert (await manager.cancel("tiny-tutor", "1.0.0"))["state"] == "cancelling"
    terminal = await _wait_for_terminal(manager)
    assert terminal["last"]["error_code"] == module.LOCAL_MODEL_DOWNLOAD_CANCELED
    assert store.staging_file_status("tiny-tutor", "1.0.0", "first.bin")["exists"] is False
    assert store.partial_status("tiny-tutor", "1.0.0", "second.bin")["exists"] is False
