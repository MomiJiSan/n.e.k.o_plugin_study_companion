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
PDF_FIXTURES = ROOT / "tests" / "fixtures" / "pdf"
GITATTRIBUTES = ROOT / ".gitattributes"


def test_scanned_pdf_static_contract() -> None:
    scanner = SCANNER.read_text(encoding="utf-8")
    assert "document.currentScript?.src" in scanner
    assert "`${uiBaseUrl}pdfjs/pdf.mjs`" in scanner
    assert "`${uiBaseUrl}pdfjs/pdf.worker.mjs`" in scanner
    assert "`${uiBaseUrl}pdfjs/wasm/`" in scanner
    assert "`${uiBaseUrl}pdfjs/iccs/`" in scanner
    assert "wasmUrl: PDFJS_WASM_URL" in scanner
    assert "iccUrl: PDFJS_ICC_URL" in scanner
    assert "const MAX_INSPECTED_PAGES = 40;" in scanner
    assert "const MAX_OCR_PAGES = 20;" in scanner
    assert "const MIN_RELIABLE_TEXT_CHARS = 24;" in scanner
    assert "const TARGET_DPI = 200;" in scanner
    assert "const MAX_LONG_EDGE_PX = 2600;" in scanner
    assert "const MAX_PAGE_PIXELS = 8_000_000;" in scanner
    assert "const MAX_JPEG_BYTES = 6 * 1024 * 1024;" in scanner
    assert "const MAX_TEXT_CHARS = 32_000;" in scanner
    assert "const PAGE_TIMEOUT_MS = 45_000;" in scanner
    assert "const TOTAL_TIMEOUT_MS = 5 * 60_000;" in scanner
    assert "study_ocr_document_page" in scanner
    assert "study_ocr_document_capabilities" in scanner
    assert "page.getTextContent()" in scanner
    assert "item?.hasEOL" in scanner
    assert "# Page ${pageNumber}" in scanner
    assert "canvas.width = 0;" in scanner
    assert "canvas.height = 0;" in scanner
    assert "await destroyPdf(pdfDocument, loadingTask, deadline, signal);" in scanner
    assert "waitWithinDeadline(canvasToBlob(canvas, quality), deadline, undefined, signal)" in scanner
    assert "waitWithinDeadline(blob.arrayBuffer(), deadline, undefined, signal)" in scanner
    assert "console." not in scanner
    assert "study_start_document_analysis" not in scanner


def test_pdf_hybrid_import_and_fail_open_contract() -> None:
    controller = CONTROLLER.read_text(encoding="utf-8")
    assert "scannedPdfOcr?.shouldFallback?.(sourceType, parseCode)" in controller
    assert "scannedPdfOcr.extract(file" in controller
    assert "throw new Error(parseCode);" in controller
    assert "Number(ocrDocument.ocrPageCount) > 0" in controller
    assert "documentPdfPartialOcrSkipped: true" in controller
    assert "ui.document.partial_ocr_skipped_warning" in controller
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
    assert "./main.js?v=study-scanned-pdf-hybrid-0.2.3" in index
    assert "study_ocr_document_capabilities: 15000" in MAIN.read_text(encoding="utf-8")
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


def test_pdf_fixtures_are_checkout_safe_binary_files() -> None:
    attributes = GITATTRIBUTES.read_text(encoding="utf-8")
    assert "tests/fixtures/pdf/*.pdf binary" in attributes
    assert all(path.read_bytes().startswith(b"%PDF-") for path in PDF_FIXTURES.glob("*.pdf"))


