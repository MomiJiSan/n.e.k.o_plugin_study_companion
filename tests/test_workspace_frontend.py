from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = PLUGIN_ROOT / "static"
FRONTEND_TEST_ROOT = Path(__file__).resolve().parent / "frontend"
LOCALES = ("en", "ja", "ko", "zh-CN", "zh-TW", "ru", "pt", "es")


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


def test_workspace_assets_routes_and_locales_are_complete() -> None:
    index = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
    main = (STATIC_ROOT / "main.js").read_text(encoding="utf-8")
    controller = (STATIC_ROOT / "workspace-controller.js").read_text(encoding="utf-8")

    assert "./workspace.css" in index
    assert "./workspace-controller.js" in index
    assert index.index("./workspace-controller.js") < index.index("./main.js")
    assert "window.StudyCompanionWorkspaceController" in main
    assert "workspaceController = factory.create(" in main
    assert "localStorage" not in controller

    workspace_targets = set(re.findall(r'data-workspace-target="([^"]+)"', index))
    assert {"study", "practice", "memory", "knowledge", "focus", "notebook"} <= workspace_targets
    assert 'id="workspaceNav"' in index

    # These form controls are stateful and must remain the same long-lived DOM nodes.
    for element_id in (
        "studyInput",
        "studyInputImage",
        "practicePanel",
        "answerInput",
        "memoryFrontInput",
        "memoryBackInput",
    ):
        assert index.count(f'id="{element_id}"') == 1, element_id

    workspace_keys = set(re.findall(r'["\'](ui\.workspace\.[a-z0-9_.-]+)["\']', index + controller + main))
    assert workspace_keys
    for locale in LOCALES:
        messages = json.loads((PLUGIN_ROOT / "i18n" / f"{locale}.json").read_text(encoding="utf-8"))
        missing = workspace_keys - messages.keys()
        assert not missing, f"{locale}: missing {sorted(missing)}"
        assert all(str(messages[key]).strip() for key in workspace_keys), locale

    for surface_id, workspace_id in (
        ("pomodoro-panel", "focus"),
        ("notebook-panel", "notebook"),
        ("knowledge-map", "knowledge"),
        ("due-review-panel", "memory"),
        ("habit-dashboard", "focus"),
        ("note-exporter", "notebook"),
        ("session-summary", "notebook"),
    ):
        assert re.search(
            rf"['\"]{re.escape(surface_id)}['\"]:\s*\{{\s*workspaceId:\s*['\"]{workspace_id}['\"]",
            main,
        ), surface_id
    assert "bindButton(button, () => routeSurfaceEntry(button.getAttribute('data-open-surface')" in main

    assert 'data-workspace-link="focus"' in index
    assert 'data-workspace-link="memory" data-workspace-secondary="due-review-panel"' in index
    assert 'data-workspace-link="focus" data-workspace-secondary="habit-dashboard"' in index

    coach_start = main.index("async function handleNekoCoachAction")
    coach_end = main.index("const scannedPdfOcr", coach_start)
    coach_source = main[coach_start:coach_end]
    assert coach_source.index("activateWorkspace('study'") < coach_source.index("runOcr(")
    assert coach_source.index("activateWorkspace('practice'") < coach_source.index("generateQuestion()")
    assert "routeSurfaceEntry('due-review-panel', { source: 'neko-coach' })" in coach_source
    assert "routeSurfaceEntry('session-summary', { source: 'neko-coach' })" in coach_source

    knowledge_start = main.index("function activateKnowledgeWorkspace")
    knowledge_end = main.index("function initializeWorkspaceController", knowledge_start)
    knowledge_activation = main[knowledge_start:knowledge_end]
    assert "mountKnowledgeWorkspaceHost()" in knowledge_activation
    assert "syncKnowledgeMapContent()" in knowledge_activation
    assert "loadKnowledgeMap(requestId)" in knowledge_activation
    assert "openSurfaceDrawer('knowledge-map')" not in knowledge_activation

    escape_start = main.index("if (event.key === 'Escape'")
    escape_source = main[escape_start : escape_start + 180]
    assert "closeSurfaceDrawer()" in escape_source
    assert "activateWorkspace(" not in escape_source


