from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONTROLLER = ROOT / "surfaces" / "scanned_pdf_ocr.ts"
PANEL = ROOT / "surfaces" / "study_panel.tsx"
PDFJS_LOADER = ROOT / "static" / "pdfjs-loader.mjs"
PDFJS_HOSTED = ROOT / "static" / "pdfjs" / "pdf.hosted.js"


def _resolve_sucrase() -> Path | None:
    override = os.environ.get("STUDY_COMPANION_SUCRASE_PATH", "").strip()
    if override:
        return Path(override)
    candidate = ROOT / "tests" / "frontend" / "node_modules" / "sucrase"
    if candidate.is_dir():
        return candidate
    return None


SUCRASE = _resolve_sucrase()


def test_hosted_surface_uses_local_pdfjs_and_exact_fallback_contract() -> None:
    controller = CONTROLLER.read_text(encoding="utf-8")
    panel = PANEL.read_text(encoding="utf-8")

    assert "import * as vendoredPdfJs" not in controller
    assert "pdf.hosted.js" in controller
    assert "import(" not in controller
    assert "import * as pdfjs from './pdfjs/pdf.mjs';" in PDFJS_LOADER.read_text(encoding="utf-8")
    hosted_loader = PDFJS_HOSTED.read_text(encoding="utf-8")
    assert "__studyCompanionPdfJs" in hosted_loader
    assert "__studyCompanionCreatePdfWorker" in hosted_loader
    assert "URL.createObjectURL" in hosted_loader
    assert "new Worker" in hosted_loader
    assert "return `/plugin/${encodeURIComponent(normalized)}/ui/pdfjs/`;" in controller
    assert "`${assetBaseUrl}pdf.worker.mjs`" in controller
    assert "`${assetBaseUrl}wasm/`" in controller
    assert "`${assetBaseUrl}iccs/`" in controller
    assert "workerLease.worker.terminate();" in controller
    assert "URL.revokeObjectURL(workerLease.url);" in controller
    assert "sourceType === 'pdf' && parseCode === 'no_readable_text'" in controller
    assert "shouldFallbackToScannedPdfOcr(definition.sourceType, error)" in panel
    assert "study_ocr_document_page" in panel
    assert "study_start_document_analysis" not in controller
    assert "console." not in controller


def test_hosted_surface_progress_cancel_and_error_contract() -> None:
    panel = PANEL.read_text(encoding="utf-8")
    for code in (
        "document_pdf_ocr_disabled",
        "document_pdf_ocr_unavailable",
        "document_pdf_ocr_too_many_pages",
        "document_pdf_render_failed",
        "document_pdf_page_too_large",
        "document_pdf_ocr_timeout",
        "document_pdf_ocr_failed",
    ):
        assert f"ui.error.{code}" in panel

    assert "ui.document.progress_ocr_pages" in panel
    assert "ui.document.scanned_pdf_ocr" in panel
    assert "ui.document.ocr_truncated_warning" in panel
    assert "documentControllerRef.current?.abort();" in panel
    assert "setDocumentOcrCanceling(true);" in panel
    assert "documentControllerRef.current = null;" in panel


