/** Real loopback UI QA; reads credentials at runtime and never changes account/settings data. */
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { chromium } from 'playwright';

const rawBase = process.env.DRA_UI_BASE_URL;
const credentialsFile = process.env.DRA_UI_CREDENTIALS_FILE;
const screenshots = process.env.DRA_UI_SCREENSHOTS;
assert.ok(rawBase && credentialsFile && screenshots,
  'Set DRA_UI_BASE_URL, DRA_UI_CREDENTIALS_FILE, and DRA_UI_SCREENSHOTS');
const base = new URL(rawBase);
const loopback = new Set(['localhost', '127.0.0.1', '[::1]']);
assert.ok(['http:', 'https:'].includes(base.protocol) && loopback.has(base.hostname),
  'Only an HTTP(S) loopback application may be tested');
assert.equal(base.username + base.password + base.search + base.hash, '',
  'The base URL must not include credentials, query parameters, or a fragment');
assert.equal(base.pathname, '/', 'Use the application root URL');
const suppliedCredentials = JSON.parse(await fs.readFile(credentialsFile, 'utf8'));
assert.equal(typeof suppliedCredentials.email, 'string', 'Credentials require an email');
assert.equal(typeof suppliedCredentials.password, 'string', 'Credentials require a password');
const loginData = { email: suppliedCredentials.email, password: suppliedCredentials.password };
await fs.mkdir(screenshots, { recursive: true });

const report = {
  baseUrl: base.origin,
  browser: '',
  viewports: [],
  screenshots: [],
  checks: [],
  pageErrors: [],
  consoleErrors: [],
  httpErrors: [],
  blockedExternal: [],
  blockedWrites: [],
  layoutIssues: [],
  chartGeometry: [],
};
const browser = await chromium.launch({
  executablePath: process.env.DRA_BROWSER_EXECUTABLE || '/usr/bin/chromium',
  headless: true,
  chromiumSandbox: true,
});
report.browser = browser.version();

const safeUrl = value => {
  try { const url = new URL(value); return `${url.origin}${url.pathname}`; }
  catch { return '(invalid URL)'; }
};

async function context(viewport, anonymous = false) {
  const ctx = await browser.newContext({
    viewport,
    locale: 'zh-CN',
    timezoneId: 'Asia/Shanghai',
    reducedMotion: 'reduce',
    isMobile: viewport.width < 600,
    hasTouch: viewport.width < 600,
  });
  await ctx.route('**/*', async route => {
    const request = route.request();
    const url = new URL(request.url());
    if (!loopback.has(url.hostname)) {
      report.blockedExternal.push(safeUrl(url.href));
      return route.abort('blockedbyclient');
    }
    if (!['GET', 'HEAD', 'OPTIONS'].includes(request.method())) {
      report.blockedWrites.push(`${request.method()} ${url.pathname}`);
      return route.abort('blockedbyclient');
    }
    return route.continue();
  });
  ctx.on('page', page => {
    page.setDefaultTimeout(15_000);
    page.on('pageerror', error => report.pageErrors.push(error.message));
    page.on('console', message => {
      if (message.type() !== 'error') return;
      const location = message.location().url;
      if (anonymous && location && new URL(location).pathname === '/api/me'
          && message.text().includes('401')) return;
      report.consoleErrors.push({ message: message.text(), url: safeUrl(location) });
    });
    page.on('response', response => {
      if (response.status() < 400) return;
      const url = new URL(response.url());
      if (anonymous && url.pathname === '/api/me' && response.status() === 401) return;
      report.httpErrors.push({ status: response.status(), url: safeUrl(url.href) });
    });
  });
  return ctx;
}

async function capture(page, viewportName, screen) {
  await page.evaluate(async () => {
    await document.fonts.ready;
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  });
  const filename = `${viewportName}-${screen}.png`;
  await page.screenshot({ path: path.join(screenshots, filename), fullPage: false });
  report.screenshots.push(filename);
  const dimensions = await page.evaluate(() => ({
    viewport: innerWidth,
    document: document.documentElement.scrollWidth,
    body: document.body.scrollWidth,
  }));
  if (Math.max(dimensions.document, dimensions.body) > dimensions.viewport + 1) {
    report.layoutIssues.push({ viewport: viewportName, screen, ...dimensions });
  }
  const dialog = page.getByRole('dialog');
  if (await dialog.count()) {
    const box = await dialog.boundingBox();
    if (box && (box.x < -1 || box.x + box.width > dimensions.viewport + 1)) {
      report.layoutIssues.push({ viewport: viewportName, screen, dialog: box });
    }
  }
}

