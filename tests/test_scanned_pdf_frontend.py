from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCANNER = ROOT / "static" / "scanned-pdf-ocr.js"
CONTROLLER = ROOT / "static" / "document-controller.js"
MAIN = ROOT / "static" / "main.js"
INDEX = ROOT / "static" / "index.html"
PDFJS = ROOT / "static" / "pdfjs"
PYPROJECT = ROOT / "pyproject.toml"


def test_scanned_pdf_static_contract() -> None:
    scanner = SCANNER.read_text(encoding="utf-8")
    assert "document.currentScript?.src" in scanner
    assert "`${uiBaseUrl}pdfjs/pdf.mjs`" in scanner
    assert "`${uiBaseUrl}pdfjs/pdf.worker.mjs`" in scanner
    assert "`${uiBaseUrl}pdfjs/wasm/`" in scanner
    assert "`${uiBaseUrl}pdfjs/iccs/`" in scanner
    assert "wasmUrl: PDFJS_WASM_URL" in scanner
    assert "iccUrl: PDFJS_ICC_URL" in scanner
    assert "const MAX_PAGES = 20;" in scanner
    assert "const TARGET_DPI = 200;" in scanner
    assert "const MAX_LONG_EDGE_PX = 2600;" in scanner
    assert "const MAX_PAGE_PIXELS = 8_000_000;" in scanner
    assert "const MAX_JPEG_BYTES = 6 * 1024 * 1024;" in scanner
    assert "const MAX_TEXT_CHARS = 32_000;" in scanner
    assert "const PAGE_TIMEOUT_MS = 45_000;" in scanner
    assert "const TOTAL_TIMEOUT_MS = 5 * 60_000;" in scanner
    assert "study_ocr_document_page" in scanner
    assert "# Page ${pageNumber}" in scanner
    assert "canvas.width = 0;" in scanner
    assert "canvas.height = 0;" in scanner
    assert "await destroyPdf(pdfDocument, loadingTask, deadline);" in scanner
    assert "waitWithinDeadline(canvasToBlob(canvas, quality), deadline)" in scanner
    assert "waitWithinDeadline(blob.arrayBuffer(), deadline)" in scanner
    assert "console." not in scanner
    assert "study_start_document_analysis" not in scanner


def test_fallback_is_limited_to_exact_pdf_parse_error() -> None:
    controller = CONTROLLER.read_text(encoding="utf-8")
    assert "scannedPdfOcr?.shouldFallback?.(sourceType, parseCode)" in controller
    assert "scannedPdfOcr.extract(file" in controller
    assert "throw new Error(parseCode);" in controller
    assert "documentRequestController?.abort();" in controller
    assert "documentImportActive" in controller
    assert "t('ui.error.document_canceled')" in controller
    assert "ui.document.progress_ocr_pages" in controller
    assert "ui.document.ocr_truncated_warning" in controller


def test_scanner_loads_before_document_controller_and_timeout_is_registered() -> None:
    index = INDEX.read_text(encoding="utf-8")
    scanner_position = index.index("./scanned-pdf-ocr.js")
    controller_position = index.index("./document-controller.js")
    assert scanner_position < controller_position
    assert "study_ocr_document_page: 45000" in MAIN.read_text(encoding="utf-8")


