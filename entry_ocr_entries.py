from __future__ import annotations

import io
from typing import Any

from .entry_common import (
    Err,
    Ok,
    SdkError,
    _entry_exception_error,
    _normalize_submitted_image_payload,
    asyncio,
    base64,
    build_ocr_payload,
    plugin_entry,
    rapidocr_support,
    tesseract_support,
    tr,
    update_install_task_state,
)
from .interactive_screenshot import (
    InteractiveCaptureError,
    capture_interactive_region,
)
from .models import OcrSnapshot

_DOCUMENT_PAGE_OCR_TIMEOUT_SECONDS = 45.0
_DOCUMENT_PAGE_MAX_PIXELS = 8_000_000
_DOCUMENT_PAGE_MAX_BYTES = 6 * 1024 * 1024
_DOCUMENT_PAGE_DATA_URL_PREFIXES = (
    "data:image/jpeg;base64,",
    "data:image/png;base64,",
)


def _decode_document_page_data_url(image_data_url: str) -> Any:
    payload = str(image_data_url or "").strip()
    if not payload.lower().startswith(_DOCUMENT_PAGE_DATA_URL_PREFIXES):
        raise ValueError("only JPEG/PNG data URLs are supported")

    normalized = _normalize_submitted_image_payload(payload)
    encoded = normalized.partition(",")[2]
    raw = base64.b64decode(encoded, validate=True)
    if len(raw) > _DOCUMENT_PAGE_MAX_BYTES:
        raise ValueError("document_pdf_page_too_large")

    from PIL import Image

    try:
        with Image.open(io.BytesIO(raw)) as source:
            width, height = source.size
            if width <= 0 or height <= 0 or width * height > _DOCUMENT_PAGE_MAX_PIXELS:
                raise ValueError("document_pdf_page_too_large")
            source.load()
            if source.format == "PNG" and (
                "A" in source.getbands() or "transparency" in source.info
            ):
                rgba = source.convert("RGBA")
                image = Image.new("RGB", source.size, "white")
                image.paste(rgba, mask=rgba.getchannel("A"))
                return image
            return source.convert("RGB")
    except Image.DecompressionBombError as exc:
        raise ValueError("document_pdf_page_too_large") from exc


def _document_page_ocr_payload(snapshot: OcrSnapshot) -> dict[str, str]:
    return {
        "text": str(snapshot.text or ""),
        "status": str(snapshot.status or ""),
        "diagnostic": str(snapshot.diagnostic or ""),
        "backend": str(snapshot.backend or ""),
    }


def _release_document_page_image(task: Any, image: Any) -> None:
    try:
        task.exception()
    except BaseException:
        pass
    try:
        image.close()
    except Exception:
        pass


def _release_decoded_document_page(task: Any) -> None:
    try:
        image = task.result()
    except BaseException:
        return
    try:
        image.close()
    except Exception:
        pass


def _ocr_request_lanlan(kwargs: dict[str, object]) -> str | None:
    context = kwargs.get("_ctx")
    if isinstance(context, dict):
        return str(context.get("lanlan_name") or "").strip() or None
    return None


