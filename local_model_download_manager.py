"""Explicit, resumable, local-model package downloads.

This module is intentionally isolated from model inference and from the host
LLM client.  It has no credentials, disables proxy environment variables, and
only follows validated HTTPS redirects to catalog-approved hosts.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from .local_model_manifest import (
    LocalModelAssetError,
    LocalModelCatalog,
    ModelFile,
    ModelPackage,
)
from .local_model_store import LocalModelStore

DOWNLOAD_BUFFER_BYTES = 1024 * 1024
MIN_DOWNLOAD_BUFFER_BYTES = 1024 * 1024
MAX_DOWNLOAD_BUFFER_BYTES = 4 * 1024 * 1024
MAX_REDIRECTS = 3

LOCAL_MODEL_BUSY = "local_model_busy"
LOCAL_MODEL_DOWNLOAD_CANCELED = "local_model_download_canceled"
LOCAL_MODEL_DOWNLOAD_PAUSED = "local_model_download_paused"
LOCAL_MODEL_REDIRECT_REJECTED = "local_model_redirect_rejected"
LOCAL_MODEL_SIZE_MISMATCH = "local_model_size_mismatch"
LOCAL_MODEL_HASH_MISMATCH = "local_model_hash_mismatch"
LOCAL_MODEL_DOWNLOAD_FAILED = "local_model_download_failed"
LOCAL_MODEL_LICENSE_NOT_ACCEPTED = "local_model_license_not_accepted"

_PUBLIC_ERROR_CODES = frozenset(
    {
        LOCAL_MODEL_BUSY,
        LOCAL_MODEL_DOWNLOAD_CANCELED,
        LOCAL_MODEL_DOWNLOAD_PAUSED,
        LOCAL_MODEL_REDIRECT_REJECTED,
        LOCAL_MODEL_SIZE_MISMATCH,
        LOCAL_MODEL_HASH_MISMATCH,
        LOCAL_MODEL_DOWNLOAD_FAILED,
        LOCAL_MODEL_LICENSE_NOT_ACCEPTED,
        "local_model_catalog_invalid",
        "local_model_unknown",
        "local_model_version_unsupported",
        "local_model_path_invalid",
        "local_model_store_unavailable",
        "local_model_disk_insufficient",
        "local_model_manifest_invalid",
        "local_model_install_failed",
        "local_model_uninstall_failed",
    }
)

_ASSET_ERROR_CODE_MAP = {
    "local_model_catalog_invalid": "local_model_catalog_invalid",
    "local_model_unknown": "local_model_unknown",
    "local_model_version_unsupported": "local_model_version_unsupported",
    "local_model_path_invalid": "local_model_path_invalid",
    "local_model_store_unavailable": "local_model_store_unavailable",
    "local_model_disk_insufficient": "local_model_disk_insufficient",
    "local_model_installed_manifest_invalid": "local_model_manifest_invalid",
    "local_model_uninstall_failed": "local_model_uninstall_failed",
    "local_model_size_invalid": LOCAL_MODEL_SIZE_MISMATCH,
    "local_model_file_validation_failed": "local_model_install_failed",
    "local_model_file_not_found": "local_model_install_failed",
    "local_model_staging_cleanup_failed": "local_model_install_failed",
    "local_model_staging_conflict": "local_model_install_failed",
    "local_model_staging_incomplete": "local_model_install_failed",
    "local_model_staging_invalid": "local_model_install_failed",
    "local_model_install_conflict": "local_model_install_failed",
    "local_model_install_failed": "local_model_install_failed",
    "local_model_installed_manifest_write_failed": "local_model_install_failed",
    "local_model_busy": LOCAL_MODEL_BUSY,
}


class LocalModelDownloadError(RuntimeError):
    """A fixed, non-sensitive downloader error for UI diagnostics."""

    def __init__(self, code: str, message: str = "") -> None:
        candidate = str(code or "")
        self.code = candidate if candidate in _PUBLIC_ERROR_CODES else LOCAL_MODEL_DOWNLOAD_FAILED
        self.diagnostic = self.code
        super().__init__(message or self.code)

    def to_payload(self) -> dict[str, dict[str, str]]:
        return {"error": {"code": self.code}}


@dataclass(slots=True)
class _DownloadJob:
    package: ModelPackage
    task: asyncio.Task[dict[str, object]] | None = None
    resume_event: asyncio.Event = field(default_factory=asyncio.Event)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    paused: bool = False
    explicit_cancel: bool = False
    shutdown_requested: bool = False
    state: str = "queued"
    downloaded_bytes: int = 0
    total_bytes: int = 0
    current_file: str = ""
    lease: Any | None = None

    def __post_init__(self) -> None:
        self.resume_event.set()
        self.total_bytes = self.package.total_size_bytes

    @property
    def identity(self) -> tuple[str, str]:
        return self.package.package_id, self.package.version


class LocalModelDownloadManager:
    """Serialises user-initiated package installs and owns partial downloads."""

    def __init__(
        self,
        directory: Path | str | None = None,
        logger: Any | None = None,
        *,
        catalog: LocalModelCatalog | None = None,
        store: LocalModelStore | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 30.0,
        buffer_bytes: int = DOWNLOAD_BUFFER_BYTES,
    ) -> None:
        self._logger = logger
        self._catalog = catalog or LocalModelCatalog.load(Path(__file__).with_name("local_models") / "catalog.v1.json")
        self._directory = self._validated_model_directory(directory) if store is None else None
        self._store = store or LocalModelStore(self._catalog, root=self._directory)
        self._transport = transport
        self._timeout_seconds = max(1.0, float(timeout_seconds))
        self._buffer_bytes = min(
            MAX_DOWNLOAD_BUFFER_BYTES,
            max(MIN_DOWNLOAD_BUFFER_BYTES, int(buffer_bytes)),
        )
        self._lock = asyncio.Lock()
        self._active: _DownloadJob | None = None
        self._last: dict[str, object] | None = None
        self._closed = False

    async def catalog(self) -> dict[str, object]:
        """Return safe catalog metadata only; never starts a download."""

        return {
            "catalog_version": self._catalog.version,
            "packages": [self._package_payload(item) for item in self._catalog.packages],
        }

    async def status(self) -> dict[str, object]:
        """Report installs and any active transfer without networking."""

        try:
            installed = await self._store_call("installed_packages")
            partials = await self._recoverable_partials()
            store_status = await self._store_call("get_status")
        except LocalModelDownloadError as exc:
            self._last = {"state": "failed", "error_code": exc.code}
            return {
                "state": "failed",
                "installed": [],
                "downloads": [],
                "disk": {},
                "last": dict(self._last),
                "recoverable_partials": [],
            }
        async with self._lock:
            job = self._active
            active = (
                {
                    "package_id": job.package.package_id,
                    "version": job.package.version,
                    "state": "paused" if job.paused else job.state,
                    "current_file": job.current_file,
                    "downloaded_bytes": job.downloaded_bytes,
                    "total_bytes": job.total_bytes,
                }
                if job is not None
                else None
            )
        last = dict(self._last) if self._last is not None else None
        return {
            "state": str((active or last or {}).get("state") or "ready"),
            "installed": self._installed_payloads(installed),
            "downloads": [active] if active is not None else [],
            "disk": store_status if isinstance(store_status, dict) else {},
            "last": last,
            "recoverable_partials": partials,
        }

    async def install(self, package_id: str, version: str, *, license_accepted: bool = False) -> dict[str, object]:
        """Queue one explicitly requested install without awaiting its transfer."""

        if self._closed:
            raise LocalModelDownloadError(LOCAL_MODEL_DOWNLOAD_CANCELED)
        try:
            package = self._catalog.package(package_id, version)
        except LocalModelAssetError as exc:
            raise self._asset_error(exc) from exc
        if package.requires_acceptance and not license_accepted:
            raise LocalModelDownloadError(LOCAL_MODEL_LICENSE_NOT_ACCEPTED)
        async with self._lock:
            if self._active is not None:
                if self._active.identity != (package.package_id, package.version):
                    raise LocalModelDownloadError(LOCAL_MODEL_BUSY)
                return self._job_payload(self._active)
        await self._store_call("ensure_disk_space", package.total_size_bytes)
        lease = await self._store_call("acquire_download_lease")
        async with self._lock:
            if self._active is not None:
                await self._release_lease(lease)
                if self._active.identity != (package.package_id, package.version):
                    raise LocalModelDownloadError(LOCAL_MODEL_BUSY)
                if self._active.task is None:  # pragma: no cover - construction is atomic below.
                    raise LocalModelDownloadError(LOCAL_MODEL_BUSY)
                return self._job_payload(self._active)
            else:
                job = _DownloadJob(package)
                job.lease = lease
                job.task = asyncio.create_task(self._install_job(job))
                job.task.add_done_callback(
                    lambda task, active_job=job: self._consume_background_result(task, active_job)
                )
                self._active = job
                self._last = None
        return {"state": "queued", "package_id": package.package_id, "version": package.version}

    async def pause(self, package_id: str, version: str) -> dict[str, object]:
        job = await self._active_job(package_id, version)
        if job.paused:
            raise LocalModelDownloadError(LOCAL_MODEL_DOWNLOAD_PAUSED)
        job.paused = True
        job.state = "paused"
        job.resume_event.clear()
        return {"state": "paused", "package_id": job.package.package_id, "version": job.package.version}

    async def resume(self, package_id: str, version: str) -> dict[str, object]:
        job = await self._active_job(package_id, version)
        job.paused = False
        job.state = "downloading"
        job.resume_event.set()
        return {"state": "downloading", "package_id": job.package.package_id, "version": job.package.version}

    async def cancel(self, package_id: str, version: str) -> dict[str, object]:
        job = await self._active_job(package_id, version)
        job.explicit_cancel = True
        # Make the transition visible to a polling UI immediately.  In
        # particular, do not leave a previously-paused job looking paused
        # while its task is being unwound and its staging area is removed.
        job.paused = False
        job.state = "cancelling"
        job.cancel_event.set()
        job.resume_event.set()
        if job.task is not None:
            job.task.cancel()
        return {"state": "cancelling", "package_id": job.package.package_id, "version": job.package.version}

    async def uninstall(self, package_id: str, version: str) -> object:
        """Remove a package only when no transfer for it is active."""

        async with self._lock:
            if self._active is not None and self._active.package.package_id == package_id:
                raise LocalModelDownloadError(LOCAL_MODEL_BUSY)
        try:
            return await self._store_call("uninstall", package_id, version)
        except LocalModelDownloadError as exc:
            self._last = {"state": "failed", "error_code": exc.code}
            raise

    async def set_directory(self, directory: Path | str | None) -> dict[str, object]:
        """Switch storage roots only while idle; no files are copied implicitly."""

        async with self._lock:
            if self._active is not None:
                raise LocalModelDownloadError(LOCAL_MODEL_BUSY)
            installed = await self._store_call("installed_packages")
            if installed:
                raise LocalModelDownloadError(LOCAL_MODEL_BUSY)
            self._directory = self._validated_model_directory(directory)
            try:
                self._store = LocalModelStore(self._catalog, root=self._directory)
            except LocalModelAssetError as exc:
                error = self._asset_error(exc)
                self._last = {"state": "failed", "error_code": error.code}
                raise error from exc
        try:
            await self._store_call("recover")
        except LocalModelDownloadError as exc:
            self._last = {"state": "failed", "error_code": exc.code}
            raise
        return await self.status()

    async def shutdown(self) -> None:
        """Stop active transfer, preserve recoverable staging, and wait for it."""

        self._closed = True
        async with self._lock:
            job = self._active
            if job is not None:
                job.shutdown_requested = True
                job.resume_event.set()
                task = job.task
            else:
                task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (LocalModelDownloadError, asyncio.CancelledError):
                pass

    async def _install_job(self, job: _DownloadJob) -> dict[str, object]:
        try:
            job.state = "checking"
            await self._store_call("recover")
            staged_statuses: dict[str, dict[str, object]] = {}
            for item in job.package.files:
                status = await self._store_call(
                    "staging_file_status",
                    job.package.package_id,
                    job.package.version,
                    item.path,
                )
                staged_statuses[item.path] = status if isinstance(status, dict) else {}
            if any(status.get("exists") and not status.get("valid") for status in staged_statuses.values()):
                # A promoted staging file without the catalog hash cannot be
                # resumed safely. Clear only this package version and rebuild
                # it; other packages and installed versions remain untouched.
                await self._cleanup_package_staging(job.package)
                staged_statuses = {}
            for item in job.package.files:
                job.current_file = item.path
                staged = staged_statuses.get(item.path, {})
                if isinstance(staged, dict) and staged.get("valid"):
                    # A prior run may already have promoted this file.  It is
                    # catalog-validated by the Store, so leave it in place and
                    # continue from the next file without opening the network.
                    job.downloaded_bytes += item.size_bytes
                    continue
                job.state = "downloading"
                partial = await self._download_file(job, item)
                job.state = "verifying"
                await self._store_call("promote_partial", job.package.package_id, job.package.version, item.path)
                self._remove_file(partial.with_name(f"{partial.name}.download.json"))
                self._remove_empty_partial_parents(partial.parent)
            await self._store_call("validate_staging", job.package.package_id, job.package.version)
            job.state = "installing"
            installed = await self._store_call("install_from_staging", job.package.package_id, job.package.version)
            result = {
                "state": "installed",
                "package_id": job.package.package_id,
                "version": job.package.version,
                "installed": self._installed_payload(installed),
            }
            self._last = {
                "state": "installed",
                "package_id": job.package.package_id,
                "version": job.package.version,
                "error_code": "",
            }
            return result
        except asyncio.CancelledError as exc:
            if job.shutdown_requested:
                raise
            if job.explicit_cancel:
                try:
                    await self._cleanup_package_staging(job.package)
                except LocalModelDownloadError as cleanup_error:
                    self._last = {
                        "state": "failed",
                        "package_id": job.package.package_id,
                        "version": job.package.version,
                        "error_code": "local_model_install_failed",
                    }
                    raise cleanup_error from exc
            self._last = {
                "state": "canceled",
                "package_id": job.package.package_id,
                "version": job.package.version,
                "error_code": LOCAL_MODEL_DOWNLOAD_CANCELED,
            }
            raise LocalModelDownloadError(LOCAL_MODEL_DOWNLOAD_CANCELED) from exc
        except LocalModelDownloadError as exc:
            if job.cancel_event.is_set():
                if job.explicit_cancel:
                    try:
                        await self._cleanup_package_staging(job.package)
                    except LocalModelDownloadError as cleanup_error:
                        self._last = {
                            "state": "failed",
                            "package_id": job.package.package_id,
                            "version": job.package.version,
                            "error_code": "local_model_install_failed",
                        }
                        raise cleanup_error from exc
                self._last = {
                    "state": "canceled",
                    "package_id": job.package.package_id,
                    "version": job.package.version,
                    "error_code": LOCAL_MODEL_DOWNLOAD_CANCELED,
                }
                raise LocalModelDownloadError(LOCAL_MODEL_DOWNLOAD_CANCELED)
            self._last = {
                "state": "failed",
                "package_id": job.package.package_id,
                "version": job.package.version,
                "error_code": exc.code,
            }
            raise
        finally:
            await self._release_lease(job.lease)
            async with self._lock:
                if self._active is job:
                    self._active = None

    def _consume_background_result(self, task: asyncio.Task[dict[str, object]], job: _DownloadJob) -> None:
        """Consume task failures so background work never emits unhandled warnings."""

        try:
            task.result()
        except (LocalModelDownloadError, asyncio.CancelledError):
            return
        except Exception:
            if self._last is None:
                self._last = {
                    "state": "failed",
                    "package_id": job.package.package_id,
                    "version": job.package.version,
                    "error_code": LOCAL_MODEL_DOWNLOAD_FAILED,
                }

    @staticmethod
    def _job_payload(job: _DownloadJob) -> dict[str, object]:
        return {
            "state": "paused" if job.paused else job.state,
            "package_id": job.package.package_id,
            "version": job.package.version,
        }

    async def _download_file(self, job: _DownloadJob, item: ModelFile) -> Path:
        target = await self._store_call("staging_partial_path", job.package.package_id, job.package.version, item.path)
        partial_path = Path(target)
        partial_path.parent.mkdir(parents=True, exist_ok=True)
        metadata_path = partial_path.with_name(f"{partial_path.name}.download.json")
        existing = partial_path.stat().st_size if partial_path.exists() else 0
        metadata = self._read_metadata(metadata_path)
        if existing and not self._metadata_matches(metadata, item):
            self._remove_file(partial_path)
            self._remove_file(metadata_path)
            existing = 0
            metadata = {}
        if existing > item.size_bytes:
            self._remove_file(partial_path)
            self._remove_file(metadata_path)
            existing = 0
        job.current_file = item.path
        completed_before_file = job.downloaded_bytes
        job.downloaded_bytes = completed_before_file + existing

        # A queued/checking task may be paused before it has opened a network
        # connection.  Honour that state before constructing the HTTP request.
        await self._wait_for_resume_or_cancel(job)
        response, resume_from = await self._open_download(item, existing, metadata)
        try:
            mode = "ab" if resume_from else "wb"
            if not resume_from:
                existing = 0
            etag = response.headers.get("ETag", "")
            self._write_metadata(metadata_path, item, etag)
            with partial_path.open(mode) as handle:
                async for chunk in response.aiter_bytes(self._buffer_bytes):
                    await self._wait_for_resume_or_cancel(job)
                    if not chunk:
                        continue
                    handle.write(chunk)
                    existing += len(chunk)
                    job.downloaded_bytes += len(chunk)
                    if existing > item.size_bytes:
                        raise LocalModelDownloadError(LOCAL_MODEL_SIZE_MISMATCH)
            if existing != item.size_bytes:
                raise LocalModelDownloadError(LOCAL_MODEL_SIZE_MISMATCH)
            digest = await self._sha256(partial_path, job)
            if digest != item.sha256:
                raise LocalModelDownloadError(LOCAL_MODEL_HASH_MISMATCH)
            return partial_path
        finally:
            await self._close_response(response)

    async def _open_download(
        self, item: ModelFile, existing: int, metadata: dict[str, object]
    ) -> tuple[httpx.Response, int]:
        url = self._validated_url(item.url)
        retry_without_range = False
        for _attempt in range(2):
            headers = {
                "Accept": "application/octet-stream",
                "Accept-Encoding": "identity",
            }
            if existing and not retry_without_range:
                headers["Range"] = f"bytes={existing}-"
                etag = self._metadata_etag(metadata)
                if etag:
                    headers["If-Range"] = etag
            response = await self._stream_with_validated_redirects(url, headers=headers)
            if existing and not retry_without_range:
                if (
                    response.status_code == 206
                    and self._valid_content_range(response, existing)
                    and self._etag_matches(response, metadata)
                    and self._content_length_matches(response, item.size_bytes - existing)
                ):
                    return response, existing
                if response.status_code in {200, 206, 416}:
                    await self._close_response(response)
                    retry_without_range = True
                    existing = 0
                    continue
                await self._close_response(response)
                raise LocalModelDownloadError(LOCAL_MODEL_DOWNLOAD_FAILED)
            if response.status_code == 200:
                if self._content_length_matches(response, item.size_bytes):
                    return response, 0
                await self._close_response(response)
                raise LocalModelDownloadError(LOCAL_MODEL_SIZE_MISMATCH)
            await self._close_response(response)
            raise LocalModelDownloadError(LOCAL_MODEL_DOWNLOAD_FAILED)
        raise LocalModelDownloadError(LOCAL_MODEL_DOWNLOAD_FAILED)

    async def _stream_with_validated_redirects(self, url: str, *, headers: dict[str, str]) -> httpx.Response:
        current = url
        client = httpx.AsyncClient(
            transport=self._transport,
            timeout=httpx.Timeout(self._timeout_seconds),
            trust_env=False,
            follow_redirects=False,
        )
        # Keep the client alive until caller closes the returned response.
        # httpx attaches the transport to the response; the explicit close below
        # is paired with the client close callback installed here.
        try:
            for _redirect in range(MAX_REDIRECTS + 1):
                request = client.build_request("GET", current, headers=headers)
                response = await client.send(request, stream=True)
                if response.status_code not in {301, 302, 303, 307, 308}:
                    response.extensions["local_model_client"] = client
                    return response
                location = response.headers.get("Location")
                await response.aclose()
                if not location or _redirect >= MAX_REDIRECTS:
                    raise LocalModelDownloadError(LOCAL_MODEL_REDIRECT_REJECTED)
                current = self._validated_url(urljoin(current, location))
            raise LocalModelDownloadError(LOCAL_MODEL_REDIRECT_REJECTED)
        except httpx.TimeoutException as exc:
            await client.aclose()
            raise LocalModelDownloadError(LOCAL_MODEL_DOWNLOAD_FAILED) from exc
        except httpx.HTTPError as exc:
            await client.aclose()
            raise LocalModelDownloadError(LOCAL_MODEL_DOWNLOAD_FAILED) from exc
        except Exception:
            await client.aclose()
            raise

    async def _wait_for_resume_or_cancel(self, job: _DownloadJob) -> None:
        while not job.resume_event.is_set():
            if job.cancel_event.is_set():
                raise LocalModelDownloadError(LOCAL_MODEL_DOWNLOAD_CANCELED)
            try:
                await asyncio.wait_for(job.resume_event.wait(), timeout=0.1)
            except TimeoutError:
                continue
        if job.cancel_event.is_set():
            raise LocalModelDownloadError(LOCAL_MODEL_DOWNLOAD_CANCELED)

    async def _active_job(self, package_id: str, version: str) -> _DownloadJob:
        async with self._lock:
            job = self._active
            if job is None or job.identity != (package_id, version):
                raise LocalModelDownloadError(LOCAL_MODEL_BUSY)
            return job

    async def _cleanup_package_staging(self, package: ModelPackage) -> None:
        """Remove one package's complete staging tree after an explicit cancel.

        Failed and shutdown jobs deliberately do not call this method: their
        already-promoted files and valid byte partials are recovery state.
        """

        await self._store_call("cleanup_staging", package.package_id, package.version)

    async def _store_call(self, name: str, *args: object) -> Any:
        try:
            result = getattr(self._store, name)(*args)
            return await result if inspect.isawaitable(result) else result
        except LocalModelAssetError as exc:
            raise self._asset_error(exc) from exc

    async def _release_lease(self, lease: Any | None) -> None:
        if lease is None:
            return
        try:
            result = lease.release()
            if inspect.isawaitable(result):
                await result
        except LocalModelAssetError as exc:
            error = self._asset_error(exc)
            self._last = {"state": "failed", "error_code": error.code}

    @staticmethod
    def _asset_error(exc: LocalModelAssetError) -> LocalModelDownloadError:
        return LocalModelDownloadError(_ASSET_ERROR_CODE_MAP.get(exc.code, LOCAL_MODEL_DOWNLOAD_FAILED))

    async def _recoverable_partials(self) -> list[dict[str, object]]:
        """Expose resumable byte counts without leaking the model directory."""

        results: list[dict[str, object]] = []
        for package in self._catalog.packages:
            bytes_present = 0
            file_count = 0
            for item in package.files:
                payload = await self._store_call("partial_status", package.package_id, package.version, item.path)
                if not isinstance(payload, dict) or not payload.get("exists"):
                    continue
                partial_path = Path(
                    await self._store_call(
                        "staging_partial_existing_path",
                        package.package_id,
                        package.version,
                        item.path,
                    )
                )
                metadata_path = partial_path.with_name(f"{partial_path.name}.download.json")
                if not self._metadata_matches(self._read_metadata(metadata_path), item):
                    await self._store_call("cleanup_partial", package.package_id, package.version, item.path)
                    self._remove_file(metadata_path)
                    continue
                file_count += 1
                try:
                    bytes_present += max(0, int(payload.get("size_bytes") or 0))
                except (TypeError, ValueError, OverflowError):
                    continue
            if file_count:
                results.append(
                    {
                        "package_id": package.package_id,
                        "version": package.version,
                        "file_count": file_count,
                        "bytes_present": bytes_present,
                    }
                )
        return results

    @staticmethod
    def _validated_model_directory(directory: Path | str | None) -> Path | None:
        """Reject the plugin source tree without disclosing the candidate path."""

        if directory is None:
            return None
        try:
            candidate = Path(directory).resolve(strict=False)
            source_root = Path(__file__).resolve().parent
            candidate.relative_to(source_root)
        except ValueError:
            return Path(directory)
        except OSError as exc:
            raise LocalModelDownloadError("local_model_path_invalid") from exc
        raise LocalModelDownloadError("local_model_path_invalid")

    def _validated_url(self, value: str) -> str:
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError as exc:
            raise LocalModelDownloadError(LOCAL_MODEL_REDIRECT_REJECTED) from exc
        host = (parsed.hostname or "").lower().rstrip(".")
        if (
            parsed.scheme != "https"
            or host not in self._catalog.allowed_hosts
            or parsed.username is not None
            or parsed.password is not None
            or port not in {None, 443}
            or not parsed.path.startswith("/")
            or parsed.query
            or parsed.fragment
        ):
            raise LocalModelDownloadError(LOCAL_MODEL_REDIRECT_REJECTED)
        return value

    @staticmethod
    def _valid_content_range(response: httpx.Response, expected_start: int) -> bool:
        value = response.headers.get("Content-Range", "")
        try:
            unit, range_and_total = value.split(" ", 1)
            byte_range, _total = range_and_total.split("/", 1)
            start, _end = byte_range.split("-", 1)
            return unit == "bytes" and int(start) == expected_start
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _content_length_matches(response: httpx.Response, expected: int) -> bool:
        value = response.headers.get("Content-Length")
        if not value:
            return True
        try:
            return int(value) == expected
        except ValueError:
            return False

    def _etag_matches(self, response: httpx.Response, metadata: dict[str, object]) -> bool:
        expected = self._metadata_etag(metadata)
        actual = response.headers.get("ETag", "")
        return not expected or (bool(actual) and actual == expected)

    @staticmethod
    async def _close_response(response: httpx.Response) -> None:
        await response.aclose()
        client = response.extensions.get("local_model_client")
        if isinstance(client, httpx.AsyncClient):
            await client.aclose()

    async def _sha256(self, path: Path, job: _DownloadJob) -> str:
        """Hash in bounded, cancellable chunks without a worker-thread handle.

        Keeping the handle in this coroutine means an explicit cancellation
        closes it before Store cleanup runs on Windows.  The cooperative yield
        also lets pause/cancel controls take effect for large local files.
        """

        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                await self._wait_for_resume_or_cancel(job)
                block = handle.read(DOWNLOAD_BUFFER_BYTES)
                if not block:
                    break
                digest.update(block)
                await asyncio.sleep(0)
        return digest.hexdigest()

    @staticmethod
    def _read_metadata(path: Path) -> dict[str, object]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return dict(payload) if isinstance(payload, dict) else {}
        except (OSError, ValueError, UnicodeError):
            return {}

    def _metadata_matches(self, metadata: dict[str, object], item: ModelFile) -> bool:
        return (
            metadata.get("catalog_version") == self._catalog.version
            and metadata.get("size_bytes") == item.size_bytes
            and metadata.get("sha256") == item.sha256
            and metadata.get("url") == self._stable_url(item.url)
        )

    @staticmethod
    def _metadata_etag(metadata: dict[str, object]) -> str:
        value = metadata.get("etag")
        return value if isinstance(value, str) else ""

    def _write_metadata(self, path: Path, item: ModelFile, etag: str) -> None:
        payload = {
            "catalog_version": self._catalog.version,
            "size_bytes": item.size_bytes,
            "sha256": item.sha256,
            "url": self._stable_url(item.url),
            "etag": etag,
        }
        path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")

    @staticmethod
    def _stable_url(value: str) -> str:
        parsed = urlsplit(value)
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))

    @staticmethod
    def _remove_file(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _remove_empty_partial_parents(path: Path) -> None:
        """Remove only empty partial ancestors so staging validation can pass."""

        current = path
        while current.name:
            try:
                current.rmdir()
            except OSError:
                return
            if current.name == ".partial":
                return
            current = current.parent

    @staticmethod
    def _package_payload(package: ModelPackage) -> dict[str, object]:
        return {
            "id": package.package_id,
            "version": package.version,
            "role": package.role,
            "size_bytes": package.total_size_bytes,
            "requires_acceptance": package.requires_acceptance,
            "license": {
                "name": package.license.name,
                "spdx": package.license.spdx,
                "url": package.license.url,
                "requires_acceptance": package.license.requires_acceptance,
            },
        }

    @staticmethod
    def _installed_payload(value: object) -> object:
        serializer = getattr(value, "to_payload", None)
        return serializer() if callable(serializer) else value

    @classmethod
    def _installed_payloads(cls, values: object) -> list[object]:
        if isinstance(values, (tuple, list)):
            return [cls._installed_payload(value) for value in values]
        return []


__all__ = [
    "LocalModelDownloadError",
    "LocalModelDownloadManager",
    "LOCAL_MODEL_BUSY",
    "LOCAL_MODEL_DOWNLOAD_CANCELED",
    "LOCAL_MODEL_DOWNLOAD_PAUSED",
    "LOCAL_MODEL_DOWNLOAD_FAILED",
    "LOCAL_MODEL_HASH_MISMATCH",
    "LOCAL_MODEL_SIZE_MISMATCH",
    "LOCAL_MODEL_REDIRECT_REJECTED",
    "LOCAL_MODEL_LICENSE_NOT_ACCEPTED",
]
