from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_TEST_ROOT = Path(__file__).resolve().parent / "frontend"


def test_study_companion_notebook_is_integrated_with_static_exporter() -> None:
    plugin_dir = PLUGIN_ROOT
    index_html = (plugin_dir / "static" / "index.html").read_text(encoding="utf-8")
    main_js = (plugin_dir / "static" / "main.js").read_text(encoding="utf-8")
    notebook = (plugin_dir / "static" / "notebook-controller.js").read_text(encoding="utf-8")
    exporter = (plugin_dir / "static" / "surface-panels.js").read_text(encoding="utf-8")

    assert 'data-open-surface="notebook-panel"' in index_html
    assert "./notebook-controller.js" in index_html
    assert "./notebook.css" in index_html
    for entry_id in (
        "study_notebook_list",
        "study_notebook_create",
        "study_notebook_update",
        "study_notebook_delete",
        "study_note_list",
        "study_note_get",
        "study_note_upsert",
        "study_note_delete",
    ):
        assert entry_id in notebook
    assert "selectedNoteIds" in notebook
    assert "ctx.openSurface('note-exporter')" in notebook
    assert "openSurface: openSurfaceDrawer" in main_js
    assert "study_note_ai_expand: 90000" in main_js
    assert "listFromCsv(value)" in notebook
    assert ".split(/[,，]+/)" in notebook
    assert "confirmDiscardDraft()" in notebook
    assert "notebooksRequest" in notebook
    assert "Discard unsaved changes?" in notebook
    assert "activeBeforeClose" in notebook
    assert "captureEditorDraft()" in notebook
    assert "setEditorLocked" in notebook
    assert "consumeExportSelectionIntent" in notebook
    assert "restoreExportSelectionIntent" in notebook
    assert "contentSnapshot" in notebook
    assert "button.disabled = busyCount > 0;" in notebook
    assert "window.StudyCompanionNotebook?.close?.() === false" in exporter
    assert "const drawerBody = renderSurfaceDrawerBody(surfaceId);" in main_js
    assert "if (!drawerBody) return false;" in main_js
    assert "${raw.replace(' ', 'T')}Z" in notebook
    assert "getSelectedNoteIds" in exporter
    assert "note_ids: notebookNoteIds" in exporter
    assert "dataset.notebookSelection" in exporter
    assert "restoreExportSelectionIntent?.();" in main_js


def test_study_companion_selected_notebooks_reach_export_entry() -> None:
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    plugin_dir = PLUGIN_ROOT
    frontend_dir = FRONTEND_TEST_ROOT
    if not (frontend_dir / "node_modules" / "happy-dom").is_dir():
        pytest.skip("tests/frontend node_modules with happy-dom is not installed")

    script = r"""
import { Window } from 'happy-dom';
import fs from 'node:fs';
import path from 'node:path';

const staticDir = process.env.STUDY_COMPANION_STATIC_DIR;
const notebookJs = fs.readFileSync(path.join(staticDir, 'notebook-controller.js'), 'utf8');
const surfacePanelsJs = fs.readFileSync(path.join(staticDir, 'surface-panels.js'), 'utf8');
const window = new Window({ url: 'http://testserver/plugin/study_companion/ui/' });
const { document } = window;
const calls = [];
const notebooks = Array.from({ length: 101 }, (_, index) => ({
  id: `book-${index + 1}`, name: `Book ${index + 1}`,
  note_count: index === 0 ? 201 : 0,
}));
const notes = Array.from({ length: 201 }, (_, index) => ({
  id: `note-${index + 1}`, notebook_id: 'book-1', title: `Note ${index + 1}`,
  content: `Body ${index + 1}`, snippet: `Summary ${index + 1}`,
  topic_ids: ['calculus'], tags: ['exam'], updated_at: '2026-08-20T00:00:00Z',
}));
async function callPlugin(entryId, args = {}) {
  calls.push({ entryId, args });
  if (entryId === 'study_notebook_list') {
    const offset = Number(args.offset || 0);
    const limit = Number(args.limit || 100);
    const page = notebooks.slice(offset, offset + limit);
    const hasMore = offset + page.length < notebooks.length;
    return { notebooks: page, has_more: hasMore, next_offset: hasMore ? offset + page.length : null };
  }
  if (entryId === 'study_note_list') {
    const offset = Number(args.offset || 0);
    const limit = Number(args.limit || 200);
    const page = notes.slice(offset, offset + limit);
    const hasMore = offset + page.length < notes.length;
    return { notes: page, has_more: hasMore, next_offset: hasMore ? offset + page.length : null };
  }
  if (entryId === 'study_note_get') return { note: notes.find((item) => item.id === args.note_id) };
  if (entryId === 'study_get_settings_config') return { config: { doc_export: { enabled: true, xmind_enabled: false } } };
  if (entryId === 'study_export_notes') return { markdown: '# Limits', filename: 'limits.md' };
  throw new Error(`Unexpected entry: ${entryId}`);
}
const ctx = {
  t: (_key, fallback) => fallback,
  tf: (_key, fallback, values) => fallback.replace(/\{([^}]+)\}/g, (_, name) => values[name] ?? ''),
  label: (surfaceId) => surfaceId,
  callPlugin,
  openSurface: (surfaceId) => {
    const opened = window.StudyCompanionSurfacePanels.render(surfaceId, ctx);
    if (opened) document.body.appendChild(opened);
  },
};
window.eval(notebookJs);
window.eval(surfacePanelsJs);
const notebook = window.StudyCompanionNotebook.render('notebook-panel', ctx);
document.body.appendChild(notebook);
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));
if (notebook.querySelectorAll('.notebook-field select option').length !== 103) {
  throw new Error('notebook picker omitted options after the first page');
}
if (!notebook.querySelector('.notebook-field select option[value="book-101"]')) {
  throw new Error('notebook picker did not render the final paginated notebook');
}
const notebookListCalls = calls.filter((call) => call.entryId === 'study_notebook_list');
if (notebookListCalls.length !== 2 || notebookListCalls[0].args.offset !== 0 || notebookListCalls[1].args.offset !== 100) {
  throw new Error(`notebook picker used invalid page offsets: ${JSON.stringify(notebookListCalls)}`);
}
if (notebook.querySelectorAll('.notebook-note-row').length !== 200) {
  throw new Error('notebook did not render the first 200-note page');
}
const loadMoreButton = notebook.querySelector('.notebook-list__load-more');
if (!loadMoreButton) throw new Error('notebook did not offer the next note page');
loadMoreButton.click();
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));
if (notebook.querySelectorAll('.notebook-note-row').length !== 201) {
  throw new Error('notebook omitted notes after loading the next page');
}
if (notebook.querySelector('.notebook-list__load-more')) {
  throw new Error('notebook kept a load-more action after the final page');
}
const listCalls = calls.filter((call) => call.entryId === 'study_note_list');
if (listCalls.length !== 2 || listCalls[0].args.offset !== 0 || listCalls[1].args.offset !== 200) {
  throw new Error(`notebook used invalid page offsets: ${JSON.stringify(listCalls)}`);
}
const checkbox = notebook.querySelectorAll('.notebook-note-row__check')[200];
if (!checkbox) throw new Error('notebook final-page checkbox was not rendered');
checkbox.checked = true;
checkbox.dispatchEvent(new window.Event('change', { bubbles: true }));
const exportSelectedButton = [...notebook.querySelectorAll('.notebook-selection__actions button')]
  .find((button) => button.textContent === 'Export selected');
exportSelectedButton.click();
await new Promise((resolve) => setTimeout(resolve, 0));
const exporter = document.querySelector('[data-surface="note-exporter"]');
if (!exporter) throw new Error('notebook export action did not open the exporter');
await new Promise((resolve) => setTimeout(resolve, 0));
if (exporter.dataset.notebookSelection !== 'true') {
  throw new Error('selected-note exporter did not expose its active scope');
}
const selectedStyleSelect = exporter.querySelectorAll('select')[1];
if (!selectedStyleSelect?.disabled || selectedStyleSelect.value !== 'neko') {
  throw new Error('selected-note exporter did not disable the unused style picker');
}
exporter.querySelector('[data-surface-action="export-preview"]').click();
await new Promise((resolve) => setTimeout(resolve, 0));
const exportCall = calls.find((call) => call.entryId === 'study_export_notes');
if (!exportCall || JSON.stringify(exportCall.args.note_ids) !== JSON.stringify(['note-201'])) {
  throw new Error(`selected notes did not reach export: ${JSON.stringify(exportCall)}`);
}
window.StudyCompanionNotebook.restoreExportSelectionIntent();
const rerenderedExporter = window.StudyCompanionSurfacePanels.render('note-exporter', ctx);
document.body.appendChild(rerenderedExporter);
await new Promise((resolve) => setTimeout(resolve, 0));
if (rerenderedExporter.dataset.notebookSelection !== 'true' || !rerenderedExporter.querySelectorAll('select')[1]?.disabled) {
  throw new Error('settings-style rerender did not preserve selected-note scope');
}
rerenderedExporter.querySelector('[data-surface-action="export-preview"]').click();
await new Promise((resolve) => setTimeout(resolve, 0));
const rerenderedExportCall = calls.filter((call) => call.entryId === 'study_export_notes').at(-1);
if (!rerenderedExportCall || JSON.stringify(rerenderedExportCall.args.note_ids) !== JSON.stringify(['note-201'])) {
  throw new Error(`rerendered exporter lost selected notes: ${JSON.stringify(rerenderedExportCall)}`);
}
const standaloneExporter = window.StudyCompanionSurfacePanels.render('note-exporter', ctx);
document.body.appendChild(standaloneExporter);
await new Promise((resolve) => setTimeout(resolve, 0));
const standaloneStyleSelect = standaloneExporter.querySelectorAll('select')[1];
if (!standaloneStyleSelect || standaloneStyleSelect.disabled) {
  throw new Error('standalone exporter style picker should remain enabled');
}
standaloneExporter.querySelector('[data-surface-action="export-preview"]').click();
await new Promise((resolve) => setTimeout(resolve, 0));
const standaloneExportCall = calls.filter((call) => call.entryId === 'study_export_notes').at(-1);
if (!standaloneExportCall || JSON.stringify(standaloneExportCall.args.note_ids) !== JSON.stringify([])) {
  throw new Error(`standalone exporter reused stale note IDs: ${JSON.stringify(standaloneExportCall)}`);
}
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=frontend_dir,
        env={**os.environ, "STUDY_COMPANION_STATIC_DIR": str(plugin_dir / "static")},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout

def test_study_companion_notebook_ignores_stale_note_detail_responses() -> None:
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    plugin_dir = PLUGIN_ROOT
    frontend_dir = FRONTEND_TEST_ROOT
    if not (frontend_dir / "node_modules" / "happy-dom").is_dir():
        pytest.skip("tests/frontend node_modules with happy-dom is not installed")

    script = r"""