class _OcrEntriesMixin:
    @plugin_entry(
        id="study_dependency_status",
        name=tr(
            "entries.dependency_status.name", default="Study OCR Dependency Status"
        ),
        description=tr(
            "entries.dependency_status.description",
            default="Inspect RapidOCR, Tesseract, and capture dependencies used by study_companion.",
        ),
        input_schema={"type": "object", "properties": {}},
        llm_result_fields=["missing_installable"],
    )
    async def study_dependency_status(self, **_):
        status = await self._refresh_dependency_status()
        await self._persist_state()
        return Ok(status)

    @plugin_entry(
        id="study_ocr_snapshot",
        name=tr("entries.ocr_snapshot.name", default="Study OCR Snapshot"),
        description=tr(
            "entries.ocr_snapshot.description",
            default="Run a lightweight OCR snapshot. Phase 1 attempts fullscreen capture and returns diagnostics on failure.",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "capture_mode": {
                    "type": "string",
                    "enum": ["fullscreen", "interactive"],
                    "default": "fullscreen",
                }
            },
        },
        timeout=90.0,
        llm_result_fields=[
            "summary",
            "status",
            "diagnostic",
            "capture_mode_requested",
            "capture_mode_used",
            "interactive_fallback_reason",
        ],
    )
    async def study_ocr_snapshot(self, capture_mode: str = "fullscreen", **kwargs):
        if self._ocr_pipeline is None:
            return Err(SdkError("study OCR pipeline is not initialized"))
        if capture_mode not in {"fullscreen", "interactive"}:
            return Err(SdkError("invalid capture_mode"))
        capture_mode_used = capture_mode
        interactive_fallback_reason = ""
        if capture_mode == "interactive":
            try:
                capture = await capture_interactive_region(
                    lanlan_name=_ocr_request_lanlan(kwargs)
                )
            except InteractiveCaptureError as exc:
                exc_text = str(exc)
                interactive_fallback_reason = next(
                    (
                        code
                        for code in (
                            "no_renderer",
                            "main_server_unavailable",
                            "interactive_unavailable",
                        )
                        if code in exc_text
                    ),
                    "",
                )
                if not interactive_fallback_reason:
                    return _entry_exception_error(
                        self,
                        exc,
                        operation="study_ocr_snapshot",
                    )
                capture_mode_used = "fullscreen"
                snapshot = await asyncio.to_thread(self._ocr_pipeline.capture_snapshot)
            else:
                if capture.canceled:
                    payload = build_ocr_payload(OcrSnapshot(status="canceled"))
                    payload["capture_mode_requested"] = "interactive"
                    payload["capture_mode_used"] = "interactive"
                    return Ok(payload)
                if capture.image is None:
                    return Err(SdkError("interactive_capture: missing_image_data"))
                snapshot = await asyncio.to_thread(
                    self._ocr_pipeline.snapshot_from_image,
                    capture.image,
                )
        else:
            snapshot = await asyncio.to_thread(self._ocr_pipeline.capture_snapshot)
        payload = build_ocr_payload(snapshot)
        if capture_mode == "interactive":
            payload["capture_mode_requested"] = "interactive"
            payload["capture_mode_used"] = capture_mode_used
            if interactive_fallback_reason:
                payload["interactive_fallback_reason"] = interactive_fallback_reason
        if self._supervision is not None:
            sensor_available = snapshot.status in {"ok", "empty"}
            payload["supervision"] = self._supervision.observe_activity(
                ocr_text=snapshot.text,
                sensor_available=sensor_available,
            )
        if snapshot.text.strip():
            async with self._lock:
                self._state.last_ocr_text = snapshot.text
                self._state.last_ocr_at = snapshot.captured_at
            payload["screen_classification"] = await self._update_screen_classification(
                snapshot.text, update_empty=False
            )
        elif snapshot.status == "empty":
            payload["screen_classification"] = await self._update_screen_classification(
                "", update_empty=True
            )
        await self._persist_state()
        return Ok(payload)

    @plugin_entry(
        id="study_ocr_document_page",
        name="Study OCR Document Page",
        description="Recognize one validated JPEG/PNG page from an imported document.",
        input_schema={
            "type": "object",
            "properties": {
                "image_data_url": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 8_388_640,
                }
            },
            "required": ["image_data_url"],
            "additionalProperties": False,
        },
        timeout=50.0,
        llm_result_fields=["status", "diagnostic", "backend"],
    )
    async def study_ocr_document_page(self, image_data_url: str, **_):
        backend_name = str(
            getattr(getattr(self, "_cfg", None), "ocr_backend_selection", "") or ""
        ).strip()
        if self._ocr_pipeline is None:
            return Ok(
                {
                    "text": "",
                    "status": "unavailable",
                    "diagnostic": "document_pdf_ocr_unavailable",
                    "backend": backend_name,
                }
            )

        decode_task = asyncio.create_task(
            asyncio.to_thread(
                _decode_document_page_data_url,
                image_data_url,
            )
        )
        try:
            image = await asyncio.shield(decode_task)
        except asyncio.CancelledError:
            decode_task.add_done_callback(_release_decoded_document_page)
            raise
        except (TypeError, ValueError, OSError) as exc:
            return Err(SdkError(str(exc)))

        ocr_task = asyncio.create_task(
            asyncio.to_thread(self._ocr_pipeline.recognize_document_page, image)
        )
        try:
            snapshot = await asyncio.wait_for(
                asyncio.shield(ocr_task),
                timeout=_DOCUMENT_PAGE_OCR_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            return Ok(
                {
                    "text": "",
                    "status": "timeout",
                    "diagnostic": "document_pdf_ocr_timeout",
                    "backend": backend_name,
                }
            )
        finally:
            if ocr_task.done():
                try:
                    image.close()
                except Exception:
                    pass
            else:
                ocr_task.add_done_callback(
                    lambda task: _release_document_page_image(task, image)
                )
        return Ok(_document_page_ocr_payload(snapshot))

    @plugin_entry(
        id="study_install_tesseract",
        name=tr(
            "entries.install_tesseract.name", default="Install Tesseract for Study OCR"
        ),
        description=tr(
            "entries.install_tesseract.description",
            default="Install local Tesseract OCR for study_companion and refresh dependency status.",
        ),
        input_schema={
            "type": "object",
            "properties": {"force": {"type": "boolean", "default": False}},
        },
        timeout=300.0,
        llm_result_fields=["summary"],
    )
    async def study_install_tesseract(self, force: bool = False, **kwargs):
        async with self._lock:
            if self._install_in_progress:
                return Err(SdkError("Tesseract install is already running"))
            self._install_in_progress = True
        try:
            run_id = self._resolve_current_run_id(kwargs)
            result = await tesseract_support.install_tesseract(
                logger=self.logger,
                configured_path=self._cfg.ocr_tesseract_path,
                install_target_dir_raw=self._cfg.ocr_install_target_dir,
                manifest_url=self._cfg.ocr_install_manifest_url,
                timeout_seconds=self._cfg.ocr_install_timeout_seconds,
                languages=self._cfg.ocr_languages,
                force=bool(force),
                task_id=run_id or None,
                plugin_id=self.plugin_id,
                progress_callback=self._resolve_install_progress_callback(run_id),
            )
            await self._refresh_dependency_status()
            await self._persist_state()
            return Ok(
                {
                    "summary": str(result.get("summary") or "Tesseract is ready"),
                    "install_result": result,
                }
            )
        except Exception as exc:
            return _entry_exception_error(
                self,
                exc,
                operation="study_install_tesseract",
                message=f"Tesseract install failed: {exc}",
            )
        finally:
            async with self._lock:
                self._install_in_progress = False

    @plugin_entry(
        id="study_download_rapidocr_models",
        name=tr(
            "entries.download_rapidocr_models.name",
            default="Download RapidOCR Models for Study OCR",
        ),
        description=tr(
            "entries.download_rapidocr_models.description",
            default="Download missing RapidOCR model files for the configured study_companion OCR language.",
        ),
        input_schema={
            "type": "object",
            "properties": {"force": {"type": "boolean", "default": False}},
        },
        timeout=600.0,
        llm_result_fields=["summary"],
    )
    async def study_download_rapidocr_models(self, force: bool = False, **kwargs):
        async with self._lock:
            if self._rapidocr_models_in_progress:
                return Err(SdkError("RapidOCR model download is already running"))
            self._rapidocr_models_in_progress = True
        try:
            run_id = self._resolve_current_run_id(kwargs)
            result = await rapidocr_support.download_rapidocr_models(
                logger=self.logger,
                install_target_dir_raw=self._cfg.rapidocr_install_target_dir,
                ocr_version=self._cfg.rapidocr_ocr_version,
                lang_type=self._cfg.rapidocr_lang_type,
                timeout_seconds=float(self._cfg.ocr_install_timeout_seconds or 180.0),
                force=bool(force),
                task_id=run_id or None,
                plugin_id=self.plugin_id,
                progress_callback=self._resolve_install_progress_callback(run_id),
                before_completed_callback=lambda: None,
                install_state_updater=update_install_task_state,
            )
            await self._refresh_dependency_status()
            await self._persist_state()
            downloaded = result.get("downloaded") or []
            return Ok(
                {
                    "summary": (
                        f"RapidOCR models ready ({len(downloaded)} file(s) downloaded)"
                        if downloaded
                        else "RapidOCR models already present"
                    ),
                    "download_result": result,
                }
            )
        except Exception as exc:
            return _entry_exception_error(
                self,
                exc,
                operation="study_download_rapidocr_models",
                message=f"RapidOCR model download failed: {exc}",
            )
        finally:
            async with self._lock:
                self._rapidocr_models_in_progress = False