def test_scanned_pdf_surface_executable_contract() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is not available")
    if SUCRASE is None or not SUCRASE.is_dir():
        pytest.skip("The Hosted Surface TypeScript runtime is not available")

    harness = r"""
const fs = require('fs');
const assert = require('assert');
const sucrase = require(SUCRASE_PATH);
let source = fs.readFileSync(CONTROLLER_PATH, 'utf8');
const compiled = sucrase.transform(source, { transforms: ['typescript', 'imports'] }).code;
const moduleUnderTest = { exports: {} };
new Function('module', 'exports', 'require', compiled)(
  moduleUnderTest,
  moduleUnderTest.exports,
  require,
);
const {
  SCANNED_PDF_OCR_LIMITS,
  createScannedPdfOcrController,
  scannedPdfAssetBaseUrl,
  shouldFallbackToScannedPdfOcr,
} = moduleUnderTest.exports;

assert.strictEqual(SCANNED_PDF_OCR_LIMITS.maxPages, 20);
assert.strictEqual(SCANNED_PDF_OCR_LIMITS.targetDpi, 200);
assert.strictEqual(SCANNED_PDF_OCR_LIMITS.maxLongEdgePx, 2600);
assert.strictEqual(SCANNED_PDF_OCR_LIMITS.maxPagePixels, 8000000);
assert.strictEqual(SCANNED_PDF_OCR_LIMITS.maxJpegBytes, 6 * 1024 * 1024);
assert.strictEqual(SCANNED_PDF_OCR_LIMITS.maxTextChars, 32000);
assert.strictEqual(SCANNED_PDF_OCR_LIMITS.pageTimeoutMs, 45000);
assert.strictEqual(SCANNED_PDF_OCR_LIMITS.totalTimeoutMs, 300000);
assert.strictEqual(scannedPdfAssetBaseUrl('study_companion-1'), '/plugin/study_companion-1/ui/pdfjs/');
assert.throws(() => scannedPdfAssetBaseUrl('../escape'), /document_pdf_render_failed/);
assert.strictEqual(shouldFallbackToScannedPdfOcr('pdf', { code: 'no_readable_text' }), true);
assert.strictEqual(shouldFallbackToScannedPdfOcr('docx', { code: 'no_readable_text' }), false);
assert.strictEqual(shouldFallbackToScannedPdfOcr('pdf', { code: 'invalid_pdf' }), false);
assert.strictEqual(shouldFallbackToScannedPdfOcr('pdf', { message: ' no_readable_text ' }), true);

function makeCanvas(state, options = {}) {
  const canvas = {
    width: 0,
    height: 0,
    getContext() {
      if (options.noContext) return null;
      return { save() {}, restore() {}, fillRect() {}, fillStyle: '' };
    },
    toBlob(callback) {
      callback({
        size: options.blobSize || 3,
        async arrayBuffer() { return Uint8Array.from([1, 2, 3]).buffer; },
      });
    },
    remove() { state.removed += 1; },
  };
  state.canvases.push(canvas);
  return canvas;
}

function makePdf(state, pageCount, options = {}) {
  return {
    numPages: pageCount,
    async getPage(pageNumber) {
      if (options.getPageError) throw new Error('render exploded');
      return {
        getViewport({ scale }) {
          if (options.badViewport) return scale === 1
            ? { width: 612, height: 792 }
            : { width: 3000, height: 3000 };
          return { width: 612 * scale, height: 792 * scale };
        },
        render() { return { promise: Promise.resolve(), cancel() { state.renderCanceled += 1; } }; },
        cleanup() { state.pageCleanup += 1; },
      };
    },
    async destroy() { state.destroyed += 1; },
  };
}

function dependencies(state, pdf, callPageOcr, extra = {}) {
  return {
    assetBaseUrl: '/plugin/custom_plugin/ui/pdfjs/',
    createCanvas: () => makeCanvas(state, extra.canvas || {}),
    loadPdfJs: async () => ({
      GlobalWorkerOptions: state.workerOptions,
      getDocument(options) {
        state.documentOptions = options;
        return { promise: Promise.resolve(pdf), destroy() { state.loadingDestroyed += 1; } };
      },
    }),
    callPageOcr,
    ...extra.controller,
  };
}

function state() {
  return {
    calls: [], active: 0, maxActive: 0, canvases: [], removed: 0,
    pageCleanup: 0, destroyed: 0, loadingDestroyed: 0, renderCanceled: 0,
    workerOptions: {}, documentOptions: null,
  };
}

async function expectCode(promise, code) {
  await assert.rejects(promise, (error) => error && error.message === code);
}

async function run() {
  const successState = state();
  const progress = [];
  const successPdf = makePdf(successState, 3);
  const success = createScannedPdfOcrController(dependencies(
    successState,
    successPdf,
    async (args) => {
      successState.active += 1;
      successState.maxActive = Math.max(successState.maxActive, successState.active);
      successState.calls.push(args);
      await Promise.resolve();
      successState.active -= 1;
      const page = successState.calls.length;
      return page === 1 ? { status: 'ok', text: 'alpha' }
        : page === 2 ? { status: 'empty', text: '' }
          : { status: 'ok', text: 'omega' };
    },
  ));
  const result = await success.extract(
    { async arrayBuffer() { return Uint8Array.from([37, 80, 68, 70]).buffer; } },
    { onProgress(value) { progress.push(value); } },
  );
  assert.strictEqual(result.text, '# Page 1\n\nalpha\n\n# Page 3\n\nomega');
  assert.strictEqual(result.pageCount, 3);
  assert.strictEqual(result.truncated, false);
  assert.strictEqual(successState.calls.length, 3);
  assert.strictEqual(successState.maxActive, 1);
  assert.ok(successState.calls.every((args) =>
    Object.keys(args).join(',') === 'image_data_url'
    && args.image_data_url === 'data:image/jpeg;base64,AQID'));
  assert.strictEqual(successState.workerOptions.workerSrc, '/plugin/custom_plugin/ui/pdfjs/pdf.worker.mjs');
  assert.strictEqual(successState.documentOptions.wasmUrl, '/plugin/custom_plugin/ui/pdfjs/wasm/');
  assert.strictEqual(successState.documentOptions.iccUrl, '/plugin/custom_plugin/ui/pdfjs/iccs/');
  assert.strictEqual(successState.pageCleanup, 3);
  assert.strictEqual(successState.destroyed, 1);
  assert.ok(successState.canvases.every((canvas) => canvas.width === 0 && canvas.height === 0));
  assert.ok(progress.length >= 4);

  const tooManyState = state();
  const tooMany = createScannedPdfOcrController(dependencies(
    tooManyState,
    makePdf(tooManyState, 21),
    async () => { throw new Error('must not OCR'); },
  ));
  await expectCode(tooMany.extract({ async arrayBuffer() { return new ArrayBuffer(1); } }), 'document_pdf_ocr_too_many_pages');
  assert.strictEqual(tooManyState.destroyed, 1);

  const emptyState = state();
  const empty = createScannedPdfOcrController(dependencies(
    emptyState,
    makePdf(emptyState, 2),
    async () => ({ status: 'empty', text: '' }),
  ));
  await expectCode(empty.extract({ async arrayBuffer() { return new ArrayBuffer(1); } }), 'no_readable_text');

  for (const [payload, code] of [
    [{ status: 'disabled', diagnostic: 'document_pdf_ocr_disabled' }, 'document_pdf_ocr_disabled'],
    [{ status: 'unavailable', diagnostic: 'document_pdf_ocr_unavailable' }, 'document_pdf_ocr_unavailable'],
    [{ status: 'failed', diagnostic: 'document_pdf_ocr_failed' }, 'document_pdf_ocr_failed'],
    [{ status: 'timeout', diagnostic: 'document_pdf_ocr_timeout' }, 'document_pdf_ocr_timeout'],
  ]) {
    const failureState = state();
    const failure = createScannedPdfOcrController(dependencies(
      failureState,
      makePdf(failureState, 1),
      async () => payload,
    ));
    await expectCode(failure.extract({ async arrayBuffer() { return new ArrayBuffer(1); } }), code);
  }

  const renderState = state();
  const renderFailure = createScannedPdfOcrController(dependencies(
    renderState,
    makePdf(renderState, 1, { getPageError: true }),
    async () => ({ status: 'ok', text: 'never' }),
  ));
  await expectCode(renderFailure.extract({ async arrayBuffer() { return new ArrayBuffer(1); } }), 'document_pdf_render_failed');

  const largeState = state();
  const largePage = createScannedPdfOcrController(dependencies(
    largeState,
    makePdf(largeState, 1, { badViewport: true }),
    async () => ({ status: 'ok', text: 'never' }),
  ));
  await expectCode(largePage.extract({ async arrayBuffer() { return new ArrayBuffer(1); } }), 'document_pdf_page_too_large');

  const jpegState = state();
  const largeJpeg = createScannedPdfOcrController(dependencies(
    jpegState,
    makePdf(jpegState, 1),
    async () => ({ status: 'ok', text: 'never' }),
    { canvas: { blobSize: 7 * 1024 * 1024 } },
  ));
  await expectCode(largeJpeg.extract({ async arrayBuffer() { return new ArrayBuffer(1); } }), 'document_pdf_page_too_large');

  const truncationState = state();
  const truncation = createScannedPdfOcrController(dependencies(
    truncationState,
    makePdf(truncationState, 1),
    async () => ({ status: 'ok', text: 'x'.repeat(40000) }),
  ));
  const truncated = await truncation.extract({ async arrayBuffer() { return new ArrayBuffer(1); } });
  assert.strictEqual(truncated.text.length, 32000);
  assert.strictEqual(truncated.truncated, true);

  const surrogateState = state();
  const surrogate = createScannedPdfOcrController(dependencies(
    surrogateState,
    makePdf(surrogateState, 1),
    async () => ({ status: 'ok', text: 'x'.repeat(31989) + '\ud83d\ude00tail' }),
  ));
  const surrogateSafe = await surrogate.extract({ async arrayBuffer() { return new ArrayBuffer(1); } });
  assert.strictEqual(surrogateSafe.truncated, true);
  assert.strictEqual(surrogateSafe.text.length, 31999);
  assert.ok(!/[\ud800-\udfff]$/.test(surrogateSafe.text));

  const cancelState = state();
  const cancelController = new AbortController();
  let cancelCalls = 0;
  const canceled = createScannedPdfOcrController(dependencies(
    cancelState,
    makePdf(cancelState, 3),
    async () => {
      cancelCalls += 1;
      cancelController.abort();
      await Promise.resolve();
      return { status: 'ok', text: 'current page finishes' };
    },
  ));
  await assert.rejects(
    canceled.extract(
      { async arrayBuffer() { return new ArrayBuffer(1); } },
      { signal: cancelController.signal },
    ),
    (error) => error && error.name === 'AbortError',
  );
  assert.strictEqual(cancelCalls, 1);

  const timeoutState = state();
  let timedOutSignal;
  const pageTimeout = createScannedPdfOcrController(dependencies(
    timeoutState,
    makePdf(timeoutState, 1),
    async (_args, signal) => {
      timedOutSignal = signal;
      return new Promise(() => {});
    },
    {
      controller: {
        setTimer(callback, timeoutMs) {
          if (timeoutMs <= 45000) queueMicrotask(callback);
          return 1;
        },
        clearTimer() {},
      },
    },
  ));
  await expectCode(pageTimeout.extract({ async arrayBuffer() { return new ArrayBuffer(1); } }), 'document_pdf_ocr_timeout');
  assert.strictEqual(timedOutSignal.aborted, true);

  const deadlineState = state();
  let now = 0;
  const totalTimeout = createScannedPdfOcrController(dependencies(
    deadlineState,
    makePdf(deadlineState, 1),
    async () => {
      now = 300001;
      return { status: 'ok', text: 'too late' };
    },
    { controller: { now: () => now } },
  ));
  await expectCode(totalTimeout.extract({ async arrayBuffer() { return new ArrayBuffer(1); } }), 'document_pdf_ocr_timeout');
}

run().catch((error) => {
  process.stderr.write(String(error && (error.stack || error)));
  process.exit(1);
});
"""
    script = (
        harness.replace("SUCRASE_PATH", json.dumps(str(SUCRASE)))
        .replace("CONTROLLER_PATH", json.dumps(str(CONTROLLER)))
    )
    completed = subprocess.run(
        [node, "-e", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
