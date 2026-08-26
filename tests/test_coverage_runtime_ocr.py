from __future__ import annotations

import importlib
import importlib.machinery
import sys
import time
from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]


@dataclass(slots=True)
class _OcrSnapshot:
    text: str = ""
    boxes: list[dict[str, Any]] = field(default_factory=list)
    status: str = "empty"
    backend: str = ""
    captured_at: str = ""
    diagnostic: str = ""


class _ActivitySnapshot:
    def __init__(self, **values: Any) -> None:
        self.__dict__.update(values)


class _Logger:
    def __init__(self) -> None:
        self.warnings: list[tuple[Any, ...]] = []
        self.debugs: list[tuple[Any, ...]] = []

    def warning(self, *args: Any, **_kwargs: Any) -> None:
        self.warnings.append(args)

    def debug(self, *args: Any, **_kwargs: Any) -> None:
        self.debugs.append(args)


class _OcrBackend:
    def __init__(self, result: Any = "recognized text", *, available: bool = True) -> None:
        self.result = result
        self.available = available
        self.error: Exception | None = None
        self.closed = False
        self.calls = 0

    def is_available(self) -> bool:
        return self.available

    def extract_text(self, _image: Any) -> Any:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result

    def close(self) -> None:
        self.closed = True


class _CaptureBackend:
    def __init__(self, frame: Any) -> None:
        self.frame = frame
        self.error: Exception | None = None
        self.calls: list[tuple[Any, Any]] = []

    def capture_frame(self, target: Any, profile: Any) -> Any:
        self.calls.append((target, profile))
        if self.error is not None:
            raise self.error
        return self.frame