import { Window } from 'happy-dom';
import fs from 'node:fs';
import path from 'node:path';

const staticDir = process.env.STUDY_COMPANION_STATIC_DIR;
const notebookJs = fs.readFileSync(path.join(staticDir, 'notebook-controller.js'), 'utf8');
const window = new Window({ url: 'http://testserver/plugin/study_companion/ui/' });
const { document } = window;
const notes = [
  { id: 'note-1', title: 'First', snippet: 'First summary', content: 'First body', updated_at: '2026-08-20T00:00:00Z' },
  { id: 'note-2', title: 'Second', snippet: 'Second summary', content: 'Second body', updated_at: '2026-08-20T00:00:00Z' },
];
const detailRequests = new Map();
const notebookListRequests = [];
const noteListRequests = [];
let deferNotebookLists = false;
let deferNoteLists = false;
let returnSummariesOnly = false;
let noteListCallCount = 0;
function noteListPayload() {
  if (!returnSummariesOnly) return notes;
  return notes.map(({ content, ...summary }) => summary);
}
async function callPlugin(entryId, args = {}) {
  if (entryId === 'study_notebook_list') {
    if (!deferNotebookLists) return { notebooks: [] };
    return await new Promise((resolve, reject) => notebookListRequests.push({ args, resolve, reject }));
  }
  if (entryId === 'study_note_list') {
    noteListCallCount += 1;
    if (!deferNoteLists) return { notes: noteListPayload() };
    return await new Promise((resolve, reject) => noteListRequests.push({ args, resolve, reject }));
  }
  if (entryId === 'study_note_get') {
    return await new Promise((resolve, reject) => detailRequests.set(args.note_id, { resolve, reject }));
  }
  throw new Error(`Unexpected entry: ${entryId}`);
}
const ctx = {
  t: (_key, fallback) => fallback,
  tf: (_key, fallback, values) => fallback.replace(/\{([^}]+)\}/g, (_, name) => values[name] ?? ''),
  label: (surfaceId) => surfaceId,
  callPlugin,
  openSurface: () => undefined,
};
window.eval(notebookJs);
const notebook = window.StudyCompanionNotebook.render('notebook-panel', ctx);
document.body.appendChild(notebook);
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));

const noteButtons = notebook.querySelectorAll('.notebook-note-row__open');
noteButtons[0].click();
await new Promise((resolve) => setTimeout(resolve, 0));
detailRequests.get('note-1').resolve({ note: notes[0] });
await new Promise((resolve) => setTimeout(resolve, 0));
if (notebook.querySelector('.notebook-editor input')?.value !== 'First') {
  throw new Error('the first note detail did not open');
}

const dirtyContentBeforeOverlap = notebook.querySelector('.notebook-editor__content');
dirtyContentBeforeOverlap.value = 'Dirty draft before overlapping selection';
dirtyContentBeforeOverlap.dispatchEvent(new window.Event('input', { bubbles: true }));
window.confirm = () => true;
notebook.querySelectorAll('.notebook-note-row__open')[1].click();
await new Promise((resolve) => setTimeout(resolve, 0));
const lockedOpenButtons = notebook.querySelectorAll('.notebook-note-row__open');
if (![...lockedOpenButtons].every((button) => button.disabled)) {
  throw new Error('note open buttons stayed enabled during a detail request');
}
const previousFirstDetailRequest = detailRequests.get('note-1');
lockedOpenButtons[0].click();
await new Promise((resolve) => setTimeout(resolve, 0));
if (detailRequests.get('note-1') !== previousFirstDetailRequest) {
  throw new Error('a disabled note open button started an overlapping detail request');
}
detailRequests.get('note-2').reject(new Error('detail failed after dirty draft'));
await new Promise((resolve) => setTimeout(resolve, 0));
if (notebook.querySelector('.notebook-editor__content')?.value !== 'Dirty draft before overlapping selection') {
  throw new Error('failed detail request did not restore the original dirty draft');
}
const restoredUnload = new window.Event('beforeunload', { cancelable: true });
window.dispatchEvent(restoredUnload);
if (!restoredUnload.defaultPrevented) {
  throw new Error('failed detail request lost the restored dirty draft guard');
}
const restoredOpenButtons = notebook.querySelectorAll('.notebook-note-row__open');
if ([...restoredOpenButtons].some((button) => button.disabled)) {
  throw new Error('note open buttons stayed disabled after a detail request failed');
}
window.confirm = () => true;
restoredOpenButtons[1].click();
await new Promise((resolve) => setTimeout(resolve, 0));
detailRequests.get('note-2').resolve({ note: notes[1] });
await new Promise((resolve) => setTimeout(resolve, 0));
if (notebook.querySelector('.notebook-editor input')?.value !== 'Second') {
  throw new Error('the second note detail did not open after the failed navigation');
}

notebook.querySelectorAll('.notebook-note-row__open')[0].click();
const refreshButton = [...notebook.querySelectorAll('.notebook-toolbar__actions button')]
  .find((button) => button.textContent === 'Refresh');
refreshButton.click();
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));
detailRequests.get('note-1').resolve({ note: notes[0] });
await new Promise((resolve) => setTimeout(resolve, 0));
if (notebook.querySelector('.notebook-editor input')?.value !== 'Second') {
  throw new Error('a detail response survived a note-list refresh');
}

const refreshDiscardContent = notebook.querySelector('.notebook-editor__content');
refreshDiscardContent.value = 'Dirty draft before refresh invalidation';
refreshDiscardContent.dispatchEvent(new window.Event('input', { bubbles: true }));
window.confirm = () => true;
notebook.querySelectorAll('.notebook-note-row__open')[0].click();
await new Promise((resolve) => setTimeout(resolve, 0));
returnSummariesOnly = true;
refreshButton.click();
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));
returnSummariesOnly = false;
detailRequests.get('note-1').resolve({ note: { ...notes[0], content: 'Stale after refresh' } });
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));
if (notebook.querySelector('.notebook-editor__content')?.value !== 'Dirty draft before refresh invalidation') {
  throw new Error('refresh-invalidated detail response did not restore the pending dirty draft');
}
const refreshInvalidatedUnload = new window.Event('beforeunload', { cancelable: true });
window.dispatchEvent(refreshInvalidatedUnload);
if (!refreshInvalidatedUnload.defaultPrevented) {
  throw new Error('refresh-invalidated detail response lost the pending dirty draft guard');
}

