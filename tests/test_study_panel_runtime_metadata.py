from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PANEL = ROOT / "surfaces" / "study_panel.tsx"


def _panel_source() -> str:
    return PANEL.read_text(encoding="utf-8")


def test_hosted_runtime_card_supports_safe_api_metadata_and_legacy_payloads() -> None:
    source = _panel_source()

    for field in ("api_source", "provider_code", "is_free", "endpoint_hint"):
        assert f"{field}?:" in source

    for code in ("neko_free", "custom", "neko_managed", "unknown"):
        assert f"{code}:" in source
    assert "ui.settings.model_runtime.source_${sourceCode}" in source

    for code in ("neko", "qwen", "openai", "anthropic", "openrouter", "custom", "unknown"):
        assert f"{code}:" in source
    assert "ui.settings.model_runtime.provider_${providerCode}" in source

    assert "hasApiMetadata" in source
    assert "ui.settings.model_runtime.api_meta" in source
    assert "ui.settings.model_runtime.meta" in source
    assert "modelRuntimeMeta(role, item)" in source


def test_hosted_runtime_endpoint_hint_is_allowlisted_and_localized() -> None:
    source = _panel_source()

    for hostname in (
        "lanlan.tech",
        "www.lanlan.tech",
        "dashscope.aliyuncs.com",
        "dashscope-intl.aliyuncs.com",
        "dashscope-us.aliyuncs.com",
        "api.openai.com",
        "api.anthropic.com",
        "openrouter.ai",
        "api.openrouter.ai",
    ):
        assert f"'{hostname}'" in source

    assert "endpointHint === 'local_or_private'" in source
    assert "ui.settings.model_runtime.endpoint_local_or_private" in source
    assert "SAFE_MODEL_RUNTIME_ENDPOINTS.has(endpointHint)" in source
    assert "parts.push(endpointHint)" in source

    runtime_type = source[source.index("type StudyModelRuntime"):source.index("type StudyMode")]
    assert "base_url" not in runtime_type
    assert "api_key" not in runtime_type
    assert "authorization" not in runtime_type
