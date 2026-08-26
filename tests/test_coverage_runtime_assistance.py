from __future__ import annotations

import asyncio
import importlib
import math
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]


class _PomodoroConfig:
    def __init__(
        self,
        *,
        focus_minutes: int = 1,
        short_break_minutes: int = 1,
        long_break_minutes: int = 2,
        long_break_interval: int = 2,
        allow_custom_duration: bool = True,
        allow_skip_break: bool = True,
    ) -> None:
        self.focus_minutes = focus_minutes
        self.short_break_minutes = short_break_minutes
        self.long_break_minutes = long_break_minutes
        self.long_break_interval = long_break_interval
        self.allow_custom_duration = allow_custom_duration
        self.allow_skip_break = allow_skip_break

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


class _SupervisionConfig:
    def __init__(
        self,
        *,
        enabled: bool = True,
        remind_interval_minutes: float = 1,
        inactivity_timeout_minutes: float = 1,
        idle_away_seconds: float = 120,
    ) -> None:
        self.enabled = enabled
        self.remind_interval_minutes = remind_interval_minutes
        self.inactivity_timeout_minutes = inactivity_timeout_minutes
        self.idle_away_seconds = idle_away_seconds

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@pytest.fixture()
def assistance_modules(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    package_name = f"_coverage_runtime_assistance_{time.time_ns()}"
    package = ModuleType(package_name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package_name, package)

    models = ModuleType(f"{package_name}.models")
    models.PomodoroConfig = _PomodoroConfig
    models.SupervisionConfig = _SupervisionConfig
    models._range_or_default = lambda value, low, high, default: (
        int(value) if low <= int(value) <= high else default
    )
    monkeypatch.setitem(sys.modules, models.__name__, models)

    habits = ModuleType(f"{package_name}.study_habit_store")
    habits.StudyHabitStore = object
    monkeypatch.setitem(sys.modules, habits.__name__, habits)

    return SimpleNamespace(
        pomodoro=importlib.import_module(f"{package_name}.pomodoro_timer"),
        supervision=importlib.import_module(f"{package_name}.supervision"),
        voice_contracts=importlib.import_module(f"{package_name}.voice_contracts"),
        voice_filter=importlib.import_module(f"{package_name}.voice_filter"),
    )


class _Habits:
    def __init__(self) -> None:
        self.sessions: list[dict[str, Any]] = []
        self.finished: list[dict[str, Any]] = []
        self.goals: dict[str, dict[str, Any]] = {}
        self.goal_updates: list[tuple[str, float]] = []
        self.checkins: list[dict[str, Any]] = []

    def create_focus_session(self, **kwargs: Any) -> dict[str, Any]:
        session = {"id": f"session-{len(self.sessions) + 1}", **kwargs}
        self.sessions.append(session)
        return dict(session)

    def finish_focus_session(self, session_id: str, **kwargs: Any) -> dict[str, Any]:
        result = {"id": session_id, "date": kwargs["ended_at"][:10], **kwargs}
        self.finished.append(result)
        return dict(result)

    def get_goal(self, goal_id: str) -> dict[str, Any] | None:
        return self.goals.get(goal_id)

    def update_goal(self, goal_id: str, *, progress_delta: float) -> None:
        self.goal_updates.append((goal_id, progress_delta))

    def record_checkin(self, **kwargs: Any) -> None:
        self.checkins.append(kwargs)


def test_pomodoro_pause_resume_and_cancel_records_active_time(assistance_modules: Any) -> None:
    now = [1_700_000_000.0]
    habits = _Habits()
    timer = assistance_modules.pomodoro.PomodoroTimer(
        habits,
        config=_PomodoroConfig(focus_minutes=25),
        clock=lambda: now[0],
        checkin_timezone="Invalid/Timezone",
    )

    started = timer.start(goal_id="goal", focus_minutes=5)
    assert started["state"] == "focusing"
    assert started["remaining_seconds"] == 300
    assert timer.start(goal_id="ignored")["goal_id"] == "goal"

    now[0] += 30
    paused = timer.pause()
    assert paused["state"] == "paused"
    assert paused["pause_count"] == 1
    assert timer.pause()["pause_count"] == 1

    now[0] += 20
    resumed = timer.resume()
    assert resumed["state"] == "focusing"
    assert resumed["remaining_seconds"] == 270
    assert timer.resume()["state"] == "focusing"

    now[0] += 30
    canceled = timer.stop()
    assert canceled["state"] == "cancelled"
    assert habits.finished[0]["status"] == "cancelled"
    assert habits.finished[0]["actual_minutes"] == pytest.approx(1.0)
    assert timer.stop()["state"] == "cancelled"


def test_pomodoro_completion_progress_breaks_and_skip_controls(assistance_modules: Any) -> None:
    now = [1_700_000_000.0]
    habits = _Habits()
    habits.goals = {
        "minutes": {"unit": "minutes"},
        "pomodoros": {"unit": "pomodoro"},
        "ignored": {"unit": "pages"},
    }
    config = _PomodoroConfig(
        focus_minutes=1,
        short_break_minutes=1,
        long_break_minutes=2,
        long_break_interval=2,
    )
    timer = assistance_modules.pomodoro.PomodoroTimer(
        habits, config=config, clock=lambda: now[0]
    )

    timer.start(goal_id="minutes")
    now[0] += 60
    first = timer.tick()
    assert first["state"] == "short_break"
    assert habits.goal_updates == [("minutes", 1.0)]
    assert habits.checkins[0]["source"] == "session_derived"
    assert timer.skip_break()["state"] == "completed"

    timer.start(goal_id="pomodoros")
    now[0] += 60
    second = timer.stop()
    assert second["state"] == "long_break"
    assert habits.goal_updates[-1] == ("pomodoros", 1.0)
    now[0] += 120
    assert timer.tick()["state"] == "completed"

    no_skip = assistance_modules.pomodoro.PomodoroTimer(
        _Habits(), config=_PomodoroConfig(allow_skip_break=False), clock=lambda: now[0]
    )
    no_skip.start()
    now[0] += 60
    no_skip.tick()
    assert no_skip.skip_break()["state"] == "short_break"

    no_derive_habits = _Habits()
    no_derive_habits.goals["ignored"] = {"unit": "pages"}
    no_derive = assistance_modules.pomodoro.PomodoroTimer(
        no_derive_habits,
        config=_PomodoroConfig(),
        clock=lambda: now[0],
        auto_derive_from_session=False,
    )
    no_derive.start(goal_id="ignored")
    now[0] += 60
    no_derive.tick()
    assert no_derive_habits.goal_updates == []
    assert no_derive_habits.checkins == []


def test_voice_contract_builders_and_arbitration(assistance_modules: Any) -> None:
    contracts = assistance_modules.voice_contracts
    assert contracts.voice_transcript_noop("", source="test") == {
        "source": "test",
        "action": "noop",
        "reason": "noop",
    }
    assert contracts.voice_transcript_cancel_response(filter_payload={"relay": True})["filter"] == {
        "relay": True
    }
    assert contracts.voice_transcript_prime_context(
        " context ", skipped=True, filter_payload={"relay": True}
    )["context"] == "context"

    result = contracts.arbitrate_voice_transcript_results(
        [
            {"success": False, "plugin_id": "failed"},
            {
                "success": True,
                "plugin_id": "prime",
                "event_id": "event-prime",
                "result": {"action": "prime_context", "context": " lesson ", "priority": 100},
            },
            {
                "success": True,
                "plugin_id": "cancel-first",
                "result": {"action": "cancel_response", "priority": "2"},
            },
            {
                "success": True,
                "plugin_id": "cancel-later",
                "result": {"action": "cancel_response", "priority": 2},
            },
        ]
    )
    assert result["action"] == "cancel_response"
    assert result["source_plugin"] == "cancel-first"

    all_noop = contracts.arbitrate_voice_transcript_results(
        [
            {"success": True, "result": {"action": "invalid", "priority": math.inf}},
            {"success": True, "result": {"action": "prime_context", "context": ""}},
        ]
    )
    assert all_noop["reason"] == "all_noop"
    assert all_noop["handlers"] == 2
    assert contracts.arbitrate_voice_transcript_results(None)["reason"] == "no_subscribers"
    assert contracts.arbitrate_voice_transcript_results(["bad", {"success": False}])[
        "reason"
    ] == "no_handler_result"


class _Clock:
    def __init__(self, value: float = 10.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def test_voice_filter_name_window_intent_overlap_and_config_fallback(assistance_modules: Any) -> None:
    voice = assistance_modules.voice_filter
    clock = _Clock()
    filter_ = voice.VoiceFilter(names=["Yui"], clock=clock)
    called = filter_.filter(
        "before Yui explain this",
        screen_text="x² equation",
        screen_type="question",
        session_key="session-a",
        extra_names=["Assistant"],
    )
    assert called == {
        "should_relay": True,
        "method": "name_call",
        "name": "Yui",
        "pre_context": "before",
        "question": "explain this",
        "screen_type": "question",
        "subject": "math",
    }
    clock.value += 1
    assert filter_.filter("continue", session_key="session-a")["method"] == "name_window"
    clock.value += 4
    assert filter_.filter("tiny", session_key="session-a")["method"] == "too_short"
    assert filter_.filter("Why is this useful", screen_text="gravity")["subject"] == "physics"
    overlap = filter_.filter(
        "value 12345",
        screen_text="the value is 12345",
        subject="math",
        session_key="other",
    )
    assert overlap["method"] == "ocr_overlap"
    assert filter_.filter("ordinary statement", screen_text="unrelated") is None
    assert filter_.filter("") is None

    manager = SimpleNamespace(
        get_character_data=lambda: (
            None,
            "Momo",
            None,
            {"Momo": {"nicknames": "Mom, 小桃"}},
        )
    )
    configured = voice.VoiceFilter(config_manager=manager)
    assert configured.names == ("Momo", "Mom", "小桃")
    plugin_configured = voice.VoiceFilter(
        config_manager=SimpleNamespace(get_character_data=lambda: None),
        plugin_config={"voice_filter": {"names": ["Neko", "neko"]}},
    )
    assert plugin_configured.names == ("Neko",)

    warnings: list[tuple[Any, ...]] = []
    fallback = voice.VoiceFilter(
        config_manager=SimpleNamespace(
            get_character_data=lambda: (_ for _ in ()).throw(RuntimeError("unavailable"))
        ),
        logger=SimpleNamespace(warning=lambda *args: warnings.append(args)),
    )
    assert fallback.names == voice.CATGIRL_NAMES
    assert warnings


def test_voice_context_staleness_and_subject_helpers(assistance_modules: Any) -> None:
    voice = assistance_modules.voice_filter
    stale = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    state = SimpleNamespace(
        last_ocr_text="x² + 2 = 6",
        last_ocr_at=stale,
        last_screen_classification={"screen_type": "question"},
        active_mode="focus",
    )
    context = voice.build_context_for_catgirl(
        "please help",
        state,
        {"topic": "algebra"},
        {"pre_context": "I tried substitution", "question": "Where did I go wrong?"},
    )
    assert "数学符号可能有识别误差" in context
    assert "屏幕内容可能已切换" in context
    assert "[状态] algebra | focus" in context
    assert "[铺垫] I tried substitution" in context
    assert context.endswith("[问题] Where did I go wrong?")

    assert voice._derive_subject("H2O and NaCl") == "chemistry"
    assert voice._derive_subject("velocity") == "physics"
    assert voice._derive_subject("plain prose") == "default"
    assert voice._find_earliest_name("yui and 结衣", ["结衣", "Yui"]) == ("Yui", 0)
    assert voice._number_sequence_match("一 and ² and 十", "1 2 10") == 1.0
    assert voice._text_overlap_ratio("", "abc") == 0.0
    assert voice._has_question_intent("怎么求解") is True
    assert voice._has_question_intent("") is False
    assert voice._ocr_is_stale("invalid timestamp") is True


@pytest.mark.asyncio
async def test_voice_cancel_success_safe_failures_and_unexpected_error(assistance_modules: Any) -> None:
    voice = assistance_modules.voice_filter

    class Session:
        def __init__(self, error: Exception | None = None) -> None:
            self.error = error
            self.calls = 0

        async def cancel_response(self) -> None:
            self.calls += 1
            if self.error:
                raise self.error

    assert await voice.safe_cancel_response(None) is False
    assert await voice.safe_cancel_response(SimpleNamespace()) is False
    session = Session()
    assert await voice.safe_cancel_response(session) is True
    assert session.calls == 1
    assert await voice.safe_cancel_response(Session(asyncio.InvalidStateError())) is False
    assert await voice.safe_cancel_response(Session(RuntimeError("response already canceled"))) is False
    with pytest.raises(RuntimeError, match="transport failed"):
        await voice.safe_cancel_response(Session(RuntimeError("transport failed")))


def test_supervision_success_sensor_unavailable_inactivity_distraction_and_away(
    assistance_modules: Any,
) -> None:
    supervision = assistance_modules.supervision
    clock = _Clock(100.0)
    controller = supervision.SupervisionController(
        _SupervisionConfig(), clock=clock
    )
    started = controller.on_focus_start(
        goal={"id": "goal-1"}, planned_minutes=25
    )
    assert started["message"] == "focus_started"
    assert controller.due_reminder()["due"] is False
    clock.value = 160.0
    due = controller.due_reminder()
    assert due["due"] is True
    assert due["reminder_level"] == "low_frequency"

    unavailable = controller.observe_activity(
        ocr_text="",
        sensor_available=False,
        idle_seconds="invalid",
        foreground_category="",
    )
    assert unavailable["inactivity_detected"] is False
    assert unavailable["sensor_available"] is False

    active = controller.observe_activity(
        ocr_text="new page",
        sensor_available=True,
        idle_seconds=5,
        foreground_category="study",
    )
    assert active["reminder_level"] == "active"
    assert active["idle_seconds"] == 5

    clock.value = 230.0
    inactive = controller.observe_activity(
        ocr_text="new page",
        sensor_available=True,
        idle_seconds=70,
        foreground_category="study",
    )
    assert inactive["inactivity_detected"] is True
    assert inactive["suggested_action"] == "pause_or_switch"

    distracted = controller.observe_activity(
        ocr_text="new page",
        sensor_available=True,
        idle_seconds=10,
        foreground_category="gaming",
    )
    assert distracted["distraction_detected"] is True
    assert distracted["suggested_action"] == "return_to_focus"

    away = controller.observe_activity(
        ocr_text="new page",
        sensor_available=True,
        idle_seconds=120,
        foreground_category="study",
    )
    assert away["reminder_level"] == "away"
    assert away["inactivity_detected"] is True

    ended = controller.on_focus_end(now=240)
    assert ended["focus_active"] is False
    assert ended["reminder_level"] == "end"
    disabled = controller.set_enabled(False)
    assert disabled["enabled"] is False
    assert disabled["reminder_level"] == "disabled"
