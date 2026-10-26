// 本机隔离服务 + 真实 Edge + PostgreSQL；不访问第三方，不启动调度 worker。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(require.resolve('playwright', {paths: ['C:/Users/JIE/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules']}));
const origin = 'http://127.0.0.1:18995';
const checks = [], errors = [], unexpected = [];
(async () => {
  const browser = await chromium.launch({headless:true, channel:'msedge'});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:950}});
    page.on('pageerror', error=>errors.push(error.message));
    await page.route('**/*', route=>{
      if(!route.request().url().startsWith(origin+'/')) {unexpected.push(route.request().url());return route.abort();}
      return route.continue();
    });
    await page.goto(origin+'/admin/#sources');
    await page.locator('[name=username]').fill('multi_review');
    await page.locator('[name=password]').fill('synthetic-multi-review-2026!');
    await page.locator('#login-form [type=submit]').click();
    await page.locator('[data-edit-source=review_orders]').click();
    const form=page.locator('#source-form');
    const times=()=>form.locator('[name=schedule_time]');
    assert.equal(await times().count(),1);
    assert.equal(await times().first().inputValue(),'05:00');
    assert.equal(await form.locator('[name=write_mode]').inputValue(),'replace_day');
    assert.equal(await form.locator('[data-remove-time]').first().isDisabled(),true);
    checks.push('旧小时配置按05:00呈现，保留整日替换；唯一时间不可删除');

    await form.locator('#add-schedule-time').click();
    assert.equal(await times().nth(1).evaluate(element=>element===document.activeElement),true);
    await times().nth(1).fill('05:00');
    assert.equal(await form.evaluate(element=>element.checkValidity()),false);
    assert.match(await form.locator('#schedule-time-error').innerText(),/重复/);
    await times().nth(1).fill('23:10');
    await form.locator('#add-schedule-time').click();
    await times().nth(2).fill('14:35');
    assert.equal(await form.locator('#schedule-time-error').innerText(),'');
    checks.push('新增时间自动获得键盘焦点；重复时间被阻止，修正后错误清除');

    await form.locator('[name=approve_host]').check();
    await form.locator('[name=write_mode]').selectOption('upsert');
    assert.match(await form.locator('#write-mode-help').innerText(),/未返回.*保留|保留.*未返回/);
    await form.locator('[name=schedule_enabled]').check();
    const [saved] = await Promise.all([
      page.waitForResponse(response=>response.url().endsWith('/api/v1/admin/sources/review_orders')&&response.request().method()==='PUT'),
      page.locator('[form=source-form]').click()
    ]);
    assert.equal(saved.status(),200);
    const configured=(await saved.json()).data;
    assert.deepEqual(configured.schedule_times,['05:00','14:35','23:10']);
    assert.equal(configured.write_mode,'upsert');
    assert.equal(configured.enabled,false);
    assert.equal(configured.schedule_enabled,true);
    await page.reload();
    await page.locator('[data-edit-source=review_orders]').click();
    assert.deepEqual(await times().evaluateAll(elements=>elements.map(element=>element.value)),['05:00','14:35','23:10']);
    assert.equal(await form.locator('[name=write_mode]').inputValue(),'upsert');
    checks.push('三个分钟级时间排序保存到真实数据库，刷新后保持；按主键更新持久化');

    await form.locator('[name=approve_host]').check();
    const [unchanged] = await Promise.all([
      page.waitForResponse(response=>response.url().endsWith('/api/v1/admin/sources/review_orders')&&response.request().method()==='PUT'),
      page.locator('[form=source-form]').click()
    ]);
    assert.deepEqual((await unchanged.json()).data.schedule_times,['05:00','14:35','23:10']);
    checks.push('再次编辑保存不会把多个时点改回单小时');

    await page.locator('[data-edit-source=review_orders]').click();
    for(let index=3;index<24;index++) await form.locator('#add-schedule-time').click();
    assert.equal(await times().count(),24);
    assert.equal(await form.locator('#add-schedule-time').isDisabled(),true);
    assert.equal(await form.evaluate(element=>element.checkValidity()),false);
    for(let index=24;index>1;index--) await form.locator('[data-remove-time]').last().click();
    assert.equal(await times().count(),1);
    assert.equal(await form.locator('[data-remove-time]').first().isDisabled(),true);
    checks.push('最多24个时点，空时点阻止保存；删除到1个时保留底线');
    await page.locator('#editor [data-close]').first().click();
    await page.locator('[data-edit-source=review_orders]').click();
    assert.equal(await times().count(),3);
    checks.push('取消编辑不会持久化未保存的时点删除');

    await page.setViewportSize({width:390,height:844});
    await form.locator('#daily-schedule').scrollIntoViewIfNeeded();
    const bounds=await form.locator('#schedule-time-rows').boundingBox();
    assert(bounds.x>=0 && bounds.x+bounds.width<=391);
    await page.screenshot({path:path.join(__dirname,'narrow-schedule.png')});
    checks.push('390像素窄屏执行时间区无横向溢出');

    const noAuth = await browser.newContext();
    const denied = await noAuth.request.put(origin+'/api/v1/admin/sources/review_orders',{data:{}});
    assert.equal(denied.status(),401);
    await noAuth.close();
    assert.deepEqual(errors,[]);
    assert.deepEqual(unexpected,[]);
    checks.push('未登录保存被拒绝；无浏览器异常和第三方网络请求');
    fs.writeFileSync(path.join(__dirname,'browser-result.json'),JSON.stringify({passed:checks.length,checks,errors,unexpected},null,2));
    console.log(JSON.stringify({passed:checks.length,checks}));
  } finally {await browser.close();}
})().catch(error=>{console.error(error.stack);process.exitCode=1;});
