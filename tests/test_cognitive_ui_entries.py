from __future__ import annotations

import asyncio
import importlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_entries(monkeypatch: pytest.MonkeyPatch):
    package_name = f"_study_companion_cognitive_ui_test_{id(monkeypatch)}"
    package = ModuleType(package_name)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package_name, package)

    catalog_module = ModuleType(f"{package_name}.adaptive_learning.cognitive_catalog")

    class Catalog:
        aliases = {
            "calculus.chain_rule": "calculus.chain_rule",
            "college_chain_rule": "calculus.chain_rule",
        }
        codes = {
            "omit_inner_derivative",
            "differentiate_inner_incorrectly",
            "confuse_product_and_chain",
        }

        def canonical_topic_id(self, topic_id: str) -> str | None:
            return self.aliases.get(topic_id)

        def get(self, topic_id: str, code: str) -> object | None:
            return object() if topic_id in self.aliases and code in self.codes else None

    catalog_module.COGNITIVE_CATALOG_V1 = Catalog()
    monkeypatch.setitem(sys.modules, catalog_module.__name__, catalog_module)

    state_module = ModuleType(f"{package_name}.adaptive_learning.cognitive_state")

    class Reader:
        def __init__(self, store: object, *, model_version: str) -> None:
            self.store = store
            self.model_version = model_version

        def read_topic(self, topic_id: str) -> object:
            return self.store.read_topic(topic_id)

    state_module.CognitiveStateReader = Reader
    monkeypatch.setitem(sys.modules, state_module.__name__, state_module)

    common = ModuleType(f"{package_name}.entry_common")
    common.Ok = lambda payload: payload

    class SdkError(Exception):
        def __init__(self, message: str, *, code: str = "") -> None:
            super().__init__(message)
            self.code = code

    common.SdkError = SdkError

    def _raise_entry_exception(_owner: object, exc: Exception, **_kwargs: object):
        raise exc

    common._entry_exception_error = _raise_entry_exception
    common.asyncio = asyncio
    common.plugin_entry = lambda **_kwargs: lambda function: function
    common.tr = lambda _key, *, default: default

    class Ui:
        @staticmethod
        def action():
            return lambda function: function

    common.ui = Ui()
    monkeypatch.setitem(sys.modules, common.__name__, common)
    return importlib.import_module(f"{package_name}.entry_cognitive_entries")


def _config(*, enabled: bool = True, intent_policy: str = "off") -> SimpleNamespace:
    return SimpleNamespace(
        cognitive=SimpleNamespace(
            projection_enabled=enabled,
            read_mode="shadow" if enabled else "off",
            ui_enabled=enabled,
            intent_policy=intent_policy,
            model_version="cognitive-v2",
            supported_topics=("calculus.chain_rule",),
        )
    )


def _hypothesis(status: str = "supported", code: str = "omit_inner_derivative"):
    return SimpleNamespace(
        ref=SimpleNamespace(
            hypothesis_id=f"college_chain_rule:{code}",
            code=code,
            probability=0.91,
        ),
        evidence_status=status,
        support_count=2,
        diagnostic_support_count=1,
        computed_at="2026-03-01T12:00:00Z",
    )


class _Tracker:
    def __init__(self, view: object) -> None:
        self.view = view
        self.calls: list[str] = []

    def read_cognitive_state(self, topic_id: str) -> object:
        self.calls.append(topic_id)
        if isinstance(self.view, Exception):
            raise self.view
        return self.view