def test_vendored_pdfjs_assets_match_manifest_and_have_no_cdn_imports() -> None:
    manifest = (PDFJS / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    assert manifest
    for line in manifest:
        expected, relative = line.split(maxsplit=1)
        asset = PDFJS / relative
        assert asset.is_file(), relative
        assert hashlib.sha256(asset.read_bytes()).hexdigest() == expected, relative

    for relative in ("pdf.mjs", "pdf.worker.mjs"):
        source = (PDFJS / relative).read_text(encoding="utf-8")
        assert not re.search(
            r"(?:from\s*|import\s*\(|importScripts\s*\()[\"']https?://",
            source,
        )


def test_market_package_excludes_worktree_and_test_artifacts() -> None:
    pyproject = PYPROJECT.read_text(encoding="utf-8")
    assert '[tool.neko.build]' in pyproject
    assert 'exclude_dirs = ["tests"]' in pyproject
    assert 'exclude_files = [".git", ".coverage", "coverage.xml"]' in pyproject


def test_scanned_pdf_executable_flow() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is not available")
    scanner_path = json.dumps(str(SCANNER))
    harness = f"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const state = {{ cleanup: 0, destroyed: 0, calls: [], active: 0, maxActive: 0, canvases: [] }};
const window = {{}};
const context = {{
  window,
  document: {{
    currentScript: {{ src: 'http://localhost/plugin/study_companion_1/ui/scanned-pdf-ocr.js?v=1' }},
    createElement() {{ throw new Error('injected canvas factory expected'); }},
  }},
  DOMException,
  AbortController,
  Uint8Array,
  Math,
  Date,
  Promise,
  setTimeout,
  clearTimeout,
  btoa(value) {{ return Buffer.from(value, 'binary').toString('base64'); }},
}};
vm.runInNewContext(fs.readFileSync({scanner_path}, 'utf8'), context);

function makeCanvas() {{
  const canvas = {{
    width: 0,
    height: 0,
    getContext() {{ return {{ save() {{}}, restore() {{}}, fillRect() {{}} }}; }},
    toBlob(callback) {{
      callback({{ size: 3, async arrayBuffer() {{ return Uint8Array.from([1, 2, 3]).buffer; }} }});
    }},
    remove() {{}},
  }};
  state.canvases.push(canvas);
  return canvas;
}}

function makePdf(pageCount) {{
  return {{
    numPages: pageCount,
    async getPage() {{
      return {{
        getViewport({{ scale }}) {{ return {{ width: 612 * scale, height: 792 * scale }}; }},
        render() {{ return {{ promise: Promise.resolve(), cancel() {{}} }}; }},
        cleanup() {{ state.cleanup += 1; }},
      }};
    }},
    async destroy() {{ state.destroyed += 1; }},
  }};
}}

async function run() {{
  assert.strictEqual(window.StudyScannedPdfOcr.shouldFallback('pdf', 'no_readable_text'), true);
  assert.strictEqual(window.StudyScannedPdfOcr.shouldFallback('docx', 'no_readable_text'), false);
  assert.strictEqual(window.StudyScannedPdfOcr.shouldFallback('pdf', 'invalid_pdf'), false);
  assert.strictEqual(window.StudyScannedPdfOcr.shouldFallback('pdf', 'encrypted_pdf_unsupported'), false);

  const pdf = makePdf(3);
  const workerOptions = {{}};
  const scanner = window.StudyScannedPdfOcr.create({{
    canvasFactory: makeCanvas,
    loadPdfJs: async () => ({{
      GlobalWorkerOptions: workerOptions,
      getDocument(options) {{
        state.pdfOptions = options;
        return {{ promise: Promise.resolve(pdf), destroy: async () => {{}} }};
      }},
    }}),
    callPlugin: async (entry, args) => {{
      const pageNumber = state.calls.length + 1;
      state.active += 1;
      state.maxActive = Math.max(state.maxActive, state.active);
      state.calls.push([entry, args]);
      await Promise.resolve();
      state.active -= 1;
      return pageNumber === 1 ? {{ status: 'ok', text: 'alpha' }}
        : pageNumber === 2 ? {{ status: 'empty', text: '' }}
          : {{ status: 'ok', text: 'omega' }};
    }},
  }});
  const progress = [];
  const result = await scanner.extract(
    {{ async arrayBuffer() {{ return Uint8Array.from([37, 80, 68, 70]).buffer; }} }},
    {{ onProgress(value) {{ progress.push(value); }} }},
  );
  assert.strictEqual(result.text, '# Page 1\\n\\nalpha\\n\\n# Page 3\\n\\nomega');
  assert.strictEqual(result.pageCount, 3);
  assert.strictEqual(result.truncated, false);
  assert.strictEqual(state.calls.length, 3);
  assert.strictEqual(state.maxActive, 1);
  assert.strictEqual(
    workerOptions.workerSrc,
    'http://localhost/plugin/study_companion_1/ui/pdfjs/pdf.worker.mjs',
  );
  assert.strictEqual(
    state.pdfOptions.wasmUrl,
    'http://localhost/plugin/study_companion_1/ui/pdfjs/wasm/',
  );
  assert.strictEqual(
    state.pdfOptions.iccUrl,
    'http://localhost/plugin/study_companion_1/ui/pdfjs/iccs/',
  );
  assert.ok(state.calls.every(([entry, args]) => entry === 'study_ocr_document_page'
    && args.image_data_url === 'data:image/jpeg;base64,AQID'));
  assert.strictEqual(state.cleanup, 3);
  assert.strictEqual(state.destroyed, 1);
  assert.ok(state.canvases.every((canvas) => canvas.width === 0 && canvas.height === 0));
  assert.ok(progress.length >= 4);

  const tooManyPdf = makePdf(21);
  const tooManyScanner = window.StudyScannedPdfOcr.create({{
    canvasFactory: makeCanvas,
    loadPdfJs: async () => ({{
      GlobalWorkerOptions: {{}},
      getDocument() {{ return {{ promise: Promise.resolve(tooManyPdf) }}; }},
    }}),
    callPlugin: async () => {{ throw new Error('must not OCR'); }},
  }});
  await assert.rejects(
    tooManyScanner.extract({{ async arrayBuffer() {{ return new ArrayBuffer(1); }} }}),
    (error) => error.message === 'document_pdf_ocr_too_many_pages',
  );

  const cancelController = new AbortController();
  let cancelCalls = 0;
  const cancelScanner = window.StudyScannedPdfOcr.create({{
    canvasFactory: makeCanvas,
    loadPdfJs: async () => ({{
      GlobalWorkerOptions: {{}},
      getDocument() {{ return {{ promise: Promise.resolve(makePdf(3)) }}; }},
    }}),
    callPlugin: async () => {{
      cancelCalls += 1;
      cancelController.abort();
      return {{ status: 'ok', text: 'first page finishes' }};
    }},
  }});
  await assert.rejects(
    cancelScanner.extract(
      {{ async arrayBuffer() {{ return new ArrayBuffer(1); }} }},
      {{ signal: cancelController.signal }},
    ),
    (error) => error.name === 'AbortError',
  );
  assert.strictEqual(cancelCalls, 1);

  async function expectOcrPayloadError(payload, expectedCode) {{
    const errorScanner = window.StudyScannedPdfOcr.create({{
      canvasFactory: makeCanvas,
      loadPdfJs: async () => ({{
        GlobalWorkerOptions: {{}},
        getDocument() {{ return {{ promise: Promise.resolve(makePdf(1)) }}; }},
      }}),
      callPlugin: async () => payload,
    }});
    await assert.rejects(
      errorScanner.extract({{ async arrayBuffer() {{ return new ArrayBuffer(1); }} }}),
      (error) => error.message === expectedCode,
    );
  }}

  await expectOcrPayloadError(
    {{ status: 'disabled', diagnostic: 'document_pdf_ocr_disabled' }},
    'document_pdf_ocr_disabled',
  );
  await expectOcrPayloadError(
    {{ status: 'unavailable', diagnostic: 'document_pdf_ocr_unavailable' }},
    'document_pdf_ocr_unavailable',
  );
  await expectOcrPayloadError(
    {{ status: 'ocr_failed', diagnostic: 'document_pdf_ocr_failed' }},
    'document_pdf_ocr_failed',
  );
  await expectOcrPayloadError({{ status: 'empty', text: '' }}, 'no_readable_text');

  const timeoutScanner = window.StudyScannedPdfOcr.create({{
    canvasFactory: makeCanvas,
    loadPdfJs: async () => ({{
      GlobalWorkerOptions: {{}},
      getDocument() {{ return {{ promise: Promise.resolve(makePdf(1)) }}; }},
    }}),
    callPlugin: async () => {{ throw new Error('Plugin call timed out'); }},
  }});
  await assert.rejects(
    timeoutScanner.extract({{ async arrayBuffer() {{ return new ArrayBuffer(1); }} }}),
    (error) => error.message === 'document_pdf_ocr_timeout',
  );

  const renderFailureScanner = window.StudyScannedPdfOcr.create({{
    canvasFactory: makeCanvas,
    loadPdfJs: async () => ({{
      GlobalWorkerOptions: {{}},
      getDocument() {{ return {{ promise: Promise.resolve({{
        numPages: 1,
        async getPage() {{ throw new Error('sensitive render detail'); }},
        async destroy() {{}},
      }}) }}; }},
    }}),
    callPlugin: async () => {{ throw new Error('must not OCR'); }},
  }});
  await assert.rejects(
    renderFailureScanner.extract({{ async arrayBuffer() {{ return new ArrayBuffer(1); }} }}),
    (error) => error.message === 'document_pdf_render_failed',
  );

  const oversizedCanvas = () => ({{
    width: 0,
    height: 0,
    getContext() {{ return {{ save() {{}}, restore() {{}}, fillRect() {{}} }}; }},
    toBlob(callback) {{ callback({{ size: 6 * 1024 * 1024 + 1 }}); }},
    remove() {{}},
  }});
  const oversizedScanner = window.StudyScannedPdfOcr.create({{
    canvasFactory: oversizedCanvas,
    loadPdfJs: async () => ({{
      GlobalWorkerOptions: {{}},
      getDocument() {{ return {{ promise: Promise.resolve(makePdf(1)) }}; }},
    }}),
    callPlugin: async () => {{ throw new Error('must not OCR'); }},
  }});
  await assert.rejects(
    oversizedScanner.extract({{ async arrayBuffer() {{ return new ArrayBuffer(1); }} }}),
    (error) => error.message === 'document_pdf_page_too_large',
  );

  const truncateScanner = window.StudyScannedPdfOcr.create({{
    canvasFactory: makeCanvas,
    loadPdfJs: async () => ({{
      GlobalWorkerOptions: {{}},
      getDocument() {{ return {{ promise: Promise.resolve(makePdf(2)) }}; }},
    }}),
    callPlugin: async () => ({{ status: 'ok', text: 'x'.repeat(33000) }}),
  }});
  const truncated = await truncateScanner.extract({{
    async arrayBuffer() {{ return new ArrayBuffer(1); }},
  }});
  assert.strictEqual(truncated.text.length, 32000);
  assert.strictEqual(truncated.truncated, true);

  const surrogateScanner = window.StudyScannedPdfOcr.create({{
    canvasFactory: makeCanvas,
    loadPdfJs: async () => ({{
      GlobalWorkerOptions: {{}},
      getDocument() {{ return {{ promise: Promise.resolve(makePdf(1)) }}; }},
    }}),
    callPlugin: async () => ({{
      status: 'ok',
      text: 'x'.repeat(31989) + '\\ud83d\\ude00tail',
    }}),
  }});
  const surrogateSafe = await surrogateScanner.extract({{
    async arrayBuffer() {{ return new ArrayBuffer(1); }},
  }});
  assert.strictEqual(surrogateSafe.truncated, true);
  assert.strictEqual(surrogateSafe.text.length, 31999);
  assert.ok(!/[\\ud800-\\udfff]$/.test(surrogateSafe.text));
}}

run().catch((error) => {{ process.stderr.write(String(error.stack || error)); process.exit(1); }});
"""
    completed = subprocess.run(
        [node, "-e", harness],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