deferNoteLists = true;
refreshButton.click();
await new Promise((resolve) => setTimeout(resolve, 0));
notebook.querySelectorAll('.notebook-note-row__open')[0].click();
detailRequests.get('note-1').resolve({ note: { ...notes[0], content: 'Latest First body' } });
await new Promise((resolve) => setTimeout(resolve, 0));
noteListRequests.shift().resolve({ notes });
await new Promise((resolve) => setTimeout(resolve, 0));
if (notebook.querySelector('.notebook-editor__content')?.value !== 'Latest First body') {
  throw new Error('an older note-list response replaced the latest note detail');
}

const searchInput = notebook.querySelector('input[type="search"]');
searchInput.value = 'obsolete';
searchInput.dispatchEvent(new window.Event('input', { bubbles: true }));
await new Promise((resolve) => setTimeout(resolve, 300));
searchInput.value = 'current';
searchInput.dispatchEvent(new window.Event('input', { bubbles: true }));
await new Promise((resolve) => setTimeout(resolve, 300));
const obsoleteSearch = noteListRequests.shift();
const currentSearch = noteListRequests.shift();
if (obsoleteSearch.args.search_query !== 'obsolete' || currentSearch.args.search_query !== 'current') {
  throw new Error('overlapping searches were not captured in request order');
}
currentSearch.resolve({ notes: [notes[1]] });
await new Promise((resolve) => setTimeout(resolve, 0));
obsoleteSearch.reject(new Error('stale search failed'));
await new Promise((resolve) => setTimeout(resolve, 0));
if (notebook.querySelector('.study-panel__status-chip')?.textContent.includes('stale search failed')) {
  throw new Error('a superseded search rejection overwrote the current status');
}
if (notebook.querySelector('.notebook-note-row strong')?.textContent !== 'Second') {
  throw new Error('a superseded search rejection replaced the current results');
}

window.confirm = () => true;
notebook.querySelector('.notebook-note-row__open').click();
await new Promise((resolve) => setTimeout(resolve, 0));
detailRequests.get('note-2').resolve({ note: notes[1] });
await new Promise((resolve) => setTimeout(resolve, 0));
const scopeDirtyContent = notebook.querySelector('.notebook-editor__content');
scopeDirtyContent.value = 'Dirty draft before scope change';
scopeDirtyContent.dispatchEvent(new window.Event('input', { bubbles: true }));
notebook.querySelector('.notebook-note-row__open').click();
await new Promise((resolve) => setTimeout(resolve, 0));
searchInput.value = 'scope-miss';
searchInput.dispatchEvent(new window.Event('input', { bubbles: true }));
await new Promise((resolve) => setTimeout(resolve, 300));
const scopeSearch = noteListRequests.shift();
if (scopeSearch.args.search_query !== 'scope-miss') {
  throw new Error('scope-change search was not captured');
}
scopeSearch.resolve({ notes: [] });
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));
detailRequests.get('note-2').resolve({ note: { ...notes[1], content: 'Stale scope body' } });
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));
if (notebook.querySelector('.notebook-editor__content')) {
  throw new Error('stale detail response restored a discarded draft after scope change');
}
const scopeChangedUnload = new window.Event('beforeunload', { cancelable: true });
window.dispatchEvent(scopeChangedUnload);
if (scopeChangedUnload.defaultPrevented) {
  throw new Error('stale detail response restored the dirty guard after scope change');
}

deferNotebookLists = true;
deferNoteLists = false;
const noteListCallsBeforeOverlap = noteListCallCount;
const overlappingNotebook = window.StudyCompanionNotebook.render('notebook-panel', ctx);
document.body.appendChild(overlappingNotebook);
await new Promise((resolve) => setTimeout(resolve, 0));
const staleNotebookRequest = notebookListRequests.shift();
const overlappingRefresh = [...overlappingNotebook.querySelectorAll('.notebook-toolbar__actions button')]
  .find((button) => button.textContent === 'Refresh');
overlappingRefresh.click();
await new Promise((resolve) => setTimeout(resolve, 0));
const currentNotebookRequest = notebookListRequests.shift();
currentNotebookRequest.resolve({ notebooks: [] });
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));
staleNotebookRequest.reject(new Error('stale notebook refresh failed'));
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));
if (overlappingNotebook.querySelector('.study-panel__status-chip')?.textContent.includes('stale notebook refresh failed')) {
  throw new Error('a superseded notebook-list rejection overwrote the current status');
}
if (noteListCallCount !== noteListCallsBeforeOverlap + 1) {
  throw new Error('a superseded notebook refresh continued into an extra note-list request');
}
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=frontend_dir,
        env={**os.environ, "STUDY_COMPANION_STATIC_DIR": str(plugin_dir / "static")},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_study_companion_notebook_keeps_next_name_typed_during_create() -> None:
    if shutil.which("node") is None:
        pytest.skip("node is not installed")

    plugin_dir = PLUGIN_ROOT
    frontend_dir = FRONTEND_TEST_ROOT
    if not (frontend_dir / "node_modules" / "happy-dom").is_dir():
        pytest.skip("tests/frontend node_modules with happy-dom is not installed")

    script = r"""
import { Window } from 'happy-dom';
import fs from 'node:fs';
import path from 'node:path';

const staticDir = process.env.STUDY_COMPANION_STATIC_DIR;
const notebookJs = fs.readFileSync(path.join(staticDir, 'notebook-controller.js'), 'utf8');
const window = new Window({ url: 'http://testserver/plugin/study_companion/ui/' });
const { document } = window;
let notebooks = [];
let createResolve;
let createdNotebookCount = 0;
const calls = [];

function createNotebookPayload(name) {
  const created = { id: `book-new-${createdNotebookCount += 1}`, name, note_count: 0 };
  notebooks = [created, ...notebooks];
  return { notebook: created };
}

async function callPlugin(entryId, args = {}) {
  calls.push({ entryId, args });
  if (entryId === 'study_notebook_list') return { notebooks };
  if (entryId === 'study_note_list') return { notes: [] };
  if (entryId === 'study_notebook_create') {
    return await new Promise((resolve) => {
      createResolve = () => resolve(createNotebookPayload(args.name));
    });
  }
  throw new Error(`Unexpected entry: ${entryId}`);
}

const ctx = {
  t: (_key, fallback) => fallback,
  tf: (_key, fallback, values) => fallback.replace(/\{([^}]+)\}/g, (_, name) => values[name] ?? ''),
  label: (surfaceId) => surfaceId,
  callPlugin,
  openSurface: () => undefined,
};
window.eval(notebookJs);
const notebook = window.StudyCompanionNotebook.render('notebook-panel', ctx);
document.body.appendChild(notebook);
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));

const newNotebookInput = [...notebook.querySelectorAll('.notebook-toolbar input')]
  .find((input) => input.type !== 'search');
const createNotebookButton = [...notebook.querySelectorAll('.notebook-toolbar__actions button')]
  .find((button) => button.textContent === 'Create notebook');
newNotebookInput.value = 'First Book';
createNotebookButton.click();
await new Promise((resolve) => setTimeout(resolve, 0));
newNotebookInput.value = 'Second Book';
createResolve();
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));

const createCall = calls.find((call) => call.entryId === 'study_notebook_create');
if (createCall?.args.name !== 'First Book') {
  throw new Error(`notebook create used the wrong submitted name: ${JSON.stringify(createCall)}`);
}
if (newNotebookInput.value !== 'Second Book') {
  throw new Error(`notebook create cleared a newer typed name: ${newNotebookInput.value}`);
}
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=frontend_dir,
        env={**os.environ, "STUDY_COMPANION_STATIC_DIR": str(plugin_dir / "static")},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_study_companion_notebook_invalidates_pending_lists_after_deletions() -> None:
    if shutil.which("node") is None:
        pytest.skip("node is not installed")

    plugin_dir = PLUGIN_ROOT
    frontend_dir = FRONTEND_TEST_ROOT
    if not (frontend_dir / "node_modules" / "happy-dom").is_dir():
        pytest.skip("tests/frontend node_modules with happy-dom is not installed")

    script = r"""
import { Window } from 'happy-dom';
import fs from 'node:fs';
import path from 'node:path';

const staticDir = process.env.STUDY_COMPANION_STATIC_DIR;
const notebookJs = fs.readFileSync(path.join(staticDir, 'notebook-controller.js'), 'utf8');
const window = new Window({ url: 'http://testserver/plugin/study_companion/ui/' });
const { document } = window;
window.confirm = () => true;

let notebooks = [{ id: 'book-1', name: 'Book', note_count: 1 }];
let notes = [{
  id: 'note-1',
  notebook_id: 'book-1',
  title: 'Deleted note',
  snippet: 'Deleted body',
  content: 'Deleted body',
  topic_ids: [],
  tags: [],
  updated_at: '2026-08-20T00:00:00Z',
}];
let deferNoteLists = false;
let deleteNoteResolve;
let deleteNotebookResolve;
const noteListRequests = [];

