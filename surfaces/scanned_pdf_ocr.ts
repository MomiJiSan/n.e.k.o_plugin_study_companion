export const SCANNED_PDF_OCR_LIMITS = Object.freeze({
  maxPages: 20,
  targetDpi: 200,
  maxLongEdgePx: 2600,
  maxPagePixels: 8_000_000,
  maxJpegBytes: 6 * 1024 * 1024,
  maxTextChars: 32_000,
  pageTimeoutMs: 45_000,
  totalTimeoutMs: 5 * 60_000,
});

const JPEG_QUALITIES = Object.freeze([0.88, 0.76, 0.64, 0.52, 0.4, 0.28, 0.18]);
const OCR_DIAGNOSTICS = new Set([
  'document_pdf_ocr_disabled',
  'document_pdf_ocr_unavailable',
  'document_pdf_page_too_large',
  'document_pdf_ocr_timeout',
  'document_pdf_ocr_failed',
]);

export type ScannedPdfOcrProgress = {
  page: number;
  completed: number;
  total: number;
  progress: number;
};

export type ScannedPdfOcrResult = {
  text: string;
  encoding: 'PDF OCR';
  truncated: boolean;
  pageCount: number;
};

type PageOcrPayload = {
  text?: unknown;
  status?: unknown;
  diagnostic?: unknown;
  error?: { code?: unknown } | unknown;
};

type PdfViewport = { width: number; height: number };
type PdfPage = {
  getViewport: (options: { scale: number }) => PdfViewport;
  render: (options: { canvasContext: CanvasRenderingContext2D; viewport: PdfViewport }) => {
    promise: Promise<unknown>;
    cancel?: () => void;
  };
  cleanup?: () => void;
};
type PdfDocument = {
  numPages: number;
  getPage: (pageNumber: number) => Promise<PdfPage>;
  destroy?: () => Promise<unknown> | unknown;
};
type PdfLoadingTask = {
  promise: Promise<PdfDocument>;
  destroy?: () => Promise<unknown> | unknown;
};
type PdfJs = {
  GlobalWorkerOptions: { workerSrc?: string; workerPort?: Worker | null };
  getDocument: (options: {
    data: Uint8Array;
    wasmUrl: string;
    iccUrl: string;
  }) => PdfLoadingTask;
};

type PdfJsGlobal = typeof globalThis & {
  __studyCompanionPdfJs?: PdfJs;
  __studyCompanionCreatePdfWorker?: () => { worker: Worker; url: string };
};

type ScannedPdfOcrDependencies = {
  callPageOcr: (args: { image_data_url: string }, signal: AbortSignal) => Promise<PageOcrPayload>;
  assetBaseUrl: string;
  loadPdfJs?: () => Promise<PdfJs>;
  createCanvas?: () => HTMLCanvasElement;
  now?: () => number;
  setTimer?: (callback: () => void, timeoutMs: number) => ReturnType<typeof setTimeout>;
  clearTimer?: (timer: ReturnType<typeof setTimeout>) => void;
};

type ExtractOptions = {
  signal?: AbortSignal;
  onProgress?: (progress: ScannedPdfOcrProgress) => void;
};

export class ScannedPdfOcrError extends Error {
  readonly code: string;

  constructor(code: string) {
    super(code);
    this.name = 'ScannedPdfOcrError';
    this.code = code;
  }
}

export function shouldFallbackToScannedPdfOcr(sourceType: unknown, error: unknown) {
  const candidate = error as { code?: unknown; message?: unknown } | null;
  const parseCode = String(candidate?.code || candidate?.message || '').trim();
  return sourceType === 'pdf' && parseCode === 'no_readable_text';
}

function abortError() {
  return new DOMException('Aborted', 'AbortError');
}

function throwIfAborted(signal?: AbortSignal) {
  if (signal?.aborted) throw abortError();
}

let localPdfJsLoadPromise: Promise<PdfJs> | null = null;

