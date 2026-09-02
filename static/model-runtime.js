(() => {
  'use strict';

  const DIAGNOSTIC_MESSAGES = Object.assign(Object.create(null), {
    timeout: ['ui.error.llm_timeout', 'The model request timed out. Please retry shortly.'],
    rate_limited: ['ui.error.llm_rate_limited', 'The model service is receiving too many requests. Please retry shortly.'],
    authentication_failed: ['ui.error.llm_authentication_failed', 'The configured model credential is invalid. Check it in N.E.K.O model settings.'],
    model_not_supported: ['ui.error.llm_model_not_supported', 'The configured model is unavailable or does not support this request.'],
    model_unavailable: ['ui.error.llm_model_not_supported', 'The configured model is unavailable or does not support this request.'],
    provider_unavailable: ['ui.error.llm_provider_unavailable', 'The model service is temporarily unavailable. Please retry shortly.'],
    unsupported_provider: ['ui.error.llm_unsupported_provider', 'The configured model provider protocol is not supported by Study Companion.'],
    context_limit_exceeded: ['ui.error.llm_context_limit_exceeded', 'The content exceeds the configured model context limit. Shorten it and retry.'],
    vision_not_supported: ['ui.error.llm_vision_not_supported', 'The configured Vision model does not accept image input.'],
    agent_quota_exceeded: ['ui.error.llm_agent_quota_exceeded', 'The free Agent daily quota has been used up. Try again later or configure another Agent model.'],
    invalid_endpoint: ['ui.error.llm_invalid_endpoint', 'The configured model endpoint is invalid or unsupported.'],
    invalid_request: ['ui.error.llm_invalid_request', 'The model service rejected the request as invalid.'],
    invalid_image: ['ui.error.llm_invalid_image', 'The image could not be read. Please use a valid JPEG or PNG image.'],
  });
  const STATUS_FALLBACKS = Object.assign(Object.create(null), {
    not_configured: 'Not configured',
    unsupported: 'This provider protocol is not supported by Study Companion',
    credential_missing: 'Credential is not configured',
    ready: 'Ready',
    configured_vision_unknown: 'Configured; image capability will be confirmed on the first request',
  });
  const API_SOURCE_FALLBACKS = Object.freeze({
    neko_free: 'N.E.K.O built-in free API',
    custom: 'Custom API',
    neko_managed: 'N.E.K.O managed API',
    unknown: 'Unknown API source',
  });
  const PROVIDER_FALLBACKS = Object.freeze({
    neko: 'N.E.K.O',
    qwen: 'Alibaba Cloud Model Studio',
    openai: 'OpenAI',
    anthropic: 'Anthropic',
    openrouter: 'OpenRouter',
    custom: 'Custom provider',
    unknown: 'Unknown provider',
  });
  const SAFE_PUBLIC_ENDPOINTS = new Set([
    'api.anthropic.com',
    'api.openai.com',
    'api.openrouter.ai',
    'dashscope.aliyuncs.com',
    'dashscope-intl.aliyuncs.com',
    'dashscope-us.aliyuncs.com',
    'lanlan.tech',
    'openrouter.ai',
    'www.lanlan.tech',
  ]);

  function formatDiagnostic(diagnostic, t) {
    const [key, fallback] = DIAGNOSTIC_MESSAGES[String(diagnostic || '').trim()]
      || ['ui.error.llm_call_failed', 'The model service request failed. Please retry.'];
    return t(key, fallback);
  }

  function render(cards, runtime, t, tf) {
    cards.forEach((card) => {
      const role = String(card.dataset.modelRuntime || '');
      const item = runtime && typeof runtime[role] === 'object' ? runtime[role] : {};
      const model = card.querySelector('.model-runtime__model');
      const meta = card.querySelector('.model-runtime__meta');
      const status = card.querySelector('.model-runtime__status');
      const configured = item.configured === true;
      const supported = item.transport_supported !== false;
      const group = String(item.group || (role === 'vision' ? 'vision' : 'agent'));
      const protocol = String(item.provider_type || '').trim();
      if (model) model.textContent = String(item.model || t('ui.settings.model_runtime.not_configured', 'Not configured'));
      if (meta) {
        const hasApiMetadata = ['api_source', 'provider_code', 'is_free', 'endpoint_hint']
          .some((key) => Object.prototype.hasOwnProperty.call(item, key));
        if (hasApiMetadata) {
          const sourceCandidate = String(item.api_source || '').trim();
          const sourceCode = Object.prototype.hasOwnProperty.call(API_SOURCE_FALLBACKS, sourceCandidate)
            ? sourceCandidate
            : (item.is_free === true ? 'neko_free' : 'unknown');
          const providerCandidate = String(item.provider_code || '').trim();
          const providerCode = Object.prototype.hasOwnProperty.call(PROVIDER_FALLBACKS, providerCandidate)
            ? providerCandidate
            : 'unknown';
          const source = t(
            `ui.settings.model_runtime.source_${sourceCode}`,
            API_SOURCE_FALLBACKS[sourceCode],
          );
          const provider = t(
            `ui.settings.model_runtime.provider_${providerCode}`,
            PROVIDER_FALLBACKS[providerCode],
          );
          const parts = [tf('ui.settings.model_runtime.api_meta', '{source} · {provider}', { source, provider })];
          const endpointHint = String(item.endpoint_hint || '').trim().toLowerCase();
          if (endpointHint === 'local_or_private') {
            parts.push(t('ui.settings.model_runtime.endpoint_local_or_private', 'Local or private service'));
          } else if (SAFE_PUBLIC_ENDPOINTS.has(endpointHint)) {
            parts.push(endpointHint);
          }
          meta.textContent = parts.join(' · ');
        } else {
          meta.textContent = tf('ui.settings.model_runtime.meta', 'Group: {group} · Protocol: {protocol}', {
            group,
            protocol: protocol || t('ui.settings.model_runtime.protocol_unknown', 'Unknown'),
          });
        }
      }
      if (!status) return;
      let key = 'not_configured';
      if (configured && !supported) key = 'unsupported';
      else if (configured && item.credential_configured !== true) key = 'credential_missing';
      else if (configured) key = role === 'vision' ? 'configured_vision_unknown' : 'ready';
      status.textContent = t(`ui.settings.model_runtime.${key}`, STATUS_FALLBACKS[key] || '');
      status.dataset.ready = configured && supported && item.credential_configured === true ? 'true' : 'false';
    });
  }

  async function refresh(load, t, tf) {
    try {
      const payload = await load('study_get_settings_config');
      render(Array.from(document.querySelectorAll('[data-model-runtime]')), payload.model_runtime || {}, t, tf);
      return true;
    } catch (_error) {
      return false;
    }
  }

  window.StudyModelRuntime = Object.freeze({ formatDiagnostic, refresh, render });
})();