def test_advanced_settings_is_an_accessible_responsive_drawer() -> None:
    index = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
    main = (STATIC_ROOT / "main.js").read_text(encoding="utf-8")
    style = re.sub(r"\s+", " ", (STATIC_ROOT / "style.css").read_text(encoding="utf-8"))
    workspace = re.sub(r"\s+", " ", (STATIC_ROOT / "workspace.css").read_text(encoding="utf-8"))

    assert re.search(
        r'id="advancedSettings"[^>]+class="advanced-settings"[^>]+role="dialog"[^>]+aria-modal="true"',
        index,
    )
    assert 'aria-labelledby="advancedSettingsTitle"' in index
    assert 'id="advancedSettingsCloseBtn"' in index
    assert 'class="settings-drawer__panel"' in index
    assert 'class="settings-drawer__layout"' in index
    assert 'class="settings-drawer__content"' in index
    assert index.count('data-settings-tab=') == 5

    assert ".advanced-settings { position: fixed; inset: 0;" in style
    assert ".settings-drawer__panel { display: grid; grid-template-rows: auto minmax(0, 1fr);" in style
    assert "grid-template-rows: auto minmax(0, 1fr)" in style
    assert "grid-template-columns: repeat(5, minmax(0, 1fr))" in style
    assert ".settings-drawer__content { min-width: 0; overflow: auto;" in style
    assert ".settings-form > .settings-actions { position: sticky;" in style
    assert ".advanced-settings { padding: 0;" in workspace
    assert "width: 100vw; height: 100dvh;" in workspace

    assert "advancedSettings.setAttribute('aria-hidden'" in main
    assert "classList.toggle('settings-drawer-open'" in main
    assert "advancedSettingsCloseBtn.addEventListener('click'" in main
    assert "event.target === advancedSettings" in main
    assert "handleAdvancedSettingsKeydown" in main
    assert "advancedToggleBtn?.focus?.()" in main


def test_knowledge_map_pr2_host_and_stale_response_contracts() -> None:
    index = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
    main = (STATIC_ROOT / "main.js").read_text(encoding="utf-8")
    knowledge = (STATIC_ROOT / "knowledge-map.js").read_text(encoding="utf-8")

    for element_id in (
        "knowledgeWorkspacePanel",
        "knowledgeMapContent",
        "knowledgeMapFullscreenBtn",
        "surfaceDrawerBody",
        "generateQuestionBtn",
    ):
        assert index.count(f'id="{element_id}"') == 1, element_id
    assert 'data-knowledge-map-host' in index
    assert 'data-knowledge-map-fullscreen' in index

    # Rendering and interaction state belong to the knowledge module, never a drawer DOM.
    assert "surfaceDrawer" not in knowledge
    assert "surfaceDrawerBody" not in knowledge
    for method in (
        "replaceContent",
        "setScale",
        "isActive",
        "activatePractice",
    ):
        assert method in knowledge
    assert "window.StudyCompanionKnowledgeMap" in knowledge
    assert "await activatePracticeFromKnowledgeMap()" in knowledge
    assert "closeSurfaceDrawer" not in knowledge
    assert "focusAfterScroll" not in knowledge
    assert "openPracticePanel" not in knowledge

    state_start = main.index("function setKnowledgeMapLoadState")
    state_end = main.index("function createKnowledgeMapHost", state_start)
    state_source = main[state_start:state_end]
    assert "knowledgeMapStatus.textContent" not in state_source
    assert "knowledgeMapStatus.dataset.state = state" in state_source
    assert "knowledgeMapErrorMessage.textContent" in state_source

    close_start = main.index("function closeSurfaceDrawer")
    close_end = main.index("function renderGenericLocalPanel", close_start)
    close_source = main[close_start:close_end]
    assert "options.restoreFocus !== false" in close_source
    assert "knowledgeMapFullscreenBtn?.focus?.()" in close_source

    host_start = main.index("function createKnowledgeMapHost")
    host_end = main.index("function syncKnowledgeMapContent", host_start)
    host_source = main[host_start:host_end]
    assert "activatePracticeWorkspace" not in host_source
    assert "return activateKnowledgePracticeWorkspace()" in host_source

    bootstrap_start = main.index("async function bootstrap()")
    bootstrap_source = main[bootstrap_start:]
    assert "bindButton(knowledgeMapFullscreenBtn, openKnowledgeMapFullscreen)" in bootstrap_source

    load_start = main.index("async function loadKnowledgeMap(requestId)")
    load_end = main.index("async function activateKnowledgePracticeWorkspace", load_start)
    load_source = main[load_start:load_end]
    assert load_source.count("requestId !== mapRequestId") >= 2
    success_guard = load_source.index("requestId !== mapRequestId")
    payload_commit = load_source.index("lastKnowledgeMapPayload = payload")
    payload_render = load_source.index("knowledgeMap.rerender(payload)")
    assert success_guard < payload_commit < payload_render
    assert "!knowledgeMap.isActive()" in load_source
    assert "setKnowledgeMapLoadState('error', formatPluginError(error))" in load_source

    practice_start = main.index("async function activateKnowledgePracticeWorkspace")
    practice_end = main.index("function openKnowledgeMapFullscreen", practice_start)
    practice_source = main[practice_start:practice_end]
    assert practice_source.index("activateWorkspace('practice'") < practice_source.index("generateQuestionBtn?.focus?.()")

    fullscreen_start = main.index("function openKnowledgeMapFullscreen")
    fullscreen_end = main.index("function closeSurfaceDrawer", fullscreen_start)
    fullscreen_source = main[fullscreen_start:fullscreen_end]
    assert "if (knowledgeMapLoadState !== 'ready') syncKnowledgeMapContent()" in fullscreen_source

    leave_start = main.index("function canLeaveWorkspace")
    leave_end = main.index("function closeWorkspaceSurface", leave_start)
    leave_source = main[leave_start:leave_end]
    assert "context.from === 'knowledge'" in leave_source
    assert leave_source.index("knowledgeMapFullscreenOpen()") < leave_source.index("knowledgeWorkspaceActive = false")
    assert leave_source.index("mapRequestId += 1") < leave_source.index("releaseKnowledgeWorkspaceHost?.()")