function loadLocalPdfJs(assetBaseUrl: string): Promise<PdfJs> {
  const runtime = globalThis as PdfJsGlobal;
  if (runtime.__studyCompanionPdfJs) return Promise.resolve(runtime.__studyCompanionPdfJs);
  if (localPdfJsLoadPromise) return localPdfJsLoadPromise;

  const loaderUrl = `${assetBaseUrl}pdf.hosted.js`;
  localPdfJsLoadPromise = new Promise<PdfJs>((resolve, reject) => {
    const script = document.createElement('script');
    script.src = loaderUrl;
    script.dataset.studyCompanionPdfJsLoader = 'true';
    script.onload = () => {
      if (runtime.__studyCompanionPdfJs) resolve(runtime.__studyCompanionPdfJs);
      else reject(new ScannedPdfOcrError('document_pdf_render_failed'));
    };
    script.onerror = () => reject(new ScannedPdfOcrError('document_pdf_render_failed'));
    document.head.appendChild(script);
  }).catch((error) => {
    localPdfJsLoadPromise = null;
    throw error;
  });
  return localPdfJsLoadPromise;
}

function defaultCanvasFactory() {
  return document.createElement('canvas');
}

function canvasToBlob(canvas: HTMLCanvasElement, quality: number) {
  return new Promise<Blob>((resolve, reject) => {
    canvas.toBlob((blob) => {
      if (blob) resolve(blob);
      else reject(new ScannedPdfOcrError('document_pdf_render_failed'));
    }, 'image/jpeg', quality);
  });
}

function pageScale(page: PdfPage) {
  const viewport = page.getViewport({ scale: 1 });
  const width = Number(viewport?.width);
  const height = Number(viewport?.height);
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) {
    throw new ScannedPdfOcrError('document_pdf_render_failed');
  }
  const dpiScale = SCANNED_PDF_OCR_LIMITS.targetDpi / 72;
  const edgeScale = SCANNED_PDF_OCR_LIMITS.maxLongEdgePx / Math.max(width, height);
  const pixelScale = Math.sqrt(SCANNED_PDF_OCR_LIMITS.maxPagePixels / (width * height));
  return Math.min(dpiScale, edgeScale, pixelScale);
}

function normalizePageResult(payload: PageOcrPayload) {
  const nestedError = payload?.error && typeof payload.error === 'object'
    ? (payload.error as { code?: unknown }).code
    : '';
  const diagnostic = String(payload?.diagnostic || nestedError || '').trim();
  const status = String(payload?.status || '').trim().toLowerCase();
  if (diagnostic === 'no_readable_text' || status === 'empty') return '';
  if (OCR_DIAGNOSTICS.has(diagnostic)) throw new ScannedPdfOcrError(diagnostic);
  if (['disabled', 'unavailable', 'failed', 'ocr_failed', 'error', 'timeout'].includes(status)) {
    const code = status === 'disabled' ? 'document_pdf_ocr_disabled'
      : status === 'unavailable' ? 'document_pdf_ocr_unavailable'
        : status === 'timeout' ? 'document_pdf_ocr_timeout'
          : 'document_pdf_ocr_failed';
    throw new ScannedPdfOcrError(code);
  }
  if (typeof payload?.text !== 'string') {
    throw new ScannedPdfOcrError('document_pdf_ocr_failed');
  }
  return payload.text.trim();
}

function appendPageText(current: string, pageNumber: number, pageText: string) {
  if (!pageText) return { text: current, truncated: false };
  const chunk = `${current ? '\n\n' : ''}# Page ${pageNumber}\n\n${pageText}`;
  const available = SCANNED_PDF_OCR_LIMITS.maxTextChars - current.length;
  if (chunk.length <= available) return { text: current + chunk, truncated: false };
  return {
    text: current + chunk.slice(0, Math.max(0, available)),
    truncated: true,
  };
}