async function callPlugin(entryId, args = {}) {
  if (entryId === 'study_notebook_list') return { notebooks };
  if (entryId === 'study_note_list') {
    if (deferNoteLists) {
      return await new Promise((resolve) => noteListRequests.push({ args, resolve }));
    }
    return { notes };
  }
  if (entryId === 'study_note_get') return { note: notes.find((item) => item.id === args.note_id) };
  if (entryId === 'study_note_delete') {
    return await new Promise((resolve) => {
      deleteNoteResolve = () => {
        notes = notes.filter((item) => item.id !== args.note_id);
        resolve({});
      };
    });
  }
  if (entryId === 'study_notebook_delete') {
    return await new Promise((resolve) => {
      deleteNotebookResolve = () => {
        notebooks = notebooks.filter((item) => item.id !== args.notebook_id);
        notes = [];
        resolve({});
      };
    });
  }
  throw new Error(`Unexpected entry: ${entryId}`);
}

const ctx = {
  t: (_key, fallback) => fallback,
  tf: (_key, fallback, values) => fallback.replace(/\{([^}]+)\}/g, (_, name) => values[name] ?? ''),
  label: (surfaceId) => surfaceId,
  callPlugin,
  openSurface: () => undefined,
};

async function waitFor(predicate, label) {
  const deadline = Date.now() + 3000;
  while (Date.now() < deadline) {
    if (predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  throw new Error(`timed out waiting for ${label}`);
}

async function settle() {
  await new Promise((resolve) => setTimeout(resolve, 0));
  await new Promise((resolve) => setTimeout(resolve, 0));
}

window.eval(notebookJs);
const noteDeletePanel = window.StudyCompanionNotebook.render('notebook-panel', ctx);
document.body.appendChild(noteDeletePanel);
await settle();
noteDeletePanel.querySelector('.notebook-note-row__open').click();
await settle();
deferNoteLists = true;
const deleteNoteButton = [...noteDeletePanel.querySelectorAll('.notebook-editor__actions button')]
  .find((button) => button.textContent === 'Delete');
deleteNoteButton.click();
await settle();
const noteDeleteRefresh = [...noteDeletePanel.querySelectorAll('.notebook-toolbar__actions button')]
  .find((button) => button.textContent === 'Refresh');
noteDeleteRefresh.click();
await waitFor(() => noteListRequests.length === 1, 'stale note-delete refresh list');
deleteNoteResolve();
await waitFor(() => noteListRequests.length === 2, 'post-delete refresh list');
noteListRequests[0].resolve({ notes: [{
  id: 'note-1',
  notebook_id: 'book-1',
  title: 'Stale deleted note',
  snippet: 'Stale body',
  content: 'Stale body',
  topic_ids: [],
  tags: [],
  updated_at: '2026-08-20T00:00:00Z',
}] });
await settle();
if (noteDeletePanel.querySelector('.notebook-note-row')) {
  throw new Error('stale note list reinserted a locally deleted note');
}
noteListRequests[1].resolve({ notes: [] });
await settle();

notebooks = [{ id: 'book-1', name: 'Book', note_count: 1 }];
notes = [{
  id: 'note-1',
  notebook_id: 'book-1',
  title: 'Notebook note',
  snippet: 'Notebook body',
  content: 'Notebook body',
  topic_ids: [],
  tags: [],
  updated_at: '2026-08-20T00:00:00Z',
}];
deferNoteLists = false;
noteListRequests.length = 0;

const notebookDeletePanel = window.StudyCompanionNotebook.render('notebook-panel', ctx);
document.body.appendChild(notebookDeletePanel);
await settle();
const notebookSelect = notebookDeletePanel.querySelector('.notebook-toolbar select');
notebookSelect.value = 'book-1';
notebookSelect.dispatchEvent(new window.Event('change', { bubbles: true }));
await settle();
deferNoteLists = true;
const deleteNotebookButton = [...notebookDeletePanel.querySelectorAll('.notebook-toolbar__actions button')]
  .find((button) => button.textContent === 'Delete notebook');
deleteNotebookButton.click();
await settle();
const notebookDeleteRefresh = [...notebookDeletePanel.querySelectorAll('.notebook-toolbar__actions button')]
  .find((button) => button.textContent === 'Refresh');
notebookDeleteRefresh.click();
await waitFor(() => noteListRequests.length === 1, 'stale notebook-delete refresh list');
deleteNotebookResolve();
await waitFor(() => noteListRequests.length === 2, 'post-notebook-delete refresh list');
noteListRequests[0].resolve({ notes: [{
  id: 'note-1',
  notebook_id: 'book-1',
  title: 'Stale notebook note',
  snippet: 'Stale body',
  content: 'Stale body',
  topic_ids: [],
  tags: [],
  updated_at: '2026-08-20T00:00:00Z',
}] });
await settle();
if (notebookDeletePanel.querySelector('.notebook-note-row')) {
  throw new Error('stale note list repopulated notes after notebook deletion reset');
}
noteListRequests[1].resolve({ notes: [] });
await settle();
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=frontend_dir,
        env={**os.environ, "STUDY_COMPANION_STATIC_DIR": str(plugin_dir / "static")},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_study_companion_notebook_keeps_saved_note_body_after_list_refresh() -> None:
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    plugin_dir = PLUGIN_ROOT
    frontend_dir = FRONTEND_TEST_ROOT
    if not (frontend_dir / "node_modules" / "happy-dom").is_dir():
        pytest.skip("tests/frontend node_modules with happy-dom is not installed")

    script = r"""
import { Window } from 'happy-dom';
import fs from 'node:fs';
import path from 'node:path';

const staticDir = process.env.STUDY_COMPANION_STATIC_DIR;
const notebookJs = fs.readFileSync(path.join(staticDir, 'notebook-controller.js'), 'utf8');
const window = new Window({ url: 'http://testserver/plugin/study_companion/ui/' });
const { document } = window;
let fullNote = {
  id: 'note-1',
  notebook_id: 'book-1',
  title: 'Original',
  content: 'Original body',
  content_plain: 'Original body',
  snippet: 'Original body',
  topic_ids: [],
  tags: [],
  updated_at: '2026-08-20T00:00:00Z',
};
function noteSummary() {
  return {
    id: fullNote.id,
    notebook_id: fullNote.notebook_id,
    title: fullNote.title,
    content: '',
    content_plain: '',
    snippet: fullNote.content.slice(0, 80),
    topic_ids: fullNote.topic_ids,
    tags: fullNote.tags,
    updated_at: fullNote.updated_at,
  };
}
async function callPlugin(entryId, args = {}) {
  if (entryId === 'study_notebook_list') return { notebooks: [{ id: 'book-1', name: 'Book', note_count: 1 }] };
  if (entryId === 'study_note_list') return { notes: [noteSummary()] };
  if (entryId === 'study_note_get') return { note: { ...fullNote } };
  if (entryId === 'study_note_upsert') {
    fullNote = {
      ...fullNote,
      id: args.note_id,
      notebook_id: args.notebook_id,
      title: args.title,
      content: args.content,
      content_plain: args.content,
      snippet: args.content.slice(0, 80),
      topic_ids: args.topic_ids,
      tags: args.tags,
      updated_at: '2026-08-20T00:01:00Z',
    };
    return { note: { ...fullNote } };
  }
  throw new Error(`Unexpected entry: ${entryId}`);
}
const ctx = {
  t: (_key, fallback) => fallback,
  tf: (_key, fallback, values) => fallback.replace(/\{([^}]+)\}/g, (_, name) => values[name] ?? ''),
  label: (surfaceId) => surfaceId,
  callPlugin,
  openSurface: () => undefined,
};
window.eval(notebookJs);
const notebook = window.StudyCompanionNotebook.render('notebook-panel', ctx);
document.body.appendChild(notebook);
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));

notebook.querySelector('.notebook-note-row__open').click();
await new Promise((resolve) => setTimeout(resolve, 0));
const contentInput = notebook.querySelector('.notebook-editor__content');
contentInput.value = 'Saved body';
contentInput.dispatchEvent(new window.Event('input', { bubbles: true }));
const dirtyUnload = new window.Event('beforeunload', { cancelable: true });
window.dispatchEvent(dirtyUnload);
if (!dirtyUnload.defaultPrevented) {
  throw new Error('dirty editor did not register a page-unload guard');
}
const saveButton = [...notebook.querySelectorAll('.notebook-editor__actions button')]
  .find((button) => button.textContent === 'Save');
saveButton.click();
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));

if (notebook.querySelector('.notebook-editor__content')?.value !== 'Saved body') {
  throw new Error('list refresh replaced the saved full note body with a summary row');
}
const savedUnload = new window.Event('beforeunload', { cancelable: true });
window.dispatchEvent(savedUnload);
if (savedUnload.defaultPrevented) {
  throw new Error('saved editor kept the page-unload guard registered');
}
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=frontend_dir,
        env={**os.environ, "STUDY_COMPANION_STATIC_DIR": str(plugin_dir / "static")},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_study_companion_notebook_preserves_tags_and_blocks_concurrent_ai_save() -> None:
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    plugin_dir = PLUGIN_ROOT
    frontend_dir = FRONTEND_TEST_ROOT
    if not (frontend_dir / "node_modules" / "happy-dom").is_dir():
        pytest.skip("tests/frontend node_modules with happy-dom is not installed")

    script = r"""
import { Window } from 'happy-dom';
import fs from 'node:fs';
import path from 'node:path';

const staticDir = process.env.STUDY_COMPANION_STATIC_DIR;
const notebookJs = fs.readFileSync(path.join(staticDir, 'notebook-controller.js'), 'utf8');
const window = new Window({ url: 'http://testserver/plugin/study_companion/ui/' });
const { document } = window;
let expandResolve;
let saveResolve;
let deleteNotebookResolve;
let failNotebookList = false;
let exportOpenCount = 0;
const calls = [];
let note = {
  id: 'note-1',
  notebook_id: 'book-1',
  title: 'Original',
  content: 'Original body',
  content_plain: 'Original body',
  snippet: 'Original body',
  topic_ids: ['machine learning'],
  tags: ['spaced tag'],
  updated_at: '2026-08-20T00:00:00Z',
};
async function callPlugin(entryId, args = {}) {
  calls.push({ entryId, args });
  if (entryId === 'study_notebook_list') {
    if (failNotebookList) {
      failNotebookList = false;
      throw new Error('forced notebook refresh failure');
    }
    return { notebooks: [{ id: 'book-1', name: 'Book', note_count: 1 }] };
  }
  if (entryId === 'study_note_list') return { notes: [note] };
  if (entryId === 'study_note_get') return { note };
  if (entryId === 'study_note_upsert') {
    if (args.content === 'Saving body') {
      return await new Promise((resolve) => { saveResolve = resolve; });
    }
    note = { ...note, ...args, id: args.note_id, updated_at: '2026-08-20T00:01:00Z' };
    return { note };
  }
  if (entryId === 'study_note_ai_expand') {
    return await new Promise((resolve) => { expandResolve = resolve; });
  }
  if (entryId === 'study_notebook_delete') {
    return await new Promise((resolve) => { deleteNotebookResolve = resolve; });
  }
  throw new Error(`Unexpected entry: ${entryId}`);
}
const ctx = {
  t: (_key, fallback) => fallback,
  tf: (_key, fallback, values) => fallback.replace(/\{([^}]+)\}/g, (_, name) => values[name] ?? ''),
  label: (surfaceId) => surfaceId,
  callPlugin,
  openSurface: () => { exportOpenCount += 1; },
};
window.eval(notebookJs);
const notebook = window.StudyCompanionNotebook.render('notebook-panel', ctx);
document.body.appendChild(notebook);
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));

