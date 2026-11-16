/** Opt-in browser acceptance for research-workspace.html, using synthetic data only.
 * DRA_RESEARCH_QA_URL=http://127.0.0.1:5197/research-workspace.html
 * DRA_BROWSER_EXECUTABLE=/usr/bin/chromium node qa/check-research-workspace.mjs
 */
import { chromium } from 'playwright';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';

const base = process.env.DRA_RESEARCH_QA_URL;
if (!base || !['localhost', '127.0.0.1'].includes(new URL(base).hostname) || !new URL(base).pathname.endsWith('/research-workspace.html')) {
  throw new Error('DRA_RESEARCH_QA_URL must identify the explicitly supplied loopback research fixture');
}
const directory = process.env.DRA_QA_SCREENSHOTS ?? '/tmp/evisee-research-qa';
await fs.mkdir(directory, { recursive: true });
const browser = await chromium.launch({ headless: true, chromiumSandbox: true, executablePath: process.env.DRA_BROWSER_EXECUTABLE || undefined });
const errors = [], blockedRequests = [], checks = [];
const firstQuote = '试点馆拟在每周五和周六延长开放至 21:00，连续观察八周。';
try {
  for (const [name, viewport] of [['desktop', { width: 1440, height: 1000 }], ['mobile', { width: 390, height: 844 }]]) {
    const page = await browser.newPage({ viewport });
    page.on('pageerror', error => errors.push(`${name}: ${error}`));
    await page.context().route('**/*', route => {
      const url = new URL(route.request().url());
      if (url.origin !== new URL(base).origin || url.pathname.startsWith('/api/')) {
        blockedRequests.push(route.request().url());
        return route.abort();
      }
      return route.continue();
    });
    const capture = async step => {
      const overflow = await page.evaluate(() => {
        const elements = [document.documentElement, ...document.querySelectorAll('.cw-workspace, .cw-scroll, .cw-inspector, .cw-evidence-list, .cw-evidence-main')];
        return elements.filter(element => element.scrollWidth > element.clientWidth + 1).map(element => ({ selector: element.className || element.tagName, width: element.clientWidth, scrollWidth: element.scrollWidth }));
      });
      assert.deepEqual(overflow, [], `${name}/${step}: no page or pane horizontal overflow`);
      await page.screenshot({ path: `${directory}/${name}-${step}.png`, fullPage: false });
      checks.push(`${name}: ${step}`);
    };
    const tab = index => page.locator('nav[aria-label="研究视图"] button').nth(index);
    await page.goto(base);
    await page.locator('.cw-answer').waitFor();
    assert.equal(await page.locator('.cw-warning, .cw-notice-bar, [role="alert"]').count(), 0);
    await capture('report');

    await page.locator('[data-cite="c1"]').first().click();
    await page.locator('.cw-inspector').waitFor();
    assert.match(await page.locator('.cw-inspector h2').innerText(), /八周试点/);
    await capture('report-evidence');
    await page.getByRole('button', { name: '关闭详情', exact: true }).click();
    assert.equal(await page.locator('.cw-inspector').count(), 0);
    await page.waitForFunction(() => document.querySelector('[data-cite="c1"]') === document.activeElement);

    await tab(1).click();
    assert.equal(await tab(1).getAttribute('aria-current'), 'page');
    assert.equal(await page.locator('.evidence-item').count(), 3);
    await page.locator('.evidence-item').first().click();
    assert.equal(await page.locator('.evidence-item').first().getAttribute('aria-pressed'), 'true');
    await capture('evidence');

    await page.getByLabel('搜索证据').fill('运营投入');
    assert.equal(await page.locator('.evidence-item').count(), 1);
    await page.getByLabel('搜索证据').fill('');
    assert.equal(await page.locator('.evidence-item').count(), 3);
    await page.getByRole('button', { name: '看前后文', exact: true }).click();
    await page.locator('.cw-source-text mark').waitFor();
    assert.equal(await page.locator('.cw-source-text mark').innerText(), firstQuote);
    await page.locator('.cw-source-text mark').scrollIntoViewIfNeeded();
    await capture('context');

    await page.getByRole('button', { name: '读保存的全文', exact: true }).click();
    await page.locator('.cw-source-wide .cw-source-text mark').waitFor();
    assert.equal(await page.locator('.cw-source-wide mark').innerText(), firstQuote);
    const markBox = await page.locator('.cw-source-wide mark').boundingBox();
    assert.ok(markBox && markBox.y >= 0 && markBox.y < viewport.height, 'the located quotation is visible');
    await page.waitForFunction(() => {
      const link = document.querySelector('.cw-source-wide > .cw-link:first-child');
      const scroll = document.querySelector('.cw-scroll');
      if (!link || !scroll) return false;
      const button = link.getBoundingClientRect(), pane = scroll.getBoundingClientRect();
      return button.top >= pane.top && button.left >= pane.left && button.right <= pane.right && button.bottom <= pane.bottom;
    });
    await capture('source');
    await page.getByRole('button', { name: '返回证据', exact: true }).click();
    assert.equal(await page.locator('.evidence-item').first().getAttribute('aria-pressed'), 'true');

    await tab(2).click();
    assert.equal(await tab(2).getAttribute('aria-current'), 'page');
    await tab(0).click();
    await page.getByRole('button', { name: '关闭详情', exact: true }).click();
    await page.locator('.cw-answer').waitFor();
    assert.equal(await page.locator('.cw-warning, .cw-notice-bar, [role="alert"]').count(), 0);
    await page.close();
  }
  assert.deepEqual(errors, [], 'no browser JavaScript errors');
  assert.deepEqual(blockedRequests, [], 'no external or backend network requests');
  const result = { status: 'PASS', checks, browserErrors: errors, blockedRequests, syntheticOnly: true };
  await fs.writeFile(`${directory}/result.json`, JSON.stringify(result, null, 2) + '\n');
  console.log(JSON.stringify(result, null, 2));
} finally {
  await browser.close();
}
