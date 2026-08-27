"""UI entry points for optional local-model assets.

The asset manager is intentionally separate from model inference.  These
entries catalog and manage files only; neither status nor catalog requests may
start ``LocalRuntimeSupervisor`` or load a model.
"""

from __future__ import annotations

import inspect
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from ...entry_common import Err, Ok, SdkError, plugin_entry, tr, ui
from ...models import StudyConfig


def _directory_argument(config: StudyConfig) -> Path | None:
    value = str(getattr(config, "local_models_directory", "") or "").strip()
    return Path(value) if value else None


async def _await_if_needed(value: object) -> object:
    return await value if inspect.isawaitable(value) else value


_PUBLIC_ERROR_CODES = frozenset(
    {
        "local_model_busy",
        "local_model_download_canceled",
        "local_model_redirect_rejected",
        "local_model_download_paused",
        "local_model_size_mismatch",
        "local_model_hash_mismatch",
        "local_model_download_failed",
        "local_model_license_not_accepted",
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


def _safe_error_code(value: object, default: str = "local_model_download_failed") -> str:
    """Return a fixed diagnostic code, never an exception message or path."""

    candidate = str(value or "")
    if candidate in _PUBLIC_ERROR_CODES:
        return candidate
    return default


def _error_code(exc: BaseException, default: str = "local_model_download_failed") -> str:
    return _safe_error_code(getattr(exc, "code", ""), default)


def _asset_error(code: str) -> Err:
    """Create a deliberately non-sensitive entry error for asset operations."""

    return Err(SdkError("local model asset operation failed", code=_safe_error_code(code)))


def _safe_https_url(value: object) -> str:
    candidate = str(value or "").strip()
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme == "https"
        and parsed.hostname
        and not parsed.username
        and not parsed.password
        and port in {None, 443}
        and not parsed.query
        and not parsed.fragment
    ):
        return candidate
    return ""


def _payload(value: object) -> object:
    if callable(getattr(value, "to_payload", None)):
        return value.to_payload()
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Mapping):
        return {str(key): _payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_payload(item) for item in value]
    return value


def _catalog_packages(catalog: object) -> list[dict[str, object]]:
    packages = getattr(catalog, "packages", None)
    if not isinstance(packages, (list, tuple)) and isinstance(catalog, Mapping):
        packages = catalog.get("packages")
    if not isinstance(packages, (list, tuple)):
        return []
    payloads: list[dict[str, object]] = []
    for package in packages:
        raw = package if isinstance(package, Mapping) else {}
        license_info = (
            raw.get("license") if isinstance(raw.get("license"), Mapping) else getattr(package, "license", None)
        )
        package_id = raw.get("id") or raw.get("package_id") or getattr(package, "package_id", "")
        version = raw.get("version") or getattr(package, "version", "")
        role = raw.get("role") or getattr(package, "role", "")
        size_bytes = raw.get("size_bytes") or raw.get("total_size_bytes") or getattr(package, "total_size_bytes", 0)
        requires_acceptance = (
            raw.get("requires_acceptance")
            if "requires_acceptance" in raw
            else getattr(license_info, "requires_acceptance", False)
        )
        if isinstance(license_info, Mapping):
            license_name = license_info.get("name")
            license_url = license_info.get("url")
            requires_acceptance = license_info.get("requires_acceptance", requires_acceptance)
        else:
            license_name = getattr(license_info, "name", "")
            license_url = getattr(license_info, "url", "")
        payloads.append(
            {
                "id": str(package_id or ""),
                "version": str(version or ""),
                "role": str(role or ""),
                "size_bytes": int(size_bytes or 0),
                "requires_license_acceptance": bool(requires_acceptance),
                "license": str(license_name or ""),
                "license_url": _safe_https_url(license_url),
            }
        )
    return payloads


def _safe_package_status(items: object, *, active: bool = False) -> list[dict[str, object]]:
    if not isinstance(items, (list, tuple)):
        return []
    result: list[dict[str, object]] = []
    for item in items:
        raw = item if isinstance(item, Mapping) else {}
        package_id = raw.get("package_id") or raw.get("id")
        entry: dict[str, object] = {
            "id": str(package_id or ""),
            "version": str(raw.get("version") or ""),
            "state": str(raw.get("state") or ""),
        }
        error_code = _safe_error_code(raw.get("error_code"), "")
        if error_code:
            entry["error_code"] = error_code
        if active:
            entry["downloaded_bytes"] = int(raw.get("downloaded_bytes") or 0)
            entry["total_bytes"] = int(raw.get("total_bytes") or 0)
        else:
            entry["role"] = str(raw.get("role") or "")
            entry["size_bytes"] = int(raw.get("size_bytes") or 0)
        result.append(entry)
    return result


