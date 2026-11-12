/** Invoked by tests/test_operations_browser.py after explicit --run-browser opt-in. */
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { chromium } from 'playwright';

const base = process.env.DRA_BROWSER_TEST_URL;
if (!base || new URL(base).hostname !== '127.0.0.1') throw new Error('Only the isolated loopback test server is allowed');
const out = process.env.DRA_OPERATIONS_SCREENSHOTS;
if (!out) throw new Error('Screenshot output directory required');
await fs.mkdir(out, { recursive: true });
const executablePath = process.env.DRA_BROWSER_EXECUTABLE || (process.platform === 'linux' ? '/usr/bin/chromium' : undefined);
const browser = await chromium.launch({ headless: true, executablePath, args: ['--no-sandbox'] });
const errors = [], external = [];
const password = 'synthetic-browser-test-only';
async function context(viewport) {
  const ctx = await browser.newContext({ viewport, deviceScaleFactor: 1 });
  await ctx.route('**/*', route => {
    if (new URL(route.request().url()).origin !== new URL(base).origin) {
      external.push(route.request().url()); return route.abort();
    }
    return route.continue();
  });
  return ctx;
}
async function login(ctx, email) {
  const response = await ctx.request.post(`${base}/api/auth/login`, { data: { email, password } });
  assert.equal(response.status(), 200);
}
async function openPanel(ctx) {
  const page = await ctx.newPage();
  page.on('pageerror', e => errors.push(e.message));
  await page.goto(base);
  await page.getByRole('button', { name: '设置', exact: true }).click();
  await page.getByRole('button', { name: '运营概览', exact: true }).click();
  await page.getByText('最近成功获取：', { exact: false }).waitFor();
  return page;
}
async function badge(page) {
  await page.evaluate(() => {
    const tag = document.createElement('div');
    tag.textContent = '本地验收 · 合成测试数据（非生产）';
    tag.style.cssText = 'position:fixed;top:0;left:50%;transform:translateX(-50%);z-index:9999;background:#fff4ce;color:#5e4418;padding:4px 12px;font:12px sans-serif;border:1px solid #d5b858;border-radius:0 0 6px 6px;white-space:nowrap';
    document.body.append(tag);
  });
}
async function metric(page, label) { return await page.locator('.operations-metric').filter({ has: page.locator('dt', { hasText: label }) }).locator('dd').innerText(); }
try {
  const admin = await context({ width: 1440, height: 1150 });
  await login(admin, 'admin@browser-test.example');
  const page = await openPanel(admin);
  assert.equal(await metric(page, '账号总数'), '12');
  assert.equal(await metric(page, '研究任务'), '7');
  assert.equal(await metric(page, '研究用户（去重）'), '1');
  const response = await admin.request.get(`${base}/api/admin/operations`);
  assert.equal(response.status(), 200); assert.equal(response.headers()['cache-control'], 'no-store');
  assert.equal(JSON.stringify(await response.json()).includes('合成问题'), false);
  await badge(page);
  await page.screenshot({ path: path.join(out, 'admin-operations-implemented-desktop.png'), fullPage: true });
  // Keyboard focus remains visible despite the base select reset.
  const select = page.getByLabel('时间范围（UTC）');
  await page.keyboard.press('Tab'); await select.focus();
  const outline = await select.evaluate(el => getComputedStyle(el).outlineStyle);
  assert.notEqual(outline, 'none');
  await select.selectOption('today');
  await page.waitForFunction(() => document.querySelectorAll('.operations-table tbody tr').length === 1);
  assert.equal(await metric(page, '研究任务'), '1');
  await page.getByRole('button', { name: '刷新', exact: true }).click();
  await page.getByRole('button', { name: '刷新', exact: true }).waitFor({ state: 'visible' });
  await page.waitForFunction(() => !document.querySelector('.operations-heading button').disabled);
  await select.selectOption('custom');
  const today = new Date().toISOString().slice(0, 10);
  await page.getByLabel('开始日期', { exact: true }).fill(today);
  await page.getByLabel('结束日期', { exact: true }).fill(today);
  await page.getByRole('button', { name: '应用日期', exact: true }).click();
  await page.waitForFunction(() => !document.querySelector('.operations-heading button').disabled);
  assert.equal(await metric(page, '研究任务'), '1');
  await select.selectOption('all');
  await page.waitForFunction(() => document.querySelectorAll('.operations-table tbody tr').length === 10);
  assert.equal(await metric(page, '账号总数'), '12');
  // Leaving settings and coming back preserves app navigation, remounts a fresh default snapshot.
  await page.getByRole('button', { name: '设置', exact: true }).click();
  assert.equal(await page.locator('.operations-dashboard').count(), 0);
  await page.getByRole('button', { name: '设置', exact: true }).click();
  await page.getByText('最近成功获取：', { exact: false }).waitFor();
  assert.equal(await page.getByLabel('时间范围（UTC）').inputValue(), '7');
  assert.equal(await metric(page, '研究任务'), '7');
  assert.equal(await page.evaluate(() => localStorage.getItem('operations')), null);
  const mobile = await context({ width: 390, height: 1400 });
  await login(mobile, 'admin@browser-test.example');
  const phone = await openPanel(mobile);
  await badge(phone);
  assert.equal(await phone.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  assert.equal(await phone.locator('.operations-metrics').first().evaluate(el => getComputedStyle(el).gridTemplateColumns.split(' ').length), 2);
  await phone.screenshot({ path: path.join(out, 'admin-operations-implemented-mobile.png'), fullPage: true });
  const member = await context({ width: 1100, height: 800 });
  await login(member, 'member@browser-test.example');
  const memberPage = await member.newPage(); await memberPage.goto(base);
  await memberPage.getByRole('button', { name: '设置', exact: true }).click();
  assert.equal(await memberPage.getByRole('button', { name: '运营概览', exact: true }).count(), 0);
  assert.equal((await member.request.get(`${base}/api/admin/operations`)).status(), 403);
  const anonymous = await context({ width: 1100, height: 800 });
  assert.equal((await anonymous.request.get(`${base}/api/admin/operations`)).status(), 401);
  assert.deepEqual(errors, []); assert.deepEqual(external, []);
  console.log(JSON.stringify({ passed: ['real admin cookie + aggregate API', 'desktop + mobile layout', 'keyboard focus', 'today/custom/all and refresh', 'leave/return', 'member hidden + 403', 'anonymous 401', 'no external requests or page errors'], screenshots: out }));
} finally { await browser.close(); }