def test_knowledge_map_loader_ignores_stale_success_failure_and_inactive_host() -> None:
    main = (STATIC_ROOT / "main.js").read_text(encoding="utf-8")
    load_start = main.index("async function loadKnowledgeMap(requestId)")
    load_end = main.index("async function activateKnowledgePracticeWorkspace", load_start)
    load_function = main[load_start:load_end]
    script = r"""
import { Window } from 'happy-dom';

const window = new Window({ url: 'http://testserver/plugin/study_companion/ui/' });
const requests = [];
window.callPlugin = async (entryId, args) => await new Promise((resolve, reject) => {
  requests.push({ entryId, args, resolve, reject });
});
window.formatPluginError = (error) => error?.message === 'plugin_call_timeout'
  ? 'Localized plugin timeout'
  : error?.message || String(error);
const loadFunction = """ + json.dumps(load_function) + r""";
window.eval(`
window.__knowledgeLoadHarness = (() => {
  let mapRequestId = 0;
  let lastKnowledgeMapPayload = null;
  let active = true;
  const renders = [];
  const states = [];
  const knowledgeMap = {
    isActive: () => active,
    rerender: (payload) => renders.push(payload),
  };
  function studyKnowledgeMap() { return knowledgeMap; }
  function setKnowledgeMapLoadState(state, error = '') { states.push({ state, error }); }
  function syncKnowledgeMapContent() { states.push({ state: 'sync' }); }
  ${loadFunction}
  return {
    load: loadKnowledgeMap,
    setRequestId(value) { mapRequestId = value; },
    setActive(value) { active = value; },
    getPayload() { return lastKnowledgeMapPayload; },
    renders,
    states,
  };
})();`);
const harness = window.__knowledgeLoadHarness;

harness.setRequestId(1);
const staleSuccess = harness.load(1);
await Promise.resolve();
harness.setRequestId(2);
const currentSuccess = harness.load(2);
await Promise.resolve();
if (requests.length !== 2) throw new Error('overlapping knowledge requests were not started');
if (requests.some((request) => request.entryId !== 'study_query_knowledge_map'
    || request.args.page_size !== 100 || request.args.include_boundary !== true
    || typeof request.args.scope !== 'object')) {
  throw new Error('knowledge loader did not issue the V2 scoped request first');
}
requests[1].resolve({ nodes: [{ id: 'current' }], edges: [] });
await currentSuccess;
if (harness.getPayload()?.nodes?.[0]?.id !== 'current' || harness.renders.length !== 1) {
  throw new Error('current knowledge response was not rendered');
}
requests[0].resolve({ nodes: [{ id: 'stale' }], edges: [] });
await staleSuccess;
if (harness.getPayload()?.nodes?.[0]?.id !== 'current' || harness.renders.length !== 1) {
  throw new Error('stale knowledge success overwrote the current payload');
}

harness.setRequestId(3);
const staleFailure = harness.load(3);
await Promise.resolve();
harness.setRequestId(4);
requests[2].reject(new Error('stale failure'));
await staleFailure;
if (harness.states.some((state) => state.state === 'error')) {
  throw new Error('stale knowledge failure replaced the current state');
}

harness.setRequestId(5);
harness.setActive(false);
const inactiveSuccess = harness.load(5);
await Promise.resolve();
requests[3].resolve({ nodes: [{ id: 'inactive' }], edges: [] });
await inactiveSuccess;
if (harness.getPayload()?.nodes?.[0]?.id !== 'current' || harness.renders.length !== 1) {
  throw new Error('response for an inactive knowledge host was rendered');
}

harness.setRequestId(6);
harness.setActive(true);
const currentFailure = harness.load(6);
await Promise.resolve();
requests[4].reject(new Error('plugin_call_timeout'));
await new Promise((resolve) => setTimeout(resolve, 0));
if (requests[5]?.entryId !== 'study_knowledge_map' || requests[5]?.args?.limit !== 1000) {
  throw new Error('knowledge loader did not fall back to the V1 map entry');
}
requests[5].reject(new Error('plugin_call_timeout'));
await currentFailure;
const currentError = harness.states.find((state) => state.state === 'error');
if (currentError?.error !== 'Localized plugin timeout') {
  throw new Error(`knowledge failure was not localized: ${JSON.stringify(currentError)}`);
}
if (harness.states.at(-1)?.state !== 'sync') {
  throw new Error('current knowledge failure did not synchronize the error content');
}
"""
    _run_frontend_script(script)


