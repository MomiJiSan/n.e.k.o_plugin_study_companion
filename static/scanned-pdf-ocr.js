(function initializeScannedPdfOcr() {
  'use strict';

  const currentScriptUrl = String(document.currentScript?.src || '');
  const uiBaseUrl = currentScriptUrl
    ? currentScriptUrl.slice(0, currentScriptUrl.lastIndexOf('/') + 1)
    : '/plugin/study_companion/ui/';
  const PDFJS_URL = `${uiBaseUrl}pdfjs/pdf.mjs`;
  const PDFJS_WORKER_URL = `${uiBaseUrl}pdfjs/pdf.worker.mjs`;
  const PDFJS_WASM_URL = `${uiBaseUrl}pdfjs/wasm/`;
  const PDFJS_ICC_URL = `${uiBaseUrl}pdfjs/iccs/`;
  const MAX_PAGES = 20;
  const TARGET_DPI = 200;
  const MAX_LONG_EDGE_PX = 2600;
  const MAX_PAGE_PIXELS = 8_000_000;
  const MAX_JPEG_BYTES = 6 * 1024 * 1024;
  const MAX_TEXT_CHARS = 32_000;
  const PAGE_TIMEOUT_MS = 45_000;
  const TOTAL_TIMEOUT_MS = 5 * 60_000;
  const JPEG_QUALITIES = Object.freeze([0.88, 0.76, 0.64, 0.52, 0.4, 0.28, 0.18]);
  const OCR_DIAGNOSTICS = new Set([
    'document_pdf_ocr_disabled',
    'document_pdf_ocr_unavailable',
    'document_pdf_page_too_large',
    'document_pdf_ocr_timeout',
    'document_pdf_ocr_failed',
  ]);

  class ScannedPdfError extends Error {
    constructor(code) {
      super(code);
      this.name = 'ScannedPdfError';
    }
  }

  function abortError() {
    return new DOMException('Aborted', 'AbortError');
  }

  function throwIfAborted(signal) {
    if (signal?.aborted) throw abortError();
  }

  function timeLeft(deadline) {
    return Math.max(0, deadline - Date.now());
  }

  function throwIfExpired(deadline) {
    if (timeLeft(deadline) <= 0) throw new ScannedPdfError('document_pdf_ocr_timeout');
  }

  async function waitWithinDeadline(promise, deadline, onTimeout) {
    const timeoutMs = timeLeft(deadline);
    if (timeoutMs <= 0) throw new ScannedPdfError('document_pdf_ocr_timeout');
    let timeout;
    try {
      return await Promise.race([
        Promise.resolve(promise),
        new Promise((_, reject) => {
          timeout = setTimeout(() => {
            try { onTimeout?.(); } catch (_error) {}
            reject(new ScannedPdfError('document_pdf_ocr_timeout'));
          }, timeoutMs);
        }),
      ]);
    } finally {
      clearTimeout(timeout);
    }
  }

  async function loadPdfJs() {
    return import(PDFJS_URL);
  }

  function createCanvas() {
    return document.createElement('canvas');
  }

  function canvasToBlob(canvas, quality) {
    return new Promise((resolve, reject) => {
      canvas.toBlob((blob) => {
        if (blob) resolve(blob);
        else reject(new ScannedPdfError('document_pdf_render_failed'));
      }, 'image/jpeg', quality);
    });
  }

  async function encodeJpeg(canvas, deadline) {
    for (const quality of JPEG_QUALITIES) {
      throwIfExpired(deadline);
      const blob = await waitWithinDeadline(canvasToBlob(canvas, quality), deadline);
      if (blob.size <= MAX_JPEG_BYTES) return blob;
    }
    throw new ScannedPdfError('document_pdf_page_too_large');
  }

  async function blobToBase64(blob, deadline) {
    throwIfExpired(deadline);
    let buffer = await waitWithinDeadline(blob.arrayBuffer(), deadline);
    let bytes = new Uint8Array(buffer);
    const chunks = [];
    const chunkSize = 0x8000;
    for (let offset = 0; offset < bytes.length; offset += chunkSize) {
      throwIfExpired(deadline);
      chunks.push(String.fromCharCode(...bytes.subarray(offset, offset + chunkSize)));
    }
    const encoded = btoa(chunks.join(''));
    chunks.length = 0;
    bytes = null;
    buffer = null;
    return encoded;
  }

  function pageScale(page) {
    const baseViewport = page.getViewport({ scale: 1 });
    const width = Number(baseViewport?.width);
    const height = Number(baseViewport?.height);
    if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) {
      throw new ScannedPdfError('document_pdf_render_failed');
    }
    const dpiScale = TARGET_DPI / 72;
    const edgeScale = MAX_LONG_EDGE_PX / Math.max(width, height);
    const pixelScale = Math.sqrt(MAX_PAGE_PIXELS / (width * height));
    return Math.min(dpiScale, edgeScale, pixelScale);
  }

  function normalizePageResult(payload) {
    const diagnostic = String(payload?.diagnostic || payload?.error?.code || '').trim();
    const status = String(payload?.status || '').trim().toLowerCase();
    if (diagnostic === 'no_readable_text' || status === 'empty') return '';
    if (OCR_DIAGNOSTICS.has(diagnostic)) throw new ScannedPdfError(diagnostic);
    if (['disabled', 'unavailable', 'failed', 'ocr_failed', 'error', 'timeout'].includes(status)) {
      const statusCode = status === 'disabled' ? 'document_pdf_ocr_disabled'
        : status === 'unavailable' ? 'document_pdf_ocr_unavailable'
          : status === 'timeout' ? 'document_pdf_ocr_timeout'
            : 'document_pdf_ocr_failed';
      throw new ScannedPdfError(statusCode);
    }
    if (typeof payload?.text !== 'string') {
      throw new ScannedPdfError('document_pdf_ocr_failed');
    }
    return payload.text.trim();
  }

  function shouldFallback(sourceType, parseCode) {
    return sourceType === 'pdf' && parseCode === 'no_readable_text';
  }

  function appendPageText(current, pageNumber, pageText) {
    if (!pageText) return { text: current, truncated: false };
    const chunk = `${current ? '\n\n' : ''}# Page ${pageNumber}\n\n${pageText}`;
    const available = MAX_TEXT_CHARS - current.length;
    if (chunk.length <= available) return { text: current + chunk, truncated: false };
    return { text: current + chunk.slice(0, Math.max(0, available)), truncated: true };
  }

  async function destroyPdf(pdfDocument, loadingTask, deadline) {
    try {
      const destroy = pdfDocument?.destroy
        ? pdfDocument.destroy()
        : loadingTask?.destroy?.();
      if (destroy) await waitWithinDeadline(destroy, deadline);
    } catch (_error) {}
  }

  function create(options = {}) {
    const callPlugin = options.callPlugin;
    const pdfJsLoader = options.loadPdfJs || loadPdfJs;
    const canvasFactory = options.canvasFactory || createCanvas;
    if (typeof callPlugin !== 'function') throw new TypeError('callPlugin is required');

    async function extract(file, { signal, onProgress } = {}) {
      const deadline = Date.now() + TOTAL_TIMEOUT_MS;
      let sourceBuffer = null;
      let pdfData = null;
      let loadingTask = null;
      let pdfDocument = null;
      try {
        throwIfAborted(signal);
        const pdfjs = await waitWithinDeadline(pdfJsLoader(), deadline);
        if (!pdfjs?.getDocument || !pdfjs?.GlobalWorkerOptions) {
          throw new ScannedPdfError('document_pdf_render_failed');
        }
        pdfjs.GlobalWorkerOptions.workerSrc = PDFJS_WORKER_URL;
        sourceBuffer = await waitWithinDeadline(file.arrayBuffer(), deadline);
        pdfData = new Uint8Array(sourceBuffer);
        sourceBuffer = null;
        loadingTask = pdfjs.getDocument({
          data: pdfData,
          wasmUrl: PDFJS_WASM_URL,
          iccUrl: PDFJS_ICC_URL,
        });
        pdfDocument = await waitWithinDeadline(
          loadingTask.promise,
          deadline,
          () => loadingTask?.destroy?.(),
        );
        const pageCount = Number(pdfDocument?.numPages);
        if (!Number.isInteger(pageCount) || pageCount <= 0) {
          throw new ScannedPdfError('document_pdf_render_failed');
        }
        if (pageCount > MAX_PAGES) {
          throw new ScannedPdfError('document_pdf_ocr_too_many_pages');
        }
        throwIfAborted(signal);
        onProgress?.({ page: 0, completed: 0, total: pageCount, progress: 0 });

        let text = '';
        let truncated = false;
        for (let pageNumber = 1; pageNumber <= pageCount && !truncated; pageNumber += 1) {
          throwIfAborted(signal);
          throwIfExpired(deadline);
          onProgress?.({
            page: pageNumber,
            completed: pageNumber - 1,
            total: pageCount,
            progress: (pageNumber - 1) / pageCount,
          });
          let page = null;
          let canvas = null;
          let renderTask = null;
          let imageBase64 = '';
          try {
            page = await waitWithinDeadline(pdfDocument.getPage(pageNumber), deadline);
            const viewport = page.getViewport({ scale: pageScale(page) });
            const width = Math.max(1, Math.round(viewport.width));
            const height = Math.max(1, Math.round(viewport.height));
            if (
              Math.max(width, height) > MAX_LONG_EDGE_PX
              || width * height > MAX_PAGE_PIXELS
            ) {
              throw new ScannedPdfError('document_pdf_page_too_large');
            }
            canvas = canvasFactory();
            canvas.width = width;
            canvas.height = height;
            const context = canvas.getContext('2d', { alpha: false });
            if (!context) throw new ScannedPdfError('document_pdf_render_failed');
            context.save?.();
            context.fillStyle = '#fff';
            context.fillRect?.(0, 0, width, height);
            context.restore?.();
            renderTask = page.render({ canvasContext: context, viewport });
            await waitWithinDeadline(
              renderTask.promise,
              deadline,
              () => renderTask?.cancel?.(),
            );
            const jpeg = await encodeJpeg(canvas, deadline);
            imageBase64 = await blobToBase64(jpeg, deadline);
          } catch (error) {
            if (error instanceof ScannedPdfError) throw error;
            throw new ScannedPdfError('document_pdf_render_failed');
          } finally {
            page?.cleanup?.();
            if (canvas) {
              canvas.width = 0;
              canvas.height = 0;
              canvas.remove?.();
            }
          }

          const pageDeadline = Math.min(deadline, Date.now() + PAGE_TIMEOUT_MS);
          const pageController = new AbortController();
          let pageTimedOut = false;
          let result;
          try {
            result = await waitWithinDeadline(
              callPlugin('study_ocr_document_page', {
                image_data_url: `data:image/jpeg;base64,${imageBase64}`,
              }, pageController.signal),
              pageDeadline,
              () => {
                pageTimedOut = true;
                pageController.abort();
              },
            );
          } catch (error) {
            if (signal?.aborted) throw abortError();
            if (pageTimedOut || timeLeft(deadline) <= 0 || /timed?\s*out|timeout/i.test(String(error?.message || ''))) {
              throw new ScannedPdfError('document_pdf_ocr_timeout');
            }
            const diagnostic = String(error?.message || '').trim();
            if (OCR_DIAGNOSTICS.has(diagnostic)) throw new ScannedPdfError(diagnostic);
            throw new ScannedPdfError('document_pdf_ocr_failed');
          } finally {
            imageBase64 = '';
          }
          throwIfAborted(signal);
          const appended = appendPageText(text, pageNumber, normalizePageResult(result));
          text = appended.text;
          truncated = appended.truncated;
          onProgress?.({
            page: pageNumber,
            completed: pageNumber,
            total: pageCount,
            progress: pageNumber / pageCount,
          });
        }
        if (!text.trim()) throw new ScannedPdfError('no_readable_text');
        return { text, encoding: 'PDF OCR', truncated, pageCount };
      } catch (error) {
        if (signal?.aborted || error?.name === 'AbortError') throw abortError();
        if (error instanceof ScannedPdfError) throw error;
        throw new ScannedPdfError('document_pdf_render_failed');
      } finally {
        await destroyPdf(pdfDocument, loadingTask, deadline);
        pdfDocument = null;
        loadingTask = null;
        pdfData = null;
        sourceBuffer = null;
      }
    }

    return Object.freeze({ extract, shouldFallback });
  }

  window.StudyScannedPdfOcr = Object.freeze({ create, shouldFallback });
}());