export function createScannedPdfOcrController(dependencies: ScannedPdfOcrDependencies) {
  if (typeof dependencies?.callPageOcr !== 'function') {
    throw new TypeError('callPageOcr is required');
  }
  const assetBaseUrl = String(dependencies.assetBaseUrl || '');
  if (!/^\/plugin\/[A-Za-z0-9._~-]+\/ui\/pdfjs\/$/.test(assetBaseUrl)) {
    throw new TypeError('A local PDF.js asset base URL is required');
  }
  const loadPdfJs = dependencies.loadPdfJs || (() => loadLocalPdfJs(assetBaseUrl));
  const createCanvas = dependencies.createCanvas || defaultCanvasFactory;
  const now = dependencies.now || Date.now;
  const setTimer = dependencies.setTimer || ((callback, timeoutMs) => setTimeout(callback, timeoutMs));
  const clearTimer = dependencies.clearTimer || ((timer) => clearTimeout(timer));

  function remaining(deadline: number) {
    return Math.max(0, deadline - now());
  }

  function throwIfExpired(deadline: number) {
    if (remaining(deadline) <= 0) {
      throw new ScannedPdfOcrError('document_pdf_ocr_timeout');
    }
  }

  async function withinDeadline<T>(
    promise: Promise<T> | T,
    deadline: number,
    onTimeout?: () => void,
  ): Promise<T> {
    const timeoutMs = remaining(deadline);
    if (timeoutMs <= 0) throw new ScannedPdfOcrError('document_pdf_ocr_timeout');
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      return await Promise.race([
        Promise.resolve(promise),
        new Promise<T>((_resolve, reject) => {
          timer = setTimer(() => {
            try {
              onTimeout?.();
            } catch {
              // Timeout diagnostics must not be replaced by cleanup failures.
            }
            reject(new ScannedPdfOcrError('document_pdf_ocr_timeout'));
          }, timeoutMs);
        }),
      ]);
    } finally {
      if (timer !== undefined) clearTimer(timer);
    }
  }

  async function encodeJpeg(canvas: HTMLCanvasElement, deadline: number) {
    for (const quality of JPEG_QUALITIES) {
      throwIfExpired(deadline);
      const blob = await withinDeadline(canvasToBlob(canvas, quality), deadline);
      if (blob.size <= SCANNED_PDF_OCR_LIMITS.maxJpegBytes) return blob;
    }
    throw new ScannedPdfOcrError('document_pdf_page_too_large');
  }

  async function blobToBase64(blob: Blob, deadline: number) {
    throwIfExpired(deadline);
    const buffer = await withinDeadline(blob.arrayBuffer(), deadline);
    const bytes = new Uint8Array(buffer);
    const chunks: string[] = [];
    for (let offset = 0; offset < bytes.length; offset += 0x8000) {
      throwIfExpired(deadline);
      chunks.push(String.fromCharCode(...bytes.subarray(offset, offset + 0x8000)));
    }
    const encoded = btoa(chunks.join(''));
    chunks.length = 0;
    return encoded;
  }

  async function destroyPdf(
    pdfDocument: PdfDocument | null,
    loadingTask: PdfLoadingTask | null,
    deadline: number,
  ) {
    try {
      const destroy = pdfDocument?.destroy
        ? pdfDocument.destroy()
        : loadingTask?.destroy?.();
      if (destroy) await withinDeadline(destroy, deadline);
    } catch {
      // Cleanup failures must not mask the import result.
    }
  }

  async function extract(file: File, options: ExtractOptions = {}): Promise<ScannedPdfOcrResult> {
    const { signal, onProgress } = options;
    const deadline = now() + SCANNED_PDF_OCR_LIMITS.totalTimeoutMs;
    let sourceBuffer: ArrayBuffer | null = null;
    let pdfData: Uint8Array | null = null;
    let loadingTask: PdfLoadingTask | null = null;
    let pdfDocument: PdfDocument | null = null;
    let workerLease: { worker: Worker; url: string } | null = null;
    try {
      throwIfAborted(signal);
      const pdfjs = await withinDeadline(loadPdfJs(), deadline);
      if (!pdfjs?.getDocument || !pdfjs?.GlobalWorkerOptions) {
        throw new ScannedPdfOcrError('document_pdf_render_failed');
      }
      const runtime = globalThis as PdfJsGlobal;
      if (typeof runtime.__studyCompanionCreatePdfWorker === 'function') {
        workerLease = runtime.__studyCompanionCreatePdfWorker();
        pdfjs.GlobalWorkerOptions.workerPort = workerLease.worker;
      } else {
        pdfjs.GlobalWorkerOptions.workerSrc = `${assetBaseUrl}pdf.worker.mjs`;
      }
      sourceBuffer = await withinDeadline(file.arrayBuffer(), deadline);
      pdfData = new Uint8Array(sourceBuffer);
      sourceBuffer = null;
      loadingTask = pdfjs.getDocument({
        data: pdfData,
        wasmUrl: `${assetBaseUrl}wasm/`,
        iccUrl: `${assetBaseUrl}iccs/`,
      });
      pdfDocument = await withinDeadline(
        loadingTask.promise,
        deadline,
        () => { void loadingTask?.destroy?.(); },
      );
      const pageCount = Number(pdfDocument?.numPages);
      if (!Number.isInteger(pageCount) || pageCount <= 0) {
        throw new ScannedPdfOcrError('document_pdf_render_failed');
      }
      if (pageCount > SCANNED_PDF_OCR_LIMITS.maxPages) {
        throw new ScannedPdfOcrError('document_pdf_ocr_too_many_pages');
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
        let page: PdfPage | null = null;
        let canvas: HTMLCanvasElement | null = null;
        let renderTask: ReturnType<PdfPage['render']> | null = null;
        let imageBase64 = '';
        try {
          page = await withinDeadline(pdfDocument.getPage(pageNumber), deadline);
          const viewport = page.getViewport({ scale: pageScale(page) });
          const width = Math.max(1, Math.round(viewport.width));
          const height = Math.max(1, Math.round(viewport.height));
          if (
            Math.max(width, height) > SCANNED_PDF_OCR_LIMITS.maxLongEdgePx
            || width * height > SCANNED_PDF_OCR_LIMITS.maxPagePixels
          ) {
            throw new ScannedPdfOcrError('document_pdf_page_too_large');
          }
          canvas = createCanvas();
          canvas.width = width;
          canvas.height = height;
          const context = canvas.getContext('2d', { alpha: false });
          if (!context) throw new ScannedPdfOcrError('document_pdf_render_failed');
          context.save();
          context.fillStyle = '#fff';
          context.fillRect(0, 0, width, height);
          context.restore();
          renderTask = page.render({ canvasContext: context, viewport });
          await withinDeadline(renderTask.promise, deadline, () => renderTask?.cancel?.());
          const jpeg = await encodeJpeg(canvas, deadline);
          imageBase64 = await blobToBase64(jpeg, deadline);
        } catch (error) {
          if (error instanceof ScannedPdfOcrError) throw error;
          throw new ScannedPdfOcrError('document_pdf_render_failed');
        } finally {
          page?.cleanup?.();
          if (canvas) {
            canvas.width = 0;
            canvas.height = 0;
            canvas.remove();
          }
        }

        const pageDeadline = Math.min(deadline, now() + SCANNED_PDF_OCR_LIMITS.pageTimeoutMs);
        const pageController = new AbortController();
        let pageTimedOut = false;
        let result: PageOcrPayload;
        try {
          result = await withinDeadline(
            dependencies.callPageOcr(
              { image_data_url: `data:image/jpeg;base64,${imageBase64}` },
              pageController.signal,
            ),
            pageDeadline,
            () => {
              pageTimedOut = true;
              pageController.abort();
            },
          );
        } catch (error) {
          if (signal?.aborted) throw abortError();
          const candidate = error as { code?: unknown; message?: unknown } | null;
          const diagnostic = String(candidate?.code || candidate?.message || '').trim();
          if (pageTimedOut || remaining(deadline) <= 0 || /timed?\s*out|timeout/i.test(diagnostic)) {
            throw new ScannedPdfOcrError('document_pdf_ocr_timeout');
          }
          if (OCR_DIAGNOSTICS.has(diagnostic)) throw new ScannedPdfOcrError(diagnostic);
          throw new ScannedPdfOcrError('document_pdf_ocr_failed');
        } finally {
          imageBase64 = '';
        }
        throwIfAborted(signal);
        throwIfExpired(deadline);
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
      throwIfExpired(deadline);
      if (!text.trim()) throw new ScannedPdfOcrError('no_readable_text');
      return { text, encoding: 'PDF OCR', truncated, pageCount };
    } catch (error) {
      if (signal?.aborted || (error as { name?: unknown } | null)?.name === 'AbortError') {
        throw abortError();
      }
      if (error instanceof ScannedPdfOcrError) throw error;
      throw new ScannedPdfOcrError('document_pdf_render_failed');
    } finally {
      await destroyPdf(pdfDocument, loadingTask, deadline);
      const pdfjs = (globalThis as PdfJsGlobal).__studyCompanionPdfJs;
      if (workerLease) {
        if (pdfjs?.GlobalWorkerOptions.workerPort === workerLease.worker) {
          pdfjs.GlobalWorkerOptions.workerPort = null;
        }
        workerLease.worker.terminate();
        URL.revokeObjectURL(workerLease.url);
      }
      pdfDocument = null;
      loadingTask = null;
      workerLease = null;
      pdfData = null;
      sourceBuffer = null;
    }
  }

  return Object.freeze({ extract });
}

export function scannedPdfAssetBaseUrl(pluginId: unknown) {
  const normalized = String(pluginId || '').trim();
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(normalized)) {
    throw new ScannedPdfOcrError('document_pdf_render_failed');
  }
  return `/plugin/${encodeURIComponent(normalized)}/ui/pdfjs/`;
}
