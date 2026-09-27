'use strict';

// Keep PDF coordinates, rather than window pixels, so resizing the sidebar
// doesn't change the part of the paper we return to.
const PaperPDF = (() => {
  let active = null;
  const prefix = 'paper-reading-position:';
  const zoomModes = new Set(['auto', 'page-width', 'page-height', 'page-fit']);

  function readPosition(key) {
    try {
      const value = JSON.parse(localStorage.getItem(key));
      if (!value || value.version !== 1 || !Number.isInteger(value.page) || value.page < 1 ||
          ![value.left, value.top].every(Number.isFinite) ||
          !(zoomModes.has(value.zoom) || Number.isFinite(value.zoom) && value.zoom > 0) ||
          ![0, 90, 180, 270].includes(value.rotation) ||
          ![0, 1, 2, 3].includes(value.scrollMode) || ![0, 1, 2].includes(value.spreadMode)) return null;
      return value;
    } catch { return null; }
  }

  function save(refresh = true) {
    const reader = active;
    if (!reader?.ready || !reader.frame.isConnected || reader.app.pdfViewer.isInPresentationMode) return;
    // A PDF opened with the viewer's own file picker isn't the library paper.
    if (new URL(reader.app.url, location.href).href !== new URL(reader.frame.dataset.documentUrl, location.href).href) return;
    // A paper can be switched before the viewer's next scroll animation frame.
    if (refresh) reader.app.pdfViewer.update();
    clearTimeout(reader.timer);
    if (!reader.location) return;
    const {pageNumber, scale, left, top, rotation} = reader.location;
    const viewer = reader.app.pdfViewer;
    try {
      localStorage.setItem(reader.key, JSON.stringify({version: 1,
        page: pageNumber, zoom: scale, left, top, rotation,
        scrollMode: viewer.scrollMode, spreadMode: viewer.spreadMode}));
    } catch { /* Reading still works when browser storage is unavailable. */ }
  }

  function close() {
    save();
    clearTimeout(active?.timer);
    active = null;
  }

  function prepare(frame, paper, url, page) {
    frame.dataset.positionKey = prefix + JSON.stringify([paper.project_id, paper.id, url]);
    frame.dataset.documentUrl = url;
    const params = new URLSearchParams({file: url});
    frame.src = '/static/vendor/pdfjs/web/viewer.html?' + params + (page ? '#page=' + page : '');
  }

  function jumpTo(url, page) {
    if (!active?.ready || active.frame.dataset.documentUrl !== url) return false;
    active.app.pdfLinkService.setHash('page=' + page);
    save();
    return true;
  }

  function setTheme() {
    if (active) active.frame.contentDocument.documentElement.style.colorScheme =
      document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light';
  }

  // The official viewer dispatches this event to its parent before initializing.
  // Configure it here; all vendored PDF.js files remain unmodified.
  document.addEventListener('webviewerloaded', event => {
    const frame = document.querySelector('#viewer-container iframe');
    const source = event.detail?.source;
    if (!frame?.dataset.positionKey || frame.contentWindow !== source) return;
    const app = source.PDFViewerApplication;
    const options = source.PDFViewerApplicationOptions;
    const stored = readPosition(frame.dataset.positionKey);
    const explicitPage = Boolean(app.initialBookmark);
    options.setAll({disablePreferences: true, disableHistory: true, viewOnLoad: 1,
      defaultZoomValue: 'page-width', sidebarViewOnLoad: 0,
      scrollModeOnLoad: stored?.scrollMode ?? 0, spreadModeOnLoad: stored?.spreadMode ?? 0,
      enableScripting: false, enableAltTextModelDownload: false, externalLinkTarget: 2,
      viewerCssTheme: document.documentElement.dataset.theme === 'dark' ? 2 : 1});
    // Explicit outline navigation takes precedence over the saved location.
    if (stored) {
      app.initialBookmark = app.initialBookmark
        ? app.initialBookmark + '&zoom=' + stored.zoom
        : `page=${stored.page}&zoom=${stored.zoom},${stored.left},${stored.top}`;
      app.initialRotation = stored.rotation;
    }
    const reader = active = {frame, app, key: frame.dataset.positionKey, ready: false, location: null, timer: null};
    app.initializedPromise.then(() => {
      if (active !== reader) return;
      app.eventBus.on('documentinit', async () => {
        reader.ready = false;
        // The viewer may adjust the initial view again after learning the sizes
        // of all pages. Its URL parser also rounds coordinates to integers.
        // Restore full-precision coordinates after those adjustments finish.
        try { await app.pdfViewer.pagesPromise; } catch { return; }
        source.requestAnimationFrame(() => {
          if (active !== reader) return;
          if (stored && !explicitPage) {
            app.pdfViewer.scrollPageIntoView({pageNumber: Math.min(stored.page, app.pagesCount),
              destArray: [null, {name: 'XYZ'}, stored.left, stored.top,
                typeof stored.zoom === 'number' ? stored.zoom / 100 : stored.zoom],
              allowNegativeOffset: true});
          }
          reader.ready = true;
          frame.dataset.readerReady = 'true';
          app.pdfViewer.update();
        });
      });
      app.eventBus.on('updateviewarea', ({location}) => {
        if (active !== reader || !reader.ready) return;
        reader.location = location;
        clearTimeout(reader.timer);
        reader.timer = setTimeout(() => { if (active === reader) save(false); }, 150);
      });
    });
  });

  new MutationObserver(setTheme).observe(document.documentElement, {attributes: true, attributeFilter: ['data-theme']});
  window.addEventListener('pagehide', () => save());
  window.addEventListener('beforeunload', () => save());
  document.addEventListener('visibilitychange', () => { if (document.hidden) save(); });
  return {prepare, jumpTo, close};
})();
