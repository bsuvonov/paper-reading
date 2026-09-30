// Run with Playwright/Chromium installed; optional PLAYWRIGHT_MODULE, CHROMIUM_PATH, PYTHON.
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {once} = require('node:events');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

// Exercise the real API and queue without launching download workers or network requests.
const fixture = `
import json, signal, tempfile, time
from pathlib import Path
from werkzeug.serving import make_server
from web_app import create_app
from projects import ProjectStore
def stop(*args):
    raise KeyboardInterrupt
signal.signal(signal.SIGTERM, stop)
class PendingProcess:
    def poll(self):
        return None
with tempfile.TemporaryDirectory(prefix='paper-reading-downloads-') as directory:
    root = Path(directory)
    local = ProjectStore(root).create('Local papers')
    app = create_app(root)
    jobs = app.config['JOBS']
    def launch(job):
        jobs.current = job
        job.update(status='running', started=time.time())
        jobs.process = PendingProcess()
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
  const server = spawn(process.env.PYTHON || 'python3', ['-u', '-c', fixture], {
    cwd: path.resolve(__dirname, '..'), stdio: ['ignore', 'pipe', 'pipe'],
  });
  let browser;
  let log = '';
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
    const page = await browser.newPage({viewport: {width: 1300, height: 850}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(`http://127.0.0.1:${info.port}`);
    await page.waitForFunction(() => state.projects.length > 0);
    const conference = name => page.locator(`#download-conferences input[value="${name}"]`);
    const year = number => page.locator(`#download-years input[value="${number}"]`);
    const setYears = async values => {
      for (const input of await page.locator('#download-years input:checked').all()) await input.uncheck();
      for (const value of values) await year(value).check();
    };
    const queue = async () => {
      const response = page.waitForResponse(response => response.url().endsWith('/api/downloads') && response.request().method() === 'POST');
      await page.click('#download-submit');
      const result = await response;
      assert.equal(result.status(), 202);
      await page.locator('#download-dialog').waitFor({state: 'hidden'});
      return result.json();
    };
    await page.click('#download-scope');
    assert.equal(await conference('osdi').isChecked(), true);
    assert.equal(await page.locator('#download-all-years').isChecked(), false);
    await conference('fast').check();
    await setYears([2024, 2025]);
    assert.equal(await page.locator('#download-summary').innerText(), '4 editions across 2 conferences');
    await page.click('#download-preview summary');
    assert.deepEqual(await page.locator('#download-editions li').allTextContents(), ['OSDI: 2025, 2024', 'FAST: 2025, 2024']);
    let result = await queue();
    assert.equal(result.added, 4);
    assert.equal(result.job.title, 'OSDI 2025');
    assert.deepEqual(result.job.queue.map(job => job.title), ['OSDI 2024', 'FAST 2025', 'FAST 2024']);
    assert.match(await page.locator('#job-log').innerText(), /FAST 2024/);
    await page.selectOption('#year', '2024');
    assert.equal(await page.locator('#download-year').innerText(), '2024 queued');
    assert.equal(await page.locator('#download-year').isDisabled(), true);

    // Specific years across all conferences; annual gaps aren't fabricated.
    await page.click('#download-scope');
    assert.equal(await year(2024).isChecked(), true);
    await page.check('#download-all-conferences');
    assert.equal(await page.locator('#download-conferences input:not(:checked)').count(), 0);
    await setYears([2024, 2025]);
    const expectedSelected = await page.evaluate(() => state.projects.filter(p => p.kind === 'conference')
      .flatMap(p => p.years.filter(y => [2024, 2025].includes(y)).map(y => `${p.id}/${y}`)));
    result = await queue();
    assert.equal(result.editions, expectedSelected.length);
    assert.equal(result.added, expectedSelected.length - 4);
    assert.equal(result.already_queued, 4);

    // All editions of particular conferences, while the previous queue runs.
    await page.click('#download-scope');
    await conference('asplos').check();
    await page.check('#download-all-years');
    assert.equal(await page.locator('#download-years input:not(:disabled)').count(), 0);
    const expectedAllYears = await page.evaluate(() => state.projects.filter(p => ['osdi', 'asplos'].includes(p.id))
      .reduce((count, p) => count + p.years.length, 0));
    result = await queue();
    assert.equal(result.editions, expectedAllYears);
    assert.equal(result.already_queued, 4);
    await page.reload();
    await page.waitForFunction(() => state.job?.queue?.length > 0);
    assert.equal(await page.evaluate(() => state.job.queue.length), result.job.queue.length);

    await page.click('#download-scope');
    await conference('osdi').uncheck();
    assert.equal(await page.locator('#download-submit').isDisabled(), true);
    await conference('sosp').check();
    assert.equal(await year(1994).count(), 0);
    await setYears([2025]);
    await page.route('**/api/downloads', route => route.fulfill({status: 409, contentType: 'application/json', body: JSON.stringify({error: 'Try again'})}));
    await page.click('#download-submit');
    await page.waitForFunction(() => document.querySelector('#download-error').textContent === 'Try again');
    assert.equal(await page.locator('#download-submit').isEnabled(), true);
    await page.unroute('**/api/downloads');
    result = await queue();
    assert.equal(result.added, 0);
    assert.match(await page.locator('#toast').innerText(), /already in the download queue/);

    // All conferences and all supported years together, including repeat submission.
    await page.click('#toggle-theme');
    await page.click('#download-scope');
    await page.check('#download-all-conferences');
    await page.check('#download-all-years');
    if (process.env.SCREENSHOT_PATH) await page.screenshot({path: process.env.SCREENSHOT_PATH});
    result = await queue();
    const expectedTotal = await page.evaluate(() => state.projects.filter(p => p.kind === 'conference').reduce((count, p) => count + p.years.length, 0));
    assert.equal(result.editions, expectedTotal);
    assert.equal(result.job.queue.length + 1, expectedTotal);
    await page.click('#download-scope');
    await page.check('#download-all-conferences');
    await page.check('#download-all-years');
    result = await queue();
    assert.equal(result.added, 0);
    assert.equal(result.already_queued, expectedTotal);
    await page.selectOption('#project', info.local);
    await page.waitForFunction(id => state.project?.id === id, info.local);
    assert.equal(await page.locator('#download-scope').isVisible(), false);
    assert.deepEqual(errors, []);
    console.log('Download scope browser checks passed: multi-conference/year selection, both all modes, edition preview, queue labels, deduplication, reload, validation, retry, and local-project isolation.');
  } finally {
    await browser?.close();
    server.kill('SIGTERM');
    if (server.exitCode === null) await once(server, 'exit');
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
