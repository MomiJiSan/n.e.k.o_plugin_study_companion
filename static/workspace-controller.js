(function () {
  'use strict';

  const WORKSPACE_IDS = Object.freeze([
    'overview',
    'study',
    'practice',
    'memory',
    'knowledge',
    'focus',
    'notebook',
  ]);

  function firstElement(doc, selectors) {
    for (const selector of selectors) {
      const element = doc.querySelector(selector);
      if (element) return element;
    }
    return null;
  }

  function resolveElement(value, doc, scope = doc) {
    if (!value) return null;
    if (typeof value === 'function') return resolveElement(value(), doc, scope);
    if (typeof value === 'string') return scope.querySelector(value);
    return value && value.nodeType === 1 ? value : null;
  }

  function defaultRegistry(doc) {
    return {
      overview: {
        kind: 'static',
        element: doc.querySelector('#overviewPanel'),
      },
      study: {
        kind: 'static',
        element: firstElement(doc, ['[data-workspace-panel="study"]', '#explainPanel']),
      },
      practice: {
        kind: 'static',
        element: firstElement(doc, ['[data-workspace-panel="practice"]', '#practicePanel']),
      },
      memory: {
        kind: 'static',
        element: firstElement(doc, ['[data-workspace-panel="memory"]', '#memoryPanel']),
      },
      knowledge: {
        kind: 'knowledge',
        element: doc.querySelector('#knowledgeWorkspacePanel'),
        reactivate: true,
      },
      focus: {
        kind: 'surface',
        surfaceId: 'pomodoro-panel',
        element: doc.querySelector('#focusWorkspaceHost'),
      },
      notebook: {
        kind: 'surface',
        surfaceId: 'notebook-panel',
        element: doc.querySelector('#notebookWorkspaceHost'),
      },
    };
  }

  function makeRegistry(doc, overrides = {}) {
    const defaults = defaultRegistry(doc);
    const registry = {};
    WORKSPACE_IDS.forEach((workspaceId) => {
      registry[workspaceId] = Object.freeze({
        ...defaults[workspaceId],
        ...(overrides[workspaceId] || {}),
      });
    });
    return Object.freeze(registry);
  }

  function isDisabledCard(card) {
    return card.disabled || card.getAttribute('aria-disabled') === 'true';
  }

  function normalizeFocusMode(value) {
    return ['workspace', 'card', 'none'].includes(value) ? value : 'workspace';
  }

  function create(options = {}) {
    const doc = options.document || window.document;
    const registry = makeRegistry(doc, options.registry || {});
    const navigationElement = resolveElement(options.navigationElement, doc)
      || doc.querySelector(options.navigationSelector || '#workspaceNav');
    const cardSelector = options.cardSelector || '[data-workspace-target]';
    const stageElement = resolveElement(options.stageElement, doc)
      || doc.querySelector('#workspaceStage');
    const themeElement = resolveElement(options.themeElement, doc) || stageElement || doc.documentElement;
    const dynamicHost = resolveElement(options.dynamicHost, doc)
      || doc.querySelector('#dynamicWorkspaceHost');
    const errorClassName = options.errorClassName || 'workspace-error-state';

    let cards = [];
    let activeWorkspace = null;
    let destroyed = false;
    let transitionSequence = 0;
    let transitionQueue = Promise.resolve();
    let activeAbortController = null;
    let pendingAbortController = null;
    let managedThemeVariables = [];

    function getEntry(workspaceId) {
      return Object.prototype.hasOwnProperty.call(registry, workspaceId)
        ? registry[workspaceId]
        : null;
    }

    function entryElement(entry) {
      return resolveElement(entry?.element, doc);
    }

    function entryHost(entry) {
      return resolveElement(entry?.host, doc) || entryElement(entry) || dynamicHost;
    }

    function getCards() {
      const scope = navigationElement || doc;
      return Array.from(scope.querySelectorAll(cardSelector));
    }

    function setEntryVisible(entry, visible) {
      const element = entryElement(entry);
      if (!element) return;
      element.hidden = !visible;
      element.setAttribute('aria-hidden', visible ? 'false' : 'true');
    }

    function clearError(entry) {
      const host = resolveElement(entry?.errorHost, doc) || entryHost(entry) || stageElement;
      host?.querySelectorAll?.(`.${errorClassName}`).forEach((node) => node.remove());
    }

    function renderDefaultError(context) {
      const host = resolveElement(context.entry?.errorHost, doc) || entryHost(context.entry) || stageElement;
      if (!host) return;
      clearError(context.entry);
      const errorNode = doc.createElement('div');
      errorNode.className = errorClassName;
      errorNode.setAttribute('role', 'alert');
      errorNode.tabIndex = -1;
      errorNode.textContent = context.error instanceof Error
        ? context.error.message
        : String(context.error || 'Unable to open workspace');
      host.appendChild(errorNode);
    }

    function notify(callback, context) {
      if (typeof callback !== 'function') return;
      try {
        callback(context);
      } catch (error) {
        window.console?.error?.(error);
      }
    }

    function updateCards(workspaceId) {
      let selectedCard = null;
      cards.forEach((card) => {
        const selected = card.getAttribute('data-workspace-target') === workspaceId;
        card.classList.toggle('is-active', selected);
        card.setAttribute('aria-selected', selected ? 'true' : 'false');
        if (selected) {
          selectedCard = card;
          card.setAttribute('aria-current', 'page');
          card.tabIndex = 0;
        } else {
          card.removeAttribute('aria-current');
          card.tabIndex = -1;
        }
      });
      if (!selectedCard) {
        const firstEnabledCard = cards.find((card) => !isDisabledCard(card));
        if (firstEnabledCard) firstEnabledCard.tabIndex = 0;
      }
    }

    function updateTheme(workspaceId, entry) {
      if (!themeElement) return;
      themeElement.dataset.workspace = workspaceId;
      themeElement.dataset.workspaceTheme = String(entry.themeName || workspaceId);
      managedThemeVariables.forEach((name) => themeElement.style.removeProperty(name));
      const variables = entry.theme && typeof entry.theme === 'object' ? entry.theme : {};
      managedThemeVariables = Object.keys(variables).filter((name) => name.startsWith('--'));
      managedThemeVariables.forEach((name) => themeElement.style.setProperty(name, variables[name]));
    }

    function focusElement(element) {
      if (!element || typeof element.focus !== 'function') return false;
      const naturallyFocusable = element.matches?.('a[href], button, input, select, textarea, [tabindex]');
      if (!naturallyFocusable) {
        element.tabIndex = -1;
        element.dataset.workspaceManagedTabindex = 'true';
      }
      element.focus({ preventScroll: true });
      return true;
    }

    function focusWorkspace(workspaceId, entry, focusMode) {
      if (focusMode === 'none') return;
      if (focusMode === 'card') {
        const card = cards.find((candidate) => candidate.getAttribute('data-workspace-target') === workspaceId);
        focusElement(card);
        card?.scrollIntoView?.({ block: 'nearest', inline: 'nearest' });
        return;
      }
      const host = entryElement(entry) || entryHost(entry);
      const explicitTarget = resolveElement(entry.focusTarget, doc, host || doc);
      const target = explicitTarget
        || host?.querySelector?.('[data-workspace-focus], h1, h2, input:not([disabled]), button:not([disabled])')
        || host;
      focusElement(target);
    }

    function validateTarget(workspaceId) {
      const entry = getEntry(workspaceId);
      if (!entry) throw new RangeError(`Unknown workspace: ${workspaceId}`);
      if (!['static', 'surface', 'knowledge'].includes(entry.kind)) {
        throw new TypeError(`Invalid workspace kind for ${workspaceId}: ${entry.kind}`);
      }
      if (entry.kind === 'surface' && !entry.surfaceId) {
        throw new TypeError(`Surface workspace ${workspaceId} is missing surfaceId`);
      }
      return entry;
    }

    function transitionContext(from, to, entry, transitionId, controller, activationOptions) {
      return {
        controller: api,
        from,
        to,
        workspaceId: to,
        entry,
        host: entryHost(entry),
        transitionId,
        signal: controller.signal,
        options: activationOptions,
        isCurrent() {
          return !destroyed && (activeAbortController === controller || pendingAbortController === controller);
        },
      };
    }

    async function allowsLeave(context) {
      const currentEntry = getEntry(context.from);
      if (typeof currentEntry?.canLeave === 'function' && await currentEntry.canLeave(context) === false) {
        return false;
      }
      if (typeof options.canLeave === 'function' && await options.canLeave(context) === false) {
        return false;
      }
      return true;
    }

    async function closeActiveSurface(context) {
      const currentEntry = getEntry(context.from);
      if (!currentEntry || currentEntry.kind !== 'surface') return true;
      const close = currentEntry.closeSurface || options.closeSurface;
      if (typeof close === 'function' && await close(context) === false) return false;
      const unmount = currentEntry.unmountSurface || options.unmountSurface;
      if (typeof unmount === 'function') await unmount(context);
      const host = entryHost(currentEntry);
      if (host) host.replaceChildren();
      return true;
    }

    function mountReturnedNode(result, entry) {
      const node = result?.nodeType ? result : result?.node;
      if (!node?.nodeType) return;
      const host = entryHost(entry);
      if (!host) throw new Error('Dynamic workspace host is unavailable');
      host.replaceChildren(node);
    }

    async function showTarget(context) {
      const { entry } = context;
      setEntryVisible(entry, true);
      clearError(entry);
      if (typeof entry.beforeShow === 'function') await entry.beforeShow(context);

      let result;
      if (entry.kind === 'surface') {
        const mount = entry.mountSurface || options.mountSurface;
        if (typeof mount !== 'function') throw new Error(`No surface renderer for ${entry.surfaceId}`);
        result = await mount(entry.surfaceId, context);
        if (result === false) throw new Error(`Unable to render surface ${entry.surfaceId}`);
        mountReturnedNode(result, entry);
      } else if (entry.kind === 'knowledge') {
        const activateKnowledge = entry.activate || options.activateKnowledge;
        if (typeof activateKnowledge === 'function') {
          result = await activateKnowledge(context);
          if (result === false) throw new Error('Unable to open knowledge workspace');
          mountReturnedNode(result, entry);
        }
      } else if (!entryElement(entry)) {
        throw new Error(`Workspace panel is unavailable: ${context.to}`);
      }

      if (typeof entry.afterShow === 'function') await entry.afterShow({ ...context, result });
      return result;
    }

    function renderTargetError(context) {
      const renderError = context.entry.renderError || options.renderError;
      if (typeof renderError === 'function') {
        try {
          renderError(context);
          return;
        } catch (error) {
          window.console?.error?.(error);
        }
      }
      renderDefaultError(context);
    }

    async function runActivation(workspaceId, activationOptions) {
      if (destroyed) throw new Error('Workspace controller has been destroyed');
      const targetEntry = validateTarget(workspaceId);
      const focusMode = normalizeFocusMode(activationOptions.focus);
      const from = activeWorkspace;
      const shouldReactivate = activationOptions.force === true || targetEntry.reactivate === true;

      if (from === workspaceId && !shouldReactivate) {
        updateCards(workspaceId);
        updateTheme(workspaceId, targetEntry);
        focusWorkspace(workspaceId, targetEntry, focusMode);
        return { ok: true, workspaceId, unchanged: true };
      }

      const transitionId = transitionSequence += 1;
      const nextAbortController = new AbortController();
      pendingAbortController = nextAbortController;
      const context = transitionContext(from, workspaceId, targetEntry, transitionId, nextAbortController, activationOptions);
      notify(options.onTransition, { ...context, phase: 'validate' });

      try {
        if (from && !await allowsLeave(context)) {
          nextAbortController.abort();
          return { ok: false, cancelled: true, workspaceId: from };
        }
        notify(options.onTransition, { ...context, phase: 'leave-guard' });

        if (!await closeActiveSurface(context)) {
          nextAbortController.abort();
          return { ok: false, cancelled: true, workspaceId: from };
        }
        notify(options.onTransition, { ...context, phase: 'close-surface' });

        activeAbortController?.abort();
        if (from) setEntryVisible(getEntry(from), false);
        notify(options.onTransition, { ...context, phase: 'hide-previous' });

        let result;
        let renderError = null;
        try {
          result = await showTarget(context);
        } catch (error) {
          renderError = error;
          setEntryVisible(targetEntry, true);
          renderTargetError({ ...context, error });
        }
        notify(options.onTransition, { ...context, phase: renderError ? 'render-error' : 'show-target' });

        activeWorkspace = workspaceId;
        activeAbortController = nextAbortController;
        updateCards(workspaceId);
        updateTheme(workspaceId, targetEntry);
        if (stageElement) stageElement.dataset.activeWorkspace = workspaceId;
        const changeContext = { ...context, result, error: renderError };
        notify(options.onChange, changeContext);
        if (stageElement && typeof window.CustomEvent === 'function') {
          stageElement.dispatchEvent(new window.CustomEvent('workspacechange', {
            detail: { from, to: workspaceId, error: renderError },
          }));
        }

        const focusHandled = Boolean(result?.focusHandled);
        if (!focusHandled) focusWorkspace(workspaceId, targetEntry, focusMode);
        notify(options.onTransition, { ...changeContext, phase: 'complete' });
        return renderError
          ? { ok: false, workspaceId, error: renderError }
          : { ok: true, workspaceId };
      } finally {
        if (pendingAbortController === nextAbortController) pendingAbortController = null;
        if (activeAbortController !== nextAbortController) nextAbortController.abort();
      }
    }

    function activateWorkspace(workspaceId, activationOptions = {}) {
      const normalizedId = String(workspaceId || '').trim();
      const task = transitionQueue.then(() => runActivation(normalizedId, activationOptions));
      transitionQueue = task.catch(() => undefined);
      return task;
    }

    function handleKeydown(event) {
      const card = event.target?.closest?.(cardSelector);
      if (!card || !cards.includes(card) || isDisabledCard(card)) return false;
      const enabledCards = cards.filter((candidate) => !isDisabledCard(candidate));
      const currentIndex = enabledCards.indexOf(card);
      if (currentIndex < 0) return false;

      let targetIndex = -1;
      if (event.key === 'ArrowRight' || event.key === 'ArrowDown') {
        targetIndex = (currentIndex + 1) % enabledCards.length;
      } else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') {
        targetIndex = (currentIndex - 1 + enabledCards.length) % enabledCards.length;
      } else if (event.key === 'Home') {
        targetIndex = 0;
      } else if (event.key === 'End') {
        targetIndex = enabledCards.length - 1;
      } else {
        return false;
      }

      event.preventDefault();
      const targetCard = enabledCards[targetIndex];
      targetCard.focus();
      activateWorkspace(targetCard.getAttribute('data-workspace-target'), { focus: 'card' })
        .catch((error) => window.console?.error?.(error));
      return true;
    }

    function handleClick(event) {
      const card = event.target?.closest?.(cardSelector);
      if (!card || !cards.includes(card) || isDisabledCard(card)) return false;
      const workspaceId = card.getAttribute('data-workspace-target');
      if (!workspaceId) return false;
      event.preventDefault();
      activateWorkspace(workspaceId, { focus: 'workspace' })
        .catch((error) => window.console?.error?.(error));
      return true;
    }

    function refreshCards() {
      if (navigationElement) navigationElement.removeEventListener('keydown', handleKeydown);
      if (navigationElement) navigationElement.removeEventListener('click', handleClick);
      cards = getCards();
      if (navigationElement) navigationElement.addEventListener('keydown', handleKeydown);
      if (navigationElement) navigationElement.addEventListener('click', handleClick);
      if (activeWorkspace) updateCards(activeWorkspace);
      return cards.slice();
    }

    function preparePanels() {
      const seen = new Set();
      WORKSPACE_IDS.forEach((workspaceId) => {
        const element = entryElement(registry[workspaceId]);
        if (!element || seen.has(element)) return;
        seen.add(element);
        element.hidden = true;
        element.setAttribute('aria-hidden', 'true');
      });
    }

    function destroy() {
      if (destroyed) return;
      destroyed = true;
      navigationElement?.removeEventListener('keydown', handleKeydown);
      navigationElement?.removeEventListener('click', handleClick);
      activeAbortController?.abort();
      pendingAbortController?.abort();
      doc.querySelectorAll('[data-workspace-managed-tabindex="true"]').forEach((element) => {
        element.removeAttribute('tabindex');
        delete element.dataset.workspaceManagedTabindex;
      });
    }

    const api = {
      activateWorkspace,
      destroy,
      getActiveWorkspace() {
        return activeWorkspace;
      },
      getRegistry() {
        return registry;
      },
      handleClick,
      handleKeydown,
      refreshCards,
    };

    preparePanels();
    refreshCards();
    const initialWorkspace = options.initialWorkspace === false
      ? null
      : String(options.initialWorkspace || 'overview');
    const ready = options.autoActivate === false || !initialWorkspace
      ? Promise.resolve({ ok: true, workspaceId: null })
      : activateWorkspace(initialWorkspace, { focus: 'none', initial: true });
    api.ready = ready;
    return Object.freeze(api);
  }

  window.StudyCompanionWorkspaceController = Object.freeze({
    WORKSPACE_IDS,
    create,
    createRegistry(overrides = {}, doc = window.document) {
      return makeRegistry(doc, overrides);
    },
  });
}());