def _safe_disk_status(value: object) -> dict[str, object]:
    raw = value if isinstance(value, Mapping) else {}
    keys = (
        "disk_available",
        "free_bytes",
        "installed_package_count",
        "installed_bytes",
        "manual_or_invalid_package_count",
        "stale_staging_count",
    )
    return {key: raw[key] for key in keys if key in raw}


class _LocalModelEntriesMixin:
    """Own the optional file-asset manager without coupling it to LLM calls."""

    async def _initialize_local_model_manager(self) -> None:
        """Construct and catalog-scan only; do not download or start inference."""

        await self._shutdown_local_model_manager()
        self._local_model_manager_error = ""
        try:
            from .local_model_download_manager import LocalModelDownloadManager

            manager = LocalModelDownloadManager(directory=_directory_argument(self._cfg), logger=self.logger)
            self._local_model_manager = manager
            catalog = await _await_if_needed(manager.catalog())
            self._local_model_catalog_cache = _catalog_packages(catalog)
            # Status is local-only: it rebuilds the installed-package view and
            # lets the download layer reconcile stale partial files.  It must
            # never initiate a transfer or start LocalRuntimeSupervisor.
            try:
                await _await_if_needed(manager.status())
            except Exception as exc:
                code = _error_code(exc, "local_model_store_unavailable")
                self._local_model_manager_error = code
                self.logger.warning("study local model asset startup recovery unavailable: {}", code)
        except Exception as exc:
            self._local_model_manager = None
            self._local_model_catalog_cache = []
            code = _error_code(exc, "local_model_store_unavailable")
            self._local_model_manager_error = code
            self.logger.warning("study local model asset manager unavailable: {}", code)

    async def _shutdown_local_model_manager(self) -> None:
        manager = getattr(self, "_local_model_manager", None)
        self._local_model_manager = None
        if manager is None:
            return
        shutdown = getattr(manager, "shutdown", None)
        if callable(shutdown):
            await _await_if_needed(shutdown())

    async def _set_local_model_manager_directory(self, config: StudyConfig) -> None:
        """Repoint the asset manager without loading a model or downloading."""

        manager = getattr(self, "_local_model_manager", None)
        if manager is None:
            await self._initialize_local_model_manager()
            return
        set_directory = getattr(manager, "set_directory", None)
        if not callable(set_directory):
            await self._initialize_local_model_manager()
            return
        await _await_if_needed(set_directory(_directory_argument(config)))
        catalog = await _await_if_needed(manager.catalog())
        self._local_model_catalog_cache = _catalog_packages(catalog)

    async def _local_model_catalog_payload(self) -> dict[str, object]:
        directory_mode = "custom" if str(getattr(self._cfg, "local_models_directory", "") or "").strip() else "default"
        manager = getattr(self, "_local_model_manager", None)
        if manager is None:
            return {
                "directory_mode": directory_mode,
                "packages": [],
                "error_code": str(
                    _safe_error_code(
                        getattr(self, "_local_model_manager_error", ""),
                        "local_model_store_unavailable",
                    )
                ),
            }
        try:
            catalog = await _await_if_needed(manager.catalog())
            packages = _catalog_packages(catalog)
            self._local_model_catalog_cache = packages
            return {
                "directory_mode": directory_mode,
                "packages": packages,
                "error_code": "",
            }
        except Exception as exc:
            return {
                "directory_mode": directory_mode,
                "packages": [],
                "error_code": _error_code(exc),
            }

    async def _local_model_status_payload(self) -> dict[str, object]:
        catalog = await self._local_model_catalog_payload()
        manager = getattr(self, "_local_model_manager", None)
        status: object = {}
        if manager is not None:
            try:
                status = await _await_if_needed(manager.status())
            except Exception as exc:
                return {
                    **catalog,
                    "state": "unavailable",
                    "error_code": _error_code(exc),
                    "installed": [],
                    "downloads": [],
                    "disk": {},
                }
        payload = _payload(status)
        status_payload = dict(payload) if isinstance(payload, Mapping) else {}
        return {
            **catalog,
            "directory_mode": str(status_payload.get("directory_mode") or catalog["directory_mode"]),
            "state": str(status_payload.get("state") or "ready"),
            "error_code": _safe_error_code(status_payload.get("error_code") or catalog["error_code"], ""),
            "installed": _safe_package_status(status_payload.get("installed")),
            "downloads": _safe_package_status(status_payload.get("downloads"), active=True),
            "disk": _safe_disk_status(status_payload.get("disk")),
            "last": _safe_package_status([status_payload.get("last")], active=True)[0]
            if isinstance(status_payload.get("last"), Mapping)
            else None,
        }

    async def _local_model_manager_action(
        self,
        action: str,
        *args: object,
        **kwargs: object,
    ) -> dict[str, object]:
        manager = getattr(self, "_local_model_manager", None)
        if manager is None:
            raise SdkError(
                "local model assets are unavailable",
                code=str(getattr(self, "_local_model_manager_error", "") or "local_model_store_unavailable"),
            )
        method = getattr(manager, action, None)
        if not callable(method):
            raise SdkError(
                "local model asset action is unavailable",
                code="local_model_download_failed",
            )
        result = await _await_if_needed(method(*args, **kwargs))
        return {"result": _payload(result), "status": await self._local_model_status_payload()}

    @ui.action()
    @plugin_entry(
        id="study_local_models_catalog",
        name=tr("entries.local_models.catalog.name", default="List Local Model Catalog"),
        description=tr(
            "entries.local_models.catalog.description",
            default="List optional local model packages without starting local inference.",
        ),
        input_schema={"type": "object", "properties": {}},
        llm_result_fields=["directory_mode", "packages", "error_code"],
    )
    async def study_local_models_catalog(self, **_):
        return Ok(await self._local_model_catalog_payload())

    @ui.action()
    @plugin_entry(
        id="study_local_models_status",
        name=tr("entries.local_models.status.name", default="Get Local Model Status"),
        description=tr(
            "entries.local_models.status.description",
            default="Read local model asset status without starting local inference.",
        ),
        input_schema={"type": "object", "properties": {}},
        llm_result_fields=["state", "directory_mode", "packages", "installed", "downloads", "disk", "error_code"],
    )
    async def study_local_models_status(self, **_):
        return Ok(await self._local_model_status_payload())

    @ui.action()
    @plugin_entry(
        id="study_local_model_install",
        name=tr("entries.local_models.install.name", default="Install Local Model"),
        description=tr(
            "entries.local_models.install.description",
            default="Install one catalog package after explicit confirmation.",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "package_id": {"type": "string", "maxLength": 63},
                "version": {"type": "string", "maxLength": 64},
                "confirmed": {"type": "boolean", "default": False},
                "license_accepted": {"type": "boolean", "default": False},
            },
            "required": ["package_id", "version", "confirmed", "license_accepted"],
        },
        llm_result_fields=["status"],
    )
    async def study_local_model_install(
        self,
        package_id: str,
        version: str,
        confirmed: bool = False,
        license_accepted: bool = False,
        **_,
    ):
        if confirmed is not True:
            return _asset_error("local_model_install_failed")
        try:
            return Ok(
                await self._local_model_manager_action(
                    "install",
                    str(package_id or ""),
                    str(version or ""),
                    license_accepted=bool(license_accepted),
                )
            )
        except SdkError as exc:
            return _asset_error(_error_code(exc, "local_model_install_failed"))
        except Exception as exc:
            code = _error_code(exc, "local_model_install_failed")
            self.logger.warning("study local model asset operation install failed: {}", code)
            return _asset_error(code)

    async def _local_model_transfer_entry(self, action: str, package_id: str, version: str):
        try:
            return Ok(await self._local_model_manager_action(action, str(package_id or ""), str(version or "")))
        except SdkError as exc:
            return _asset_error(_error_code(exc, "local_model_download_failed"))
        except Exception as exc:
            code = _error_code(exc, "local_model_download_failed")
            self.logger.warning("study local model asset operation {} failed: {}", action, code)
            return _asset_error(code)

    @ui.action()
    @plugin_entry(
        id="study_local_model_pause",
        name=tr("entries.local_models.pause.name", default="Pause Local Model Download"),
        description=tr("entries.local_models.pause.description", default="Pause an explicit local model download."),
        input_schema={
            "type": "object",
            "properties": {"package_id": {"type": "string"}, "version": {"type": "string"}},
            "required": ["package_id", "version"],
        },
        llm_result_fields=["status"],
    )
    async def study_local_model_pause(self, package_id: str, version: str, **_):
        return await self._local_model_transfer_entry("pause", package_id, version)

    @ui.action()
    @plugin_entry(
        id="study_local_model_resume",
        name=tr("entries.local_models.resume.name", default="Resume Local Model Download"),
        description=tr("entries.local_models.resume.description", default="Resume an explicit local model download."),
        input_schema={
            "type": "object",
            "properties": {"package_id": {"type": "string"}, "version": {"type": "string"}},
            "required": ["package_id", "version"],
        },
        llm_result_fields=["status"],
    )
    async def study_local_model_resume(self, package_id: str, version: str, **_):
        return await self._local_model_transfer_entry("resume", package_id, version)

    @ui.action()
    @plugin_entry(
        id="study_local_model_cancel",
        name=tr("entries.local_models.cancel.name", default="Cancel Local Model Download"),
        description=tr("entries.local_models.cancel.description", default="Cancel an explicit local model download."),
        input_schema={
            "type": "object",
            "properties": {"package_id": {"type": "string"}, "version": {"type": "string"}},
            "required": ["package_id", "version"],
        },
        llm_result_fields=["status"],
    )
    async def study_local_model_cancel(self, package_id: str, version: str, **_):
        return await self._local_model_transfer_entry("cancel", package_id, version)

    @ui.action()
    @plugin_entry(
        id="study_local_model_uninstall",
        name=tr("entries.local_models.uninstall.name", default="Uninstall Local Model"),
        description=tr(
            "entries.local_models.uninstall.description",
            default="Remove one local model package after explicit confirmation.",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "package_id": {"type": "string", "maxLength": 63},
                "version": {"type": "string", "maxLength": 64},
                "confirmed": {"type": "boolean", "default": False},
            },
            "required": ["package_id", "version", "confirmed"],
        },
        llm_result_fields=["status"],
    )
    async def study_local_model_uninstall(self, package_id: str, version: str, confirmed: bool = False, **_):
        if confirmed is not True:
            return _asset_error("local_model_uninstall_failed")
        try:
            return Ok(await self._local_model_manager_action("uninstall", str(package_id or ""), str(version or "")))
        except SdkError as exc:
            return _asset_error(_error_code(exc, "local_model_uninstall_failed"))
        except Exception as exc:
            code = _error_code(exc, "local_model_uninstall_failed")
            self.logger.warning("study local model asset operation uninstall failed: {}", code)
            return _asset_error(code)

    @ui.action()
    @plugin_entry(
        id="study_local_models_set_directory",
        name=tr("entries.local_models.set_directory.name", default="Set Local Model Directory"),
        description=tr(
            "entries.local_models.set_directory.description",
            default="Set the local model asset directory without downloading or loading a model.",
        ),
        input_schema={
            "type": "object",
            "properties": {"directory": {"type": "string", "maxLength": 4096}},
            "required": ["directory"],
        },
        llm_result_fields=["config", "status"],
    )
    async def study_local_models_set_directory(self, directory: str = "", **_):
        previous_config = self._cfg
        next_values = previous_config.to_dict()
        next_values["local_models_directory"] = directory
        next_config = StudyConfig(**next_values)
        try:
            await self._set_local_model_manager_directory(next_config)
            self._apply_runtime_settings_config(next_config)
            await self._persist_local_models_directory(next_config)
            return Ok(
                {
                    "config": {"local_models_directory": next_config.local_models_directory},
                    "status": await self._local_model_status_payload(),
                }
            )
        except Exception as exc:
            try:
                await self._set_local_model_manager_directory(previous_config)
            except Exception:
                pass
            self._apply_runtime_settings_config(previous_config)
            try:
                await self._persist_local_models_directory(previous_config)
            except Exception as rollback_exc:
                rollback_code = _error_code(rollback_exc, "local_model_path_invalid")
                self.logger.warning("study local model asset directory rollback failed: {}", rollback_code)
            code = _error_code(exc, "local_model_path_invalid")
            self.logger.warning("study local model asset directory update failed: {}", code)
            return _asset_error(code)

    async def _persist_local_models_directory(self, config: StudyConfig) -> None:
        """Persist the directory in host config and the study state together."""

        await self.config.set("llm.local_models_directory", config.local_models_directory, timeout=5.0)
        await self._persist_state()