def test_workspace_css_bounds_desktop_tablet_mobile_and_reduced_motion() -> None:
    css = (STATIC_ROOT / "workspace.css").read_text(encoding="utf-8")
    compact = re.sub(r"\s+", " ", css)

    assert "grid-template-columns: minmax(0, 1fr) clamp(340px, 25vw, 440px)" in compact
    assert ".neko-coach { position: sticky" in compact
    assert "html, body { max-width: 100%; overflow-x: clip" in compact
    assert ".workspace-nav { display: flex" in compact
    assert "overflow-x: auto" in compact
    for scale in ("60", "75", "90", "100"):
        assert f'.knowledge-workspace[data-knowledge-scale="{scale}"]' in css
    assert "width: max(var(--knowledge-map-window-width), min(100%, 720px))" in compact
    for breakpoint in ("1199px", "1023px", "767px", "430px"):
        assert breakpoint in css
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert ".workspace-panel { animation: none" in compact


def test_workspace_controller_preserves_dom_guards_dynamic_surfaces_and_keyboard() -> None:
    script = r"""
import { Window } from 'happy-dom';
import fs from 'node:fs';
import path from 'node:path';

const staticDir = process.env.STUDY_COMPANION_STATIC_DIR;
const source = fs.readFileSync(path.join(staticDir, 'workspace-controller.js'), 'utf8');
const window = new Window({ url: 'http://testserver/plugin/study_companion/ui/' });
const { document } = window;
document.body.innerHTML = `
  <nav id="workspaceNav">
    <button type="button" data-workspace-target="overview">Overview</button>
    <button type="button" data-workspace-target="study">Study</button>
    <button type="button" data-workspace-target="practice">Practice</button>
    <button type="button" data-workspace-target="memory">Memory</button>
    <button type="button" data-workspace-target="knowledge">Knowledge</button>
    <button type="button" data-workspace-target="focus">Focus</button>
    <button type="button" data-workspace-target="notebook">Notebook</button>
  </nav>
  <main id="workspaceStage">
    <section id="overviewPanel" data-workspace-panel="overview"><button>Overview action</button></section>
    <section id="explainPanel" data-workspace-panel="study"><textarea id="persistentStudy"></textarea><img id="persistentImage"></section>
    <section id="practicePanel" data-workspace-panel="practice"><textarea id="persistentAnswer"></textarea><article id="persistentQuestion"></article><div id="persistentFeedback"></div></section>
    <section id="memoryPanel" data-workspace-panel="memory"><input id="persistentMemory"></section>
    <section id="knowledgeWorkspacePanel" data-workspace-panel="knowledge"></section>
    <section id="focusWorkspaceHost" data-workspace-panel="focus"></section>
    <section id="notebookWorkspaceHost" data-workspace-panel="notebook"></section>
    <div id="dynamicWorkspaceHost"></div>
  </main>`;
window.eval(source);
async function flushTransitions() {
  for (let index = 0; index < 12; index += 1) await Promise.resolve();
}

const mounted = [];
const transitions = [];
let cancelNotebookClose = false;
let failSurface = '';
const controller = window.StudyCompanionWorkspaceController.create({
  document,
  initialWorkspace: 'overview',
  closeSurface: async ({ from }) => {
    if (from === 'notebook' && cancelNotebookClose) return false;
    return true;
  },
  mountSurface: async (surfaceId, context) => {
    if (surfaceId === failSurface) throw new Error(`failed ${surfaceId}`);
    const node = document.createElement('article');
    node.dataset.surface = surfaceId;
    node.dataset.workspace = context.workspaceId;
    mounted.push(node);
    return node;
  },
  unmountSurface: async () => undefined,
  activateKnowledge: async () => ({ focusHandled: true }),
  renderError: ({ host, error }) => {
    const node = document.createElement('p');
    node.dataset.workspaceError = 'true';
    node.textContent = error.message;
    host?.replaceChildren(node);
  },
  onTransition: (event) => transitions.push(event),
});
await controller.ready;
if (controller.getActiveWorkspace() !== 'overview') {
  throw new Error(`initial workspace was ${controller.getActiveWorkspace()}`);
}

const studyPanel = document.querySelector('[data-workspace-panel="study"]');
const practicePanel = document.querySelector('[data-workspace-panel="practice"]');
const studyInput = document.querySelector('#persistentStudy');
const studyImage = document.querySelector('#persistentImage');
const answerInput = document.querySelector('#persistentAnswer');
const question = document.querySelector('#persistentQuestion');
const feedback = document.querySelector('#persistentFeedback');
studyInput.value = 'Keep this explanation input';
studyImage.src = 'data:image/png;base64,c3R1ZHk=';
answerInput.value = 'Keep this generated answer';
question.textContent = 'Generated question';
feedback.textContent = 'Evaluation feedback';
await controller.activateWorkspace('study', { focus: 'none' });
await controller.activateWorkspace('practice', { focus: 'none' });
await controller.activateWorkspace('memory', { focus: 'none' });
await controller.activateWorkspace('study', { focus: 'none' });
if (document.querySelector('[data-workspace-panel="study"]') !== studyPanel
    || document.querySelector('[data-workspace-panel="practice"]') !== practicePanel) {
  throw new Error('static workspaces were rebuilt during navigation');
}
if (document.querySelector('#persistentStudy') !== studyInput || studyInput.value !== 'Keep this explanation input') {
  throw new Error('study input was replaced or cleared during navigation');
}
if (document.querySelector('#persistentImage') !== studyImage || !studyImage.src.startsWith('data:image/png')) {
  throw new Error('study image was replaced or cleared during navigation');
}
if (document.querySelector('#persistentAnswer') !== answerInput || answerInput.value !== 'Keep this generated answer') {
  throw new Error('practice answer was replaced or cleared during navigation');
}
if (document.querySelector('#persistentQuestion') !== question || question.textContent !== 'Generated question'
    || document.querySelector('#persistentFeedback') !== feedback || feedback.textContent !== 'Evaluation feedback') {
  throw new Error('practice question or feedback was replaced during navigation');
}

await controller.activateWorkspace('focus', { focus: 'none' });
await controller.activateWorkspace('focus', { focus: 'none' });
if (mounted.filter((node) => node.dataset.surface === 'pomodoro-panel').length !== 1) {
  throw new Error('reselecting focus remounted its dynamic surface');
}
await controller.activateWorkspace('notebook', { focus: 'none' });
if (document.querySelectorAll('[data-surface]').length !== 1
    || document.querySelector('[data-surface]')?.dataset.surface !== 'notebook-panel') {
  throw new Error('more than one dynamic surface remained mounted');
}

cancelNotebookClose = true;
const notebookCard = document.querySelector('[data-workspace-target="notebook"]');
const canceled = await controller.activateWorkspace('study', { focus: 'none' });
if (!canceled.cancelled || controller.getActiveWorkspace() !== 'notebook') {
  throw new Error('canceling the notebook close changed the active workspace');
}
if (!notebookCard.classList.contains('is-active') && notebookCard.getAttribute('aria-selected') !== 'true') {
  throw new Error('canceling the notebook close changed the active card');
}
if (document.querySelector('[data-surface="notebook-panel"]') === null) {
  throw new Error('canceling the notebook close unmounted the notebook');
}
cancelNotebookClose = false;
const confirmed = await controller.activateWorkspace('study', { focus: 'none' });
if (!confirmed.ok || controller.getActiveWorkspace() !== 'study') {
  throw new Error('confirming notebook abandonment did not switch workspace');
}

failSurface = 'pomodoro-panel';
await controller.activateWorkspace('focus', { focus: 'none' });
if (controller.getActiveWorkspace() !== 'focus') {
  throw new Error('failed target did not remain the active workspace error state');
}
if (!document.querySelector('#focusWorkspaceHost [data-workspace-error]')) {
  throw new Error('failed dynamic workspace did not render an error state');
}
if (studyInput.value !== 'Keep this explanation input' || answerInput.value !== 'Keep this generated answer') {
  throw new Error('a dynamic workspace failure cleared static workspace state');
}
failSurface = '';

await controller.activateWorkspace('study', { focus: 'card' });
const studyCard = document.querySelector('[data-workspace-target="study"]');
studyCard.focus();
studyCard.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }));
await flushTransitions();
if (controller.getActiveWorkspace() !== 'practice'
    || document.activeElement?.dataset.workspaceTarget !== 'practice') {
  throw new Error('ArrowRight did not switch and focus the next workspace card');
}
document.activeElement.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Home', bubbles: true }));
await flushTransitions();
if (controller.getActiveWorkspace() !== 'overview') {
  throw new Error('Home did not switch to the first workspace card');
}
document.activeElement.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'End', bubbles: true }));
await flushTransitions();
if (controller.getActiveWorkspace() !== 'notebook') {
  throw new Error('End did not switch to the last workspace card');
}
const beforeEscape = controller.getActiveWorkspace();
document.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
await flushTransitions();
if (controller.getActiveWorkspace() !== beforeEscape) {
  throw new Error('Escape exited the central workspace');
}
if (!transitions.length) throw new Error('workspace transitions were not observable');
controller.destroy();
"""
    _run_frontend_script(script)


