// Run: node tests/pdf_reader_browser.cjs (requires Playwright and Chromium).
// Optional: PLAYWRIGHT_MODULE, CHROMIUM_PATH, PYTHON.
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {once} = require('node:events');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const fixture = `
import json, signal, tempfile
from pathlib import Path
import pymupdf
from werkzeug.serving import make_server
from web_app import Library, create_app, run_worker
from projects import ProjectStore, save_pdf
def stop(*args):
    raise KeyboardInterrupt
signal.signal(signal.SIGTERM, stop)
with tempfile.TemporaryDirectory(prefix='paper-reading-position-') as directory:
    root = Path(directory)
    store = ProjectStore(root)
    project = store.create('Position test')
    other = store.create('Other project')
    for title, ident in [('Paper A', project['id']), ('Paper B', project['id']), ('Paper C', other['id'])]:
        library = Library(root, ident)
        with pymupdf.open() as pdf:
            for number in range(10):
                page = pdf.new_page(width=612, height=792 if number % 2 == 0 else 900)
                page.insert_text((72, 72), title + ' - page ' + str(number + 1))
                for line in range(25):
                    page.insert_text((72, 120 + line * 22), 'A paragraph to read at position ' + str(line + 1))
            pdf.set_toc([[1, 'Introduction', 1], [1, 'Evaluation', 5], [1, 'Conclusion', 9]])
            save_pdf(library, pdf.tobytes(), title, 'upload:' + title)
    library = Library(root, project['id'])
    run_worker(root, dict(action='outline', project_id=project['id'], paper_id=library.catalog()[0]['id']))
    server = make_server('127.0.0.1', 0, create_app(root), threaded=True)
    print('READY ' + json.dumps(dict(port=server.server_port, project=project['id'], other=other['id'])), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
`;

