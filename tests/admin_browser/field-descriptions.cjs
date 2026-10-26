// 仅验证真实页面脚本；HTTP API 用内存桩隔离，不连接数据库或供应商。
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const assert = require('node:assert/strict');
const { chromium } = require(require.resolve('playwright', {paths: [process.env.BROWSER_NODE_MODULES || 'C:/Users/JIE/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules']}));

const root = path.resolve(__dirname, '../..');
const staticRoot = path.join(root, 'data_service/static');
const unsafeDescription = '商品主图地址 <img src=x onerror="window.descriptionInjected=true">；' + '保留来源返回的完整地址。'.repeat(12);
const definition = {
  slug: 'field_description_ui', name: '字段说明验收表', date_field: 'dt', key_fields: ['product_day_id'], retention_days: 180,
  fields: [
    {name: 'product_day_id', type: 'string', required: true, dimension: false, description: '唯一行标识，业务日期_商品ID，重跑保持一致'},
    {name: 'dt', type: 'date', required: true, dimension: true, description: '北京时间业务自然日'},
    {name: 'product_image', type: 'string', required: false, dimension: false, description: unsafeDescription},
    {name: 'legacy_field', type: 'string', required: false, dimension: false},
  ],
};
const datasets = [{slug: definition.slug, name: definition.name, definition}];
let source = {
  dataset_slug: definition.slug, title: '分页配置验收来源', origin: 'managed', secret_names: ['UI_KEY', 'UI_SECRET'],
  enabled: false, schedule_enabled: false, schedule_hour: 3, refresh_days: 7, revision: 1,
  config: {
    dataset_slug: definition.slug, protocol: 'vendor_batch', url: 'https://example.com/query', method: 'POST', parameter_location: 'json',
    date_param: 'dt', page_param: 'pageIndex', page_size_param: 'pageSize', page_start: 1, page_size: 200, rows_path: 'data',
    field_mapping: Object.fromEntries(definition.fields.map(field=>[field.name, field.name])), source_timezone: 'Asia/Shanghai', date_fields_from_datetime: ['dt'],
    vendor_api_id: 'UI_ONLY', vendor_app_key_env: 'UI_KEY', vendor_app_secret_env: 'UI_SECRET',
  },
};
const submitted = {datasets: [], sources: []};
const server = http.createServer((request, response)=>{
  const pathname = new URL(request.url, 'http://127.0.0.1').pathname;
  const filename = pathname === '/admin/' ? 'index.html' : pathname.slice('/admin/'.length);
  if(!pathname.startsWith('/admin/') || !/^[a-z-]+\.(html|js|css)$/.test(filename)) {response.writeHead(404).end();return;}
  const file = path.join(staticRoot, filename);
  if(!fs.existsSync(file)) {response.writeHead(404).end();return;}
  response.setHeader('Content-Type', filename.endsWith('.js')?'text/javascript':filename.endsWith('.css')?'text/css':'text/html');
  response.end(fs.readFileSync(file));
});

