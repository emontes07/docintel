export async function runBatchBrowserAttempt(page, { batchId, attempt, startup }) {
  const report = { attempt, startup, startedAt: new Date().toISOString(), views: [], consoleErrors: [], pageErrors: [], failedRequests: [], httpErrors: [], blockedWrites: [], warnings: [] };
  const origin = 'http://127.0.0.1:3100';
  const base = `${origin}/api/batches`;
  const onConsole = message => {
    if (message.type() === 'error') report.consoleErrors.push(message.text());
    if (message.type() === 'warning') report.warnings.push(message.text());
  };
  const onPageError = error => report.pageErrors.push(error.message);
  const onFailure = request => report.failedRequests.push({ method: request.method(), url: request.url(), error: request.failure()?.errorText });
  const onResponse = response => { if (response.status() >= 400) report.httpErrors.push({ status: response.status(), url: response.url() }); };
  const readOnly = async route => {
    if (!['GET', 'HEAD', 'OPTIONS'].includes(route.request().method())) {
      report.blockedWrites.push({ method: route.request().method(), url: route.request().url() });
      await route.abort('blockedbyclient');
    } else await route.continue();
  };
  function check(condition, message) { if (!condition) throw new Error(message); }
  async function json(path) {
    return page.evaluate(async url => {
      const response = await fetch(url, { cache: 'no-store' });
      if (!response.ok) throw new Error(`GET ${new URL(url).pathname}: ${response.status}`);
      return response.json();
    }, base + path);
  }
  async function responseFor(path, action) {
    const pending = page.waitForResponse(response => response.url() === base + path && response.request().method() === 'GET');
    const [response] = await Promise.all([pending, action()]);
    check(response.status() === 200, `GET ${path}: ${response.status()}`);
    return response.json();
  }
  async function rowsReady(expected) {
    await page.waitForFunction(ids => {
      const actual = [...document.querySelectorAll('tbody tr')].map(row => row.querySelector('td')?.textContent);
      return JSON.stringify(actual) === JSON.stringify(ids);
    }, expected.map(item => item.original['PIMITEM Number']));
  }
  page.on('console', onConsole);
  page.on('pageerror', onPageError);
  page.on('requestfailed', onFailure);
  page.on('response', onResponse);
  await page.route('**/api/**', readOnly);
  try {
    await page.setViewportSize({ width: 1440, height: 1000 });
    const history = await responseFor('', () => page.goto(`${origin}/batches`, { waitUntil: 'domcontentloaded' }));
    check(history.some(batch => batch.id === batchId && batch.state === 'completed'), 'Synthetic completed batch missing');
    const before = await json(`/${batchId}`);
    const items = await json(`/${batchId}/items?offset=0&limit=50&view=all`);
    check(items.total === 2 && items.items.every(item => item.original['Vendor Name'] === 'Synthetic'), 'Only the two-product synthetic fixture is permitted');
    const detailBefore = await json(`/${batchId}/items/row-2`);
    for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
      await page.setViewportSize(viewport);
      if (viewport.width === 390) await responseFor('', () => page.goto(`${origin}/batches`, { waitUntil: 'domcontentloaded' }));
      const all = await responseFor(`/${batchId}/items?offset=0&limit=50&view=all`, () => page.getByRole('combobox', { name: 'Saved batch', exact: true }).selectOption(batchId));
      await rowsReady(all.items);
      const pending = await responseFor(`/${batchId}/items?offset=0&limit=50&view=pending`, () => page.getByRole('combobox', { name: 'Item filter', exact: true }).selectOption('pending'));
      await rowsReady(pending.items);
      const restored = await responseFor(`/${batchId}/items?offset=0&limit=50&view=all`, () => page.getByRole('combobox', { name: 'Item filter', exact: true }).selectOption('all'));
      await rowsReady(restored.items);
      check(!await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), 'Queue page horizontal overflow');
      await responseFor(`/${batchId}/items/row-2`, () => page.locator(`a[href="/batches/${batchId}/row-2"]`).click());
      await page.getByRole('heading', { name: 'Pressure Rating', exact: true }).waitFor();
      await page.getByTestId('recorded-review').waitFor();
      check(!await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), 'Item page horizontal overflow');
      report.views.push({ viewport, all: all.total, pending: pending.total, restored: restored.total, detailReady: true, overflow: false });
    }
    report.export = await page.evaluate(async url => {
      const response = await fetch(url, { cache: 'no-store' });
      const bytes = new Uint8Array(await response.arrayBuffer());
      return { status: response.status, cache: response.headers.get('cache-control'), type: response.headers.get('content-type'), bytes: bytes.length, zip: bytes[0] === 80 && bytes[1] === 75 };
    }, `${base}/${batchId}/export`);
    check(report.export.status === 200 && report.export.zip && report.export.cache === 'no-store', 'Qualified export response invalid');
    check(JSON.stringify(before) === JSON.stringify(await json(`/${batchId}`)), 'Batch changed during read-only acceptance');
    check(JSON.stringify(detailBefore) === JSON.stringify(await json(`/${batchId}/items/row-2`)), 'Machine result or review changed');
    check(!report.blockedWrites.length, 'Unexpected application write attempted');
    check(!report.consoleErrors.length && !report.pageErrors.length && !report.httpErrors.length, 'Browser/application error captured');
    check(!report.failedRequests.some(request => request.url === base || request.url.startsWith(base + '/')), 'Batch request failed');
    report.outcome = 'passed';
  } catch (error) {
    report.outcome = 'failed';
    report.failure = error.message;
  } finally {
    await page.unroute('**/api/**', readOnly);
    page.off('console', onConsole);
    page.off('pageerror', onPageError);
    page.off('requestfailed', onFailure);
    page.off('response', onResponse);
  }
  report.finishedAt = new Date().toISOString();
  return report;
}