const isGet = (response, pathname) => response.request().method() === 'GET'
  && new URL(response.url()).pathname === pathname;

async function actionResponse(page, pathname, action, matches = () => true) {
  const pending = page.waitForResponse(response => isGet(response, pathname) && matches(response));
  const [response] = await Promise.all([pending, action()]);
  assert.equal(response.status(), 200, `Read-only API ${pathname} should succeed`);
  return response;
}

async function tab(page, name) {
  await page.getByRole('navigation', { name: '设置分类', exact: true })
    .getByRole('button', { name, exact: true }).click();
  await page.getByRole('heading', { name, exact: true }).waitFor();
}

async function operations(page, viewportName) {
  const initial = await actionResponse(page, '/api/admin/operations', () => tab(page, '运营概览'));
  assert.equal(initial.headers()['cache-control'], 'no-store');
  await page.getByText('最近成功获取：', { exact: false }).waitFor();
  await capture(page, viewportName, 'operations');
  const trend = page.getByRole('img', { name: /^每日研究任务与研究用户（UTC）/ });
  assert.equal(await trend.count(), 1, 'Daily records should render an accessible trend chart');
  await trend.scrollIntoViewIfNeeded();
  await page.waitForFunction(svg => Math.abs(svg.viewBox.baseVal.width - svg.getBoundingClientRect().width) <= 2,
    await trend.elementHandle());
  const chart = await trend.evaluate(svg => ({
    width: svg.getBoundingClientRect().width,
    viewBoxWidth: svg.viewBox.baseVal.width,
    labelFontSizes: Array.from(svg.querySelectorAll('text'), label =>
      Number.parseFloat(getComputedStyle(label).fontSize) * Math.abs(label.getScreenCTM()?.a ?? 0)),
  }));
  assert.ok(Math.abs(chart.width - chart.viewBoxWidth) <= 2,
    'The SVG viewBox width should track the rendered chart width');
  assert.ok(chart.labelFontSizes.length > 0 && chart.labelFontSizes.every(size => size >= 10),
    'Every chart axis label should render at 10 CSS pixels or larger');
  report.chartGeometry.push({ viewport: viewportName, ...chart });
  await capture(page, viewportName, 'operations-trend');
  const range = page.getByRole('combobox', { name: /^时间范围（UTC）/ });
  const today = new Date().toISOString().slice(0, 10);
  const todayResponse = await actionResponse(page, '/api/admin/operations',
    () => range.selectOption('today'), response => new URL(response.url()).searchParams.get('start_date') === today);
  const todayData = await todayResponse.json();
  assert.equal(todayData.range.start_date, today);
  assert.equal(todayData.range.end_date, today);
  await actionResponse(page, '/api/admin/operations', () => range.selectOption('all'),
    response => new URL(response.url()).searchParams.get('all_time') === 'true');
  await range.selectOption('custom');
  await page.getByLabel('开始日期', { exact: true }).fill(today);
  await page.getByLabel('结束日期', { exact: true }).fill(today);
  const custom = await actionResponse(page, '/api/admin/operations',
    () => page.getByRole('button', { name: '应用日期', exact: true }).click());
  assert.equal((await custom.json()).range.end_date, today);
  report.checks.push(`${viewportName}: operations today/all/custom GET filters`);
}

