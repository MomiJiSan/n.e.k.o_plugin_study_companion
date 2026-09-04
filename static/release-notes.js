(function initializeReleaseNotesModule(global) {
  'use strict';

  const RELEASE_HIGHLIGHTS = Object.freeze([
    'ui.release_notes.highlight.persistence',
    'ui.release_notes.highlight.cognitive_transfer',
    'ui.release_notes.highlight.workspace',
    'ui.release_notes.highlight.announcement',
  ]);
  const STORAGE_KEY = 'study_companion.release_notes.seen_version';

  function storageFromOptions(options) {
    if (Object.prototype.hasOwnProperty.call(options, 'storage')) {
      return options.storage;
    }
    try {
      return global.localStorage;
    } catch (_error) {
      return null;
    }
  }

  function readSeenVersion(storage) {
    try {
      return String(storage?.getItem(STORAGE_KEY) || '').trim();
    } catch (_error) {
      return '';
    }
  }

  function rememberVersion(storage, version) {
    try {
      storage?.setItem(STORAGE_KEY, version);
    } catch (_error) {
      // Storage may be unavailable in private or restricted webviews.
    }
  }

  function initialize(options = {}) {
    const doc = options.document || global.document;
    const i18n = options.i18n || global.I18n;
    const storage = storageFromOptions(options);
    const dialog = doc?.getElementById('releaseNotesDialog');
    const closeButton = doc?.getElementById('releaseNotesCloseBtn');
    const title = doc?.getElementById('releaseNotesTitle');
    const versionLabel = doc?.getElementById('releaseNotesVersion');
    const list = doc?.getElementById('releaseNotesList');
    const version = String(dialog?.dataset.releaseVersion || '').trim();

    if (!dialog || !closeButton || !title || !versionLabel || !list) {
      return { shown: false, reason: 'missing_elements', version };
    }
    if (!version) {
      return { shown: false, reason: 'missing_version', version };
    }
    if (readSeenVersion(storage) === version) {
      return { shown: false, reason: 'already_seen', version };
    }

    const translate = (key, fallback) => (
      typeof i18n?.t === 'function' ? i18n.t(key, fallback) : fallback
    );
    const format = (key, fallback, values) => {
      if (typeof i18n?.tf === 'function') {
        return i18n.tf(key, fallback, values);
      }
      return fallback.replace(/\{([a-zA-Z0-9_]+)\}/g, (match, name) => (
        Object.prototype.hasOwnProperty.call(values, name) ? String(values[name]) : match
      ));
    };

    title.textContent = format(
      'ui.release_notes.title',
      "What's new in v{version}",
      { version },
    );
    versionLabel.textContent = `v${version}`;
    list.replaceChildren(...RELEASE_HIGHLIGHTS.map((key) => {
      const item = doc.createElement('li');
      item.textContent = translate(key, key);
      return item;
    }));

    const dismiss = () => {
      rememberVersion(storage, version);
      if (typeof dialog.close === 'function') {
        dialog.close();
      } else {
        dialog.removeAttribute('open');
      }
    };

    if (dialog.dataset.releaseNotesBound !== 'true') {
      dialog.dataset.releaseNotesBound = 'true';
      closeButton.addEventListener('click', dismiss);
      dialog.addEventListener('cancel', (event) => {
        event.preventDefault();
        dismiss();
      });
      dialog.addEventListener('click', (event) => {
        if (event.target === dialog) dismiss();
      });
    }

    if (typeof dialog.showModal === 'function') {
      dialog.showModal();
    } else {
      dialog.setAttribute('open', '');
    }
    closeButton.focus?.();
    return { shown: true, reason: 'new_version', version };
  }

  global.StudyReleaseNotes = Object.freeze({
    highlights: RELEASE_HIGHLIGHTS,
    storageKey: STORAGE_KEY,
    initialize,
  });
})(window);
