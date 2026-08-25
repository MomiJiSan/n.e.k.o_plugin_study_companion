(() => {
  'use strict';

  const asArray = (value) => Array.isArray(value) ? value : [];

  function formatBytes(value) {
    const bytes = Number(value);
    if (!Number.isFinite(bytes) || bytes < 0) return '—';
    if (bytes < 1024) return `${Math.round(bytes)} B`;
    const units = ['KB', 'MB', 'GB', 'TB'];
    let amount = bytes / 1024;
    let index = 0;
    while (amount >= 1024 && index < units.length - 1) {
      amount /= 1024;
      index += 1;
    }
    return `${amount >= 10 ? amount.toFixed(0) : amount.toFixed(1)} ${units[index]}`;
  }

  function create({ root = document, callPlugin, t, onDirectoryUpdated } = {}) {
    const get = (id) => root.getElementById ? root.getElementById(id) : root.querySelector(`#${id}`);
    const directory = get('settingsLocalModelsDirectory');
    const directoryApply = get('settingsLocalModelsDirectoryApply');
    const refresh = get('settingsLocalModelsAssetsRefresh');
    const summary = get('settingsLocalModelsAssetsSummary');
    const disk = get('settingsLocalModelsAssetsDisk');
    const catalog = get('settingsLocalModelsCatalog');
    let latest = { packages: [], installed: [], downloads: [], disk: {}, state: 'stopped' };
    let statusRefreshInFlight = null;
    let statusPollTimer = null;
    const STATUS_POLL_MS = 750;

    const text = (key, fallback) => t ? t(key, fallback) : fallback;
    const packageKey = (item) => `${String(item?.id || '')}@${String(item?.version || '')}`;
    const installedKeys = () => new Set(asArray(latest.installed).map(packageKey));
    const activeFor = (id, version) => asArray(latest.downloads).find((item) =>
      String(item?.id || item?.package_id || '') === id && String(item?.version || '') === version,
    ) || ((latest.last && ['failed', 'canceled'].includes(String(latest.last.state || '').toLowerCase())
      && String(latest.last.id || latest.last.package_id || '') === id
      && String(latest.last.version || '') === version) ? latest.last : null);

    function transferIsActive() {
      const state = String(latest.state || '').toLowerCase();
      return asArray(latest.downloads).length > 0 || [
        'queued', 'checking', 'downloading', 'paused', 'verifying', 'installing', 'cancelling',
      ].includes(state);
    }

    function scheduleStatusRefresh() {
      if (!callPlugin || statusPollTimer !== null || !transferIsActive()) return;
      statusPollTimer = window.setTimeout(() => {
        statusPollTimer = null;
        refreshStatus().catch(() => {});
      }, STATUS_POLL_MS);
    }

    function safeHttpsUrl(value) {
      try {
        const url = new URL(String(value || ''));
        return url.protocol === 'https:' && !url.username && !url.password ? url.href : '';
      } catch (_error) {
        return '';
      }
    }

    function actionButton(label, action, id, version) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'button button-secondary';
      button.textContent = label;
      button.addEventListener('click', async () => {
        button.disabled = true;
        try {
          const response = await callPlugin(`study_local_model_${action}`, { package_id: id, version });
          apply(response?.status || latest);
        } finally {
          button.disabled = false;
        }
      });
      return button;
    }

    function renderCatalog() {
      if (!catalog) return;
      catalog.replaceChildren();
      const packages = asArray(latest.packages);
      if (!packages.length) {
        const empty = document.createElement('p');
        empty.className = 'local-models-assets__empty';
        empty.textContent = text('ui.settings.local_models.catalog.empty_stage', 'The local installer is ready. Model packages arrive in a later C stage.');
        catalog.appendChild(empty);
        return;
      }
      const installed = installedKeys();
      packages.forEach((item) => {
        const id = String(item?.id || '');
        const version = String(item?.version || '');
        if (!id || !version) return;
        const card = document.createElement('article');
        card.className = 'local-model-card';
        const heading = document.createElement('strong');
        heading.textContent = `${id} · ${version}`;
        const meta = document.createElement('span');
        meta.textContent = `${String(item.role || 'model')} · ${formatBytes(item.size_bytes)}`;
        const license = document.createElement('span');
        license.textContent = `${text('ui.settings.local_models.license', 'License')}: ${String(item.license || '—')}`;
        const licenseUrl = safeHttpsUrl(item.license_url);
        if (licenseUrl) {
          const link = document.createElement('a');
          link.href = licenseUrl;
          link.target = '_blank';
          link.rel = 'noopener noreferrer';
          link.textContent = text('ui.settings.local_models.license_link', 'View license');
          license.append(' · ', link);
        }
        const actions = document.createElement('div');
        actions.className = 'local-model-card__actions';
        const isInstalled = installed.has(packageKey(item));
        const active = activeFor(id, version);
        if (active) {
          const progress = document.createElement('span');
          const state = String(active.state || '').toLowerCase();
          const downloaded = formatBytes(active.downloaded_bytes || 0);
          const total = formatBytes(active.total_bytes || item.size_bytes || 0);
          progress.textContent = state === 'failed' || state === 'canceled'
            ? text(`ui.settings.local_models.assets.status.${state}`, state)
            : `${state || text('ui.settings.local_models.assets.status.installing', 'Installing')} · ${downloaded} / ${total}`;
          actions.appendChild(progress);
        }
        if (active) {
          const activeState = String(active.state || '').toLowerCase();
          const pausable = ['queued', 'checking', 'downloading', 'paused'].includes(activeState);
          if (pausable && activeState === 'paused') {
            actions.appendChild(actionButton(text('ui.button.resume_local_model', 'Resume'), 'resume', id, version));
          } else if (pausable) {
            actions.appendChild(actionButton(text('ui.button.pause_local_model', 'Pause'), 'pause', id, version));
          }
          if (asArray(latest.downloads).includes(active) && activeState !== 'cancelling') {
            actions.appendChild(actionButton(text('ui.button.cancel_local_model', 'Cancel'), 'cancel', id, version));
          }
        }
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'button button-secondary';
        button.textContent = isInstalled
          ? text('ui.button.uninstall_local_model', 'Uninstall')
          : text('ui.button.install_local_model', 'Install');
        button.disabled = asArray(latest.downloads).some((download) =>
          String(download?.id || download?.package_id || '') === id
          && String(download?.version || '') === version
        ) || !callPlugin;
        button.addEventListener('click', async () => {
          const action = isInstalled ? 'uninstall' : 'install';
          const directoryMode = String(latest.directory_mode || 'default');
          const confirmation = isInstalled
            ? text('ui.settings.local_models.confirm_uninstall', 'Remove this local model from this device?')
            : text('ui.settings.local_models.confirm_install_details', 'Download and install {model} ({size}) under the {directory} directory? License: {license}.')
              .replace('{model}', `${id} · ${version}`)
              .replace('{size}', formatBytes(item.size_bytes))
              .replace('{directory}', directoryMode)
              .replace('{license}', String(item.license || '—'));
          if (!window.confirm(confirmation)) return;
          let licenseAccepted = false;
          if (!isInstalled && item.requires_license_acceptance === true) {
            licenseAccepted = window.confirm(
              text('ui.settings.local_models.confirm_license', 'I have reviewed and accept the required license for this model.'),
            );
            if (!licenseAccepted) return;
          }
          button.disabled = true;
          try {
            const args = action === 'install'
              ? { package_id: id, version, confirmed: true, license_accepted: licenseAccepted }
              : { package_id: id, version, confirmed: true };
            const response = await callPlugin(`study_local_model_${action}`, args);
            apply(response?.status || latest);
          } finally {
            button.disabled = false;
          }
        });
        actions.appendChild(button);
        card.append(heading, meta, license, actions);
        catalog.appendChild(card);
      });
    }

    function render() {
      const state = String(latest.state || 'stopped').toLowerCase();
      const key = ['checking', 'queued', 'downloading', 'verifying', 'installing', 'cancelling'].includes(state)
        ? 'installing'
        : (state === 'installed' ? 'ready' : (['ready', 'paused', 'unavailable', 'failed', 'canceled'].includes(state) ? state : 'not_started'));
      if (summary) {
        summary.textContent = text(
          `ui.settings.local_models.assets.status.${key}`,
          ({ ready: 'Local model assets are ready.', installing: 'Local model download in progress.', paused: 'Local model download is paused.', unavailable: 'Local model assets are unavailable.', failed: 'Local model operation failed.', canceled: 'Local model download was canceled.', not_started: 'Local model assets are not started.' })[key],
        );
      }
      if (disk) {
        const installedBytes = latest.disk?.installed_bytes ?? latest.installed_bytes;
        const freeBytes = latest.disk?.free_bytes;
        const notices = [];
        const staleCount = Number(latest.disk?.stale_staging_count || 0);
        const manualCount = Number(latest.disk?.manual_or_invalid_package_count || 0);
        if (staleCount > 0) {
          notices.push(text('ui.settings.local_models.assets.stale_staging', '{count} old temporary install(s) need review.')
            .replace('{count}', String(staleCount)));
        }
        if (manualCount > 0) {
          notices.push(text('ui.settings.local_models.assets.manual_review', '{count} unknown or damaged model folder(s) need review.')
            .replace('{count}', String(manualCount)));
        }
        const diskUsage = text('ui.settings.local_models.assets.disk', 'Disk: {used} used · {free} free')
          .replace('{used}', formatBytes(installedBytes || 0))
          .replace('{free}', formatBytes(freeBytes));
        disk.textContent = [diskUsage, ...notices].join(' · ');
      }
      renderCatalog();
    }

    function apply(status = {}, config = {}) {
      latest = { ...latest, ...(status && typeof status === 'object' ? status : {}) };
      if (directory && Object.prototype.hasOwnProperty.call(config || {}, 'local_models_directory')) {
        directory.value = String(config.local_models_directory || '');
      }
      render();
      scheduleStatusRefresh();
    }

    async function refreshStatus() {
      if (statusRefreshInFlight) return statusRefreshInFlight;
      statusRefreshInFlight = (async () => {
        const [status, catalogPayload] = await Promise.all([
          callPlugin('study_local_models_status'),
          callPlugin('study_local_models_catalog'),
        ]);
        apply({ ...status, packages: catalogPayload?.packages || status?.packages || [] });
        return latest;
      })();
      try {
        return await statusRefreshInFlight;
      } finally {
        statusRefreshInFlight = null;
        scheduleStatusRefresh();
      }
    }

    if (refresh) refresh.addEventListener('click', () => refreshStatus().catch(() => {}));
    if (directoryApply) {
      directoryApply.addEventListener('click', async () => {
        directoryApply.disabled = true;
        try {
          const response = await callPlugin('study_local_models_set_directory', {
            directory: String(directory?.value || '').trim(),
          });
          const nextDirectory = String(response?.config?.local_models_directory || '');
          if (directory) directory.value = nextDirectory;
          if (typeof onDirectoryUpdated === 'function') onDirectoryUpdated(nextDirectory);
          apply(response?.status || latest, { local_models_directory: nextDirectory });
        } finally {
          directoryApply.disabled = false;
        }
      });
    }

    return Object.freeze({ apply, refresh: refreshStatus, directory: () => String(directory?.value || '').trim() });
  }

  window.StudyLocalModels = Object.freeze({ create, formatBytes });
})();