notebook.querySelector('.notebook-note-row__open').click();
await new Promise((resolve) => setTimeout(resolve, 0));
const rowCheckbox = notebook.querySelector('.notebook-note-row__check');
rowCheckbox.checked = true;
rowCheckbox.dispatchEvent(new window.Event('change', { bubbles: true }));
const inputs = notebook.querySelectorAll('.notebook-editor input');
inputs[1].value = 'machine learning, spaced topic';
inputs[1].dispatchEvent(new window.Event('input', { bubbles: true }));
inputs[2].value = 'spaced tag, two words';
inputs[2].dispatchEvent(new window.Event('input', { bubbles: true }));
let buttons = [...notebook.querySelectorAll('.notebook-editor__actions button')];
let saveButton = buttons.find((button) => button.textContent === 'Save');
saveButton.click();
await new Promise((resolve) => setTimeout(resolve, 0));
const saveCall = calls.find((call) => call.entryId === 'study_note_upsert');
if (JSON.stringify(saveCall.args.topic_ids) !== JSON.stringify(['machine learning', 'spaced topic'])) {
  throw new Error(`topics were split on spaces: ${JSON.stringify(saveCall.args.topic_ids)}`);
}
if (JSON.stringify(saveCall.args.tags) !== JSON.stringify(['spaced tag', 'two words'])) {
  throw new Error(`tags were split on spaces: ${JSON.stringify(saveCall.args.tags)}`);
}

const savingContent = notebook.querySelector('.notebook-editor__content');
savingContent.value = 'Saving body';
savingContent.dispatchEvent(new window.Event('input', { bubbles: true }));
buttons = [...notebook.querySelectorAll('.notebook-editor__actions button')];
saveButton = buttons.find((button) => button.textContent === 'Save');
saveButton.click();
await new Promise((resolve) => setTimeout(resolve, 0));
const exportSelectedButtonDuringSave = [...notebook.querySelectorAll('.notebook-selection__actions button')]
  .find((button) => button.textContent === 'Export selected');
if (!exportSelectedButtonDuringSave?.disabled) {
  throw new Error('selected-note export stayed enabled while save was in flight');
}
exportSelectedButtonDuringSave.click();
if (exportOpenCount !== 0) {
  throw new Error('selected-note export opened during an in-flight save');
}
const refreshButton = [...notebook.querySelectorAll('.notebook-toolbar__actions button')]
  .find((button) => button.textContent === 'Refresh');
refreshButton.click();
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));
if (!notebook.querySelector('.notebook-editor__content')?.disabled) {
  throw new Error('redrawn editor fields were editable while save was in flight');
}
saveResolve({ note: { ...note, content: 'Saving body', content_plain: 'Saving body', snippet: 'Saving body' } });
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));

buttons = [...notebook.querySelectorAll('.notebook-editor__actions button')];
saveButton = buttons.find((button) => button.textContent === 'Save');
const expandButton = buttons.find((button) => button.textContent === 'AI expand');
expandButton.click();
await new Promise((resolve) => setTimeout(resolve, 0));
if (!saveButton.disabled || !expandButton.disabled) {
  throw new Error('editor actions stayed enabled while AI expansion was in flight');
}
expandResolve({ content: 'Expanded body' });
await new Promise((resolve) => setTimeout(resolve, 0));
if (notebook.querySelector('.notebook-editor__content')?.value !== 'Expanded body') {
  throw new Error('AI expansion result was not written back to the editor');
}

buttons = [...notebook.querySelectorAll('.notebook-editor__actions button')];
const refreshExpandButton = buttons.find((button) => button.textContent === 'AI expand');
refreshExpandButton.click();
await new Promise((resolve) => setTimeout(resolve, 0));
refreshButton.click();
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));
expandResolve({ content: 'Expanded after refresh' });
await new Promise((resolve) => setTimeout(resolve, 0));
if (notebook.querySelector('.notebook-editor__content')?.value !== 'Expanded after refresh') {
  throw new Error('refresh discarded the AI expansion response for the same note');
}

buttons = [...notebook.querySelectorAll('.notebook-editor__actions button')];
const secondExpandButton = buttons.find((button) => button.textContent === 'AI expand');
const editedContent = notebook.querySelector('.notebook-editor__content');
secondExpandButton.click();
await new Promise((resolve) => setTimeout(resolve, 0));
editedContent.value = 'User edit while AI is pending';
editedContent.dispatchEvent(new window.Event('input', { bubbles: true }));
expandResolve({ content: 'Stale AI body' });
await new Promise((resolve) => setTimeout(resolve, 0));
if (notebook.querySelector('.notebook-editor__content')?.value !== 'User edit while AI is pending') {
  throw new Error('stale AI expansion overwrote a newer editor draft');
}

const latestSaveButton = [...notebook.querySelectorAll('.notebook-editor__actions button')]
  .find((button) => button.textContent === 'Save');
latestSaveButton.click();
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));
const notebookFilter = notebook.querySelector('.notebook-toolbar select');
notebookFilter.value = 'book-1';
notebookFilter.dispatchEvent(new window.Event('change', { bubbles: true }));
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));
window.confirm = () => true;
const toolbarButtons = [...notebook.querySelectorAll('.notebook-toolbar__actions button')];
const deleteNotebookButton = toolbarButtons.find((button) => button.textContent === 'Delete notebook');
const refreshButtonDuringDelete = toolbarButtons.find((button) => button.textContent === 'Refresh');
const newNoteButtonDuringDelete = toolbarButtons.find((button) => button.textContent === 'New note');
const upsertsBeforeDelete = calls.filter((call) => call.entryId === 'study_note_upsert').length;
deleteNotebookButton.click();
await new Promise((resolve) => setTimeout(resolve, 0));
const exportSelectedButtonDuringDelete = [...notebook.querySelectorAll('.notebook-selection__actions button')]
  .find((button) => button.textContent === 'Export selected');
