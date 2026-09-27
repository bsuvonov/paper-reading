Vendored official npm distributions (MIT licensed):

- @xterm/xterm 6.0.0
- @xterm/addon-fit 0.11.0

Downloaded from registry.npmjs.org and verified against npm SHA-512 integrity metadata.
License files are included. No CDN or npm install is needed to run the app.

PDF reader: the unmodified Mozilla PDF.js **6.3.289 legacy** distribution
(Apache-2.0; license included in `pdfjs/LICENSE`). The legacy build includes
compatibility polyfills. Source maps and the sample PDF are omitted.

- Release: https://github.com/mozilla/pdf.js/releases/tag/v6.3.289
- Archive: https://github.com/mozilla/pdf.js/releases/download/v6.3.289/pdfjs-6.3.289-legacy-dist.zip
- Archive SHA-256: `51683fac4aff7dd31ed91e9ab735a2098a78d50899d1ec529aed6dc8aa19400d`

The app configures the viewer through its `webviewerloaded` event in
`web/pdf-reader.js`; no external service is used to display PDFs or save positions.

The position regression check uses real PDFs in a temporary library:
`node tests/pdf_reader_browser.cjs` (install Playwright and its Chromium browser
for this optional check; neither is needed to run the app).
