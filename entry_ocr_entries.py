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
    ui,
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
_AUTO_SAVE_QUESTION_MARKERS = (
    "?",
    "？",
    "求",
    "证明",
    "計算",
    "计算",
    "解下列",
    "选择",
    "選擇",
    "填空",
    "判断",
    "判斷",
    "solve",
    "calculate",
    "determine",
    "evaluate",
    "prove",
    "find ",
    "what ",
    "which ",
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


def _looks_like_study_question(text: str) -> bool:
    """Use a deliberately narrow minimum gate for opt-in OCR auto-save."""
    normalized = " ".join(str(text or "").split())
    if not 6 <= len(normalized) <= 10_000:
        return False
    lowered = normalized.lower()
    return any(marker in lowered for marker in _AUTO_SAVE_QUESTION_MARKERS)


class _OcrEntriesMixin:
    async def _is_current_ocr_text(self, text: str) -> bool:
        candidate = str(text or "").strip()
        if not candidate:
            return False
        async with self._lock:
            self._state.clear_expired_ocr_session()
            return candidate == str(self._state.last_ocr_text or "").strip()

    async def _save_current_ocr_question(
        self,
        *,
        consent_origin: str,
        topic_id: str = "",
        text: str | None = None,
    ) -> dict[str, Any]:
        """Persist the current OCR text only after an explicit learning action."""
        async with self._lock:
            self._state.clear_expired_ocr_session()
            current_ocr_text = str(self._state.last_ocr_text or "").strip()
            classification_value = getattr(
                self._state, "last_screen_classification", {}
            )
            classification = (
                dict(classification_value)
                if isinstance(classification_value, dict)
                else {}
            )
        text_to_save = str(text if text is not None else current_ocr_text).strip()
        if not text_to_save:
            raise SdkError(
                "a non-empty OCR snapshot is required",
                code="MISSING_OCR_TEXT",
            )
        try:
            record = await asyncio.to_thread(
                self._store.save_captured_question,
                text=text_to_save,
                consent_origin=consent_origin,
                source_type="ocr",
                topic_id=str(topic_id or "").strip(),
                classification=classification,
            )
        except Exception as exc:
            raise SdkError(
                "failed to save OCR question",
                code="QUESTION_CAPTURE_PERSISTENCE_FAILED",
            ) from exc
        captured_question_id = str(record.get("id") or "").strip()
        if not captured_question_id:
            raise SdkError(
                "saved OCR question is missing an id",
                code="QUESTION_CAPTURE_PERSISTENCE_FAILED",
            )
        async with self._lock:
            self._state.last_captured_question_id = captured_question_id
        return record

    @ui.action()
    @plugin_entry(
        id="study_save_ocr_question",
        name=tr("entries.save_ocr_question.name", default="Save OCR Question"),
        description=tr(
            "entries.save_ocr_question.description",
            default="Save the latest OCR text as a local study question.",
        ),
        input_schema={
            "type": "object",
            "properties": {"topic_id": {"type": "string", "default": ""}},
        },
        timeout=20.0,
        llm_result_fields=["captured_question_id", "topic_id", "status"],
    )
    async def study_save_ocr_question(self, topic_id: str = "", **_):
        try:
            record = await self._save_current_ocr_question(
                consent_origin="explicit_save",
                topic_id=topic_id,
            )
            return Ok(
                {
                    "captured_question_id": str(record.get("id") or ""),
                    "topic_id": str(record.get("topic_id") or ""),
                    "status": str(record.get("status") or "active"),
                }
            )
        except SdkError as exc:
            return Err(exc)
        except Exception as exc:
            return _entry_exception_error(
                self, exc, operation="study_save_ocr_question"
            )

    @ui.action()
    @plugin_entry(
        id="study_list_captured_questions",
        name=tr(
            "entries.list_captured_questions.name",
            default="List Saved Study Questions",
        ),
        description=tr(
            "entries.list_captured_questions.description",
            default="List locally saved OCR question assets.",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "topic_id": {"type": "string", "default": ""},
                "status": {"type": "string", "default": "active"},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 500,
                    "default": 100,
                },
                "include_question_text": {"type": "boolean", "default": False},
            },
        },
        timeout=20.0,
        llm_result_fields=["count"],
    )
    async def study_list_captured_questions(
        self,
        topic_id: str = "",
        status: str = "active",
        limit: int = 100,
        include_question_text: bool = False,
        **_,
    ):
        try:
            records = await asyncio.to_thread(
                self._store.list_captured_questions,
                topic_id=str(topic_id or "").strip(),
                status=str(status or "").strip(),
                limit=max(1, min(500, int(limit))),
            )
            questions = []
            for record in records:
                payload = {
                    key: record.get(key)
                    for key in (
                        "id",
                        "source_type",
                        "topic_id",
                        "subject",
                        "question_type",
                        "classification_confidence",
                        "consent_origin",
                        "status",
                        "created_at",
                        "last_used_at",
                        "expires_at",
                    )
                }
                if include_question_text:
                    payload["question_text"] = str(record.get("question_text") or "")
                questions.append(payload)
            return Ok({"count": len(questions), "questions": questions})
        except Exception as exc:
            return _entry_exception_error(
                self, exc, operation="study_list_captured_questions"
            )

    @ui.action()
    @plugin_entry(
        id="study_delete_captured_question",
        name=tr(
            "entries.delete_captured_question.name",
            default="Delete Saved Study Question",
        ),
        description=tr(
            "entries.delete_captured_question.description",
            default="Delete one saved question and unlink its retained answer history.",
        ),
        input_schema={
            "type": "object",
            "properties": {"question_id": {"type": "string", "minLength": 1}},
            "required": ["question_id"],
        },
        timeout=20.0,
        llm_result_fields=["question_id", "deleted"],
    )
    async def study_delete_captured_question(self, question_id: str, **_):
        question_key = str(question_id or "").strip()
        if not question_key:
            return Err(SdkError("question_id is required", code="MISSING_QUESTION_ID"))
        try:
            deleted = await asyncio.to_thread(
                self._store.delete_captured_question, question_key
            )
            if deleted:
                async with self._lock:
                    if self._state.last_captured_question_id == question_key:
                        self._state.last_captured_question_id = ""
            return Ok({"question_id": question_key, "deleted": bool(deleted)})
        except Exception as exc:
            return _entry_exception_error(
                self, exc, operation="study_delete_captured_question"
            )

    @ui.action()
    @plugin_entry(
        id="study_clear_captured_questions",
        name=tr(
            "entries.clear_captured_questions.name",
            default="Clear Saved Study Questions",
        ),
        description=tr(
            "entries.clear_captured_questions.description",
            default="Clear saved question assets while retaining answer history without source links.",
        ),
        input_schema={"type": "object", "properties": {}},
        timeout=30.0,
        llm_result_fields=["deleted_count"],
    )
    async def study_clear_captured_questions(self, **_):
        try:
            deleted_count = await asyncio.to_thread(
                self._store.clear_captured_questions
            )
            async with self._lock:
                self._state.last_captured_question_id = ""
            return Ok({"deleted_count": int(deleted_count)})
        except Exception as exc:
            return _entry_exception_error(
                self, exc, operation="study_clear_captured_questions"
            )

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
        # A completed OCR request replaces the short-lived session buffer.  Do
        # this before capture so a failed/empty replacement never exposes a
        # previous screen's text as the current one.
        async with self._lock:
            self._state.clear_ocr_session()
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
                self._state.set_ocr_session_text(
                    snapshot.text,
                    captured_at=snapshot.captured_at,
                )
            payload["screen_classification"] = await self._update_screen_classification(
                snapshot.text, update_empty=False
            )
            classification = payload["screen_classification"]
            auto_save_enabled = (
                str(
                    getattr(self._cfg, "ocr_question_persistence_mode", "") or ""
                ).strip().lower()
                == "auto_save_questions"
            )
            try:
                classification_confidence = float(
                    classification.get("confidence") or 0.0
                )
            except (TypeError, ValueError):
                classification_confidence = 0.0
            if (
                auto_save_enabled
                and str(classification.get("screen_type") or "").strip().lower()
                == "question"
                and classification_confidence >= 0.80
                and _looks_like_study_question(snapshot.text)
            ):
                try:
                    record = await _OcrEntriesMixin._save_current_ocr_question(
                        self,
                        consent_origin="auto_save",
                        text=snapshot.text,
                    )
                    payload["captured_question_id"] = str(record.get("id") or "")
                    payload["auto_save_status"] = "saved"
                except SdkError as exc:
                    self.logger.warning("study OCR question auto-save failed: {}", exc)
                    payload["auto_save_status"] = "failed"
        elif snapshot.status == "empty":
            async with self._lock:
                self._state.clear_ocr_session(captured_at=snapshot.captured_at)
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
        except Exception:
            return Ok(
                {
                    "text": "",
                    "status": "ocr_failed",
                    "diagnostic": "document_pdf_ocr_failed",
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
