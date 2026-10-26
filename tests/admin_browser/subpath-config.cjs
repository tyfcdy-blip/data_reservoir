// 真实浏览器加载实际管理页面；HTTP 桩只隔离认证和数据边界，不连接真实数据库。
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const assert = require('node:assert/strict');
const { chromium } = require(require.resolve('playwright', { paths: [process.env.BROWSER_NODE_MODULES || 'C:/Users/JIE/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules'] }));
const staticRoot = path.resolve(__dirname, '../../data_service/static');
const prefixes = ['', '/data-service', '/nested/data-service'];
const calls = [], errors = [];
const services = [{ slug: 'sales', name: '销售汇总', dataset_slug: 'sales_daily', definition: { fields: ['amount'], group_by: [], aggregates: [], filters: [], order_by: [] }, enabled: true, allowed_filter_fields: [], max_limit: 100, revision: 1 }];
const datasets = [{ slug: 'sales_daily', name: '测试销售日表', definition: { fields: [{ name: 'amount', type: 'decimal' }], key_fields: ['amount'], date_field: 'dt' } }];
let activePrefix = '', expireNext = false;
const server = http.createServer((request, response) => {
  const pathname = new URL(request.url, 'http://localhost').pathname;
  const prefix = [...prefixes].reverse().find(candidate => pathname.startsWith(candidate + '/admin/'));
  if (prefix === undefined) { response.writeHead(404).end(); return; }
  const filename = pathname.slice((prefix + '/admin/').length) || 'index.html';
  if (!/^[a-z-]+\.(html|css|js)$/.test(filename)) { response.writeHead(404).end(); return; }
  response.setHeader('Content-Type', filename.endsWith('.js') ? 'text/javascript' : filename.endsWith('.css') ? 'text/css' : 'text/html');
  response.end(fs.readFileSync(path.join(staticRoot, filename)));
});
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  let browser;
  let passed = 0;
  const pass = name => { passed++; console.log('PASS ' + name); };
  try {
    browser = await chromium.launch({ headless: true, channel: 'msedge' });
    const page = await browser.newPage();
    page.setDefaultTimeout(10000);
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url());
      if (url.origin !== origin) { errors.push('检测到非隔离环境请求'); return route.abort(); }
      if (!url.pathname.includes('/api/')) return route.continue();
      calls.push({ path: url.pathname, method: request.method(), authorization: request.headers().authorization });
      if (!url.pathname.startsWith(activePrefix + '/api/')) { errors.push('API 前缀错误 ' + url.pathname); return route.fulfill({ status: 500, json: { message: '路径错误' } }); }
      const endpoint = url.pathname.slice(activePrefix.length);
      if (expireNext) { expireNext = false; return route.fulfill({ status: 401, json: { message: '会话失效' } }); }
      let data;
      if (endpoint === '/api/v1/auth/login') data = { token: 'isolated-session-' + (activePrefix || 'root') };
      else if (endpoint === '/api/v1/auth/logout') data = {};
      else if (endpoint === '/api/v1/datasets') data = datasets;
      else if (endpoint === '/api/v1/admin/status') data = { workers: [], recent_batches: [], coverage: [] };
      else if (endpoint === '/api/v1/admin/query-services') data = services;
      else if (['/api/v1/admin/sources', '/api/v1/admin/keys'].includes(endpoint)) data = [];
      else { errors.push('未定义 API ' + endpoint); return route.fulfill({ status: 500, json: { message: '未定义' } }); }
      return route.fulfill({ status: 200, json: { data } });
    });
    for (const prefix of prefixes) {
      activePrefix = prefix;
      await page.goto(origin + prefix + '/admin/');
      await page.locator('#login-form').waitFor();
      assert.equal(await page.locator('#login-form a').getAttribute('href'), prefix + '/docs');
      assert.ok(await page.locator('link[rel=stylesheet]').evaluate(node => !!node.sheet));
      pass((prefix || '根路径') + ' 静态资源与登录文档链接');
      const key = prefix ? 'data-service-session:' + prefix : 'data-service-session';
      if (prefix) {
        await page.evaluate(() => sessionStorage.setItem('data-service-session', 'root-sentinel'));
        await page.reload();
        await page.locator('#login-form').waitFor();
        pass(prefix + ' 不复用其他服务会话');
      }
      await page.locator('[name=username]').fill('isolated-admin');
      await page.locator('[name=password]').fill('test-only-password');
      await page.locator('#login-form [type=submit]').click();
      await page.locator('#content h1').filter({ hasText: '服务概览' }).waitFor();
      const token = await page.evaluate(key => sessionStorage.getItem(key), key);
      assert.equal(token, 'isolated-session-' + (prefix || 'root'));
      assert.equal(await page.locator('.account a').getAttribute('href'), prefix + '/docs');
      pass((prefix || '根路径') + ' 登录、概览、会话存储');
      await page.locator('[data-page=services]').click();
      await page.locator('[data-example-service=sales]').click();
      const command = await page.locator('textarea[aria-label=调用示例]').inputValue();
      assert.ok(command.includes(origin + prefix + '/api/v1/services/sales/query'));
      assert.equal(await page.locator('#editor a[href]').getAttribute('href'), prefix + '/docs');
      await page.evaluate(() => { window.testCopied = ''; Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async value => { window.testCopied = value; } } }); });
      await page.locator('#copy-call').click();
      assert.equal(await page.evaluate(() => window.testCopied), command);
      pass((prefix || '根路径') + ' 调用说明及复制含正确前缀');
      await page.locator('#editor .dialog-head [data-close]').click();
      await page.reload();
      await page.locator('[data-example-service=sales]').waitFor();
      pass((prefix || '根路径') + ' 刷新后会话恢复');
      const moduleChecks = await page.evaluate(async prefix => {
        const core = await import(prefix + '/admin/core.js');
        const invalid = ['https://example.com', '//example.com', '/\\example.com'].map(value => { try { core.servicePath(value); return false; } catch { return true; } });
        return { invalid, path: core.servicePath('/api/v1/datasets'), url: core.serviceUrl('/api/v1/datasets') };
      }, prefix);
      assert.deepEqual(moduleChecks.invalid, [true, true, true]);
      assert.equal(moduleChecks.path, prefix + '/api/v1/datasets');
      assert.equal(moduleChecks.url, origin + prefix + '/api/v1/datasets');
      pass((prefix || '根路径') + ' 公用地址解析及外部地址拒绝');
      await page.locator('#logout').click();
      await page.locator('#login-form').waitFor();
      assert.equal(await page.evaluate(key => sessionStorage.getItem(key), key), null);
      if (prefix) assert.equal(await page.evaluate(() => sessionStorage.getItem('data-service-session')), 'root-sentinel');
      pass((prefix || '根路径') + ' 主动退出只清理当前服务会话');
      await page.locator('[name=username]').fill('isolated-admin');
      await page.locator('[name=password]').fill('test-only-password');
      await page.locator('#login-form [type=submit]').click();
      await page.locator('[data-example-service=sales]').waitFor();
      expireNext = true;
      await page.evaluate(async prefix => { const core = await import(prefix + '/admin/core.js'); try { await core.api('/api/v1/datasets'); } catch {} }, prefix);
      await page.locator('#login-form').waitFor();
      assert.equal(await page.evaluate(key => sessionStorage.getItem(key), key), null);
      if (prefix) assert.equal(await page.evaluate(() => sessionStorage.getItem('data-service-session')), 'root-sentinel');
      pass((prefix || '根路径') + ' 401 仅清理当前服务会话');
    }
    assert.equal(calls.filter(call => call.path.endsWith('/auth/login')).length, 6);
    assert.ok(calls.filter(call => !call.path.endsWith('/auth/login')).every(call => call.authorization?.startsWith('Bearer isolated-session-')));
    assert.deepEqual(errors, []);
    pass('全部请求携带当前会话、无浏览器错误、无外部请求');
    console.log(JSON.stringify({ passed, failed: 0, apiCalls: calls.length }));
  } finally { if (browser) await browser.close(); await new Promise(resolve => server.close(resolve)); }
})().catch(error => { console.error(error); process.exitCode = 1; });
