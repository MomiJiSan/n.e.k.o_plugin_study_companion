from __future__ import annotations

import asyncio
import base64
import importlib
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest


@dataclass(slots=True)
class _OcrSnapshot:
    text: str = ""
    boxes: list[dict[str, Any]] = field(default_factory=list)
    status: str = "empty"
    backend: str = ""
    captured_at: str = ""
    diagnostic: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "boxes": self.boxes,
            "status": self.status,
            "backend": self.backend,
            "captured_at": self.captured_at,
            "diagnostic": self.diagnostic,
        }


class _ActivitySnapshot:
    def __init__(self, **values: Any) -> None:
        self.__dict__.update(values)


class _FakeImage:
    def __init__(
        self,
        api: type[_FakeImageApi],
        *,
        image_format: str = "JPEG",
        size: tuple[int, int] = (100, 100),
        mode: str = "RGB",
    ) -> None:
        self._api = api
        self.format = image_format
        self.size = size
        self.mode = mode
        self.info: dict[str, Any] = {}
        self.closed = False

    def __enter__(self) -> _FakeImage:
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def load(self) -> None:
        return None

    def getbands(self) -> tuple[str, ...]:
        return tuple(self.mode)

    def convert(self, mode: str) -> _FakeImage:
        converted = _FakeImage(
            self._api,
            image_format=self.format,
            size=self.size,
            mode=mode,
        )
        self._api.last_image = converted
        return converted

    def getchannel(self, _name: str) -> _FakeImage:
        return self

    def paste(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _FakeImageApi:
    class DecompressionBombError(Exception):
        pass

    class Resampling:
        LANCZOS = 1

    next_size = (100, 100)
    last_image: _FakeImage | None = None
    raise_decompression_bomb = False

    @classmethod
    def open(cls, stream: Any) -> _FakeImage:
        raw = stream.read()
        if cls.raise_decompression_bomb:
            raise cls.DecompressionBombError("image dimensions exceed Pillow limit")
        image_format = "PNG" if raw.startswith(b"\x89PNG\r\n\x1a\n") else "JPEG"
        return _FakeImage(cls, image_format=image_format, size=cls.next_size)

    @classmethod
    def new(
        cls, mode: str, size: tuple[int, int], _color: str
    ) -> _FakeImage:
        image = _FakeImage(cls, image_format="PNG", size=size, mode=mode)
        cls.last_image = image
        return image


class _Ok:
    def __init__(self, value: Any) -> None:
        self.value = value


class _Err:
    def __init__(self, error: Exception) -> None:
        self.error = error


class _SdkError(RuntimeError):
    pass


@pytest.fixture()
def document_ocr_modules(monkeypatch: pytest.MonkeyPatch):
    root = Path(__file__).resolve().parents[1]
    package_name = f"_study_companion_document_ocr_test_{time.time_ns()}"
    package = ModuleType(package_name)
    package.__path__ = [str(root)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package_name, package)

    models = ModuleType(f"{package_name}.models")
    models.OCR_SNIPPET_MAX_CHARS = 600
    models.ActivitySnapshot = _ActivitySnapshot
    models.OcrSnapshot = _OcrSnapshot
    models.StudyConfig = object
    models.utc_now_iso = lambda: "2026-08-22T00:00:00Z"
    monkeypatch.setitem(sys.modules, models.__name__, models)

    classifier = ModuleType(f"{package_name}.screen_classifier")
    classifier.classify_app_from_title = lambda _title: "other"
    classifier.classify_screen_from_ocr = lambda *_args, **_kwargs: SimpleNamespace(
        screen_type="other"
    )
    monkeypatch.setitem(sys.modules, classifier.__name__, classifier)

    pil = ModuleType("PIL")
    pil.Image = _FakeImageApi
    monkeypatch.setitem(sys.modules, "PIL", pil)
    _FakeImageApi.next_size = (100, 100)
    _FakeImageApi.last_image = None
    _FakeImageApi.raise_decompression_bomb = False

    normalize_calls: list[str] = []

    def normalize_image(payload: str) -> str:
        normalize_calls.append(payload)
        header, separator, encoded = payload.partition(",")
        if not separator:
            raise ValueError("image_base64 data URL is malformed")
        raw = base64.b64decode(encoded, validate=True)
        expected = {
            "data:image/jpeg;base64": ("image/jpeg", b"\xff\xd8\xff"),
            "data:image/png;base64": ("image/png", b"\x89PNG\r\n\x1a\n"),
        }.get(header.lower())
        if expected is None or not raw.startswith(expected[1]):
            raise ValueError("image_base64 MIME does not match image data")
        return f"data:{expected[0]};base64,{encoded}"

    def plugin_entry(**metadata: Any):
        def decorate(function: Any) -> Any:
            function.meta = metadata
            return function

        return decorate

    entry_common = ModuleType(f"{package_name}.entry_common")
    entry_common.Err = _Err
    entry_common.Ok = _Ok
    entry_common.SdkError = _SdkError
    entry_common._normalize_submitted_image_payload = normalize_image
    entry_common._entry_exception_error = lambda *_args, **_kwargs: None
    entry_common.asyncio = asyncio
    entry_common.base64 = base64
    entry_common.build_ocr_payload = lambda snapshot: snapshot.to_dict()
    entry_common.plugin_entry = plugin_entry
    entry_common.rapidocr_support = SimpleNamespace()
    entry_common.tesseract_support = SimpleNamespace()
    entry_common.tr = lambda _key, default="": default
    entry_common.ui = SimpleNamespace(action=lambda: lambda function: function)
    entry_common.update_install_task_state = lambda *_args, **_kwargs: None
    monkeypatch.setitem(sys.modules, entry_common.__name__, entry_common)

    screenshot = ModuleType(f"{package_name}.interactive_screenshot")
    screenshot.InteractiveCaptureError = RuntimeError
    screenshot.capture_interactive_region = lambda **_kwargs: None
    monkeypatch.setitem(sys.modules, screenshot.__name__, screenshot)

    pipeline_module = importlib.import_module(f"{package_name}.study_ocr_pipeline")
    entry_module = importlib.import_module(f"{package_name}.entry_ocr_entries")
    return SimpleNamespace(
        entry=entry_module,
        pipeline=pipeline_module,
        normalize_calls=normalize_calls,
        image_api=_FakeImageApi,
    )


class _Logger:
    def warning(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def debug(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _Backend:
    def __init__(
        self,
        result: Any = "recognized text",
        *,
        available: bool = True,
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.available = available
        self.error = error
        self.calls = 0

    def is_available(self) -> bool:
        return self.available

    def extract_text(self, _image: Any) -> Any:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


def _make_pipeline(modules: Any, backend: _Backend, *, enabled: bool = True):
    config = SimpleNamespace(
        ocr_enabled=enabled,
        ocr_backend_selection="rapidocr",
        llm_vision_enabled=True,
    )
    return modules.pipeline.StudyOcrPipeline(
        logger=_Logger(),
        config=config,
        ocr_backend=backend,
    )


@pytest.mark.parametrize(
    ("enabled", "pipeline_factory", "ready", "diagnostic"),
    [
        (
            False,
            lambda modules: _make_pipeline(modules, _Backend()),
            False,
            "document_pdf_ocr_disabled",
        ),
        (True, lambda _modules: None, False, "document_pdf_ocr_unavailable"),
        (
            True,
            lambda modules: _make_pipeline(modules, _Backend(available=False)),
            False,
            "document_pdf_ocr_unavailable",
        ),
        (True, lambda modules: _make_pipeline(modules, _Backend()), True, "ready"),
    ],
)
def test_document_ocr_capabilities_report_stable_readiness(
    document_ocr_modules: Any,
    enabled: bool,
    pipeline_factory: Any,
    ready: bool,
    diagnostic: str,
) -> None:
    pipeline = pipeline_factory(document_ocr_modules)
    owner = SimpleNamespace(
        _ocr_pipeline=pipeline,
        _cfg=SimpleNamespace(ocr_enabled=enabled),
    )
    try:
        result = asyncio.run(
            document_ocr_modules.entry._OcrEntriesMixin.study_ocr_document_capabilities(
                owner
            )
        )
    finally:
        if pipeline is not None:
            pipeline.close()

    assert isinstance(result, _Ok)
    assert result.value == {
        "protocol": 1,
        "enabled": enabled,
        "ready": ready,
        "backend": "rapidocr",
        "max_page_pixels": 8_000_000,
        "max_image_bytes": 6 * 1024 * 1024,
        "diagnostic": diagnostic,
    }


def test_pipeline_document_page_isolated_from_live_vision_state(
    document_ocr_modules: Any,
) -> None:
    backend = _Backend("page text")
    pipeline = _make_pipeline(document_ocr_modules, backend)
    pipeline._latest_vision_snapshot = {"sentinel": True}
    pipeline._latest_vision_image_base64 = "sentinel-image"
    try:
        result = pipeline.recognize_document_page(object())
    finally:
        pipeline.close()

    assert result.status == "ok"
    assert result.text == "page text"
    assert result.backend == "rapidocr"
    assert result.diagnostic.startswith("ocr_duration_seconds=")
    assert pipeline._latest_vision_snapshot == {"sentinel": True}
    assert pipeline._latest_vision_image_base64 == "sentinel-image"


@pytest.mark.parametrize(
    ("enabled", "backend", "status", "diagnostic"),
    [
        (False, _Backend(), "disabled", "document_pdf_ocr_disabled"),
        (
            True,
            _Backend(available=False),
            "unavailable",
            "document_pdf_ocr_unavailable",
        ),
        (
            True,
            _Backend(error=RuntimeError("sensitive backend detail")),
            "ocr_failed",
            "document_pdf_ocr_failed",
        ),
    ],
)
def test_pipeline_document_page_maps_stable_failures(
    document_ocr_modules: Any,
    enabled: bool,
    backend: _Backend,
    status: str,
    diagnostic: str,
) -> None:
    pipeline = _make_pipeline(document_ocr_modules, backend, enabled=enabled)
    try:
        result = pipeline.recognize_document_page(object())
    finally:
        pipeline.close()

    assert result.status == status
    assert result.diagnostic == diagnostic
    assert "sensitive backend detail" not in result.diagnostic


def test_pipeline_document_page_preserves_empty_result(
    document_ocr_modules: Any,
) -> None:
    pipeline = _make_pipeline(document_ocr_modules, _Backend("  "))
    try:
        result = pipeline.recognize_document_page(object())
    finally:
        pipeline.close()

    assert result.status == "empty"
    assert result.text == ""


def test_document_page_decoder_requires_data_url_and_reuses_strict_validator(
    document_ocr_modules: Any,
) -> None:
    entry = document_ocr_modules.entry
    raw = b"\xff\xd8\xfftest-jpeg"
    encoded = base64.b64encode(raw).decode("ascii")

    with pytest.raises(ValueError, match="data URLs"):
        entry._decode_document_page_data_url(encoded)
    assert document_ocr_modules.normalize_calls == []

    image = entry._decode_document_page_data_url(
        f"data:image/jpeg;base64,{encoded}"
    )
    assert document_ocr_modules.normalize_calls == [
        f"data:image/jpeg;base64,{encoded}"
    ]
    assert image.mode == "RGB"

    png_header_with_jpeg_data = f"data:image/png;base64,{encoded}"
    with pytest.raises(ValueError, match="MIME does not match"):
        entry._decode_document_page_data_url(png_header_with_jpeg_data)


def test_document_page_decoder_enforces_pixel_limit(
    document_ocr_modules: Any,
) -> None:
    document_ocr_modules.image_api.next_size = (4000, 3000)
    encoded = base64.b64encode(b"\xff\xd8\xffoversized").decode("ascii")

    with pytest.raises(ValueError, match="document_pdf_page_too_large"):
        document_ocr_modules.entry._decode_document_page_data_url(
            f"data:image/jpeg;base64,{encoded}"
        )


def test_document_page_decoder_maps_pillow_dimension_bomb_to_stable_error(
    document_ocr_modules: Any,
) -> None:
    document_ocr_modules.image_api.raise_decompression_bomb = True
    encoded = base64.b64encode(b"\x89PNG\r\n\x1a\ncompressed").decode("ascii")

    with pytest.raises(ValueError, match="document_pdf_page_too_large"):
        document_ocr_modules.entry._decode_document_page_data_url(
            f"data:image/png;base64,{encoded}"
        )


def test_document_page_decoder_enforces_six_mib_limit(
    document_ocr_modules: Any,
) -> None:
    encoded = base64.b64encode(
        b"\xff\xd8\xff" + b"x" * (6 * 1024 * 1024)
    ).decode("ascii")

    with pytest.raises(ValueError, match="document_pdf_page_too_large"):
        document_ocr_modules.entry._decode_document_page_data_url(
            f"data:image/jpeg;base64,{encoded}"
        )


def test_document_page_decoder_accepts_exact_six_mib_boundary(
    document_ocr_modules: Any,
) -> None:
    raw = b"\xff\xd8\xff" + b"x" * (6 * 1024 * 1024 - 3)
    encoded = base64.b64encode(raw).decode("ascii")

    image = document_ocr_modules.entry._decode_document_page_data_url(
        f"data:image/jpeg;base64,{encoded}"
    )

    assert image.mode == "RGB"
    image.close()


def test_document_page_entry_runs_ocr_in_thread_without_touching_owner_state(
    document_ocr_modules: Any,
) -> None:
    main_thread = threading.get_ident()

    class Pipeline:
        thread_id = main_thread

        def recognize_document_page(self, _image: Any) -> _OcrSnapshot:
            self.thread_id = threading.get_ident()
            return _OcrSnapshot(
                text="threaded text",
                status="ok",
                diagnostic="ocr_duration_seconds=0.001",
                backend="rapidocr",
            )

    pipeline = Pipeline()
    owner = SimpleNamespace(
        _ocr_pipeline=pipeline,
        _cfg=SimpleNamespace(ocr_backend_selection="rapidocr"),
        _supervision=SimpleNamespace(
            observe_activity=lambda **_kwargs: pytest.fail("supervision was updated")
        ),
        _state=SimpleNamespace(last_ocr_text="sentinel", last_ocr_at="sentinel"),
    )
    encoded = base64.b64encode(b"\xff\xd8\xffpage").decode("ascii")

    result = asyncio.run(
        document_ocr_modules.entry._OcrEntriesMixin.study_ocr_document_page(
            owner,
            f"data:image/jpeg;base64,{encoded}",
        )
    )

    assert isinstance(result, _Ok)
    assert result.value == {
        "text": "threaded text",
        "status": "ok",
        "diagnostic": "ocr_duration_seconds=0.001",
        "backend": "rapidocr",
    }
    assert pipeline.thread_id != main_thread
    assert owner._state.last_ocr_text == "sentinel"
    assert owner._state.last_ocr_at == "sentinel"
    assert document_ocr_modules.image_api.last_image.closed is True


def test_document_page_entry_rejects_concurrent_worker_as_busy(
    document_ocr_modules: Any,
) -> None:
    worker_started = threading.Event()
    finish_worker = threading.Event()

    class BlockingPipeline:
        calls = 0

        def recognize_document_page(self, _image: Any) -> _OcrSnapshot:
            self.calls += 1
            worker_started.set()
            assert finish_worker.wait(timeout=1.0)
            return _OcrSnapshot(text="first", status="ok", backend="rapidocr")

    pipeline = BlockingPipeline()
    owner = SimpleNamespace(
        _ocr_pipeline=pipeline,
        _cfg=SimpleNamespace(ocr_backend_selection="rapidocr"),
    )
    encoded = base64.b64encode(b"\xff\xd8\xffpage").decode("ascii")
    payload = f"data:image/jpeg;base64,{encoded}"

    async def scenario() -> tuple[Any, Any]:
        first_task = asyncio.create_task(
            document_ocr_modules.entry._OcrEntriesMixin.study_ocr_document_page(
                owner, payload
            )
        )
        assert await asyncio.to_thread(worker_started.wait, 1.0)
        second = await document_ocr_modules.entry._OcrEntriesMixin.study_ocr_document_page(
            owner, payload
        )
        finish_worker.set()
        first = await first_task
        return first, second

    first, second = asyncio.run(scenario())

    assert isinstance(first, _Ok)
    assert first.value["text"] == "first"
    assert isinstance(second, _Ok)
    assert second.value == {
        "text": "",
        "status": "busy",
        "diagnostic": "document_pdf_ocr_busy",
        "backend": "rapidocr",
    }
    assert pipeline.calls == 1


def test_document_page_entry_releases_decode_result_after_cancellation(
    document_ocr_modules: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = document_ocr_modules.entry
    decode_started = threading.Event()
    finish_decode = threading.Event()
    image_closed = threading.Event()

    class DecodedImage:
        def close(self) -> None:
            image_closed.set()

    def slow_decode(_payload: str) -> DecodedImage:
        decode_started.set()
        assert finish_decode.wait(timeout=1.0)
        return DecodedImage()

    monkeypatch.setattr(entry, "_decode_document_page_data_url", slow_decode)
    owner = SimpleNamespace(
        _ocr_pipeline=SimpleNamespace(
            recognize_document_page=lambda _image: pytest.fail("OCR should not run")
        ),
        _cfg=SimpleNamespace(ocr_backend_selection="rapidocr"),
    )

    async def scenario() -> None:
        task = asyncio.create_task(
            entry._OcrEntriesMixin.study_ocr_document_page(owner, "payload")
        )
        assert await asyncio.to_thread(decode_started.wait, 1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        finish_decode.set()
        for _ in range(100):
            if image_closed.is_set():
                return
            await asyncio.sleep(0.001)
        pytest.fail("decoded image was not released")

    asyncio.run(scenario())


def test_document_page_entry_cancellation_keeps_gate_until_worker_cleanup(
    document_ocr_modules: Any,
) -> None:
    worker_started = threading.Event()
    finish_worker = threading.Event()
    worker_finished = threading.Event()
    worker_image: list[Any] = []

    class BlockingPipeline:
        def recognize_document_page(self, image: Any) -> _OcrSnapshot:
            worker_image.append(image)
            worker_started.set()
            assert finish_worker.wait(timeout=1.0)
            worker_finished.set()
            return _OcrSnapshot(text="late", status="ok", backend="rapidocr")

    owner = SimpleNamespace(
        _ocr_pipeline=BlockingPipeline(),
        _cfg=SimpleNamespace(ocr_backend_selection="rapidocr"),
    )
    encoded = base64.b64encode(b"\xff\xd8\xffpage").decode("ascii")
    payload = f"data:image/jpeg;base64,{encoded}"

    async def scenario() -> Any:
        task = asyncio.create_task(
            document_ocr_modules.entry._OcrEntriesMixin.study_ocr_document_page(
                owner, payload
            )
        )
        assert await asyncio.to_thread(worker_started.wait, 1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert worker_image[0].closed is False

        busy = await document_ocr_modules.entry._OcrEntriesMixin.study_ocr_document_page(
            owner, payload
        )
        finish_worker.set()
        assert await asyncio.to_thread(worker_finished.wait, 1.0)
        for _ in range(100):
            if worker_image[0].closed:
                return busy
            await asyncio.sleep(0.001)
        pytest.fail("worker image was not released")

    busy = asyncio.run(scenario())

    assert isinstance(busy, _Ok)
    assert busy.value["diagnostic"] == "document_pdf_ocr_busy"
    assert worker_image[0].closed is True


def test_document_page_entry_maps_timeout_and_releases_image_after_worker(
    document_ocr_modules: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = document_ocr_modules.entry
    monkeypatch.setattr(entry, "_DOCUMENT_PAGE_OCR_TIMEOUT_SECONDS", 0.005)
    worker_started = threading.Event()
    finish_worker = threading.Event()
    worker_finished = threading.Event()

    class SlowPipeline:
        calls = 0

        def recognize_document_page(self, _image: Any) -> _OcrSnapshot:
            self.calls += 1
            if self.calls == 1:
                worker_started.set()
                assert finish_worker.wait(timeout=1.0)
            worker_finished.set()
            return _OcrSnapshot(
                text=f"worker text {self.calls}",
                status="ok",
                backend="rapidocr",
            )

    pipeline = SlowPipeline()
    owner = SimpleNamespace(
        _ocr_pipeline=pipeline,
        _cfg=SimpleNamespace(ocr_backend_selection="rapidocr"),
    )
    encoded = base64.b64encode(b"\xff\xd8\xffpage").decode("ascii")
    payload = f"data:image/jpeg;base64,{encoded}"

    async def scenario() -> tuple[Any, Any, Any]:
        timed_out = await entry._OcrEntriesMixin.study_ocr_document_page(
            owner, payload
        )
        assert worker_started.is_set()
        busy = await entry._OcrEntriesMixin.study_ocr_document_page(owner, payload)
        assert worker_finished.is_set() is False
        finish_worker.set()
        assert await asyncio.to_thread(worker_finished.wait, 1.0)
        recovered = None
        for _ in range(100):
            recovered = await entry._OcrEntriesMixin.study_ocr_document_page(
                owner, payload
            )
            if recovered.value.get("status") != "busy":
                return timed_out, busy, recovered
            await asyncio.sleep(0.001)
        pytest.fail("document OCR gate did not recover after worker completion")

    result, busy, recovered = asyncio.run(scenario())

    assert isinstance(result, _Ok)
    assert result.value == {
        "text": "",
        "status": "timeout",
        "diagnostic": "document_pdf_ocr_timeout",
        "backend": "rapidocr",
    }
    assert isinstance(busy, _Ok)
    assert busy.value["diagnostic"] == "document_pdf_ocr_busy"
    assert isinstance(recovered, _Ok)
    assert recovered.value["text"] == "worker text 2"
    assert pipeline.calls == 2
    assert document_ocr_modules.image_api.last_image.closed is True


def test_document_page_entry_maps_unexpected_worker_failure(
    document_ocr_modules: Any,
) -> None:
    class FailingPipeline:
        def recognize_document_page(self, _image: Any) -> _OcrSnapshot:
            raise RuntimeError("sensitive worker detail")

    owner = SimpleNamespace(
        _ocr_pipeline=FailingPipeline(),
        _cfg=SimpleNamespace(ocr_backend_selection="rapidocr"),
    )
    encoded = base64.b64encode(b"\xff\xd8\xffpage").decode("ascii")
    result = asyncio.run(
        document_ocr_modules.entry._OcrEntriesMixin.study_ocr_document_page(
            owner,
            f"data:image/jpeg;base64,{encoded}",
        )
    )

    assert isinstance(result, _Ok)
    assert result.value == {
        "text": "",
        "status": "ocr_failed",
        "diagnostic": "document_pdf_ocr_failed",
        "backend": "rapidocr",
    }
    assert document_ocr_modules.image_api.last_image.closed is True


def test_document_page_entry_reports_uninitialized_pipeline(
    document_ocr_modules: Any,
) -> None:
    owner = SimpleNamespace(
        _ocr_pipeline=None,
        _cfg=SimpleNamespace(ocr_backend_selection="tesseract"),
    )

    result = asyncio.run(
        document_ocr_modules.entry._OcrEntriesMixin.study_ocr_document_page(
            owner,
            "not-decoded-because-pipeline-is-unavailable",
        )
    )

    assert isinstance(result, _Ok)
    assert result.value == {
        "text": "",
        "status": "unavailable",
        "diagnostic": "document_pdf_ocr_unavailable",
        "backend": "rapidocr",
    }


def test_document_page_entry_metadata_keeps_ocr_text_out_of_llm_results(
    document_ocr_modules: Any,
) -> None:
    entry = document_ocr_modules.entry._OcrEntriesMixin
    metadata = entry.study_ocr_document_page.meta
    schema = metadata["input_schema"]

    assert document_ocr_modules.entry._DOCUMENT_PAGE_OCR_TIMEOUT_SECONDS == 35.0
    assert metadata["llm_result_fields"] == ["status", "diagnostic", "backend"]
    assert metadata["timeout"] == 40.0
    assert "text" not in metadata["llm_result_fields"]
    assert schema["required"] == ["image_data_url"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["image_data_url"]["maxLength"] == 8_388_640

    capability_metadata = entry.study_ocr_document_capabilities.meta
    assert capability_metadata["timeout"] == 10.0
    assert capability_metadata["input_schema"] == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    assert capability_metadata["llm_result_fields"] == [
        "protocol",
        "enabled",
        "ready",
        "backend",
        "max_page_pixels",
        "max_image_bytes",
        "diagnostic",
    ]
