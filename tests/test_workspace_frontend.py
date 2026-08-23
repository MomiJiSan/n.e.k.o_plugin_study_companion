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
    assert "openSurfaceDrawer('knowledge-map')" in main[knowledge_start:knowledge_end]

    escape_start = main.index("if (event.key === 'Escape'")
    escape_source = main[escape_start : escape_start + 180]
    assert "closeSurfaceDrawer()" in escape_source
    assert "activateWorkspace(" not in escape_source


def test_workspace_css_bounds_desktop_tablet_mobile_and_reduced_motion() -> None:
    css = (STATIC_ROOT / "workspace.css").read_text(encoding="utf-8")
    compact = re.sub(r"\s+", " ", css)

    assert "grid-template-columns: minmax(0, 1fr) clamp(340px, 25vw, 440px)" in compact
    assert ".neko-coach { position: sticky" in compact
    assert "html, body { max-width: 100%; overflow-x: clip" in compact
    assert ".workspace-nav { display: flex" in compact
    assert "overflow-x: auto" in compact
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