if (!exportSelectedButtonDuringDelete?.disabled) {
  throw new Error('selected-note export stayed enabled while notebook deletion was in flight');
}
exportSelectedButtonDuringDelete.click();
if (exportOpenCount !== 0) {
  throw new Error('selected-note export opened during notebook deletion');
}
for (const label of ['Create notebook', 'Rename', 'Delete notebook', 'New note']) {
  const button = toolbarButtons.find((item) => item.textContent === label);
  if (!button?.disabled) throw new Error(`${label} stayed enabled during notebook deletion`);
}
if (refreshButtonDuringDelete?.disabled) {
  throw new Error('read-only refresh was blocked during notebook deletion');
}
const detailCallsBeforeDelete = calls.filter((call) => call.entryId === 'study_note_get').length;
const openButtonDuringDelete = notebook.querySelector('.notebook-note-row__open');
if (!openButtonDuringDelete?.disabled) {
  throw new Error('existing note open action stayed enabled during notebook deletion');
}
if (!notebookFilter.disabled || !notebook.querySelector('input[type="search"]')?.disabled) {
  throw new Error('notebook filter navigation stayed enabled during notebook deletion');
}
refreshButtonDuringDelete.click();
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));
const redrawnOpenButtonDuringDelete = notebook.querySelector('.notebook-note-row__open');
if (!redrawnOpenButtonDuringDelete?.disabled) {
  throw new Error('redrawn note open action ignored the notebook mutation lock');
}
redrawnOpenButtonDuringDelete.click();
newNoteButtonDuringDelete.click();
await new Promise((resolve) => setTimeout(resolve, 0));
if (calls.filter((call) => call.entryId === 'study_note_get').length !== detailCallsBeforeDelete) {
  throw new Error('note open action ran concurrently with notebook deletion');
}
if (calls.filter((call) => call.entryId === 'study_note_upsert').length !== upsertsBeforeDelete) {
  throw new Error('new note mutation ran concurrently with notebook deletion');
}
failNotebookList = true;
deleteNotebookResolve({});
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));
if (notebook.querySelector('.notebook-editor__actions')) {
  throw new Error('deleted notebook editor remained actionable after refresh failure');
}
if (!notebook.querySelector('.notebook-editor')?.textContent.includes('Select a note to edit')) {
  throw new Error('deleted notebook editor was not cleared before refresh failure');
}
if (notebook.querySelector('.notebook-note-row') || notebook.querySelector('.notebook-list__load-more')) {
  throw new Error('deleted notebook left stale notes or pagination visible after refresh failure');
}
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=frontend_dir,
        env={**os.environ, "STUDY_COMPANION_STATIC_DIR": str(plugin_dir / "static")},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_study_companion_notebook_reports_open_failures_and_confirms_drafts() -> None:
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    plugin_dir = PLUGIN_ROOT
    frontend_dir = FRONTEND_TEST_ROOT
    if not (frontend_dir / "node_modules" / "happy-dom").is_dir():
        pytest.skip("tests/frontend node_modules with happy-dom is not installed")

    script = r"""
import { Window } from 'happy-dom';
import fs from 'node:fs';
import path from 'node:path';

const staticDir = process.env.STUDY_COMPANION_STATIC_DIR;
const notebookJs = fs.readFileSync(path.join(staticDir, 'notebook-controller.js'), 'utf8');
const window = new Window({ url: 'http://testserver/plugin/study_companion/ui/' });
const { document } = window;
let confirmCount = 0;
window.confirm = () => {
  confirmCount += 1;
  return false;
};
const notes = [
  { id: 'note-1', title: 'First', snippet: 'First summary', content: 'First body', updated_at: '2026-08-20T00:00:00Z' },
  { id: 'note-2', title: 'Second', snippet: 'Second summary', content: 'Second body', updated_at: '2026-08-20T00:00:00Z' },
];
async function callPlugin(entryId, args = {}) {
  if (entryId === 'study_notebook_list') return { notebooks: [] };
  if (entryId === 'study_note_list') return { notes };
  if (entryId === 'study_note_get') {
    if (args.note_id === 'note-2') throw new Error('detail failed');
    return { note: notes.find((item) => item.id === args.note_id) };
  }
  throw new Error(`Unexpected entry: ${entryId}`);
}
const ctx = {
  t: (_key, fallback) => fallback,
  tf: (_key, fallback, values) => fallback.replace(/\{([^}]+)\}/g, (_, name) => values[name] ?? ''),
  label: (surfaceId) => surfaceId,
  callPlugin,
  openSurface: () => undefined,
};
window.eval(notebookJs);
const notebook = window.StudyCompanionNotebook.render('notebook-panel', ctx);
document.body.appendChild(notebook);
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));

notebook.querySelectorAll('.notebook-note-row__open')[1].click();
await new Promise((resolve) => setTimeout(resolve, 0));
if (!notebook.querySelector('.study-panel__status-chip')?.textContent.includes('detail failed')) {
  throw new Error('open failure was not reported in the notebook status');
}

notebook.querySelectorAll('.notebook-note-row__open')[0].click();
await new Promise((resolve) => setTimeout(resolve, 0));
const titleInput = notebook.querySelector('.notebook-editor input');
titleInput.value = 'Unsaved title';
titleInput.dispatchEvent(new window.Event('input', { bubbles: true }));
const dirtyUnload = new window.Event('beforeunload', { cancelable: true });
window.dispatchEvent(dirtyUnload);
if (!dirtyUnload.defaultPrevented) {
  throw new Error('dirty editor did not guard page unload');
}
notebook.querySelectorAll('.notebook-note-row__open')[1].click();
await new Promise((resolve) => setTimeout(resolve, 0));
if (confirmCount !== 1) {
  throw new Error(`draft switch did not ask for confirmation: ${confirmCount}`);
}
if (notebook.querySelector('.notebook-editor input')?.value !== 'Unsaved title') {
  throw new Error('draft was discarded after canceling confirmation');
}
window.confirm = () => {
  confirmCount += 1;
  return true;
};
notebook.querySelectorAll('.notebook-note-row__open')[1].click();
await new Promise((resolve) => setTimeout(resolve, 0));
if (!notebook.querySelector('.study-panel__status-chip')?.textContent.includes('detail failed')) {
  throw new Error('confirmed draft switch failure was not reported');
}
if (notebook.querySelector('.notebook-editor input')?.value !== 'Unsaved title') {
  throw new Error('failed draft switch did not restore the previous draft');
}
const failedSwitchUnload = new window.Event('beforeunload', { cancelable: true });
window.dispatchEvent(failedSwitchUnload);
if (!failedSwitchUnload.defaultPrevented) {
  throw new Error('failed draft switch did not restore the page-unload guard');
}
window.confirm = () => {
  confirmCount += 1;
  return false;
};
if (window.StudyCompanionNotebook.close() !== false) {
  throw new Error('dirty notebook close did not respect canceled confirmation');
}
const canceledCloseUnload = new window.Event('beforeunload', { cancelable: true });
window.dispatchEvent(canceledCloseUnload);
if (!canceledCloseUnload.defaultPrevented) {
  throw new Error('canceled notebook close removed the page-unload guard');
}
window.confirm = () => true;
if (window.StudyCompanionNotebook.close() !== true) {
  throw new Error('confirmed notebook close did not complete');
}
const closedUnload = new window.Event('beforeunload', { cancelable: true });
window.dispatchEvent(closedUnload);
if (closedUnload.defaultPrevented) {
  throw new Error('closed notebook kept the page-unload guard registered');
}
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=frontend_dir,
        env={**os.environ, "STUDY_COMPANION_STATIC_DIR": str(plugin_dir / "static")},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_study_companion_notebook_preserves_drafts_across_refresh_and_reopen() -> None:
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    plugin_dir = PLUGIN_ROOT
    frontend_dir = FRONTEND_TEST_ROOT
    if not (frontend_dir / "node_modules" / "happy-dom").is_dir():
        pytest.skip("tests/frontend node_modules with happy-dom is not installed")

    script = r"""
import { Window } from 'happy-dom';
import fs from 'node:fs';
import path from 'node:path';