def test_surface_panels_invalidate_stale_responses_and_refresh_chains() -> None:
    script = r"""
import { Window } from 'happy-dom';
import fs from 'node:fs';
import path from 'node:path';

const staticDir = process.env.STUDY_COMPANION_STATIC_DIR;
const source = fs.readFileSync(path.join(staticDir, 'surface-panels.js'), 'utf8');
const window = new Window({ url: 'http://testserver/plugin/study_companion/ui/' });
const { document } = window;
window.StudyCompanionNotebook = { close: () => true };
window.eval(source);

const deferredReviews = [];
const reviewCtx = {
  t: (_key, fallback) => fallback,
  label: (surfaceId) => surfaceId,
  callPlugin: async (entryId) => {
    if (entryId !== 'study_memory_due_reviews') throw new Error(`Unexpected entry: ${entryId}`);
    return await new Promise((resolve) => deferredReviews.push(resolve));
  },
};
const staleReview = window.StudyCompanionSurfacePanels.render('due-review-panel', reviewCtx);
document.body.appendChild(staleReview);
const currentReview = window.StudyCompanionSurfacePanels.render('due-review-panel', reviewCtx);
document.body.appendChild(currentReview);
deferredReviews[1]({ due_reviews: [{ item_id: 'current', item: { prompt: 'Current review' } }] });
await new Promise((resolve) => setTimeout(resolve, 0));
deferredReviews[0]({ due_reviews: [{ item_id: 'stale', item: { prompt: 'Stale review' } }] });
await new Promise((resolve) => setTimeout(resolve, 0));
if (!currentReview.textContent.includes('Current review')) {
  throw new Error('the active surface did not render its response');
}
if (staleReview.textContent.includes('Stale review')) {
  throw new Error('an invalidated surface rendered a delayed response');
}

let nextTimerId = 0;
const timers = new Map();
window.setTimeout = (callback) => {
  const id = ++nextTimerId;
  timers.set(id, callback);
  return id;
};
window.clearTimeout = (id) => timers.delete(id);
async function flushMicrotasks() {
  for (let index = 0; index < 8; index += 1) await Promise.resolve();
}
let statusCalls = 0;
const pomodoroCtx = {
  t: (_key, fallback) => fallback,
  label: (surfaceId) => surfaceId,
  callPlugin: async (entryId) => {
    if (entryId !== 'study_pomodoro_status') throw new Error(`Unexpected entry: ${entryId}`);
    statusCalls += 1;
    return { state: 'focusing', mode: 'focus', remaining_seconds: 1200, config: { focus_minutes: 25 } };
  },
};

for (let index = 0; index < 5; index += 1) {
  const panel = window.StudyCompanionSurfacePanels.render('pomodoro-panel', pomodoroCtx);
  document.body.appendChild(panel);
  await flushMicrotasks();
  if (window.StudyCompanionSurfacePanels.close() === false) {
    throw new Error('pomodoro surface refused to close');
  }
  panel.remove();
}
const staleCallbacks = [...timers.values()];
timers.clear();
for (const callback of staleCallbacks) callback();
await flushMicrotasks();
if (timers.size !== 0) {
  throw new Error(`invalidated pomodoro surfaces kept ${timers.size} refresh chains`);
}

const activePomodoro = window.StudyCompanionSurfacePanels.render('pomodoro-panel', pomodoroCtx);
document.body.appendChild(activePomodoro);
await flushMicrotasks();
if (timers.size !== 1) {
  throw new Error(`active pomodoro scheduled ${timers.size} refresh tasks instead of one`);
}
const activeCallback = timers.values().next().value;
timers.clear();
activeCallback();
await flushMicrotasks();
if (timers.size !== 1) {
  throw new Error(`active pomodoro continued with ${timers.size} refresh chains instead of one`);
}
if (statusCalls < 6) throw new Error('pomodoro status was not refreshed');
"""
    _run_frontend_script(script)