class _Store:
    def __init__(self) -> None:
        self.evidence_calls: list[dict[str, Any]] = []
        self.control_calls: list[dict[str, Any]] = []
        self.controls: list[dict[str, Any]] = []

    def list_cognitive_evidence(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.evidence_calls.append(dict(kwargs))
        return [
            {
                "evidence_id": "evidence-1",
                "direction": "support",
                "evidence_span": "cos(x²) is present, but 2x is missing",
                "source_kind": "practice",
                "created_at": "2026-03-01T11:58:00Z",
                "strength": 0.99,
                "extractor_confidence": 0.98,
            }
        ]

    def list_cognitive_user_controls(self, **_kwargs: Any) -> list[dict[str, Any]]:
        return list(self.controls)

    def record_cognitive_user_control(self, **kwargs: Any) -> dict[str, Any]:
        self.control_calls.append(dict(kwargs))
        self.controls.insert(0, dict(kwargs))
        return dict(kwargs)


def _view(*hypotheses: object, usable: bool = True) -> SimpleNamespace:
    return SimpleNamespace(usable=usable, hypotheses=hypotheses)


def test_disabled_ui_returns_empty_without_reading_or_writing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = _load_entries(monkeypatch)
    tracker = _Tracker(_view(_hypothesis()))
    store = _Store()

    class Harness(entries._CognitiveEntriesMixin):
        _cfg = _config(enabled=False)
        _knowledge_tracker = tracker
        _store = store

    payload = asyncio.run(Harness().study_cognitive_evidence(topic_id="college_chain_rule"))

    assert payload == {
        "enabled": False,
        "status": "disabled",
        "topic_id": "college_chain_rule",
        "hypotheses": [],
        "restorable_controls": [],
    }
    assert tracker.calls == []
    assert store.evidence_calls == []


def test_read_endpoint_exposes_only_supported_evidence_and_no_scores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = _load_entries(monkeypatch)
    tracker = _Tracker(_view(_hypothesis(), _hypothesis("hypothesized", "confuse_product_and_chain")))
    store = _Store()

    class Harness(entries._CognitiveEntriesMixin):
        _cfg = _config()
        _knowledge_tracker = tracker
        _store = store

    payload = asyncio.run(Harness().study_cognitive_evidence(topic_id="college_chain_rule"))

    assert payload["status"] == "ready"
    assert [item["hypothesis_code"] for item in payload["hypotheses"]] == ["omit_inner_derivative"]
    assert payload["hypotheses"][0]["support_count"] == 2
    assert payload["hypotheses"][0]["evidence"] == [
        {
            "evidence_id": "evidence-1",
            "direction": "support",
            "evidence_span": "cos(x²) is present, but 2x is missing",
            "source_kind": "practice",
            "created_at": "2026-03-01T11:58:00Z",
        }
    ]
    encoded = json.dumps(payload)
    assert "probability" not in encoded
    assert "confidence" not in encoded
    assert "strength" not in encoded


def test_read_endpoint_fails_closed_when_state_reader_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = _load_entries(monkeypatch)

    class Harness(entries._CognitiveEntriesMixin):
        _cfg = _config()
        _knowledge_tracker = _Tracker(RuntimeError("projection unavailable"))
        _store = _Store()

    payload = asyncio.run(Harness().study_cognitive_evidence(topic_id="college_chain_rule"))

    assert payload["enabled"] is True
    assert payload["status"] == "unavailable"
    assert payload["hypotheses"] == []


def test_suppress_requires_supported_state_and_writes_bounded_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = _load_entries(monkeypatch)
    store = _Store()

    class Harness(entries._CognitiveEntriesMixin):
        _cfg = _config()
        _knowledge_tracker = _Tracker(_view(_hypothesis()))
        _store = store

    expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    payload = asyncio.run(
        Harness().study_cognitive_control(
            topic_id="college_chain_rule",
            hypothesis_code="omit_inner_derivative",
            action="suppress",
            expires_at=expires,
        )
    )

    assert payload["status"] == "updated"
    assert store.control_calls[0]["action"] == "suppress"
    assert store.control_calls[0]["expires_at"].endswith("Z")


def test_non_restore_control_rejects_non_supported_hypothesis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = _load_entries(monkeypatch)

    class Harness(entries._CognitiveEntriesMixin):
        _cfg = _config()
        _knowledge_tracker = _Tracker(_view(_hypothesis("hypothesized")))
        _store = _Store()

    with pytest.raises(ValueError, match="current supported"):
        asyncio.run(
            Harness().study_cognitive_control(
                topic_id="college_chain_rule",
                hypothesis_code="omit_inner_derivative",
                action="dismiss",
            )
        )


def test_control_abandons_bound_intervention_before_it_can_be_exposed_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = _load_entries(monkeypatch)
    tracker = _Tracker(_view(_hypothesis()))
    store = _Store()

    class Harness(entries._CognitiveEntriesMixin):
        _cfg = _config(intent_policy="on")
        _knowledge_tracker = tracker
        _store = store

        def __init__(self) -> None:
            self.bound_intervention = True
            self.abandon_calls: list[dict[str, str]] = []

        async def _abandon_current_cognitive_intervention(self, **kwargs: str) -> dict[str, bool]:
            self.abandon_calls.append(dict(kwargs))
            self.bound_intervention = False
            tracker.view = _view()
            return {"abandoned": True}

    harness = Harness()
    controlled = asyncio.run(
        harness.study_cognitive_control(
            topic_id="college_chain_rule",
            hypothesis_code="omit_inner_derivative",
            action="dismiss",
        )
    )
    visible = asyncio.run(harness.study_cognitive_evidence(topic_id="college_chain_rule"))

    assert controlled["abandoned_current_intervention"] is True
    assert harness.bound_intervention is False
    assert harness.abandon_calls == [
        {
            "topic_id": "college_chain_rule",
            "hypothesis_code": "omit_inner_derivative",
            "action": "dismiss",
        }
    ]
    assert visible["hypotheses"] == []
    assert visible["restorable_controls"][0]["action"] == "dismiss"


def test_active_control_fails_closed_without_abandonment_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = _load_entries(monkeypatch)
    store = _Store()

    class Harness(entries._CognitiveEntriesMixin):
        _cfg = _config(intent_policy="on")
        _knowledge_tracker = _Tracker(_view(_hypothesis()))
        _store = store

    with pytest.raises(RuntimeError, match="abandonment support"):
        asyncio.run(
            Harness().study_cognitive_control(
                topic_id="college_chain_rule",
                hypothesis_code="omit_inner_derivative",
                action="delete",
            )
        )
    assert store.control_calls == []


def test_restore_is_available_for_a_hidden_or_deleted_judgment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = _load_entries(monkeypatch)
    store = _Store()

    class Harness(entries._CognitiveEntriesMixin):
        _cfg = _config()
        _knowledge_tracker = _Tracker(_view())
        _store = store

    payload = asyncio.run(
        Harness().study_cognitive_control(
            topic_id="college_chain_rule",
            hypothesis_code="omit_inner_derivative",
            action="restore",
        )
    )

    assert payload["status"] == "updated"
    assert store.control_calls == [
        {
            "topic_id": "college_chain_rule",
            "hypothesis_code": "omit_inner_derivative",
            "action": "restore",
            "reason": "",
            "expires_at": "",
        }
    ]


def test_control_write_requests_projection_without_blocking_on_wake_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = _load_entries(monkeypatch)
    store = _Store()

    class Harness(entries._CognitiveEntriesMixin):
        _cfg = _config()
        _knowledge_tracker = _Tracker(_view())
        _store = store

        def __init__(self) -> None:
            self.wake_calls = 0

        def _request_cognitive_projection(self) -> None:
            self.wake_calls += 1
            raise RuntimeError("injected projection wake failure")

    harness = Harness()
    payload = asyncio.run(
        harness.study_cognitive_control(
            topic_id="college_chain_rule",
            hypothesis_code="omit_inner_derivative",
            action="restore",
        )
    )

    assert payload["status"] == "updated"
    assert harness.wake_calls == 1
    assert store.control_calls[0]["action"] == "restore"


def test_control_fails_closed_while_answer_evaluation_owns_question_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = _load_entries(monkeypatch)
    store = _Store()

    class Harness(entries._CognitiveEntriesMixin):
        _cfg = _config()
        _knowledge_tracker = _Tracker(_view())
        _store = store

    async def scenario() -> None:
        harness = Harness()
        assert (
            await entries.reserve_question_lifecycle(
                harness,
                "answer_evaluation",
            )
            == ""
        )
        with pytest.raises(entries.SdkError) as caught:
            await harness.study_cognitive_control(
                topic_id="college_chain_rule",
                hypothesis_code="omit_inner_derivative",
                action="restore",
            )
        assert caught.value.code == "ANSWER_EVALUATION_IN_PROGRESS"
        assert (
            await entries.reserve_question_lifecycle(
                harness,
                "question_generation",
            )
            == "answer_evaluation"
        )
        await entries.release_question_lifecycle(
            harness,
            "answer_evaluation",
        )

    asyncio.run(scenario())
    assert store.control_calls == []


def test_cancelled_control_releases_its_question_lifecycle_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = _load_entries(monkeypatch)

    class Harness(entries._CognitiveEntriesMixin):
        _cfg = _config(intent_policy="on")
        _knowledge_tracker = _Tracker(_view(_hypothesis()))
        _store = _Store()

        async def _abandon_current_cognitive_intervention(
            self,
            **_kwargs: str,
        ) -> bool:
            raise asyncio.CancelledError

    async def scenario() -> None:
        harness = Harness()
        with pytest.raises(asyncio.CancelledError):
            await harness.study_cognitive_control(
                topic_id="college_chain_rule",
                hypothesis_code="omit_inner_derivative",
                action="delete",
            )
        assert (
            await entries.reserve_question_lifecycle(
                harness,
                "question_generation",
            )
            == ""
        )
        await entries.release_question_lifecycle(
            harness,
            "question_generation",
        )

    asyncio.run(scenario())


def test_suppress_rejects_an_expiry_beyond_one_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = _load_entries(monkeypatch)

    class Harness(entries._CognitiveEntriesMixin):
        _cfg = _config()
        _knowledge_tracker = _Tracker(_view(_hypothesis()))
        _store = _Store()

    expires = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    with pytest.raises(ValueError, match="next 24 hours"):
        asyncio.run(
            Harness().study_cognitive_control(
                topic_id="college_chain_rule",
                hypothesis_code="omit_inner_derivative",
                action="suppress",
                expires_at=expires,
            )
        )


def test_hidden_control_is_returned_without_reexposing_the_judgment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = _load_entries(monkeypatch)
    store = _Store()
    store.controls = [
        {
            "topic_id": "college_chain_rule",
            "hypothesis_code": "omit_inner_derivative",
            "action": "dismiss",
            "created_at": "2026-03-01T12:00:00Z",
        }
    ]

    class Harness(entries._CognitiveEntriesMixin):
        _cfg = _config()
        _knowledge_tracker = _Tracker(_view())
        _store = store

    payload = asyncio.run(Harness().study_cognitive_evidence(topic_id="college_chain_rule"))

    assert payload["hypotheses"] == []
    assert payload["restorable_controls"] == [
        {
            "topic_id": "college_chain_rule",
            "hypothesis_code": "omit_inner_derivative",
            "action": "dismiss",
            "expires_at": "",
        }
    ]


def test_hosted_feedback_ui_is_small_fail_safe_and_fully_localized() -> None:
    surface = (ROOT / "surfaces" / "study_panel.tsx").read_text(encoding="utf-8")
    shared_style = (ROOT / "surfaces" / "study_surface_utils.ts").read_text(encoding="utf-8")
    plugin_config = (ROOT / "plugin.toml").read_text(encoding="utf-8")

    assert "expose_legacy_static_panel = false" in plugin_config
    assert "study_cognitive_evidence" in surface
    assert "study_cognitive_control" in surface
    assert "_abandon_current_cognitive_intervention" in (ROOT / "entry_cognitive_entries.py").read_text(
        encoding="utf-8"
    )
    assert "void loadCognitiveEvidence(" in surface
    assert "COGNITIVE_SUPPRESSION_MS" in surface
    assert "applyCognitiveControl('dismiss')" in surface
    assert "applyCognitiveControl('suppress')" in surface
    assert "applyCognitiveControl('delete')" in surface
    assert "applyCognitiveControl('restore')" in surface
    assert "ui.cognitive.confirm_with_question" in surface
    assert ": () => void generateQuestion()}" in surface
    assert "study_generate_cognitive" not in surface
    assert 'id="study-cognitive-evidence-drawer"' in surface
    assert "cognitiveReadControllerRef.current?.abort()" in surface
    assert "questionAttemptIdRef.current !== attemptKey" in surface
    assert "questionAttemptIdRef.current !== attemptId" in surface
    assert "probability" not in surface[surface.index("type CognitiveEvidenceItem") :]
    assert ".study-panel__cognitive-drawer-panel" in shared_style
    assert "width: min(440px" in shared_style

    required = {
        "entries.cognitive_evidence.name",
        "entries.cognitive_evidence.description",
        "entries.cognitive_control.name",
        "entries.cognitive_control.description",
        "ui.cognitive.label",
        "ui.cognitive.title",
        "ui.cognitive.view_evidence",
        "ui.cognitive.confirm_with_question",
        "ui.cognitive.dismiss",
        "ui.cognitive.suppress",
        "ui.cognitive.delete",
        "ui.cognitive.restore",
        "ui.cognitive.notice",
        "ui.cognitive.hidden_notice",
        "ui.cognitive.summary",
        "ui.cognitive.hidden_summary",
        "ui.cognitive.evidence_support",
        "ui.cognitive.evidence_counter",
        "ui.cognitive.evidence_unavailable",
        "ui.cognitive.delete_confirm",
        "ui.cognitive.saving",
        "ui.cognitive.saved",
        "ui.cognitive.hypothesis.omit_inner_derivative",
        "ui.cognitive.hypothesis.differentiate_inner_incorrectly",
        "ui.cognitive.hypothesis.confuse_product_and_chain",
        "ui.cognitive.hypothesis.unknown",
    }
    locale_paths = sorted((ROOT / "i18n").glob("*.json"))
    assert len(locale_paths) == 8
    for locale_path in locale_paths:
        locale = json.loads(locale_path.read_text(encoding="utf-8"))
        assert not required - locale.keys(), locale_path.name
        assert all(str(locale[key]).strip() for key in required), locale_path.name