def test_real_pdf_fixtures_expose_expected_text_layers() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is not available")

    fixture_paths = {
        "scan": PDF_FIXTURES / "scan-only.pdf",
        "hidden": PDF_FIXTURES / "hidden-one-char.pdf",
        "mixed": PDF_FIXTURES / "mixed-three-pages.pdf",
    }
    assert all(path.is_file() for path in fixture_paths.values())

    script = f"""
import fs from 'node:fs/promises';
import {{ pathToFileURL }} from 'node:url';
globalThis.DOMMatrix ||= class DOMMatrix {{}};
globalThis.ImageData ||= class ImageData {{}};
globalThis.Path2D ||= class Path2D {{}};
Uint8Array.prototype.toHex ||= function toHex() {{
  return Array.from(this, (value) => value.toString(16).padStart(2, '0')).join('');
}};
Map.prototype.getOrInsertComputed ||= function getOrInsertComputed(key, factory) {{
  if (!this.has(key)) this.set(key, factory(key));
  return this.get(key);
}};
Promise.try ||= function promiseTry(callback, ...args) {{
  return Promise.resolve().then(() => callback(...args));
}};
const pdfjs = await import(pathToFileURL({json.dumps(str(PDFJS / 'pdf.mjs'))}).href);
async function pageTexts(path) {{
  const bytes = new Uint8Array(await fs.readFile(path));
  const loadingTask = pdfjs.getDocument({{ data: bytes, disableWorker: true }});
  const document = await loadingTask.promise;
  const pages = [];
  try {{
    for (let pageNumber = 1; pageNumber <= document.numPages; pageNumber += 1) {{
      const page = await document.getPage(pageNumber);
      const content = await page.getTextContent();
      pages.push(content.items.map((item) => String(item.str || '')).join('').trim());
      page.cleanup();
    }}
  }} finally {{
    await loadingTask.destroy?.();
  }}
  return pages;
}}
const result = {{
  scan: await pageTexts({json.dumps(str(fixture_paths['scan']))}),
  hidden: await pageTexts({json.dumps(str(fixture_paths['hidden']))}),
  mixed: await pageTexts({json.dumps(str(fixture_paths['mixed']))}),
}};
process.stdout.write(`FIXTURE_JSON:${{JSON.stringify(result)}}\n`);
"""
    completed = subprocess.run(
        [node, "--input-type=module", "-e", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    marker = next(
        line.removeprefix("FIXTURE_JSON:")
        for line in completed.stdout.splitlines()
        if line.startswith("FIXTURE_JSON:")
    )
    result = json.loads(marker)
    assert result["scan"] == [""]
    assert result["hidden"] == ["x"]
    assert len(result["mixed"]) == 3
    assert "Reliable digital text page one" in result["mixed"][0]
    assert result["mixed"][1] == ""
    assert "Reliable digital text page three" in result["mixed"][2]


def test_market_package_excludes_worktree_and_test_artifacts() -> None:
    pyproject = PYPROJECT.read_text(encoding="utf-8")
    assert '[tool.neko.build]' in pyproject
    assert 'exclude_dirs = ["tests", "experimental"]' in pyproject
    assert 'exclude_files = [".git", ".coverage", "coverage.xml"]' in pyproject





def test_scanned_pdf_hybrid_executable_vectors() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is not available")
    scanner_path = json.dumps(str(SCANNER))
    harness = f"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const window = {{}};
const context = {{
  window,
  document: {{
    currentScript: {{ src: 'http://localhost/plugin/study_companion/ui/scanned-pdf-ocr.js' }},
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

const api = window.StudyScannedPdfOcr;
const reliable = 'Reliable Unicode text 1234567890 学习资料内容足够长';

function makeCanvas(state) {{
  const canvas = {{
    width: 0,
    height: 0,
    getContext() {{ return {{ save() {{}}, restore() {{}}, fillRect() {{}} }}; }},
    toBlob(callback) {{
      callback({{ size: 3, async arrayBuffer() {{ return Uint8Array.from([1, 2, 3]).buffer; }} }});
    }},
    remove() {{ state.removed += 1; }},
  }};
  state.canvases.push(canvas);
  return canvas;
}}

function makePdf(texts, state, options = {{}}) {{
  return {{
    numPages: texts.length,
    async getPage(pageNumber) {{
      if (options.getPageError) throw new Error('render exploded');
      const value = texts[pageNumber - 1];
      return {{
        async getTextContent() {{
          if (options.textError) throw new Error('text exploded');
          if (Array.isArray(value)) return {{ items: value }};
          return {{ items: value ? [{{ str: value, hasEOL: false }}] : [] }};
        }},
        getViewport({{ scale }}) {{ return {{ width: 612 * scale, height: 792 * scale }}; }},
        render() {{
          state.events.push(`render:${{pageNumber}}`);
          return {{ promise: Promise.resolve(), cancel() {{}} }};
        }},
        cleanup() {{ state.cleanup += 1; }},
      }};
    }},
    async destroy() {{ state.destroyed += 1; }},
  }};
}}

function makeScanner(texts, ocrResults = [], options = {{}}) {{
  const state = {{
    events: [], capabilityCalls: 0, ocrCalls: 0, cleanup: 0, destroyed: 0,
    removed: 0, canvases: [], pdfOptions: null,
  }};
  const pdf = makePdf(texts, state, options);
  const scanner = api.create({{
    canvasFactory: () => makeCanvas(state),
    loadPdfJs: async () => ({{
      GlobalWorkerOptions: {{}},
      getDocument(pdfOptions) {{
        state.pdfOptions = pdfOptions;
        return {{ promise: Promise.resolve(pdf), destroy: async () => {{}} }};
      }},
    }}),
    callCapabilities: async () => {{
      state.capabilityCalls += 1;
      state.events.push('capabilities');
      return options.capabilities || {{ protocol: 1, ready: true, enabled: true }};
    }},
    callPlugin: async (entry, args) => {{
      assert.strictEqual(entry, 'study_ocr_document_page');
      assert.strictEqual(args.image_data_url, 'data:image/jpeg;base64,AQID');
      state.ocrCalls += 1;
      state.events.push(`ocr:${{state.ocrCalls}}`);
      if (options.onOcr) return options.onOcr(state.ocrCalls);
      return ocrResults[state.ocrCalls - 1] || {{ status: 'empty', text: '' }};
    }},
  }});
  return {{ scanner, state }};
}}

async function extract(scanner, options = {{}}) {{
  return scanner.extract(
    {{ async arrayBuffer() {{ return Uint8Array.from([37, 80, 68, 70]).buffer; }} }},
    options,
  );
}}

async function expectCode(promise, code) {{
  await assert.rejects(promise, (error) => error && error.message === code);
}}

async function run() {{
  assert.strictEqual(api.shouldFallback('pdf', 'no_readable_text'), true);
  assert.strictEqual(api.shouldFallback('pdf', 'garbled_text'), true);
  assert.strictEqual(api.shouldFallback('pdf', 'invalid_pdf'), false);
  assert.strictEqual(api.shouldFallback('docx', 'no_readable_text'), false);
  assert.strictEqual(api.classifyPdfPageText(reliable), 'reliable-text');
  assert.strictEqual(api.classifyPdfPageText('x'), 'ocr-candidate');
  assert.strictEqual(api.selectPdfPageText('hidden x', ''), 'hidden x');
  assert.strictEqual(api.selectPdfPageText('hidden x', 'visible scan'), 'visible scan');
  const pageText = await api.extractPdfPageText({{
    async getTextContent() {{ return {{ items: [
      {{ str: 'first', hasEOL: true }},
      {{ str: 'second', hasEOL: false }},
    ] }}; }},
  }}, Date.now() + 1000);
  assert.strictEqual(pageText, 'first\\nsecond');
  const inlinePageText = await api.extractPdfPageText({{
    async getTextContent() {{ return {{ items: [
      {{ str: 'hello', hasEOL: false }},
      {{ str: 'world', hasEOL: false }},
    ] }}; }},
  }}, Date.now() + 1000);
  assert.strictEqual(inlinePageText, 'hello world');

  const normal = makeScanner([reliable, reliable, reliable]);
  const normalResult = await extract(normal.scanner);
  assert.strictEqual(normalResult.ocrPageCount, 0);
  assert.strictEqual(normalResult.textPageCount, 3);
  assert.strictEqual(normal.state.capabilityCalls, 0);
  assert.strictEqual(normal.state.ocrCalls, 0);
  assert.strictEqual(normal.state.events.some((event) => event.startsWith('render:')), false);

  const scan = makeScanner(['', '', ''], [
    {{ status: 'ok', text: 'alpha' }},
    {{ status: 'ok', text: 'beta' }},
    {{ status: 'ok', text: 'gamma' }},
  ]);
  const scanResult = await extract(scan.scanner);
  assert.strictEqual(scanResult.text, '# Page 1\\n\\nalpha\\n\\n# Page 2\\n\\nbeta\\n\\n# Page 3\\n\\ngamma');
  assert.strictEqual(scanResult.encoding, 'PDF OCR');
  assert.strictEqual(scanResult.ocrPageCount, 3);
  assert.strictEqual(scan.state.capabilityCalls, 1);
  assert.strictEqual(scan.state.ocrCalls, 3);
  assert.ok(scan.state.events.indexOf('capabilities') < scan.state.events.indexOf('render:1'));

  const hidden = makeScanner(['x'], [{{ status: 'ok', text: 'visible scan text' }}]);
  const hiddenResult = await extract(hidden.scanner);
  assert.strictEqual(hiddenResult.ocrPageCount, 1);
  assert.strictEqual(hiddenResult.text, '# Page 1\\n\\nvisible scan text');

  const mixedTexts = Array.from({{ length: 30 }}, (_, index) => [1, 7].includes(index) ? '' : `${{reliable}} ${{index + 1}}`);
  const mixed = makeScanner(mixedTexts, [
    {{ status: 'ok', text: 'scan page two' }},
    {{ status: 'ok', text: 'scan page eight' }},
  ]);
  const mixedResult = await extract(mixed.scanner);
  assert.strictEqual(mixedResult.encoding, 'PDF Hybrid');
  assert.strictEqual(mixedResult.ocrPageCount, 2);
  assert.strictEqual(mixedResult.textPageCount, 28);
  assert.strictEqual(JSON.stringify(mixedResult.ocrPages), '[2,8]');
  assert.ok(mixedResult.text.indexOf('# Page 2') < mixedResult.text.indexOf('# Page 8'));
  assert.strictEqual(mixed.state.ocrCalls, 2);

  const fallbackText = makeScanner(['x'], [{{ status: 'empty', text: '' }}]);
  const fallbackResult = await extract(fallbackText.scanner);
  assert.strictEqual(fallbackResult.text, '# Page 1\\n\\nx');
  assert.strictEqual(fallbackResult.textPageCount, 1);
  assert.strictEqual(fallbackResult.emptyPageCount, 0);

  const tooManyCandidates = makeScanner(Array.from({{ length: 21 }}, () => ''));
  await expectCode(extract(tooManyCandidates.scanner), 'document_pdf_ocr_too_many_pages');
  assert.strictEqual(tooManyCandidates.state.capabilityCalls, 0);
  assert.strictEqual(tooManyCandidates.state.ocrCalls, 0);

  const tooManyPages = makeScanner(Array.from({{ length: 41 }}, () => reliable));
  await expectCode(extract(tooManyPages.scanner), 'document_pdf_ocr_too_many_pages');

  const incompatible = makeScanner([''], [], {{ capabilities: {{ protocol: 2, ready: true }} }});
  await expectCode(extract(incompatible.scanner), 'document_pdf_ocr_unavailable');
  assert.strictEqual(incompatible.state.events.some((event) => event.startsWith('render:')), false);

  const canceled = new AbortController();
  const cancelScan = makeScanner(['', ''], [], {{
    onOcr() {{
      canceled.abort();
      return {{ status: 'ok', text: 'first' }};
    }},
  }});
  await assert.rejects(
    extract(cancelScan.scanner, {{ signal: canceled.signal }}),
    (error) => error.name === 'AbortError',
  );
  assert.strictEqual(cancelScan.state.ocrCalls, 1);

  const loadingCanceled = new AbortController();
  let loadingStarted;
  let loadingDestroyed = 0;
  const loadingStartedPromise = new Promise((resolve) => {{ loadingStarted = resolve; }});
  const loadingCancelScanner = api.create({{
    canvasFactory: () => makeCanvas({{ canvases: [], removed: 0 }}),
    loadPdfJs: async () => ({{
      GlobalWorkerOptions: {{}},
      getDocument() {{
        loadingStarted();
        return {{
          promise: new Promise(() => {{}}),
          async destroy() {{ loadingDestroyed += 1; }},
        }};
      }},
    }}),
    callCapabilities: async () => ({{ protocol: 1, ready: true, enabled: true }}),
    callPlugin: async () => ({{ status: 'ok', text: 'unused' }}),
  }});
  const loadingExtraction = extract(loadingCancelScanner, {{ signal: loadingCanceled.signal }});
  await loadingStartedPromise;
  loadingCanceled.abort();
  await Promise.race([
    assert.rejects(loadingExtraction, (error) => error.name === 'AbortError'),
    new Promise((_resolve, reject) => setTimeout(
      () => reject(new Error('loading cancellation did not settle promptly')),
      250,
    )),
  ]);
  assert.ok(loadingDestroyed >= 1);

  const truncate = makeScanner([''], [{{ status: 'ok', text: 'x'.repeat(33000) }}]);
  const truncated = await extract(truncate.scanner);
  assert.strictEqual(truncated.text.length, 32000);
  assert.strictEqual(truncated.truncated, true);

  const surrogate = makeScanner([''], [{{
    status: 'ok',
    text: 'x'.repeat(31989) + '\\ud83d\\ude00tail',
  }}]);
  const surrogateSafe = await extract(surrogate.scanner);
  assert.strictEqual(surrogateSafe.truncated, true);
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
