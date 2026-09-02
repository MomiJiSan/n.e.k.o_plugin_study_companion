from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = PLUGIN_ROOT / "static"
FRONTEND_TEST_ROOT = Path(__file__).resolve().parent / "frontend"
LOCALES = ("en", "ja", "ko", "zh-CN", "zh-TW", "ru", "pt", "es")
API_RUNTIME_KEYS = {
    "ui.settings.model_runtime.api_meta",
    "ui.settings.model_runtime.source_neko_free",
    "ui.settings.model_runtime.source_custom",
    "ui.settings.model_runtime.source_neko_managed",
    "ui.settings.model_runtime.source_unknown",
    "ui.settings.model_runtime.provider_neko",
    "ui.settings.model_runtime.provider_qwen",
    "ui.settings.model_runtime.provider_openai",
    "ui.settings.model_runtime.provider_anthropic",
    "ui.settings.model_runtime.provider_openrouter",
    "ui.settings.model_runtime.provider_custom",
    "ui.settings.model_runtime.provider_unknown",
    "ui.settings.model_runtime.endpoint_local_or_private",
}


def _run_frontend_script(script: str, timeout: float = 60.0) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    if not (FRONTEND_TEST_ROOT / "node_modules" / "happy-dom").is_dir():
        pytest.skip("tests/frontend node_modules with happy-dom is not installed")
    completed = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=FRONTEND_TEST_ROOT,
        env={
            **os.environ,
            "STUDY_COMPANION_STATIC_DIR": str(STATIC_ROOT),
        },
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_model_runtime_api_metadata_is_localized_in_every_locale() -> None:
    for locale in LOCALES:
        messages = json.loads((PLUGIN_ROOT / "i18n" / f"{locale}.json").read_text(encoding="utf-8"))
        missing = API_RUNTIME_KEYS - messages.keys()
        assert not missing, f"{locale}: missing {sorted(missing)}"
        assert all(str(messages[key]).strip() for key in API_RUNTIME_KEYS), locale


def test_model_runtime_renders_safe_api_metadata_and_preserves_legacy_payloads() -> None:
    script = r"""
import assert from 'node:assert/strict';
import { Window } from 'happy-dom';
import fs from 'node:fs';
import path from 'node:path';

const staticDir = process.env.STUDY_COMPANION_STATIC_DIR;
const source = fs.readFileSync(path.join(staticDir, 'model-runtime.js'), 'utf8');
const window = new Window({ url: 'http://testserver/plugin/study_companion/ui/' });
const { document } = window;
document.body.innerHTML = `
  <section data-model-runtime="text">
    <strong class="model-runtime__model"></strong>
    <span class="model-runtime__meta"></span>
    <span class="model-runtime__status"></span>
  </section>`;
window.eval(source);

const messages = {
  'ui.settings.model_runtime.source_custom': 'Custom API',
  'ui.settings.model_runtime.source_neko_free': 'N.E.K.O built-in free API',
  'ui.settings.model_runtime.source_unknown': 'Unknown API source',
  'ui.settings.model_runtime.provider_qwen': 'Alibaba Cloud Model Studio',
  'ui.settings.model_runtime.provider_unknown': 'Unknown provider',
  'ui.settings.model_runtime.endpoint_local_or_private': 'Local or private service',
  'ui.settings.model_runtime.ready': 'Ready',
};
const t = (key, fallback) => messages[key] || fallback;
const tf = (key, fallback, values) => Object.entries(values).reduce(
  (text, [name, value]) => text.replaceAll(`{${name}}`, String(value)),
  messages[key] || fallback,
);
const card = document.querySelector('[data-model-runtime]');
const model = card.querySelector('.model-runtime__model');
const meta = card.querySelector('.model-runtime__meta');
const status = card.querySelector('.model-runtime__status');

window.StudyModelRuntime.render([card], {
  text: {
    model: 'qwen3.7-plus-2026-05-26',
    configured: true,
    credential_configured: true,
    transport_supported: true,
    api_source: 'custom',
    provider_code: 'qwen',
    is_free: false,
    endpoint_hint: 'dashscope.aliyuncs.com',
    api_key: 'must-not-render',
    base_url: 'https://secret.example/v1?token=must-not-render',
  },
}, t, tf);
assert.equal(model.textContent, 'qwen3.7-plus-2026-05-26');
assert.equal(meta.textContent, 'Custom API · Alibaba Cloud Model Studio · dashscope.aliyuncs.com');
assert.equal(status.textContent, 'Ready');
assert.equal(status.dataset.ready, 'true');
assert.doesNotMatch(card.textContent, /must-not-render|secret\.example|https:\/\//);

window.StudyModelRuntime.render([card], {
  text: {
    model: 'private-model',
    configured: true,
    credential_configured: true,
    transport_supported: true,
    api_source: 'unknown',
    provider_code: 'unknown',
    endpoint_hint: 'local_or_private',
    base_url: 'http://192.168.1.8:8000/v1',
  },
}, t, tf);
assert.match(meta.textContent, /Local or private service/);
assert.doesNotMatch(card.textContent, /192\.168\.1\.8|8000|\/v1/);

window.StudyModelRuntime.render([card], {
  text: {
    model: 'unsafe-model',
    configured: true,
    credential_configured: true,
    transport_supported: true,
    api_source: 'custom',
    provider_code: 'qwen',
    endpoint_hint: 'dashscope.aliyuncs.com.evil.example',
  },
}, t, tf);
assert.doesNotMatch(meta.textContent, /evil\.example/);

window.StudyModelRuntime.render([card], {
  text: {
    model: 'free-agent-model',
    configured: true,
    credential_configured: true,
    transport_supported: true,
    provider_code: 'unknown',
    is_free: true,
  },
}, t, tf);
assert.match(meta.textContent, /N\.E\.K\.O built-in free API/);

window.StudyModelRuntime.render([card], {
  text: {
    model: 'legacy-model',
    configured: true,
    credential_configured: true,
    transport_supported: true,
    group: 'agent',
    provider_type: 'openai_compatible',
  },
}, t, tf);
assert.equal(meta.textContent, 'Group: agent · Protocol: openai_compatible');
"""
    _run_frontend_script(script)