(async()=>{
  await new Promise(resolve=>server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({headless: true, channel: 'msedge'});
  const page = await browser.newPage({viewport: {width: 1440, height: 900}, timezoneId: 'Asia/Shanghai'});
  const errors = [], checks = [];
  const passed = name=>{checks.push(name);console.log('PASS '+name);};
  const closeDialog = ()=>page.locator('#editor .dialog-head [data-close]').click();
  page.on('pageerror', error=>errors.push(error.message));
  await page.addInitScript(()=>sessionStorage.setItem('data-service-session', 'isolated-ui-token'));
  await page.route('**/*', async route=>{
    const request = route.request(), url = new URL(request.url());
    if(url.origin!==base) {await route.abort();throw new Error('禁止外部请求：'+url.origin);}
    if(!url.pathname.startsWith('/api/')) return route.continue();
    let data;
    if(url.pathname==='/api/v1/datasets' && request.method()==='GET') data=datasets;
    else if(url.pathname==='/api/v1/admin/datasets' && request.method()==='POST') {
      const body=request.postDataJSON();submitted.datasets.push(body);
      const created={slug: body.slug, name: body.name, definition: body};datasets.push(created);data=created;
    } else if(url.pathname==='/api/v1/admin/sources' && request.method()==='GET') data=[source];
    else if(url.pathname==='/api/v1/admin/sources/'+definition.slug && request.method()==='PUT') {
      const body=request.postDataJSON();submitted.sources.push(body);source={...source,...body,revision:source.revision+1};data=source;
    } else {errors.push('未预期的 API：'+request.method()+' '+url.pathname);return route.fulfill({status: 500, json: {message:'未预期的测试请求'}});}
    return route.fulfill({status: 200, json: {code: 200, data}});
  });
  try {
    await page.goto(base+'/admin/#tables');
    await page.locator(`[data-schema="${definition.slug}"]`).click();
    const rows=page.locator('#editor tbody tr');
    assert.equal(await rows.count(),4);
    assert.equal(await rows.nth(0).locator('td').nth(1).innerText(),definition.fields[0].description);
    assert.equal(await rows.nth(0).locator('td').nth(5).innerText(),'是');
    assert.equal(await rows.nth(1).locator('td').nth(5).innerText(),'—');
    assert.equal(await rows.nth(1).locator('td').nth(6).innerText(),'是');
    passed('表结构显示中文说明与单字段唯一键，业务日期独立显示');
    assert.equal(await rows.nth(3).locator('td').nth(1).innerText(),'尚未填写说明');
    passed('旧表缺少说明时显示尚未填写说明');
    const description=rows.nth(2).locator('.schema-description');
    assert.equal(await description.innerText(),unsafeDescription);
    assert.equal(await page.locator('#editor img').count(),0);
    assert.equal(await page.evaluate(()=>window.descriptionInjected),undefined);
    assert.equal(await description.evaluate(element=>getComputedStyle(element).whiteSpace),'normal');
    assert(await description.evaluate(element=>element.getBoundingClientRect().height>parseFloat(getComputedStyle(element).lineHeight)));
    passed('长说明自动换行且 HTML 文本不执行');
    if(process.env.UI_EVIDENCE_DIR) {
      fs.mkdirSync(process.env.UI_EVIDENCE_DIR,{recursive:true});
      await page.screenshot({path:path.join(process.env.UI_EVIDENCE_DIR,'field-descriptions-schema.png'),fullPage:true});
    }
    await closeDialog();await page.locator('#new-table').click();
    assert.equal(await page.locator('[name=field_description]').count(),2);
    for(const control of await page.locator('[name=field_description]').all()) {
      assert(await control.inputValue());assert.equal(await control.getAttribute('required'),'');assert.equal(await control.getAttribute('maxlength'),'500');
    }
    passed('新建表默认日期和 ID 行已有说明，所有说明必填且最多 500 字');
    await page.locator('#dataset-form [name=name]').fill('说明保存验收');
    await page.locator('#dataset-form [name=slug]').fill('field_description_created');
    await page.locator('#add-field').click();
    const added=page.locator('#dataset-fields tr').last();
    await added.locator('[name=field_name]').fill('amount');
    await added.locator('[name=field_type]').selectOption('decimal');
    await page.locator('[form=dataset-form]').click();
    assert.equal(await added.locator('[name=field_description]').evaluate(element=>element.validity.valueMissing),true);
    assert.equal(submitted.datasets.length,0);
    await added.locator('[name=field_description]').fill('   ');
    await page.locator('[form=dataset-form]').click();
    await page.getByText('请为每个字段填写中文说明。',{exact:true}).waitFor();
    assert.equal(submitted.datasets.length,0);
    passed('缺失和全空格说明均阻止提交，不发起建表请求');
    await added.locator('[name=field_description]').fill(' 支付金额，单位元，缺失保留 NULL ');
    const createResponse=page.waitForResponse(response=>response.url().endsWith('/api/v1/admin/datasets'));
    await page.locator('[form=dataset-form]').click();await createResponse;
    await page.locator('[data-schema="field_description_created"]').waitFor();
    assert.equal(submitted.datasets[0].fields[2].description,'支付金额，单位元，缺失保留 NULL');
    passed('说明 trim 后随数据表字段定义提交');
    await page.locator('.navigation a[href="#sources"]').click();
    await page.locator(`[data-edit-source="${definition.slug}"]`).click();
    assert.equal(await page.locator('[name=page_start]').inputValue(),'1');
    assert.equal(await page.locator('[name=page_start]').isDisabled(),false);
    assert.equal(await page.locator('[name=method]').isDisabled(),true);
    assert.equal(await page.locator('[name=parameter_location]').inputValue(),'json');
    assert.equal(await page.locator('[name=page_size]').getAttribute('max'),'200');
    await page.locator('[name=page_start]').selectOption('0');
    await page.locator('[name=protocol]').selectOption('http_json');
    await page.locator('[name=page_size]').fill('500');
    await page.locator('[name=protocol]').selectOption('vendor_batch');
    assert.equal(await page.locator('[name=page_start]').inputValue(),'0');
    assert.equal(await page.locator('[name=page_size]').inputValue(),'200');
    await page.locator('[name=page_start]').selectOption('1');
    passed('供应商起始页可选 0 或 1，协议切换保留页码与 POST JSON、200 条限制');
    assert.equal(await page.locator('[data-mapping="product_image"] .mapping-description').innerText(),unsafeDescription);
    assert.equal(await page.locator('[data-mapping="legacy_field"] .mapping-description').innerText(),'尚未填写说明');
    assert.equal(await page.locator('#mapping-rows img').count(),0);
    passed('来源映射显示字段说明并转义 HTML');
    await page.locator('[name=approve_host]').check();
    const sourceResponse=page.waitForResponse(response=>response.url().endsWith('/api/v1/admin/sources/'+definition.slug)&&response.request().method()==='PUT');
    await page.locator('[form=source-form]').click();await sourceResponse;
    await page.locator(`[data-edit-source="${definition.slug}"]`).click();
    assert.equal(submitted.sources[0].config.page_start,1);
    assert.equal(submitted.sources[0].config.method,'POST');
    assert.equal(submitted.sources[0].config.parameter_location,'json');
    assert.equal(await page.locator('[name=page_start]').inputValue(),'1');
    passed('供应商页码 1 提交及重新打开配置均保留');
    await closeDialog();await page.locator('.navigation a[href="#tables"]').click();
    await page.locator(`[data-schema="${definition.slug}"]`).click();
    await page.setViewportSize({width:390,height:844});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    assert(await page.locator('#editor').evaluate(element=>element.getBoundingClientRect().right<=innerWidth));
    passed('窄屏弹窗与页面无横向溢出');
    assert.deepEqual(errors,[]);passed('浏览器无脚本错误或未预期请求');
    const result={passed:checks.length,failed:0,checks,browser:'Microsoft Edge / Playwright',scope:'真实页面脚本，内存 HTTP API 桩；未验证真实后端与数据库'};
    if(process.env.UI_EVIDENCE_DIR) fs.writeFileSync(path.join(process.env.UI_EVIDENCE_DIR,'field-descriptions-result.json'),JSON.stringify(result,null,2));
    console.log(JSON.stringify(result));
  } finally {await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(error=>{console.error(error);process.exitCode=1;server.close();});
