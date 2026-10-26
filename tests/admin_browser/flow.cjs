// 使用真实浏览器、隔离服务和本机合成HTTP源的可重复验收，不打印凭据。
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { chromium } = require(require.resolve('playwright', {paths: [process.env.BROWSER_NODE_MODULES || 'C:/Users/JIE/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules']}));
const root = path.resolve(__dirname, '../..');
const configuration = Object.fromEntries(fs.readFileSync(path.join(root, '.local/test-admin-ui.env'), 'utf8').trim().split(/\r?\n/).filter(line=>line.includes('=')).map(line=>{const at=line.indexOf('=');return [line.slice(0,at),line.slice(at+1).replace(/^"|"$/g,'')];}));
const base = 'http://127.0.0.1:18091';
const suffix = Date.now().toString(36);
const slug = 'browser_sales_' + suffix;
const serviceSlug = 'browser_summary_' + suffix;
const evidence = path.join(root, 'evidence/admin-browser');
fs.mkdirSync(evidence, {recursive:true});

(async()=>{
  const browser = await chromium.launch({headless:true,channel:'msedge'});
  const page = await browser.newPage({viewport:{width:1440,height:900},timezoneId:'Asia/Shanghai'});
  const errors=[];page.on('pageerror', error=>errors.push(error.message));
  const steps=[];
  const record = name=>{steps.push(name);console.log('PASS '+name);};
  const closeDialog=()=>page.locator('#editor .dialog-head [data-close]').click();
  const submit=async(selector,url)=>{
    const [response] = await Promise.all([page.waitForResponse(r=>r.url().endsWith(url)&&r.request().method()!=='GET'),page.locator(selector).click()]);
    assert(response.ok(), 'API failure '+response.status()+' '+url+' '+await response.text());
    return response.json();
  };
  const visit=async(name)=>{
    await page.locator(`.navigation a[href="#${name}"]`).click();
    const headings={overview:'服务概览',tables:'数据表',sources:'接口接入',services:'加工与开放',keys:'调用密钥',sync:'同步记录'};
    await page.locator('#content').getByRole('heading',{name:headings[name],exact:true}).waitFor();
  };
  try {
    await page.goto(base+'/admin/');
    await page.locator('#login-form [name=username]').fill(configuration.DS_ADMIN_USERNAME);
    await page.locator('#login-form [name=password]').fill(configuration.DS_ADMIN_PASSWORD);
    await page.locator('#login-form [type=submit]').click();
    await page.getByRole('heading',{name:'服务概览',exact:true}).waitFor();record('真实管理员登录与概览');
    await visit('tables');await page.locator('#new-table').click();
    await page.locator('#dataset-form [name=name]').fill('浏览器验收销售表');
    await page.locator('#dataset-form [name=slug]').fill(slug);
    for(const [name,type,dimension,description] of [['shop','string',true,'店铺名称'],['amount','decimal',false,'销售金额，单位元'],['units','integer',false,'商品销售件数']]){
      await page.locator('#add-field').click();const row=page.locator('#dataset-fields tr').last();
      await row.locator('[name=field_name]').fill(name);await row.locator('[name=field_description]').fill(description);await row.locator('[name=field_type]').selectOption(type);
      if(dimension)await row.locator('[name=field_dimension]').check();
    }
    await submit('[form=dataset-form]','/api/v1/admin/datasets');
    await page.locator(`[data-schema="${slug}"]`).waitFor();record('页面新建真实数据表与字段');
    await visit('sources');await page.locator('#new-source').click();
    await page.locator('#source-form [name=title]').fill('本机浏览器验收来源');
    await page.locator('#source-form [name=dataset_slug]').selectOption(slug);
    await page.locator('#source-form [name=url]').fill('http://127.0.0.1:18093/sales');
    await page.locator('#source-form [name=method]').selectOption('GET');
    await page.locator('#source-form [name=approve_host]').check();
    await page.locator('#add-header').click();
    await page.locator('[name=header_name]').fill('Authorization');
    await page.locator('[name=header_reference]').fill('BROWSER_AUTH');
    await page.locator('[name=header_value]').fill(configuration.UI_SOURCE_AUTH);
    await submit('[form=source-form]','/api/v1/admin/sources/'+slug);
    await page.locator(`[data-test-source="${slug}"]`).waitFor();record('页面保存来源、凭据和字段映射');
    await page.locator(`[data-test-source="${slug}"]`).click();
    const tested=await submit('#test-source [type=submit]','/api/v1/admin/sources/'+slug+'/test');
    assert.equal(tested.data.row_count,3);await closeDialog();record('真实HTTP单页连接测试');
    await page.locator(`[data-sync-source="${slug}"]`).click();
    const businessDay=await page.locator('#sync-form [name=start_date]').inputValue();
    await submit('[form=sync-form]','/api/v1/admin/datasets/'+slug+'/sync');
    await page.getByRole('heading',{name:'同步记录',exact:true}).waitFor();
    const token=await page.evaluate(()=>sessionStorage.getItem('data-service-session'));
    for(let attempt=0;attempt<30;attempt++){
      const response=await page.request.get(base+'/api/v1/admin/status',{headers:{Authorization:'Bearer '+token}});
      const jobs=(await response.json()).data.recent_batches.filter(job=>job.dataset_slug===slug);
      if(jobs.some(job=>job.status==='succeeded'))break;
      assert(!jobs.some(job=>job.status==='failed'),'source sync failed');
      if(attempt===29)throw Error('sync timeout');await new Promise(resolve=>setTimeout(resolve,500));
    }
    await page.locator('#refresh-sync').click();record('常驻worker完整分页同步发布');
    await visit('services');await page.locator('#new-service').click();
    await page.locator('#service-form [name=name]').fill('浏览器验收店铺汇总');
    await page.locator('#service-form [name=slug]').fill(serviceSlug);
    await page.locator('#service-form [name=dataset_slug]').selectOption(slug);
    await page.locator('[name=mode][value=aggregate]').check();
    await page.locator('[name=group_field][value=shop]').check();
    await page.locator('#add-aggregate').click();
    await page.locator('[name=aggregate_field]').selectOption('amount');
    await page.locator('[name=aggregate_op]').selectOption('sum');
    await page.locator('[name=aggregate_alias]').fill('total_amount');
    await page.locator('[name=allowed_field][value=shop]').check();
    const preview=await submit('#preview-service','/api/v1/admin/query-services/preview');
    assert.deepEqual(preview.data.rows.map(row=>row.total_amount).sort(),['130.000000','20.000000']);
    record('页面聚合预览结果独立核算');
    await page.locator('#service-form [name=enabled]').check();
    await submit('[form=service-form]','/api/v1/admin/query-services/'+serviceSlug);
    await page.locator(`[data-example-service="${serviceSlug}"]`).waitFor();
    await page.screenshot({path:path.join(evidence,'services-desktop.png'),fullPage:true});record('保存并开放命名接口');
    await visit('keys');await page.locator('#new-key').click();
    await page.locator('#key-form [name=name]').fill('浏览器调用系统');
    await page.locator('#key-form [name=key_mode]').selectOption('legacy');
    await page.locator(`[name=grant_read][value="${slug}"]`).check();
    const issued=await submit('[form=key-form]','/api/v1/admin/keys');
    await page.locator('#created-key').waitFor();
    const key=await page.locator('#created-key').innerText();assert.equal(key,issued.data.api_key);
    await closeDialog();
    const called=await page.request.post(base+'/api/v1/services/'+serviceSlug+'/query',{
      headers:{'X-API-Key':key},data:{start_date:businessDay,end_date:businessDay,filters:[{field:'shop',op:'eq',value:'杭州店'}]}});
    assert.equal(called.status(),200);assert.deepEqual((await called.json()).data.rows,[{shop:'杭州店',total_amount:'130.000000'}]);record('机器密钥真实调用开放接口');
    await page.locator(`[data-revoke-key="${issued.data.id}"]`).click();await page.locator('#confirm-revoke').click();
    await page.waitForResponse(r=>r.url().endsWith('/api/v1/admin/keys')&&r.request().method()==='GET');
    assert.equal((await page.request.get(base+'/api/v1/services',{headers:{'X-API-Key':key}})).status(),401);record('页面撤销密钥后调用失效');
    await visit('overview');
    await page.waitForFunction(()=>document.querySelectorAll('.toast').length===0);
    await page.screenshot({path:path.join(evidence,'overview-desktop.png'),fullPage:true});
    await page.setViewportSize({width:390,height:844});
    await page.waitForFunction(()=>document.querySelector('.sidebar').getBoundingClientRect().right<=1);
    await page.screenshot({path:path.join(evidence,'overview-mobile.png'),fullPage:true});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    await page.locator('.nav-toggle').click();await page.locator('.navigation a[href="#sources"]').click();
    await page.getByRole('heading',{name:'接口接入',exact:true}).waitFor();record('窄屏导航与页面无横向溢出');
    await page.setViewportSize({width:1440,height:900});await page.locator('#logout').click();
    await page.locator('#login-form').waitFor();
    assert.equal(await page.evaluate(()=>sessionStorage.getItem('data-service-session')),null);
    assert.equal((await page.request.get(base+'/api/v1/admin/status',{headers:{Authorization:'Bearer '+token}})).status(),401);
    assert.deepEqual(errors,[]);record('真实退出撤销会话、浏览器无脚本错误');
    fs.writeFileSync(path.join(evidence,'result.json'),JSON.stringify({passed:steps.length,failed:0,steps,browser:'Microsoft Edge / Playwright',database:'admin_ui_test',real_source:false},null,2));
  } catch(error) {
    await page.screenshot({path:path.join(root,'.local/ui-failure.png'),fullPage:true});
    console.error(error.message);process.exitCode=1;
  } finally {await browser.close();}
})();
