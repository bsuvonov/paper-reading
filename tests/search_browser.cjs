// Run with Playwright/Chromium installed; optional PLAYWRIGHT_MODULE, CHROMIUM_PATH, PYTHON.
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {once} = require('node:events');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const fixture = `
import json, signal, tempfile, time
from pathlib import Path
import pymupdf
from werkzeug.serving import make_server
import conference_search
from web_app import Library, create_app
from projects import ProjectStore
import download_osdi as downloader
def stop(*args):
    raise KeyboardInterrupt
signal.signal(signal.SIGTERM, stop)
conference_search.CONFERENCES = {'osdi': ('OSDI', [2024, 2025]), 'fast': ('FAST', [2024, 2025])}
def discover(client, ident, year):
    time.sleep(0.2)
    return 'https://example.org/program', [downloader.Paper(year, 'Remote memory FAST', 'https://example.org/remote')]
conference_search.discover_conference = discover
with tempfile.TemporaryDirectory(prefix='paper-reading-search-') as directory:
    root = Path(directory)
    local = ProjectStore(root).create('Local search')
    for ident, year, title in [('osdi', 2024, 'Saved memory OSDI'), ('osdi', 2025, 'Other storage OSDI'), ('fast', 2024, 'Cached memory FAST'), (local['id'], 'local', 'Private memory notes')]:
        library = Library(root, ident)
        folder = library.papers / str(year)
        folder.mkdir(parents=True, exist_ok=True)
        record = dict(year=year, title=title, category='Caching', page_url='https://example.org/' + title, status='not_selected')
        if ident in ['osdi', local['id']]:
            with pymupdf.open() as pdf:
                pdf.new_page().insert_text((72,72), title)
                pdf.save(folder / 'paper.pdf')
            record.update(status='downloaded', path='paper.pdf')
        records = [record]
        if ident == 'fast':
            records += [dict(year=year, title=f'Memory result {n}', category='Caching', page_url=f'https://example.org/{n}', status='not_selected') for n in range(110)]
        downloader.save_manifest(folder / 'manifest.json', dict(year=year, categories_version=downloader.CATEGORY_VERSION, discovered=len(records), papers=records))
    app = create_app(root)
    jobs = app.config['JOBS']
    class FinishedProcess:
        def poll(self):
            return 0
    def launch(job):
        jobs.current = job
        job.update(status='running', started=time.time())
        if job['action'] == 'download':
            library = Library(root, job['project_id'])
            row = library.get(job['paper_id'])
            folder = library.papers / row['year']
            folder.mkdir(parents=True, exist_ok=True)
            with pymupdf.open() as pdf:
                pdf.new_page().insert_text((72,72), row['title'])
                pdf.save(folder / 'downloaded.pdf')
            record = dict(row['_record'], status='downloaded', path='downloaded.pdf')
            downloader.save_manifest(folder / 'manifest.json', dict(year=int(row['year']), categories_version=downloader.CATEGORY_VERSION, discovered=1, papers=[record]))
        jobs.process = FinishedProcess()
    jobs._launch = launch
    server = make_server('127.0.0.1', 0, app, threaded=True)
    print('READY ' + json.dumps(dict(port=server.server_port, local=local['id'])), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
`;
(async () => {
  const server = spawn(process.env.PYTHON || 'python3', ['-u', '-c', fixture], {cwd: path.resolve(__dirname, '..'), stdio: ['ignore', 'pipe', 'pipe']});
  let browser, log = '';
  server.stderr.on('data', chunk => { log += chunk; });
  try {
    const info = await new Promise((resolve, reject) => {
      let output = '';
      const timeout = setTimeout(() => reject(new Error('Fixture did not start: ' + log)), 15000);
      server.stdout.on('data', chunk => {
        output += chunk;
        const match = output.match(/READY (.*)\n/);
        if (match) { clearTimeout(timeout); resolve(JSON.parse(match[1])); }
      });
      server.on('error', reject);
      server.on('exit', code => { clearTimeout(timeout); reject(new Error('Fixture exited: ' + code + log)); });
    });
    browser = await chromium.launch({headless: true, executablePath: process.env.CHROMIUM_PATH || undefined, args: ['--no-sandbox']});
    const page = await browser.newPage({viewport: {width: 1360, height: 900}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(`http://127.0.0.1:${info.port}`);
    await page.waitForFunction(() => state.projects.length > 0);
    if (!await page.locator('#search').isVisible()) await page.click('#toggle-search');
    await page.selectOption('#year', '2024');
    // New static assets can be loaded while the previous server is still running.
    await page.route('**/api/search?**', route => route.fulfill({status: 404, contentType: 'text/html', body: '<h1>Not Found</h1>'}));
    await page.fill('#search', 'memory');
    await page.locator('#paper-list').getByText('The running server needs a restart', {exact: false}).waitFor();
    assert.equal(await page.locator('#paper-list .panel-empty').count(), 0);
    assert.equal(await page.locator('#paper-list').getByText('Request failed (404)', {exact: false}).count(), 0);
    await page.unroute('**/api/search?**');
    await page.locator('#paper-list').getByRole('button', {name: 'Retry', exact: true}).click();
    await page.waitForFunction(() => document.querySelector('#paper-count').textContent === '113');
    assert.equal(await page.inputValue('#project'), 'osdi');
    assert.equal(await page.inputValue('#year'), '2024');
    assert.equal(await page.locator('#year').isDisabled(), true);
    assert.equal(await page.locator('#paper-count').innerText(), '113');
    assert.equal(await page.locator('.paper-card').count(), 100);
    assert.equal(await page.locator('.paper-card:has-text("Private memory")').count(), 0);
    await page.click('.search-more');
    await page.waitForFunction(() => document.querySelectorAll('.paper-card').length === 113);
    await page.locator('.paper-card:has-text("Remote memory FAST")').click();
    await page.waitForFunction(() => state.detail?.title === 'Remote memory FAST');
    assert.equal(await page.inputValue('#project'), 'fast');
    assert.equal(await page.inputValue('#search'), 'memory');
    assert.equal(await page.inputValue('#search-scope'), 'all');
    assert.equal(await page.locator('.paper-card').count(), 113);
    const download = page.waitForResponse(r => r.url().endsWith('/api/jobs') && r.request().method() === 'POST');
    await page.locator('#viewer-container').getByRole('button', {name: 'Download paper', exact: true}).click();
    const response = await download;
    assert.equal(response.request().postDataJSON().project_id, 'fast');
    await page.locator('#viewer-container iframe[title="Remote memory FAST"]').waitFor();
    await page.selectOption('#availability', 'downloaded');
    await page.waitForFunction(() => document.querySelectorAll('.paper-card').length === 2);
    await page.locator('.paper-card:has-text("Saved memory OSDI")').click();
    await page.locator('#viewer-container iframe[title="Saved memory OSDI"]').waitFor();
    assert.equal(await page.inputValue('#search'), 'memory');
    await page.click('#toggle-theme');
    if (process.env.SCREENSHOT_PATH) await page.screenshot({path: process.env.SCREENSHOT_PATH});
    await page.selectOption('#availability', 'all');
    await page.fill('#search', 'FAST 2025 memory');
    await page.waitForFunction(() => document.querySelectorAll('.paper-card').length === 1);
    assert.match(await page.locator('.paper-card').innerText(), /Remote memory FAST/);

    // A slow earlier request must not overwrite a newer query or a cleared search.
    let release, intercepted;
    const ready = new Promise(resolve => { intercepted = resolve; });
    const gate = new Promise(resolve => { release = resolve; });
    await page.route('**/api/search?**', async route => {
      if (new URL(route.request().url()).searchParams.get('q') === 'memory') { intercepted(); await gate; }
      await route.continue().catch(() => {});
    });
    await page.fill('#search', 'memory');
    await ready;
    await page.fill('#search', 'storage');
    await page.locator('.paper-card:has-text("Other storage OSDI")').waitFor();
    release();
    await page.unroute('**/api/search?**');
    assert.equal(await page.locator('.paper-card').count(), 1);
    await page.selectOption('#search-scope', 'current');
    assert.equal(await page.locator('#year').isDisabled(), false);
    await page.fill('#search', '');
    assert.equal(await page.locator('#year').isDisabled(), false);
    await page.selectOption('#project', info.local);
    await page.waitForFunction(id => state.project?.id === id, info.local);
    assert.equal(await page.inputValue('#search-scope'), 'current');
    await page.fill('#search', 'Private');
    assert.equal(await page.locator('.paper-card').count(), 1);
    await page.selectOption('#search-scope', 'all');
    await page.fill('#search', 'FAST memory');
    await page.locator('.paper-card:has-text("Remote memory FAST")').waitFor();
    await page.click('#toggle-search');
    assert.equal(await page.locator('#search-scope').isVisible(), false);
    assert.equal(await page.inputValue('#search'), '');
    assert.match(await page.locator('.paper-card').innerText(), /Private memory/);
    assert.deepEqual(errors, []);
    console.log('Global search browser checks passed: server restart guidance and recovery, all conferences/years, uncached papers, pagination, cross-conference reading and downloading, availability filters, stale requests, scope switching, and local-project search.');
  } finally {
    await browser?.close();
    server.kill('SIGTERM');
    if (server.exitCode === null) await once(server, 'exit');
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
