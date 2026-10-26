// 只访问本机隔离服务，验证真实 DOM 转义与分页表单，无第三方请求。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(require.resolve('playwright', { paths: [
  'C:/Users/JIE/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules'
] }));
const origin = 'http://127.0.0.1:18994';
const checks = [], errors = [], unexpectedExternalRequests = [];

(async () => {
  const browser = await chromium.launch({headless:true, channel:'msedge'});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:950}});
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', route => {
      if (!route.request().url().startsWith(origin + '/')) {
        unexpectedExternalRequests.push(route.request().url());
        return route.abort();
      }
      return route.continue();
    });
    await page.goto(origin + '/admin/#tables');
    await page.locator('[name=username]').fill('key_review');
    await page.locator('[name=password]').fill('independent-description-check-2026!');
    await page.locator('#login-form [type=submit]').click();
    await page.locator('[data-schema=ind_browser_product]').click();
    const headers = await page.locator('#editor th').allTextContents();
    assert(headers.some(text => text.includes('说明')));
    assert.equal(await page.locator('#editor tbody tr').count(), 32);
    const htmlDescription = page.locator('#editor tbody tr').filter({has:page.locator('code').filter({hasText:/^product_name$/})});
    assert.match(await htmlDescription.innerText(), /<img src=x onerror="window\.__descriptionXss=1">商品名称/);
    assert.equal(await page.locator('#editor img').count(), 0);
    assert.equal(await page.evaluate(() => window.__descriptionXss), undefined);
    checks.push('32 行说明列可见；恶意 HTML 仅显示文本，不创建元素或执行');
    const keyRows = await page.locator('#editor tbody tr').allTextContents();
    assert(keyRows[0].includes('product_day_id'));
    await page.locator('#editor [data-close]').first().click();

    await page.locator('#new-table').click();
    await page.locator('#dataset-form [name=name]').fill('独立说明表单验收');
    await page.locator('#dataset-form [name=slug]').fill('ind_description_form');
    const descriptions = page.locator('#dataset-fields [name=field_description]');
    assert.equal(await descriptions.count(), 2);
    await descriptions.nth(0).fill('');
    assert.equal(await descriptions.nth(0).evaluate(node => node.required), true);
    assert.equal(await page.locator('#dataset-form').evaluate(node => node.checkValidity()), false);
    await descriptions.nth(0).fill('业务日期（北京时间）');
    await descriptions.nth(1).fill('唯一记录标识，保留前导零');
    const [created] = await Promise.all([
      page.waitForResponse(response => response.url().endsWith('/api/v1/admin/datasets') && response.request().method()==='POST'),
      page.locator('[form=dataset-form]').click()
    ]);
    assert.equal(created.status(), 201);
    const createdDefinition = (await created.json()).data.definition;
    assert.deepEqual(createdDefinition.fields.map(field => field.description), ['业务日期（北京时间）','唯一记录标识，保留前导零']);
    checks.push('新增数据表要求每行说明，说明随 API 保存并读回');

    await page.goto(origin + '/admin/#sources');
    await page.locator('#new-source').click();
    const form = page.locator('#source-form');
    await form.locator('[name=dataset_slug]').selectOption('ind_browser_product');
    await form.locator('[name=title]').fill('独立签名分页配置');
    await form.locator('[name=protocol]').selectOption('vendor_batch');
    await form.locator('[name=url]').fill('https://source.test/rows');
    await form.locator('[name=approve_host]').check();
    await form.locator('[name=enabled]').uncheck();
    assert.equal(await form.locator('[name=page_start]').isEnabled(), true);
    await form.locator('[name=page_start]').selectOption('1');
    await form.locator('[name=vendor_api_id]').fill('independent-api');
    await form.locator('[name=vendor_app_key]').fill('synthetic-key');
    await form.locator('[name=vendor_app_secret]').fill('synthetic-secret');
    const [saved] = await Promise.all([
      page.waitForResponse(response => response.url().endsWith('/api/v1/admin/sources/ind_browser_product') && response.request().method()==='PUT'),
      page.locator('[form=source-form]').click()
    ]);
    assert.equal(saved.status(), 200);
    assert.equal((await saved.json()).data.config.page_start, 1);
    await page.reload();
    const editor = page.locator('[data-edit-source=ind_browser_product]');
    await editor.click();
    assert.equal(await page.locator('#source-form [name=page_start]').inputValue(), '1');
    assert.equal(await page.locator('#source-form [name=page_start]').isEnabled(), true);
    await page.locator('#source-form [name=page_start]').selectOption('0');
    assert.equal(await page.locator('#source-form [name=page_start]').inputValue(), '0');
    checks.push('vendor 页码可选 1 并持久保存，重新打开不被改回 0，仍允许选择 0');
    assert.deepEqual(errors, []);
    assert.deepEqual(unexpectedExternalRequests, []);
    checks.push('无浏览器脚本异常，无外部网络请求');
    fs.writeFileSync(path.join(__dirname,'browser-result.json'), JSON.stringify({checks,errors,unexpectedExternalRequests},null,2));
    console.log(JSON.stringify({passed:checks.length,checks}));
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error.stack); process.exitCode=1;});