def _config(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "ocr_enabled": True,
        "ocr_backend_selection": "rapidocr",
        "ocr_capture_backend": "auto",
        "ocr_left_inset_ratio": 0.03,
        "ocr_right_inset_ratio": 0.03,
        "ocr_top_ratio": 0.0,
        "ocr_bottom_inset_ratio": 0.0,
        "ocr_tesseract_path": "",
        "ocr_install_target_dir": "",
        "ocr_languages": ["eng"],
        "rapidocr_install_target_dir": "",
        "rapidocr_engine_type": "onnxruntime",
        "rapidocr_lang_type": "ch",
        "rapidocr_model_type": "mobile",
        "rapidocr_ocr_version": "v4",
        "llm_vision_enabled": True,
        "llm_vision_max_image_px": 64,
        "awareness": SimpleNamespace(classify_mode="both", image_max_bytes=20_000),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture()
def ocr_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    package_name = f"_coverage_runtime_ocr_{time.time_ns()}"
    package = ModuleType(package_name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package_name, package)

    models = ModuleType(f"{package_name}.models")
    models.OCR_SNIPPET_MAX_CHARS = 600
    models.ActivitySnapshot = _ActivitySnapshot
    models.OcrSnapshot = _OcrSnapshot
    models.StudyConfig = object
    models.utc_now_iso = lambda: "2026-08-26T00:00:00Z"
    monkeypatch.setitem(sys.modules, models.__name__, models)

    classifier = ModuleType(f"{package_name}.screen_classifier")
    classifier.classify_app_from_title = lambda title: "browser" if title else "other"
    classifier.classify_screen_from_ocr = lambda text, **_kwargs: SimpleNamespace(
        screen_type="question" if "?" in text else "reading"
    )
    monkeypatch.setitem(sys.modules, classifier.__name__, classifier)

    imagehash = ModuleType("imagehash")
    imagehash.__spec__ = importlib.machinery.ModuleSpec("imagehash", loader=None)
    imagehash.phash = lambda _image, hash_size=8: "000000000000000f"
    monkeypatch.setitem(sys.modules, "imagehash", imagehash)
    return importlib.import_module(f"{package_name}.study_ocr_pipeline")


def test_ocr_pipeline_success_disabled_unavailable_and_capture_degradation(
    ocr_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    frame = Image.new("RGB", (80, 40), "white")
    backend = _OcrBackend(
        [
            {"text": "Solve"},
            SimpleNamespace(text="2 + 2?", to_dict=lambda: {"text": "2 + 2?"}),
        ]
    )
    capture = _CaptureBackend(frame)
    pipeline = ocr_module.StudyOcrPipeline(
        logger=_Logger(), config=_config(), ocr_backend=backend, capture_backend=capture
    )
    try:
        direct = pipeline.snapshot_from_image(frame)
        assert direct.status == "ok"
        assert direct.text == "Solve 2 + 2?"
        assert len(direct.boxes) == 2
        assert pipeline.latest_vision_snapshot()["vision_image_base64"].startswith(
            "data:image/jpeg;base64,"
        )

        captured = pipeline.capture_snapshot({"title": "Lesson"})
        assert captured.status == "ok"
        assert capture.calls[0][1].left_inset_ratio == pytest.approx(0.03)

        capture.error = RuntimeError("capture port unavailable")
        failed = pipeline.capture_snapshot({"title": "Lesson"})
        assert failed.status == "capture_failed"
        assert failed.diagnostic == "capture port unavailable"
        assert pipeline.latest_vision_snapshot() == {}

        monkeypatch.setattr(
            ocr_module.StudyOcrPipeline,
            "_capture_fullscreen",
            staticmethod(lambda: (_ for _ in ()).throw(RuntimeError("desktop unavailable"))),
        )
        fullscreen = pipeline.capture_snapshot()
        assert fullscreen.status == "capture_failed"
        assert "desktop unavailable" in fullscreen.diagnostic
    finally:
        pipeline.close()

    disabled = ocr_module.StudyOcrPipeline(
        logger=_Logger(), config=_config(ocr_enabled=False), ocr_backend=backend
    )
    try:
        assert disabled.capture_snapshot().status == "disabled"
        assert disabled.recognize_document_page(frame).diagnostic == "document_pdf_ocr_disabled"
        assert disabled.snapshot_from_image(None).status == "empty"
    finally:
        disabled.close()

    unavailable_backend = _OcrBackend(available=False)
    unavailable = ocr_module.StudyOcrPipeline(
        logger=_Logger(), config=_config(), ocr_backend=unavailable_backend
    )
    try:
        assert unavailable.recognize_document_page(frame).status == "unavailable"
        assert unavailable.recognize_document_page(None).status == "ocr_failed"
        unavailable_backend.available = True
        unavailable_backend.error = RuntimeError("engine failed")
        assert unavailable.recognize_document_page(frame).status == "ocr_failed"
    finally:
        unavailable.close()


def test_lightweight_capture_success_change_detection_and_activity(ocr_module: Any) -> None:
    frame = Image.new("RGB", (1600, 800), "white")
    backend = _OcrBackend("What is 2 + 2?")
    capture = _CaptureBackend(frame)
    pipeline = ocr_module.StudyOcrPipeline(
        logger=_Logger(), config=_config(), ocr_backend=backend, capture_backend=capture
    )
    try:
        first = pipeline.capture_lightweight({"title": "Math lesson"})
        second = pipeline.capture_lightweight({"title": "Math lesson"})

        assert first.status == "ok"
        assert first.app_type == "browser"
        assert first.activity_type == "question"
        assert first.ocr_text_snippet == "What is 2 + 2?"
        assert first.jpeg_bytes
        assert first.jpeg_base64
        assert first.has_content_change is True
        assert second.has_content_change is False
        activity = first.to_activity_snapshot()
        assert activity is not None
        assert activity.classify_method == "both"
        assert ocr_module.LightweightSnapshot(status="capture_failed", captured_at="now").to_activity_snapshot() is None
    finally:
        pipeline.close()


class _TrackedFuture(Future[Any]):
    def __init__(self, *, running: bool) -> None:
        super().__init__()
        self.cancel_attempts = 0
        if running:
            assert self.set_running_or_notify_cancel() is True

    def cancel(self) -> bool:
        self.cancel_attempts += 1
        return super().cancel()


class _TimeoutExecutor:
    def __init__(self) -> None:
        self.futures: list[_TrackedFuture] = []
        self.shutdown_calls: list[bool] = []

    def submit(self, _function: Any, *_args: Any, **_kwargs: Any) -> _TrackedFuture:
        future = _TrackedFuture(running=not self.futures)
        self.futures.append(future)
        return future

    def shutdown(self, *, wait: bool) -> None:
        self.shutdown_calls.append(wait)


def test_lightweight_timeout_degrades_to_jpeg_only(ocr_module: Any) -> None:
    frame = Image.new("RGB", (80, 40), "white")
    pipeline = ocr_module.StudyOcrPipeline(
        logger=_Logger(),
        config=_config(),
        ocr_backend=_OcrBackend("never returned"),
        capture_backend=_CaptureBackend(frame),
    )
    real_executor = pipeline._executor
    assert real_executor is not None
    real_executor.shutdown(wait=True)
    fake_executor = _TimeoutExecutor()
    pipeline._executor = fake_executor
    try:
        snapshot = pipeline.capture_lightweight({"title": "Lesson"})
        assert snapshot.status == "ok"
        assert snapshot.jpeg_bytes
        assert snapshot.ocr_text_snippet == ""
        assert "ocr_status=ocr_failed" in snapshot.diagnostic
        running_future, pending_future = fake_executor.futures
        assert running_future.cancel_attempts == 1
        assert running_future.running() is True
        assert running_future.cancelled() is False
        assert pending_future.cancel_attempts == 1
        assert pending_future.cancelled() is True
        assert pipeline._executor is None
        assert pipeline._retired_executors == [fake_executor]
        assert fake_executor.shutdown_calls == [False]
    finally:
        pipeline.close()
    assert fake_executor.shutdown_calls == [False, True]


def test_ocr_pipeline_lifecycle_normalization_and_snapshot_expiry(
    ocr_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    logger = _Logger()
    backend = _OcrBackend("text")
    pipeline = ocr_module.StudyOcrPipeline(
        logger=logger, config=_config(), ocr_backend=backend
    )
    try:
        text, boxes = pipeline._normalize_ocr_output(["甲", "B", None])
        assert text == "甲B"
        assert boxes == []
        assert pipeline._normalize_ocr_output(None) == ("", [])
        assert pipeline._normalize_ocr_output(123) == ("123", [])
        assert pipeline._has_content_change("invalid") is True
        pipeline._last_thumbnail_phash = "invalid"
        assert pipeline._has_content_change("invalid") is False

        frame = Image.new("RGB", (80, 40), "white")
        pipeline._remember_vision_snapshot(frame, now=10.0)
        monkeypatch.setattr(ocr_module.time, "monotonic", lambda: 100.0)
        assert pipeline.latest_vision_snapshot() == {}

        pipeline.update_config(_config(llm_vision_enabled=False))
        assert pipeline.latest_vision_snapshot() == {}
    finally:
        pipeline.close()
    pipeline.close()
    with pytest.raises(RuntimeError, match="closed"):
        pipeline._require_executor()


class _FakeImage:
    def __init__(self, *, width: int = 100, height: int = 80, mode: str = "RGB") -> None:
        self.width = width
        self.height = height
        self.mode = mode
        self.crops: list[tuple[int, int, int, int]] = []

    def crop(self, box: tuple[int, int, int, int]) -> "_FakeImage":
        self.crops.append(box)
        return self

    def convert(self, mode: str) -> "_FakeImage":
        self.mode = mode
        return self


@pytest.fixture()
def capture_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    package_name = f"_coverage_runtime_capture_{time.time_ns()}"
    package = ModuleType(package_name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package_name, package)
    return importlib.import_module(f"{package_name}.study_capture_backends")


def test_capture_backend_helpers_and_visibility_guards(capture_module: Any) -> None:
    profile = SimpleNamespace(
        left_inset_ratio=0.1,
        right_inset_ratio=0.2,
        top_ratio=0.25,
        bottom_inset_ratio=0.1,
    )
    image = _FakeImage()
    assert capture_module._target_window_rect({"x": 10, "y": 20, "width": 50, "height": 40}) == (
        10,
        20,
        60,
        60,
    )
    assert capture_module._crop_image_to_profile(image, profile) is image
    assert image.crops == [(10, 20, 80, 72)]
    assert "editor(7) Lesson" in capture_module.MssCaptureBackend().describe_target(
        {"process_name": "editor", "pid": 7, "title": "Lesson"}
    )
    with pytest.raises(RuntimeError, match="no usable"):
        capture_module._target_window_rect({"width": 0, "height": 0})
    with pytest.raises(RuntimeError, match="minimized"):
        capture_module._require_visible_capture_target(
            {"is_minimized": True}, backend_kind="fake"
        )
    with pytest.raises(RuntimeError, match="blocked"):
        capture_module._require_visible_capture_target(
            {"eligible": False, "exclude_reason": "blocked"}, backend_kind="fake"
        )
    with pytest.raises(RuntimeError, match="hwnd"):
        capture_module._target_hwnd({}, backend_kind="printwindow")


def test_mss_dxcam_and_pyautogui_use_fake_capture_ports(
    capture_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_image = _FakeImage(mode="RGBA")
    image_api = SimpleNamespace(
        frombytes=lambda *_args, **_kwargs: fake_image,
        fromarray=lambda *_args, **_kwargs: fake_image,
    )
    pil = ModuleType("PIL")
    pil.Image = image_api
    monkeypatch.setitem(sys.modules, "PIL", pil)
    profile = SimpleNamespace(
        left_inset_ratio=0.0,
        right_inset_ratio=0.0,
        top_ratio=0.0,
        bottom_inset_ratio=0.0,
    )
    target = {"left": 1, "top": 2, "width": 10, "height": 20}

    shot = SimpleNamespace(size=(10, 20), rgb=b"pixels")
    mss_port = SimpleNamespace(grab=lambda monitor: shot)
    mss_module = ModuleType("mss")
    mss_module.mss = lambda: mss_port
    monkeypatch.setitem(sys.modules, "mss", mss_module)
    mss_backend = capture_module.MssCaptureBackend()
    assert mss_backend.is_available() is True
    assert mss_backend.capture_frame(target, profile) is fake_image

    camera = SimpleNamespace(grab=lambda region: object())
    dxcam_module = ModuleType("dxcam")
    dxcam_module.create = lambda **_kwargs: camera
    monkeypatch.setitem(sys.modules, "dxcam", dxcam_module)
    dxcam_backend = capture_module.DxcamCaptureBackend()
    assert dxcam_backend.is_available() is True
    assert dxcam_backend.capture_frame(target, profile) is fake_image
    camera.grab = lambda region: None
    with pytest.raises(RuntimeError, match="no frame"):
        dxcam_backend.capture_frame(target, profile)

    screenshots: list[tuple[int, int, int, int]] = []
    pyautogui = ModuleType("pyautogui")
    pyautogui.size = lambda: (100, 100)
    pyautogui.screenshot = lambda *, region: screenshots.append(region) or fake_image
    monkeypatch.setitem(sys.modules, "pyautogui", pyautogui)
    py_backend = capture_module.PyAutoGuiCaptureBackend()
    assert py_backend.is_available() is True
    assert py_backend.capture_frame(target, profile) is fake_image
    assert screenshots == [(1, 2, 10, 20)]

    monkeypatch.setattr(capture_module.sys, "platform", "win32")
    with pytest.raises(RuntimeError, match="secondary_monitor"):
        py_backend.capture_frame(
            {"left": 101, "top": 0, "width": 10, "height": 10}, profile
        )


def test_capture_backend_dependency_unavailable(capture_module: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "mss", None)
    monkeypatch.setitem(sys.modules, "dxcam", None)
    monkeypatch.setitem(sys.modules, "pyautogui", None)
    assert capture_module.MssCaptureBackend().is_available() is False
    assert capture_module.DxcamCaptureBackend().is_available() is False
    assert capture_module.PyAutoGuiCaptureBackend().is_available() is False
    monkeypatch.setattr(capture_module.sys, "platform", "linux")
    assert capture_module.PrintWindowCaptureBackend().is_available() is False


def test_capture_platform_helpers_use_fake_system_ports(
    capture_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert capture_module._target_value(SimpleNamespace(title="Lesson"), "title") == "Lesson"
    with pytest.raises(RuntimeError, match="target window"):
        capture_module._require_visible_capture_target(None, backend_kind="fake")

    unchanged = _FakeImage()
    impossible_profile = SimpleNamespace(
        left_inset_ratio=1.0,
        right_inset_ratio=1.0,
        top_ratio=1.0,
        bottom_inset_ratio=1.0,
    )
    assert capture_module._crop_image_to_profile(unchanged, impossible_profile) is unchanged
    assert unchanged.crops == []

    pyautogui = ModuleType("pyautogui")
    pyautogui.size = lambda: (100, 80)
    monkeypatch.setitem(sys.modules, "pyautogui", pyautogui)
    cases = [
        ((100, 0, 120, 20), (False, "window_entirely_in_right_secondary_monitor")),
        ((-20, 0, 0, 20), (False, "window_entirely_in_left_secondary_monitor")),
        ((0, 80, 20, 100), (False, "window_entirely_in_bottom_secondary_monitor")),
        ((0, -20, 20, 0), (False, "window_entirely_in_top_secondary_monitor")),
        ((-1, 0, 20, 20), (False, "window_spans_across_primary_and_secondary_monitor")),
        ((0, 0, 20, 20), (True, "")),
    ]
    for rect, expected in cases:
        assert capture_module._is_window_on_primary_monitor(rect) == expected

    class DpiPort:
        def __init__(self) -> None:
            self.calls: list[Any] = []

        def __call__(self, context: Any) -> object:
            self.calls.append(context)
            return object() if len(self.calls) == 1 else None

    dpi_port = DpiPort()
    monkeypatch.setattr(
        capture_module.ctypes,
        "windll",
        SimpleNamespace(user32=SimpleNamespace(SetThreadDpiAwarenessContext=dpi_port)),
        raising=False,
    )
    assert capture_module._run_with_thread_dpi_awareness(lambda: (1, 2, 3, 4)) == (
        1,
        2,
        3,
        4,
    )
    assert len(dpi_port.calls) == 2


def test_printwindow_backend_uses_fake_win32_ports(
    capture_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(capture_module.sys, "platform", "win32")
    pywintypes = ModuleType("pywintypes")
    pywintypes.error = OSError
    monkeypatch.setitem(sys.modules, "pywintypes", pywintypes)

    events: list[Any] = []

    class FakeBitmap:
        def CreateCompatibleBitmap(self, _dc: Any, width: int, height: int) -> None:
            events.append(("bitmap", width, height))

        def GetInfo(self) -> dict[str, int]:
            return {"bmWidth": 10, "bmHeight": 5}

        def GetBitmapBits(self, _signed: bool) -> bytes:
            return b"pixels"

        def GetHandle(self) -> int:
            return 99

    class FakeDc:
        def CreateCompatibleDC(self) -> "FakeDc":
            return self

        def SelectObject(self, value: Any) -> str:
            events.append(("select", value))
            return "previous"

        def GetSafeHdc(self) -> int:
            return 77

        def BitBlt(self, *args: Any) -> None:
            events.append(("bitblt", args))

        def DeleteDC(self) -> None:
            events.append("delete-dc")

    win32gui = ModuleType("win32gui")
    win32gui.GetWindowRect = lambda _hwnd: (0, 0, 10, 5)
    win32gui.GetWindowDC = lambda _hwnd: 55
    win32gui.DeleteObject = lambda handle: events.append(("delete-object", handle))
    win32gui.ReleaseDC = lambda hwnd, hdc: events.append(("release", hwnd, hdc))
    monkeypatch.setitem(sys.modules, "win32gui", win32gui)
    win32con = ModuleType("win32con")
    win32con.SRCCOPY = 1
    monkeypatch.setitem(sys.modules, "win32con", win32con)
    win32ui = ModuleType("win32ui")
    win32ui.CreateDCFromHandle = lambda _hdc: FakeDc()
    win32ui.CreateBitmap = FakeBitmap
    monkeypatch.setitem(sys.modules, "win32ui", win32ui)

    fake_image = _FakeImage(width=10, height=5)
    pil = ModuleType("PIL")
    pil.Image = SimpleNamespace(frombuffer=lambda *_args, **_kwargs: fake_image)
    monkeypatch.setitem(sys.modules, "PIL", pil)
    monkeypatch.setattr(
        capture_module.ctypes,
        "windll",
        SimpleNamespace(user32=SimpleNamespace(PrintWindow=lambda *_args: False)),
        raising=False,
    )
    monkeypatch.setattr(
        capture_module.sys,
        "getwindowsversion",
        lambda: SimpleNamespace(major=10, minor=0),
        raising=False,
    )

    profile = SimpleNamespace(
        left_inset_ratio=0.0,
        right_inset_ratio=0.0,
        top_ratio=0.0,
        bottom_inset_ratio=0.0,
    )
    backend = capture_module.PrintWindowCaptureBackend()
    assert backend.is_available() is True
    assert backend.capture_frame({"hwnd": 1}, profile) is fake_image
    assert any(event[0] == "bitblt" for event in events if isinstance(event, tuple))
    assert ("delete-object", 99) in events
    assert ("release", 1, 55) in events


def test_pipeline_backend_resolution_and_platform_title_fallbacks(
    ocr_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    package_name = ocr_module.__package__

    class OwnedBackend(_OcrBackend):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__("owned")
            self.kwargs = kwargs

    tesseract = ModuleType(f"{package_name}.tesseract_support")
    tesseract.TesseractOcrBackend = OwnedBackend
    monkeypatch.setitem(sys.modules, tesseract.__name__, tesseract)

    plugin = ModuleType("plugin")
    plugin.__path__ = []  # type: ignore[attr-defined]
    plugins = ModuleType("plugin.plugins")
    plugins.__path__ = []  # type: ignore[attr-defined]
    shared = ModuleType("plugin.plugins._shared")
    shared.__path__ = []  # type: ignore[attr-defined]
    rapidocr = ModuleType("plugin.plugins._shared.rapidocr")
    rapidocr.__path__ = []  # type: ignore[attr-defined]
    ocr_backends = ModuleType("plugin.plugins._shared.rapidocr.ocr_backends")
    ocr_backends.RapidOcrBackend = OwnedBackend
    monkeypatch.setitem(sys.modules, "plugin", plugin)
    monkeypatch.setitem(sys.modules, "plugin.plugins", plugins)
    monkeypatch.setitem(sys.modules, "plugin.plugins._shared", shared)
    monkeypatch.setitem(sys.modules, "plugin.plugins._shared.rapidocr", rapidocr)
    monkeypatch.setitem(sys.modules, ocr_backends.__name__, ocr_backends)

    capture_backends = ModuleType(f"{package_name}.study_capture_backends")

    class NamedCapture:
        def __init__(self, name: str) -> None:
            self.name = name

    capture_backends.DxcamCaptureBackend = lambda: NamedCapture("dxcam")
    capture_backends.MssCaptureBackend = lambda: NamedCapture("mss")
    capture_backends.PrintWindowCaptureBackend = lambda: NamedCapture("printwindow")
    capture_backends.PyAutoGuiCaptureBackend = lambda: NamedCapture("pyautogui")
    monkeypatch.setitem(sys.modules, capture_backends.__name__, capture_backends)

    pipeline = ocr_module.StudyOcrPipeline(
        logger=_Logger(), config=_config(ocr_backend_selection="tesseract")
    )
    try:
        owned = pipeline._resolve_ocr_backend()
        assert owned.kwargs["languages"] == ["eng"]
        pipeline.update_config(_config(ocr_backend_selection="rapidocr"))
        assert owned.closed is True
        rapid = pipeline._resolve_ocr_backend()
        assert rapid.kwargs["plugin_id"] == "study_companion"

        for selection in ("dxcam", "mss", "printwindow", "pyautogui", "unknown"):
            pipeline.update_config(_config(ocr_capture_backend=selection))
            expected = "dxcam" if selection == "unknown" else selection
            assert pipeline._resolve_capture_backend().name == expected
    finally:
        pipeline.close()
    assert rapid.closed is True

    win32gui = ModuleType("win32gui")
    win32gui.GetForegroundWindow = lambda: 1
    win32gui.GetWindowText = lambda _hwnd: "Windows title"
    monkeypatch.setitem(sys.modules, "win32gui", win32gui)
    monkeypatch.setattr(ocr_module.sys, "platform", "win32")
    assert ocr_module.StudyOcrPipeline._get_active_window_title() == "Windows title"

    monkeypatch.setattr(ocr_module.sys, "platform", "darwin")
    results = iter(
        [
            SimpleNamespace(returncode=1, stdout=""),
            SimpleNamespace(returncode=0, stdout="macOS app\n"),
        ]
    )
    monkeypatch.setattr(ocr_module.subprocess, "run", lambda *_args, **_kwargs: next(results))
    assert ocr_module.StudyOcrPipeline._get_active_window_title() == "macOS app"

    monkeypatch.setattr(ocr_module.sys, "platform", "linux")
    monkeypatch.setattr(
        ocr_module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="Linux title\n"),
    )
    assert ocr_module.StudyOcrPipeline._get_active_window_title() == "Linux title"