def test_knowledge_map_hosts_share_state_scale_handlers_and_practice_activation() -> None:
    script = r"""
import { Window } from 'happy-dom';
import fs from 'node:fs';
import path from 'node:path';

const staticDir = process.env.STUDY_COMPANION_STATIC_DIR;
const source = fs.readFileSync(path.join(staticDir, 'knowledge-map.js'), 'utf8');
const window = new Window({ url: 'http://testserver/plugin/study_companion/ui/' });
const { document } = window;
document.body.innerHTML = `
  <div id="practiceScopePath"></div>
  <button id="clearPracticeScopeBtn" type="button"></button>
  <div id="questionContextCard"></div>
  <button id="generateQuestionBtn" type="button">Generate</button>
  <section id="centralHost"></section>
  <section id="fullscreenHost"></section>`;

window.t = (_key, fallback) => fallback;
window.tf = (_key, fallback, values = {}) => fallback.replace(/\{([^}]+)\}/g, (_match, name) => values[name] ?? '');
window.drawerElement = (tag, className = '', text = '') => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== '') node.textContent = String(text);
  return node;
};
window.surfacePanel = (surfaceId, subtitle = '') => {
  const root = window.drawerElement('article', 'study-panel surface-shell');
  root.dataset.surface = surfaceId;
  const header = window.drawerElement('header', 'study-panel__header');
  header.append(window.drawerElement('h1', '', surfaceId), window.drawerElement('span', '', subtitle));
  root.appendChild(header);
  return root;
};
window.appendPanelState = (parent, label, value) => {
  const item = window.drawerElement('div');
  item.append(window.drawerElement('span', '', label), window.drawerElement('strong', '', value));
  parent.appendChild(item);
};
window.countFromSummary = (summary, keys) => {
  for (const key of keys) {
    const value = Number(summary?.[key]);
    if (Number.isFinite(value)) return value;
  }
  return 0;
};
window.normalizeLearningStage = (value) => String(value || '').trim();
window.learningProfile = { stage: 'senior_high' };
window.learningStageLabel = (value = window.learningProfile.stage) => value || 'all';
window.knowledgeStageLabel = (value) => value || 'uncategorized';
window.stageValueFromNode = (node) => String(node.stage || '');
window.masteryLevelForPanel = (node) => node.weak ? 'weak' : 'new';
window.knowledgeEdgePriorityLabel = (value) => String(value || '');
window.knowledgeEdgeContextLabel = (value) => String(value || '');
window.knowledgeQuestionTypeLabel = (value) => String(value || '');
window.knowledgeEdgeReason = () => '';
window.LEARNING_STAGE_OPTIONS = ['primary', 'junior_high', 'senior_high', 'college', 'custom'];
window.KNOWLEDGE_SUBJECT_OPTIONS = ['math', 'physics'];
window.lastKnowledgeMapPayload = null;
window.lastStatusPayload = {};
window.knowledgeMapStage = '';
window.currentPracticeScope = null;
window.currentSelectionContext = null;
window.questionContextCard = document.querySelector('#questionContextCard');
window.generateQuestionBtn = document.querySelector('#generateQuestionBtn');
window.setQuestionContext = (value) => { window.lastQuestionContext = value; };
window.setStatus = (value) => { window.lastStatus = value; };
window.setReply = (value) => { window.lastReply = value; };
window.formatPluginError = (error) => error?.message || String(error);
window.setLearningProfileStage = (stage) => {
  window.learningProfile = { ...window.learningProfile, stage };
  window.StudyCompanionKnowledgeMap?.rerender();
};
const pluginCalls = [];
window.callPlugin = async (entryId, args = {}) => {
  pluginCalls.push({ entryId, args });
  if (entryId !== 'study_set_practice_scope') throw new Error(`Unexpected entry: ${entryId}`);
  return {
    active: true,
    scope: { ...args.scope, scope_key: 'scope-1', scope_revision: 7, display_path: ['Senior high', 'Math'] },
    scope_revision: 7,
  };
};
window.eval(source);

const api = window.StudyCompanionKnowledgeMap;
if (!api) throw new Error('knowledge map host API was not exported');
const requiredApi = ['registerHost', 'render', 'renderLoading', 'replaceContent', 'setScale', 'isActive', 'activatePractice', 'rerender', 'getState'];
for (const name of requiredApi) {
  if (typeof api[name] !== 'function') throw new Error(`knowledge map API is missing ${name}`);
}

const central = document.querySelector('#centralHost');
const fullscreen = document.querySelector('#fullscreenHost');
let activePresentation = 'central';
let practiceActivations = 0;
const scales = { central: [], fullscreen: [] };
function host(name, element) {
  return {
    replaceContent(node) { element.replaceChildren(node); },
    setScale(level) {
      scales[name].push(level);
      element.dataset.windowScale = String(level);
    },
    isActive() { return activePresentation === name; },
    async activatePractice() {
      practiceActivations += 1;
      window.generateQuestionBtn.focus();
      return true;
    },
  };
}
const releaseCentral = api.registerHost(host('central', central));
const payload = {
  summary: { topic_count: 2, edge_count: 0 },
  nodes: [
    { id: 'linear', name: 'Linear equations', stage: 'senior_high', subject: 'math', chapter: 'Algebra', unit: 'Equations' },
    { id: 'motion', name: 'Motion', stage: 'senior_high', subject: 'physics', chapter: 'Mechanics', unit: 'Kinematics' },
  ],
  edges: [],
};
const initialMap = api.render(payload);
api.replaceContent(initialMap);
if (central.firstElementChild !== initialMap || fullscreen.childElementCount !== 0) {
  throw new Error('knowledge map was not embedded in the central host');
}

central.querySelector('[data-stage="primary"]')?.click();
central.querySelector('[data-action="set-default-stage"]')?.click();
if (window.learningProfile.stage !== 'primary' || pluginCalls.length !== 0) {
  throw new Error('setting the graph stage default changed anything besides the local learning profile');
}
central.querySelector('[data-stage="senior_high"]')?.click();
central.querySelector('[data-action="return-default-stage"]')?.click();
if (central.querySelector('[data-stage="primary"]')?.getAttribute('aria-pressed') !== 'true') {
  throw new Error('returning to the default stage did not reset the graph filter');
}
window.setLearningProfileStage('senior_high');

const oversizedPayload = {
  summary: { topic_count: 81, edge_count: 0 },
  nodes: [
    ...Array.from({ length: 80 }, (_value, index) => ({
      id: `biology-${index}`,
      name: `Biology ${index}`,
      stage: 'senior_high',
      subject: 'biology',
      chapter: 'Life science',
      unit: 'Cells',
    })),
    { id: 'physics-after-eighty', name: 'Motion', stage: 'senior_high', subject: 'physics', chapter: 'Mechanics', unit: 'Kinematics' },
  ],
  edges: [],
};
const oversizedMap = api.render(oversizedPayload);
if (!oversizedMap.textContent.toLowerCase().includes('physics / 1')) {
  throw new Error('a subject after the first 80 topics was hidden from the knowledge map');
}
api.render(payload);

central.querySelector('[data-subject="math"]')?.click();
if (api.getState().subject !== 'math') throw new Error('subject state was not stored by the shared knowledge map');
const filteredMap = central.firstElementChild;
filteredMap.querySelector('[data-action="zoom-out"]')?.click();
if (api.getState().zoomLevel !== 90 || central.dataset.windowScale !== '90') {
  throw new Error('central zoom did not update shared scale state');
}

activePresentation = 'fullscreen';
let releaseFullscreen = api.registerHost(host('fullscreen', fullscreen));
const sharedMap = fullscreen.firstElementChild;
if (!sharedMap || central.childElementCount !== 0) {
  throw new Error('full-screen mode did not move the single map node out of the central host');
}
if (api.getState().subject !== 'math' || api.getState().zoomLevel !== 90 || fullscreen.dataset.windowScale !== '90') {
  throw new Error('full-screen mode lost filter or zoom state');
}

for (let index = 0; index < 5; index += 1) {
  activePresentation = 'central';
  releaseFullscreen();
  if (central.firstElementChild !== sharedMap) throw new Error('full-screen close did not restore the shared node');
  activePresentation = 'fullscreen';
  releaseFullscreen = api.registerHost(host('fullscreen', fullscreen));
  if (fullscreen.firstElementChild !== sharedMap) throw new Error('full-screen reopen did not reuse the shared node');
}
if (document.querySelectorAll('[data-surface="knowledge-map"]').length !== 1) {
  throw new Error('repeated presentation switches duplicated knowledge map state or DOM');
}
fullscreen.querySelector('[data-action="zoom-out"]')?.click();
if (api.getState().zoomLevel !== 75) {
  throw new Error(`a single zoom click ran duplicate handlers: ${api.getState().zoomLevel}`);
}

const practiceButton = fullscreen.querySelector('.knowledge-hierarchy-picker .button-primary');
if (!practiceButton || practiceButton.disabled) throw new Error('explicit knowledge scope did not expose practice activation');
practiceButton.click();
for (let index = 0; index < 12; index += 1) await Promise.resolve();
const scopeCall = pluginCalls.find((call) => call.entryId === 'study_set_practice_scope');
if (!scopeCall || scopeCall.args.scope.subject !== 'math' || scopeCall.args.scope.stage !== 'senior_high') {
  throw new Error(`knowledge scope did not reach the existing backend contract: ${JSON.stringify(scopeCall)}`);
}
if (practiceActivations !== 1 || document.activeElement !== window.generateQuestionBtn) {
  throw new Error('saved knowledge scope did not activate practice and focus Generate');
}

activePresentation = 'central';
const finalRelease = releaseFullscreen;
finalRelease();
if (central.firstElementChild !== sharedMap && !central.querySelector('[data-surface="knowledge-map"]')) {
  throw new Error('closing full-screen did not return the shared map to the central host');
}
if (api.getState().subject !== 'math' || api.getState().zoomLevel !== 75) {
  throw new Error('closing full-screen reset shared knowledge state');
}
if (finalRelease() !== false) throw new Error('host release was not idempotent');
releaseCentral();
if (central.childElementCount !== 0 || sharedMap.isConnected) {
  throw new Error('releasing the final inactive host left stale map content attached');
}
"""
    _run_frontend_script(script)
