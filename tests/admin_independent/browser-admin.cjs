// 独立浏览器验收，仅连接 browser_server.py 创建的本机隔离数据服务。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(require.resolve('playwright', { paths: [
  process.env.BROWSER_NODE_MODULES || 'C:/Users/JIE/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules'
] }));
const origin = 'http://127.0.0.1:18992';
const artifactDir = path.resolve('evidence/admin-independent');
const errors = [], checks = [], expectedNetwork = [];
let checkingExpiredSession = false;
let browser;
const day = new Date(Date.now() - 86400000).toLocaleDateString('sv-SE', { timeZone: 'Asia/Shanghai' });

async function closeEditor(page) {
  await page.locator('#editor [data-close]').first().click();
  await page.locator('#editor').waitFor({ state: 'hidden' });
}
async function saved(page, button) {
  await button.click();
  await page.locator('#editor').waitFor({ state: 'hidden' });
}
async function navigate(page, hash) {
  await page.goto(`${origin}/admin/#${hash}`);
  await page.locator(`#new-${({tables:'table',sources:'source',services:'service',keys:'key'})[hash]}`).waitFor();
}

(async () => {
  browser = await chromium.launch({ headless: true, channel: 'msedge' });
  const page = await browser.newPage({ viewport: { width: 1440, height: 950 } });
  page.on('pageerror', error => errors.push(error.stack));
  page.on('console', message => {
    if(message.type() !== 'error') return;
    const location = message.location().url;
    if((checkingExpiredSession && message.text().includes('401')) || location.endsWith('/favicon.ico')) {
      expectedNetwork.push({ message: message.text(), location });
    } else errors.push({ message: message.text(), location });
  });
  await page.goto(`${origin}/admin/`);
  await page.locator('input[name=username]').fill('independent_admin');
  await page.locator('input[name=password]').fill('independent-admin-local-test-only-2026!');
  await page.locator('button[type=submit]').click();
  await page.waitForFunction(() => sessionStorage.getItem('data-service-session'));
  checks.push('真实管理员登录成功');

  await navigate(page, 'tables');
  await page.locator('#new-table').click();
  await page.locator('#dataset-form [name=name]').fill('独立浏览器验收表');
  await page.locator('#dataset-form [name=slug]').fill('ind_browser_sales');
  for(const [name, type, dimension] of [['shop','string',true],['amount','decimal',false]]) {
    await page.locator('#add-field').click();
    const row = page.locator('#dataset-fields [data-row]').last();
    await row.locator('[name=field_name]').fill(name);
    await row.locator('[name=field_type]').selectOption(type);
    if(dimension) await row.locator('[name=field_dimension]').check();
  }
  await saved(page, page.locator('[form=dataset-form]'));
  await page.locator('[data-schema=ind_browser_sales]').waitFor();
  checks.push('页面新建真实数据表与可分组维度、Decimal字段');

  await navigate(page, 'sources');
  await page.locator('#new-source').click();
  await page.locator('#source-form [name=title]').fill('独立本机合成来源');
  await page.locator('#source-form [name=url]').fill(`${origin}/synthetic-source`);
  await page.locator('#source-form [name=approve_host]').check();
  await saved(page, page.locator('[form=source-form]'));
  await page.locator('[data-test-source=ind_browser_sales]').click();
  await page.locator('#test-source [name=business_date]').fill(day);
  await page.locator('#test-source [type=submit]').click();
  await page.locator('#source-test-result .notice').waitFor();
  assert.match(await page.locator('#source-test-result').innerText(), /2 行/);
  await closeEditor(page);
  checks.push('页面配置HTTP来源、映射并单页校验2条合成数据');

  await page.locator('[data-sync-source=ind_browser_sales]').click();
  await page.locator('#editor [name=start_date]').fill(day);
  await page.locator('#editor [name=end_date]').fill(day);
  await saved(page, page.locator('#editor button[type=submit]'));
  const token = await page.evaluate(() => sessionStorage.getItem('data-service-session'));
  const headers = { Authorization: `Bearer ${token}` };
  let query;
  for(let attempt = 0; attempt < 30; attempt++) {
    const response = await page.request.post(`${origin}/api/v1/datasets/ind_browser_sales/query`, {
      headers, data: { start_date: day, end_date: day }
    });
    if(response.status() === 200) { query = await response.json(); break; }
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  assert.equal(query?.data?.rows.length, 2);
  checks.push('页面触发同步，真实worker发布2条数据');

  await navigate(page, 'services');
  await page.locator('#new-service').click();
  await page.locator('#service-form [name=name]').fill('独立店铺汇总');
  await page.locator('#service-form [name=slug]').fill('ind_browser_summary');
  await page.locator('#service-form [name=mode][value=aggregate]').check();
  await page.locator('#group-fields [value=shop]').check();
  await page.locator('#add-aggregate').click();
  await page.locator('[name=aggregate_field]').selectOption('amount');
  await page.locator('[name=aggregate_op]').selectOption('sum');
  await page.locator('[name=aggregate_alias]').fill('sales');
  await page.locator('#service-form [name=start_date]').fill(day);
  await page.locator('#service-form [name=end_date]').fill(day);
  await page.locator('#preview-service').click();
  await page.locator('#service-result table').waitFor();
  assert.match(await page.locator('#service-result').innerText(), /20\.000000/);
  await page.locator('#service-form [name=enabled]').check();
  await saved(page, page.locator('[form=service-form]'));
  checks.push('页面配置店铺分组SUM、预览精确20.000000并开放接口');
  await page.locator('[data-example-service=ind_browser_summary]').click();
  assert.match(await page.locator('[aria-label=调用示例]').inputValue(), /\/api\/v1\/services\/ind_browser_summary\/query/);
  assert.match(await page.locator('#editor').innerText(), /读取权限允许查询该表全部字段/);
  await closeEditor(page);
  checks.push('调用说明展示实际URL、参数、密钥占位符和数据表权限边界');

  const creation = await page.request.post(`${origin}/api/v1/admin/keys`, {
    headers, data: { name: '独立浏览器消费方', grants: { ind_browser_sales: ['read'] } }
  });
  assert.equal(creation.status(), 201);
  const machineKey = (await creation.json()).data.api_key;
  const consume = await page.request.post(`${origin}/api/v1/services/ind_browser_summary/query`, {
    headers: { 'X-API-Key': machineKey }, data: { start_date: day, end_date: day }
  });
  assert.equal(consume.status(), 200);
  assert.equal((await consume.json()).data.rows[0].sales, '20.000000');
  checks.push('独立机器密钥成功消费已开放接口');

  await page.waitForFunction(() => !document.querySelectorAll('#notifications .toast').length);
  await page.screenshot({ path: path.join(artifactDir, 'admin-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.evaluate(() => Promise.all(document.getAnimations().map(animation => animation.finished.catch(() => {}))));
  const sidebarRight = await page.locator('.sidebar').evaluate(element => element.getBoundingClientRect().right);
  assert.ok(sidebarRight <= 1, '窄屏导航默认应收起，不遮挡内容');
  await page.screenshot({ path: path.join(artifactDir, 'admin-mobile.png'), fullPage: true });
  assert.ok(await page.locator('#new-service').isVisible());
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1);
  assert.equal(overflow, false, '窄屏不应产生页面整体横向溢出');
  await page.getByRole('button', { name: '展开导航' }).click();
  await page.locator('.shell.menu-open').waitFor();
  await page.locator('.navigation a[href="#sources"]').click();
  await page.locator('#new-source').waitFor();
  checks.push('1440桌面与390窄屏截图，导航展开收起、主操作可见且页面无横向溢出');

  // JSON接口中的恶意文本不会成为页面HTML；不污染任何真实数据表。
  const injection = '<img src=x onerror="window.__independent_xss=1">';
  const sourceList = await page.request.get(`${origin}/api/v1/admin/sources`, { headers });
  const source = (await sourceList.json()).data[0];
  const sourceUpdate = await page.request.put(`${origin}/api/v1/admin/sources/ind_browser_sales`, {
    headers, data: { title: injection, config: source.config, secrets: {}, enabled: true,
      approve_host: true, schedule_enabled: false, schedule_hour: 3, refresh_days: 7, revision: source.revision }
  });
  assert.equal(sourceUpdate.status(), 200);
  await navigate(page, 'sources');
  await page.reload();
  await page.locator('#new-source').waitFor();
  assert.equal(await page.evaluate(() => window.__independent_xss || 0), 0);
  assert.equal(await page.locator('img[onerror]').count(), 0);
  assert.ok((await page.locator('main').innerText()).includes(injection));
  checks.push('来源名称XSS输入按普通文本展示，无执行');

  checkingExpiredSession = true;
  await page.request.post(`${origin}/api/v1/auth/logout`, { headers });
  await page.reload();
  await page.locator('input[name=username]').waitFor();
  assert.equal(await page.evaluate(() => sessionStorage.getItem('data-service-session')), null);
  checks.push('服务端撤销会话后页面清理令牌并要求重新登录');
  assert.deepEqual(errors, []);
  const report = { status: 'passed', checks, browser_errors: errors, expected_network: expectedNetwork, data: '合成本机HTTP，admin_independent_test隔离数据库' };
  fs.writeFileSync(path.join(artifactDir, 'browser-result.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify({ status: report.status, checks: checks.length, browser_errors: errors.length }));
})().catch(error => {
  fs.writeFileSync(path.join(artifactDir, 'browser-result.json'), JSON.stringify({ status: 'failed', checks, browser_errors: errors, failure: error.message }, null, 2));
  console.error(error.message);
  process.exitCode = 1;
}).finally(async () => { if(browser) await browser.close(); });