async function users(page, viewportName) {
  const response = await actionResponse(page, '/api/admin/users', () => tab(page, '用户管理'));
  const data = await response.json();
  const target = data.items.find(user => user.role === 'user');
  assert.ok(target, 'Seed a synthetic ordinary user to inspect the editable account dialog');
  const table = page.getByRole('table', { name: /^平台账号及每日研究使用情况/ });
  await table.waitFor();
  await capture(page, viewportName, 'users');
  const row = table.getByRole('row').filter({ hasText: new RegExp(`ID ${target.id}(?:\\D|$)`) });
  const view = row.getByRole('button', { name: /^查看/ });
  await actionResponse(page, `/api/admin/users/${target.id}`, () => view.click());
  const dialog = page.getByRole('dialog', { name: '账号详情', exact: true });
  await dialog.getByRole('heading', { name: '个人每日免费次数', exact: true }).waitFor();
  await capture(page, viewportName, 'user-dialog');
  const quotaSave = dialog.getByRole('button', { name: '保存个人额度', exact: true });
  await quotaSave.scrollIntoViewIfNeeded();
  const quotaSaveBox = await quotaSave.boundingBox();
  assert.ok(quotaSaveBox && quotaSaveBox.y >= 0
    && quotaSaveBox.y + quotaSaveBox.height <= page.viewportSize().height + 1,
  'The bottom of a long account dialog must remain reachable by scrolling');
  await page.keyboard.press('Escape');
  await dialog.waitFor({ state: 'hidden' });
  assert.equal(await view.evaluate(element => element === document.activeElement), true,
    'Escape should return focus to the account button');
  await page.getByRole('combobox', { name: /^身份/ }).selectOption('user');
  const filtered = await actionResponse(page, '/api/admin/users',
    () => page.getByRole('button', { name: '查询', exact: true }).click(),
    reply => new URL(reply.url()).searchParams.get('role') === 'user');
  assert.ok((await filtered.json()).items.every(user => user.role === 'user'));
  report.checks.push(`${viewportName}: user list/filter, account dialog scrolling, Escape/focus restore`);
}

try {
  for (const [name, viewport] of [
    ['desktop', { width: 1440, height: 1000 }],
    ['tablet', { width: 1024, height: 900 }],
    ['mobile', { width: 390, height: 844 }],
  ]) {
    report.viewports.push({ name, ...viewport });
    const anonymous = await context(viewport, true);
    try {
      const login = await anonymous.newPage();
      await login.goto(base.href);
      await login.getByRole('button', { name: '登录', exact: true }).waitFor();
      await capture(login, name, 'login');
    } finally { await anonymous.close(); }

    const ctx = await context(viewport);
    try {
      // APIRequestContext shares its Cookie jar with browser pages. No secrets are printed or saved.
      const login = await ctx.request.post(new URL('/api/auth/login', base).href, { data: loginData });
      assert.equal(login.status(), 200, 'Runtime credentials should log in successfully');
      assert.equal((await login.json()).role, 'owner', 'An isolated owner account is required');
      const page = await ctx.newPage();
      await page.goto(base.href);
      await page.getByRole('button', { name: '设置', exact: true }).waitFor();
      await page.locator('textarea').first().waitFor();
      await capture(page, name, 'home');
      await page.getByRole('button', { name: '设置', exact: true }).click();
      await page.getByRole('navigation', { name: '设置分类', exact: true }).waitFor();
      await tab(page, '模型');
      await page.getByLabel('规划 API 连接', { exact: true }).waitFor();
      await capture(page, name, 'settings-models');
      await operations(page, name);
      await users(page, name);
      await tab(page, '负责人管理');
      await page.getByLabel('新负责人账号 ID', { exact: true }).waitFor();
      await page.getByLabel('新时区', { exact: true }).waitFor();
      await capture(page, name, 'owner');
      report.checks.push(`${name}: owner settings navigation and viewport screenshots`);
    } finally { await ctx.close(); }
  }
  assert.deepEqual(report.pageErrors, [], 'Unexpected browser page errors');
  assert.deepEqual(report.consoleErrors, [], 'Unexpected browser console errors');
  assert.deepEqual(report.httpErrors, [], 'Unexpected failed HTTP responses');
  assert.deepEqual(report.blockedExternal, [], 'The UI attempted external network access');
  assert.deepEqual(report.blockedWrites, [], 'The QA flow attempted a modifying browser request');
  assert.deepEqual(report.layoutIssues, [], 'Horizontal overflow or out-of-viewport dialog');
  report.passed = true;
} catch (error) {
  report.passed = false;
  report.failure = error instanceof Error ? error.message : String(error);
  process.exitCode = 1;
} finally {
  await browser.close();
  await fs.writeFile(path.join(screenshots, 'report.json'), JSON.stringify(report, null, 2) + '\n');
  console.log(JSON.stringify(report, null, 2));
}
