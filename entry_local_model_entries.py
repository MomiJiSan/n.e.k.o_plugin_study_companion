"""Compatibility entries for the paused local-model product.

The implementation and model assets live in ``experimental.local_models`` and
are excluded from plugin builds.  These public entries deliberately preserve
their identifiers and response shape without scanning disks, creating model
directories, downloading assets, or starting a local runtime.
"""

from __future__ import annotations

from .entry_common import Err, Ok, SdkError, plugin_entry, tr, ui
from .local_model_compat import (
    LOCAL_MODEL_UNAVAILABLE_CODE,
    catalog_payload,
    status_payload,
)
from .models import StudyConfig


def _unavailable_error() -> Err:
    return Err(
        SdkError(
            "local model assets are unavailable",
            code=LOCAL_MODEL_UNAVAILABLE_CODE,
        )
    )


class _LocalModelEntriesMixin:
    """Keep the public asset-entry surface side-effect free while paused."""

    async def _initialize_local_model_manager(self) -> None:
        self._local_model_manager = None
        self._local_model_catalog_cache = []
        self._local_model_manager_error = LOCAL_MODEL_UNAVAILABLE_CODE

    async def _shutdown_local_model_manager(self) -> None:
        self._local_model_manager = None

    async def _set_local_model_manager_directory(self, _config: StudyConfig) -> None:
        """Compatibility no-op: a configured directory is never accessed."""

        self._local_model_manager = None

    async def _local_model_catalog_payload(self) -> dict[str, object]:
        return catalog_payload(self._cfg)

    async def _local_model_status_payload(self) -> dict[str, object]:
        return status_payload(self._cfg)

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
        llm_result_fields=[
            "state",
            "directory_mode",
            "packages",
            "installed",
            "downloads",
            "disk",
            "error_code",
        ],
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
        input_schema={"type": "object", "properties": {}},
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
        del package_id, version, confirmed, license_accepted
        return _unavailable_error()

    async def _local_model_transfer_entry(self, **_) -> Err:
        return _unavailable_error()

    @ui.action()
    @plugin_entry(
        id="study_local_model_pause",
        name=tr("entries.local_models.pause.name", default="Pause Local Model Download"),
        description=tr("entries.local_models.pause.description", default="Pause an explicit local model download."),
        input_schema={"type": "object", "properties": {}},
        llm_result_fields=["status"],
    )
    async def study_local_model_pause(self, package_id: str, version: str, **_):
        del package_id, version
        return _unavailable_error()

    @ui.action()
    @plugin_entry(
        id="study_local_model_resume",
        name=tr("entries.local_models.resume.name", default="Resume Local Model Download"),
        description=tr("entries.local_models.resume.description", default="Resume an explicit local model download."),
        input_schema={"type": "object", "properties": {}},
        llm_result_fields=["status"],
    )
    async def study_local_model_resume(self, package_id: str, version: str, **_):
        del package_id, version
        return _unavailable_error()

    @ui.action()
    @plugin_entry(
        id="study_local_model_cancel",
        name=tr("entries.local_models.cancel.name", default="Cancel Local Model Download"),
        description=tr("entries.local_models.cancel.description", default="Cancel an explicit local model download."),
        input_schema={"type": "object", "properties": {}},
        llm_result_fields=["status"],
    )
    async def study_local_model_cancel(self, package_id: str, version: str, **_):
        del package_id, version
        return _unavailable_error()

    @ui.action()
    @plugin_entry(
        id="study_local_model_uninstall",
        name=tr("entries.local_models.uninstall.name", default="Uninstall Local Model"),
        description=tr(
            "entries.local_models.uninstall.description",
            default="Remove one local model package after explicit confirmation.",
        ),
        input_schema={"type": "object", "properties": {}},
        llm_result_fields=["status"],
    )
    async def study_local_model_uninstall(
        self,
        package_id: str,
        version: str,
        confirmed: bool = False,
        **_,
    ):
        del package_id, version, confirmed
        return _unavailable_error()

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
        except Exception:
            self._apply_runtime_settings_config(previous_config)
            return _unavailable_error()
        return Ok(
            {
                "config": {"local_models_directory": next_config.local_models_directory},
                "status": await self._local_model_status_payload(),
            }
        )

    async def _persist_local_models_directory(self, config: StudyConfig) -> None:
        await self.config.set(
            "llm.local_models_directory", config.local_models_directory, timeout=5.0
        )
        await self._persist_state()