const staticDir = process.env.STUDY_COMPANION_STATIC_DIR;
const notebookJs = fs.readFileSync(path.join(staticDir, 'notebook-controller.js'), 'utf8');
const surfacePanelsJs = fs.readFileSync(path.join(staticDir, 'surface-panels.js'), 'utf8');
const window = new Window({ url: 'http://testserver/plugin/study_companion/ui/' });
const { document } = window;
let confirmCount = 0;
window.confirm = () => {
  confirmCount += 1;
  return false;
};
const calls = [];
let failureEntry = '';
let deferredDeleteEntry = '';
let deleteReject;
let createdNotebookCount = 0;
let createdNoteCount = 0;
let notebooks = [{ id: 'book-1', name: 'Book', note_count: 2 }];
let notes = [
  { id: 'note-1', notebook_id: 'book-1', title: 'First', snippet: 'First summary', content: 'First body', topic_ids: [], tags: [], updated_at: '2026-08-20T00:00:00Z' },
  { id: 'note-2', notebook_id: 'book-1', title: 'Second', snippet: 'Second summary', content: 'Second body', topic_ids: [], tags: [], updated_at: '2026-08-20T00:00:00Z' },
];
async function callPlugin(entryId, args = {}) {
  calls.push({ entryId, args });
  if (entryId === deferredDeleteEntry) {
    return await new Promise((_resolve, reject) => { deleteReject = reject; });
  }
  if (entryId === failureEntry) {
    failureEntry = '';
    throw new Error(`forced ${entryId} failure`);
  }
  if (entryId === 'study_notebook_list') return { notebooks };
  if (entryId === 'study_note_list') {
    if (args.search_query === 'missing') return { notes: [] };
    return { notes };
  }
  if (entryId === 'study_note_get') return { note: notes.find((item) => item.id === args.note_id) };
  if (entryId === 'study_notebook_create') {
    const created = { id: `book-new-${createdNotebookCount += 1}`, name: args.name, note_count: 0 };
    notebooks = [created, ...notebooks];
    return { notebook: created };
  }
  if (entryId === 'study_note_upsert') {
    const noteId = args.note_id || `note-new-${createdNoteCount += 1}`;
    const previous = notes.find((item) => item.id === noteId) || notes[0];
    const saved = { ...previous, ...args, id: noteId };
    notes = [saved, ...notes.filter((item) => item.id !== noteId)];
    return { note: saved };
  }
  if (entryId === 'study_note_delete') {
    notes = notes.filter((item) => item.id !== args.note_id);
    return {};
  }
  throw new Error(`Unexpected entry: ${entryId}`);
}
const ctx = {
  t: (_key, fallback) => fallback,
  tf: (_key, fallback, values) => fallback.replace(/\{([^}]+)\}/g, (_, name) => values[name] ?? ''),
  label: (surfaceId) => surfaceId,
  callPlugin,
  openSurface: () => undefined,
};
window.eval(notebookJs);
window.eval(surfacePanelsJs);
const notebook = window.StudyCompanionSurfacePanels.render('notebook-panel', ctx);
document.body.appendChild(notebook);
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));

notebook.querySelector('.notebook-note-row__open').click();
await new Promise((resolve) => setTimeout(resolve, 0));
const contentInput = notebook.querySelector('.notebook-editor__content');
contentInput.value = 'Unsaved body';
contentInput.dispatchEvent(new window.Event('input', { bubbles: true }));

const refreshButton = [...notebook.querySelectorAll('.notebook-toolbar__actions button')]
  .find((button) => button.textContent === 'Refresh');
refreshButton.click();
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));
if (notebook.querySelector('.notebook-editor__content')?.value !== 'Unsaved body') {
  throw new Error('refresh discarded the unsaved editor body');
}

const searchInput = notebook.querySelector('input[type="search"]');
searchInput.value = 'missing';
searchInput.dispatchEvent(new window.Event('input', { bubbles: true }));
await new Promise((resolve) => setTimeout(resolve, 300));
await new Promise((resolve) => setTimeout(resolve, 0));
if (notebook.querySelector('.notebook-editor__content')?.value !== 'Unsaved body') {
  throw new Error('search filtering discarded the unsaved editor body');
}
if (confirmCount !== 0) {
  throw new Error(`search/refresh prompted repeatedly for the draft: ${confirmCount}`);
}

const newNotebookInput = [...notebook.querySelectorAll('.notebook-toolbar input')]
  .find((input) => input.type !== 'search');
newNotebookInput.value = 'New Book';
const createNotebookButton = [...notebook.querySelectorAll('.notebook-toolbar__actions button')]
  .find((button) => button.textContent === 'Create notebook');
createNotebookButton.click();
await new Promise((resolve) => setTimeout(resolve, 0));
if (confirmCount !== 1) {
  throw new Error(`notebook creation did not guard the draft: ${confirmCount}`);
}
if (calls.some((call) => call.entryId === 'study_notebook_create')) {
  throw new Error('notebook was created after draft discard was canceled');
}

const reopened = window.StudyCompanionSurfacePanels.render('notebook-panel', ctx);
if (reopened !== false) {
  throw new Error('reopening notebook replaced a dirty draft without confirmation');
}
if (confirmCount !== 2) {
  throw new Error(`reopening notebook did not use the close guard: ${confirmCount}`);
}
if (notebook.querySelector('.notebook-editor__content')?.value !== 'Unsaved body') {
  throw new Error('canceled reopen changed the dirty editor');
}

window.confirm = () => true;
searchInput.value = '';
searchInput.dispatchEvent(new window.Event('input', { bubbles: true }));
await new Promise((resolve) => setTimeout(resolve, 300));
await new Promise((resolve) => setTimeout(resolve, 0));

async function expectCreateFailure(entryId) {
  const draft = `Draft retained after ${entryId}`;
  const editorContent = notebook.querySelector('.notebook-editor__content');
  editorContent.value = draft;
  editorContent.dispatchEvent(new window.Event('input', { bubbles: true }));
  newNotebookInput.value = `Failure ${entryId}`;
  failureEntry = entryId;
  const savesBefore = calls.filter((call) => call.entryId === 'study_note_upsert').length;
  createNotebookButton.click();
  await new Promise((resolve) => setTimeout(resolve, 0));
  await new Promise((resolve) => setTimeout(resolve, 0));
  await new Promise((resolve) => setTimeout(resolve, 0));
  if (!notebook.querySelector('.study-panel__status-chip')?.textContent.includes(`forced ${entryId} failure`)) {
    throw new Error(`${entryId} failure was not reported`);
  }
  if (notebook.querySelector('.notebook-editor__content')?.value !== draft) {
    throw new Error(`${entryId} failure did not preserve the previous editor`);
  }
  const failedUnload = new window.Event('beforeunload', { cancelable: true });
  window.dispatchEvent(failedUnload);
  if (!failedUnload.defaultPrevented) {
    throw new Error(`${entryId} failure did not restore the dirty draft guard`);
  }
  const saveButton = [...notebook.querySelectorAll('.notebook-editor__actions button')]
    .find((button) => button.textContent === 'Save');
  saveButton.click();
  await new Promise((resolve) => setTimeout(resolve, 0));
  await new Promise((resolve) => setTimeout(resolve, 0));
  await new Promise((resolve) => setTimeout(resolve, 0));
  const saves = calls.filter((call) => call.entryId === 'study_note_upsert');
  if (saves.length !== savesBefore + 1 || saves.at(-1).args.note_id !== 'note-1') {
    throw new Error(`${entryId} failure left the visible editor detached from its note`);
  }
}

await expectCreateFailure('study_notebook_create');

async function expectNewNoteRefreshFailure(entryId) {
  const draft = `Draft discarded before new note ${entryId}`;
  const editorContent = notebook.querySelector('.notebook-editor__content');
  editorContent.value = draft;
  editorContent.dispatchEvent(new window.Event('input', { bubbles: true }));
  failureEntry = entryId;
  const createsBefore = calls.filter((call) => call.entryId === 'study_note_upsert' && !call.args.note_id).length;
  const savesBefore = calls.filter((call) => call.entryId === 'study_note_upsert' && call.args.note_id).length;
  const newNoteButton = [...notebook.querySelectorAll('.notebook-toolbar__actions button')]
    .find((button) => button.textContent === 'New note');
  newNoteButton.click();
  await new Promise((resolve) => setTimeout(resolve, 0));
  await new Promise((resolve) => setTimeout(resolve, 0));
  await new Promise((resolve) => setTimeout(resolve, 0));
  const status = notebook.querySelector('.study-panel__status-chip')?.textContent || '';
  if (!status.startsWith('Saved:') || !status.includes(`forced ${entryId} failure`)) {
    throw new Error(`new-note ${entryId} refresh failure did not preserve saved status: ${status}`);
  }
  if (notebook.querySelector('.notebook-editor__content')?.value !== '') {
    throw new Error(`new-note ${entryId} refresh failure restored the discarded draft`);
  }
  if (notebook.querySelector('.notebook-editor input')?.value !== 'New note') {
    throw new Error(`new-note ${entryId} refresh failure did not show the created note`);
  }
  const failedUnload = new window.Event('beforeunload', { cancelable: true });
  window.dispatchEvent(failedUnload);
  if (failedUnload.defaultPrevented) {
    throw new Error(`new-note ${entryId} refresh failure restored the discarded draft guard`);
  }
  const createdNoteId = `note-new-${createdNoteCount}`;
  const saveButton = [...notebook.querySelectorAll('.notebook-editor__actions button')]
    .find((button) => button.textContent === 'Save');
  saveButton.click();
  await new Promise((resolve) => setTimeout(resolve, 0));
  await new Promise((resolve) => setTimeout(resolve, 0));
  await new Promise((resolve) => setTimeout(resolve, 0));
  const creates = calls.filter((call) => call.entryId === 'study_note_upsert' && !call.args.note_id);
  const saves = calls.filter((call) => call.entryId === 'study_note_upsert' && call.args.note_id);
  if (creates.length !== createsBefore + 1) {
    throw new Error(`new-note ${entryId} refresh failure caused duplicate creation`);
  }
  if (saves.length !== savesBefore + 1 || saves.at(-1).args.note_id !== createdNoteId) {
    throw new Error(`new-note ${entryId} refresh failure detached the created editor from its note`);
  }
}