(async () => {
  const server = spawn(process.env.PYTHON || 'python3', ['-u', '-c', fixture], {
    cwd: path.resolve(__dirname, '..'), stdio: ['ignore', 'pipe', 'pipe'],
  });
  let browser;
  let log = '';
  server.stderr.on('data', data => { log += data; });
  try {
    const info = await new Promise((resolve, reject) => {
      let output = '';
      const timeout = setTimeout(() => reject(new Error('Fixture did not start: ' + log)), 15000);
      server.stdout.on('data', data => {
        output += data;
        const match = output.match(/READY (.*)\n/);
        if (match) { clearTimeout(timeout); resolve(JSON.parse(match[1])); }
      });
      server.on('error', reject);
      server.on('exit', code => { clearTimeout(timeout); reject(new Error('Fixture exited: ' + code + log)); });
    });
    browser = await chromium.launch({headless: true,
      executablePath: process.env.CHROMIUM_PATH || undefined, args: ['--no-sandbox']});
    const page = await browser.newPage({viewport: {width: 1400, height: 900}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(`http://127.0.0.1:${info.port}`);
    await page.locator(`#project option[value="${info.project}"]`).waitFor({state: 'attached'});
    await page.selectOption('#project', info.project);

    async function viewer(title) {
      await page.locator(`.paper-card:has-text("${title}")`).click();
      const frameElement = await page.locator(`#viewer-container iframe[title="${title}"]`).elementHandle();
      const frame = await frameElement.contentFrame();
      await page.waitForFunction(() => {
        const frame = document.querySelector('#viewer-container iframe');
        return frame?.dataset.readerReady === 'true';
      });
      await frame.evaluate(async () => {
        await PDFViewerApplication.pdfViewer.pagesPromise;
        await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      });
      return frame;
    }
    async function position(frame) {
      return frame.evaluate(() => {
        PDFViewerApplication.pdfViewer.update();
        return {...PDFViewerApplication.pdfViewer._location};
      });
    }
    async function move(frame, hash, rotation = 0) {
      await frame.evaluate(({hash, rotation}) => {
        PDFViewerApplication.pdfViewer.pagesRotation = rotation;
        PDFViewerApplication.pdfLinkService.setHash(hash);
      }, {hash, rotation});
      await frame.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      return position(frame);
    }
    async function check(frame, expected) {
      const actual = await position(frame);
      assert.equal(actual.pageNumber, expected.pageNumber);
      assert.equal(actual.scale, expected.scale);
      assert.equal(actual.rotation, expected.rotation);
      // The fixed CSS gutter occupies different PDF units at fit-to-width zoom.
      const left = value => expected.scale === 'page-width' ? Math.max(0, value) : value;
      assert.ok(Math.abs(left(actual.left) - left(expected.left)) < 1.5, JSON.stringify({actual, expected}));
      assert.ok(Math.abs(actual.top - expected.top) < 1.5, JSON.stringify({actual, expected}));
    }

    let a = await viewer('Paper A');
    const aPosition = await move(a, 'page=5&zoom=175,140,420');
    let b = await viewer('Paper B');
    assert.equal((await position(b)).pageNumber, 1);
    const bPosition = await move(b, 'page=3&zoom=150,170,360', 90);
    a = await viewer('Paper A');
    await check(a, aPosition);
    b = await viewer('Paper B');
    await check(b, bPosition);

    await page.reload();
    await page.locator('#viewer-container iframe').waitFor();
    b = await viewer('Paper B');
    await check(b, bPosition);
    await page.selectOption('#project', info.other);
    await viewer('Paper C');
    await page.selectOption('#project', info.project);
    b = await viewer('Paper B');
    await check(b, bPosition);

    // A different reader width should preserve PDF coordinates, not scroll pixels.
    await page.setViewportSize({width: 1250, height: 950});
    a = await viewer('Paper A');
    await check(a, aPosition);
    await page.click('#outline-tab');
    await page.locator('.section-link:has-text("Conclusion")').click();
    await page.waitForFunction(() => document.querySelector('#viewer-container iframe').contentWindow.PDFViewerApplication.page === 9);
    await page.click('#papers-tab');
    const outlinePosition = await position(a);
    await viewer('Paper B');
    a = await viewer('Paper A');
    await check(a, outlinePosition);

    // Save synchronously when switching immediately after a raw scroll event.
    const immediate = await a.evaluate(() => {
      const viewer = PDFViewerApplication.pdfViewer;
      viewer.container.scrollTop += 137;
      viewer.update();
      const location = {...viewer._location};
      parent.document.querySelectorAll('.paper-card')[1].click();
      return location;
    });
    await viewer('Paper B');
    a = await viewer('Paper A');
    await check(a, immediate);

    // Fit-to-width must remain a zoom mode after the reading pane is resized.
    const fitPosition = await move(a, 'page=4&zoom=page-width,0,510');
    await viewer('Paper B');
    await page.setViewportSize({width: 1500, height: 900});
    a = await viewer('Paper A');
    await check(a, fitPosition);
    for (let i = 0; i < 3; i++) {
      await viewer('Paper B');
      a = await viewer('Paper A');
      await check(a, fitPosition);
    }

    // Corrupt storage should start normally instead of breaking the reader.
    await page.evaluate(() => {
      PaperPDF.close();
      localStorage.setItem(document.querySelector('#viewer-container iframe').dataset.positionKey, '{broken json');
    });
    await viewer('Paper B');
    a = await viewer('Paper A');
    assert.equal((await position(a)).pageNumber, 1);
    await page.click('#toggle-theme');
    assert.equal(await a.evaluate(() => document.documentElement.style.colorScheme), 'dark');
    await a.locator('.page canvas').first().waitFor();
    assert.ok((await a.locator('.textLayer').first().innerText()).includes('Paper A'));
    // Categorizing and renaming groups must not replace the current PDF frame.
    const categoryGroup = name => page.locator('#paper-list .paper-category').filter({has: page.getByText(name, {exact: true})});
    async function assignCategory(name) {
      await page.click('#categorize-paper');
      await page.fill('#category-name', name);
      await page.click('#category-form button[type=submit]');
      await page.locator('#category-dialog').waitFor({state: 'hidden'});
      await page.waitForFunction(() => !savingCategory);
    }
    const beforeCategory = await move(a, 'page=4&zoom=150,100,400');
    await page.evaluate(() => { window.readerBeforeCategory = document.querySelector('#viewer-container iframe'); });
    await assignCategory('Memory');
    assert.equal(await categoryGroup('Memory').locator('.category-count').innerText(), '1');
    assert.equal(await page.evaluate(() => window.readerBeforeCategory === document.querySelector('#viewer-container iframe')), true);
    await check(a, beforeCategory);
    await categoryGroup('Memory').locator('summary').click();
    assert.equal(await categoryGroup('Memory').getAttribute('open'), null);
    await page.reload();
    await categoryGroup('Memory').waitFor();
    assert.equal(await categoryGroup('Memory').getAttribute('open'), null);
    await categoryGroup('Memory').locator('summary').click();
    if (!await page.locator('#search').isVisible()) await page.click('#toggle-search');
    await page.fill('#search', 'Memory');
    assert.equal(await page.locator('.paper-card').count(), 1);
    await page.fill('#search', '');
    const readerDuringDrag = await viewer('Paper A');
    const positionDuringDrag = await position(readerDuringDrag);
    let categoryRequests = 0;
    page.on('request', request => {
      if (request.method() === 'PATCH' && request.url().includes('/api/papers/')) categoryRequests++;
    });
    const patchResponse = () => page.waitForResponse(response => response.request().method() === 'PATCH' && response.url().includes('/api/papers/'));
    async function dragInto(category) {
      const response = patchResponse();
      await page.locator('.paper-card:has-text("Paper B")').dragTo(categoryGroup(category).locator('summary'));
      const result = await response;
      await page.waitForFunction(() => !state.movingPaper);
      return result;
    }
    await categoryGroup('Memory').locator('summary').click();
    await page.route('**/api/papers/**', route => route.request().method() === 'PATCH'
      ? route.fulfill({status: 409, contentType: 'application/json', body: JSON.stringify({error: 'Test move failure'})})
      : route.continue());
    assert.equal((await dragInto('Memory')).status(), 409);
    assert.match(await page.locator('#toast').innerText(), /Could not move paper: Test move failure/);
    assert.equal(await categoryGroup('Memory').locator('.category-count').innerText(), '1');
    assert.equal(await categoryGroup('Uncategorized').locator('.paper-card:has-text("Paper B")').count(), 1);
    assert.equal(await page.locator('.drop-target, .dragging').count(), 0);
    await page.unroute('**/api/papers/**');
    assert.equal((await dragInto('Memory')).status(), 200);
    assert.notEqual(await categoryGroup('Memory').getAttribute('open'), null);
    assert.equal(await categoryGroup('Memory').locator('.category-count').innerText(), '2');
    assert.equal(await categoryGroup('Uncategorized').isVisible(), false);
    await check(readerDuringDrag, positionDuringDrag);

    // A same-category drop or an external text drag must not change categories.
    const requestsBeforeNoops = categoryRequests;
    await page.locator('.paper-card:has-text("Paper B")').dragTo(categoryGroup('Memory').locator('summary'));
    const external = await page.evaluateHandle(() => { const data = new DataTransfer(); data.setData('text/plain', 'Paper B'); return data; });
    await categoryGroup('Memory').dispatchEvent('drop', {dataTransfer: external});
    await external.dispose();
    assert.equal(categoryRequests, requestsBeforeNoops);

    // An empty Uncategorized destination appears during dragging. Polling must
    // preserve the source DOM node until the drop completes.
    const source = page.locator('.paper-card:has-text("Paper B")');
    const sourceHandle = await source.elementHandle();
    const sourceBox = await source.boundingBox();
    await page.mouse.move(sourceBox.x + 35, sourceBox.y + 25);
    await page.mouse.down();
    await page.mouse.move(sourceBox.x + 55, sourceBox.y + 30, {steps: 5});
    await page.waitForFunction(() => Boolean(state.draggedPaper));
    await page.evaluate(() => refreshLibrary());
    assert.equal(await sourceHandle.evaluate(node => node.isConnected), true);
    assert.equal(await categoryGroup('Uncategorized').isVisible(), true);
    const targetBox = await categoryGroup('Uncategorized').locator('summary').boundingBox();
    await page.mouse.move(targetBox.x + 40, targetBox.y + 15, {steps: 8});
    await page.mouse.move(targetBox.x + 45, targetBox.y + 15);
    assert.match(await categoryGroup('Uncategorized').getAttribute('class'), /drop-target/);
    const unassignResponse = patchResponse();
    await page.mouse.up();
    assert.equal((await unassignResponse).status(), 200);
    await page.waitForFunction(() => !state.movingPaper);
    assert.equal(await categoryGroup('Uncategorized').locator('.category-count').innerText(), '1');
    assert.equal(await categoryGroup('Memory').locator('.category-count').innerText(), '1');
    await check(readerDuringDrag, positionDuringDrag);
    assert.equal((await dragInto('Memory')).status(), 200);
    await page.reload();
    await categoryGroup('Memory').waitFor();
    assert.equal(await categoryGroup('Memory').locator('.category-count').innerText(), '2');
    await viewer('Paper B');
    await assignCategory('memory');
    assert.equal(await categoryGroup('Memory').locator('.category-count').innerText(), '2');
    await categoryGroup('Memory').getByRole('button', {name: 'Edit category Memory', exact: true}).click();
    assert.notEqual(await categoryGroup('Memory').getAttribute('open'), null);
    await page.fill('#category-name', 'Systems');
    await page.click('#category-form button[type=submit]');
    await page.waitForFunction(() => !savingCategory);
    assert.equal(await categoryGroup('Systems').locator('.category-count').innerText(), '2');
    await assignCategory('Storage');
    await categoryGroup('Systems').getByRole('button', {name: 'Edit category Systems', exact: true}).click();
    await page.fill('#category-name', 'Storage');
    await page.click('#category-form button[type=submit]');
    await page.waitForFunction(() => !savingCategory);
    assert.equal(await categoryGroup('Storage').locator('.category-count').innerText(), '2');
    if (process.env.SCREENSHOT_PATH) await page.screenshot({path: process.env.SCREENSHOT_PATH});
    await categoryGroup('Storage').getByRole('button', {name: 'Edit category Storage', exact: true}).click();
    await page.click('#remove-category');
    await page.waitForFunction(() => !savingCategory);
    assert.equal(await categoryGroup('Uncategorized').locator('.category-count').innerText(), '2');
    assert.equal(await page.locator('.paper-card').count(), 2);
    await page.selectOption('#project', info.other);
    await page.locator('.paper-card:has-text("Paper C")').waitFor();
    assert.equal(await page.locator('.category-edit').count(), 0);
    await page.selectOption('#project', 'osdi');
    await page.waitForFunction(() => state.project?.id === 'osdi');
    assert.equal(await page.locator('#categorize-paper').isVisible(), false);
    assert.deepEqual(errors, []);
    console.log('Browser checks passed: PDF positions; category drag/drop, collapsed targets, empty Uncategorized, refresh during drag, failed moves, persistence, editing, and project isolation.');
  } finally {
    await browser?.close();
    server.kill('SIGTERM');
    if (server.exitCode === null) await once(server, 'exit');
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