for (const entryId of ['study_notebook_list', 'study_note_list']) {
  await expectNewNoteRefreshFailure(entryId);
}

failureEntry = 'study_notebook_list';
const deleteNoteButton = [...notebook.querySelectorAll('.notebook-editor__actions button')]
  .find((button) => button.textContent === 'Delete');
deleteNoteButton.click();
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));
if (notebook.querySelector('.notebook-editor__actions')) {
  throw new Error('deleted note editor remained actionable after refresh failure');
}
if (!notebook.querySelector('.notebook-editor')?.textContent.includes('Select a note to edit')) {
  throw new Error('deleted note editor was not cleared before refresh failure');
}

async function expectNotebookRefreshFailure(entryId) {
  const panel = window.StudyCompanionNotebook.render('notebook-panel', ctx);
  document.body.appendChild(panel);
  await new Promise((resolve) => setTimeout(resolve, 0));
  await new Promise((resolve) => setTimeout(resolve, 0));
  panel.querySelector('.notebook-note-row__open').click();
  await new Promise((resolve) => setTimeout(resolve, 0));
  const draft = `Draft discarded before notebook ${entryId}`;
  const editorContent = panel.querySelector('.notebook-editor__content');
  editorContent.value = draft;
  editorContent.dispatchEvent(new window.Event('input', { bubbles: true }));
  const nameInput = [...panel.querySelectorAll('.notebook-toolbar input')]
    .find((input) => input.type !== 'search');
  const createButton = [...panel.querySelectorAll('.notebook-toolbar__actions button')]
    .find((button) => button.textContent === 'Create notebook');
  nameInput.value = `Created before ${entryId}`;
  failureEntry = entryId;
  const createsBefore = calls.filter((call) => call.entryId === 'study_notebook_create').length;
  createButton.click();
  await new Promise((resolve) => setTimeout(resolve, 0));
  await new Promise((resolve) => setTimeout(resolve, 0));
  await new Promise((resolve) => setTimeout(resolve, 0));
  const status = panel.querySelector('.study-panel__status-chip')?.textContent || '';
  if (!status.startsWith('Saved:') || !status.includes(`forced ${entryId} failure`)) {
    throw new Error(`notebook ${entryId} refresh failure did not preserve saved status: ${status}`);
  }
  const selectedNotebookId = panel.querySelector('.notebook-field select')?.value || '';
  if (!selectedNotebookId.startsWith('book-new-')) {
    throw new Error(`notebook ${entryId} refresh failure did not keep the created notebook selected`);
  }
  const selectedOption = panel.querySelector(`.notebook-field select option[value="${selectedNotebookId}"]`);
  if (!selectedOption?.textContent.includes(`Created before ${entryId}`)) {
    throw new Error(`notebook ${entryId} refresh failure hid the created notebook`);
  }
  if (panel.querySelector('.notebook-editor__content')?.value === draft) {
    throw new Error(`notebook ${entryId} refresh failure restored the discarded draft`);
  }
  const failedUnload = new window.Event('beforeunload', { cancelable: true });
  window.dispatchEvent(failedUnload);
  if (failedUnload.defaultPrevented) {
    throw new Error(`notebook ${entryId} refresh failure restored the discarded draft guard`);
  }
  if (nameInput.value !== '') {
    throw new Error(`notebook ${entryId} refresh failure kept the submitted notebook name`);
  }
  createButton.click();
  await new Promise((resolve) => setTimeout(resolve, 0));
  const creates = calls.filter((call) => call.entryId === 'study_notebook_create');
  if (creates.length !== createsBefore + 1) {
    throw new Error(`notebook ${entryId} refresh failure caused duplicate creation`);
  }
}

for (const entryId of ['study_notebook_list', 'study_note_list']) {
  await expectNotebookRefreshFailure(entryId);
}

const deleteFailurePanel = window.StudyCompanionNotebook.render('notebook-panel', ctx);
document.body.appendChild(deleteFailurePanel);
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));
const deleteFailureNotebookSelect = deleteFailurePanel.querySelector('.notebook-field select');
deleteFailureNotebookSelect.value = 'book-1';
deleteFailureNotebookSelect.dispatchEvent(new window.Event('change', { bubbles: true }));
await new Promise((resolve) => setTimeout(resolve, 0));
deleteFailurePanel.querySelector('.notebook-note-row__open').click();
await new Promise((resolve) => setTimeout(resolve, 0));

function setDeleteFailureDraft(prefix) {
  const inputs = deleteFailurePanel.querySelectorAll('.notebook-editor__field input');
  inputs[0].value = `${prefix} title`;
  inputs[0].dispatchEvent(new window.Event('input', { bubbles: true }));
  inputs[1].value = `${prefix} topic`;
  inputs[1].dispatchEvent(new window.Event('input', { bubbles: true }));
  inputs[2].value = `${prefix} tag`;
  inputs[2].dispatchEvent(new window.Event('input', { bubbles: true }));
  const content = deleteFailurePanel.querySelector('.notebook-editor__content');
  content.value = `${prefix} body`;
  content.dispatchEvent(new window.Event('input', { bubbles: true }));
}

function assertDeleteFailureDraft(prefix) {
  const inputs = deleteFailurePanel.querySelectorAll('.notebook-editor__field input');
  if (inputs[0]?.value !== `${prefix} title`
      || inputs[1]?.value !== `${prefix} topic`
      || inputs[2]?.value !== `${prefix} tag`
      || deleteFailurePanel.querySelector('.notebook-editor__content')?.value !== `${prefix} body`) {
    throw new Error(`failed deletion did not restore the complete ${prefix} draft`);
  }
}

setDeleteFailureDraft('Dirty note deletion');
deferredDeleteEntry = 'study_note_delete';
const failedDeleteNoteButton = [...deleteFailurePanel.querySelectorAll('.notebook-editor__actions button')]
  .find((button) => button.textContent === 'Delete');
failedDeleteNoteButton.click();
await new Promise((resolve) => setTimeout(resolve, 0));
const deleteFailureRefresh = [...deleteFailurePanel.querySelectorAll('.notebook-toolbar__actions button')]
  .find((button) => button.textContent === 'Refresh');
deleteFailureRefresh.click();
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));
deferredDeleteEntry = '';
deleteReject(new Error('forced study_note_delete failure'));
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));
assertDeleteFailureDraft('Dirty note deletion');
const failedNoteDeleteUnload = new window.Event('beforeunload', { cancelable: true });
window.dispatchEvent(failedNoteDeleteUnload);
if (!failedNoteDeleteUnload.defaultPrevented) {
  throw new Error('failed note deletion did not restore the dirty draft guard');
}

const saveAfterDeleteFailure = [...deleteFailurePanel.querySelectorAll('.notebook-editor__actions button')]
  .find((button) => button.textContent === 'Save');
saveAfterDeleteFailure.click();
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));
setDeleteFailureDraft('Dirty notebook deletion');
deferredDeleteEntry = 'study_notebook_delete';
const failedDeleteNotebookButton = [...deleteFailurePanel.querySelectorAll('.notebook-toolbar__actions button')]
  .find((button) => button.textContent === 'Delete notebook');
failedDeleteNotebookButton.click();
await new Promise((resolve) => setTimeout(resolve, 0));
deleteFailureRefresh.click();
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));
deferredDeleteEntry = '';
deleteReject(new Error('forced study_notebook_delete failure'));
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));
assertDeleteFailureDraft('Dirty notebook deletion');
const failedNotebookDeleteUnload = new window.Event('beforeunload', { cancelable: true });
window.dispatchEvent(failedNotebookDeleteUnload);
if (!failedNotebookDeleteUnload.defaultPrevented) {
  throw new Error('failed notebook deletion did not restore the dirty draft guard');
}
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=frontend_dir,
        env={**os.environ, "STUDY_COMPANION_STATIC_DIR": str(plugin_dir / "static")},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
