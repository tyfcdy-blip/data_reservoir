// 真实前端脚本与 Edge；HTTP API 用内存桩隔离，不读取密钥、不连接数据库或供应商。
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const assert = require('node:assert/strict');
const { chromium } = require(require.resolve('playwright', {paths: [process.env.BROWSER_NODE_MODULES || 'C:/Users/JIE/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules']}));

const root = path.resolve(__dirname, '../..');
const staticRoot = path.join(root, 'data_service/static');
const definition = {
  slug:'multi_schedule_ui',name:'多时间配置验收表',date_field:'dt',key_fields:['row_id'],retention_days:180,
  fields:[
    {name:'row_id',type:'string',required:true,dimension:false,description:'来源唯一标识'},
    {name:'dt',type:'date',required:true,dimension:true,description:'北京时间业务日期'},
  ],
};
const datasets=[{slug:definition.slug,name:definition.name,definition}];
let source={
  dataset_slug:definition.slug,title:'原有小时配置',origin:'managed',secret_names:[],
  enabled:true,schedule_enabled:false,schedule_hour:3,refresh_days:7,revision:1,
  config:{dataset_slug:definition.slug,protocol:'http_json',url:'https://example.com/query',method:'POST',parameter_location:'json',
    date_param:'dt',page_param:'pageIndex',page_size_param:'pageSize',page_start:1,page_size:200,rows_path:'data',
    field_mapping:{row_id:'row_id',dt:'dt'},source_timezone:'Asia/Shanghai',date_fields_from_datetime:['dt']},
};
const submitted=[];
let failNextSave=false;
const server=http.createServer((request,response)=>{
  const pathname=new URL(request.url,'http://127.0.0.1').pathname;
  const filename=pathname==='/admin/'?'index.html':pathname.slice('/admin/'.length);
  if(!pathname.startsWith('/admin/') || !/^[a-z-]+\.(html|js|css)$/.test(filename)){response.writeHead(404).end();return;}
  const file=path.join(staticRoot,filename);
  if(!fs.existsSync(file)){response.writeHead(404).end();return;}
  response.setHeader('Content-Type',filename.endsWith('.js')?'text/javascript':filename.endsWith('.css')?'text/css':'text/html');
  response.end(fs.readFileSync(file));
});

(async()=>{
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const base=`http://127.0.0.1:${server.address().port}`;
  const browser=await chromium.launch({headless:true,channel:'msedge'});
  const page=await browser.newPage({viewport:{width:1440,height:1000},timezoneId:'Asia/Shanghai'});
  page.setDefaultTimeout(8000);
  const errors=[],checks=[];
  const passed=name=>{checks.push(name);console.log('PASS '+name);};
  const closeDialog=()=>page.locator('#editor .dialog-head [data-close]').click();
  const openSource=()=>page.locator(`[data-edit-source="${definition.slug}"]`).click();
  const times=()=>page.locator('[name=schedule_time]');
  const save=async()=>{
    await page.locator('[name=approve_host]').check();
    const response=page.waitForResponse(reply=>reply.url().endsWith('/api/v1/admin/sources/'+definition.slug)&&reply.request().method()==='PUT');
    const reload=page.waitForResponse(reply=>reply.url().endsWith('/api/v1/admin/sources')&&reply.request().method()==='GET');
    await page.locator('[form=source-form]').click();await response;await reload;
    await page.waitForFunction(()=>!document.querySelector('#editor').open);
    await page.locator('[data-edit-source]').waitFor({state:'visible'});
  };
  page.on('pageerror',error=>errors.push(error.message));
  await page.addInitScript(()=>sessionStorage.setItem('data-service-session','isolated-multi-schedule-token'));
  await page.route('**/*',async route=>{
    const request=route.request(),url=new URL(request.url());
    if(url.origin!==base){errors.push('禁止外部请求');return route.abort();}
    if(!url.pathname.startsWith('/api/'))return route.continue();
    let data;
    if(url.pathname==='/api/v1/datasets' && request.method()==='GET')data=datasets;
    else if(url.pathname==='/api/v1/admin/sources' && request.method()==='GET')data=[source];
    else if(url.pathname==='/api/v1/admin/sources/'+definition.slug && request.method()==='PUT'){
      const body=request.postDataJSON();submitted.push(body);
      if(failNextSave){failNextSave=false;return route.fulfill({status:503,json:{message:'测试隔离：配置服务暂不可用'}});}
      source={...source,...body,schedule_hour:Number(body.schedule_times[0].slice(0,2)),revision:source.revision+1};data=source;
    }else{errors.push('未预期 API '+request.method()+' '+url.pathname);return route.fulfill({status:500,json:{message:'未预期的测试请求'}});}
    return route.fulfill({status:200,json:{code:200,data}});
  });
  try{
    await page.goto(base+'/admin/#sources');await openSource();
    assert.deepEqual(await times().evaluateAll(controls=>controls.map(control=>control.value)),['03:00']);
    assert.equal(await page.locator('[name=write_mode]').inputValue(),'replace_day');
    assert.equal(await page.locator('[data-remove-time]').isDisabled(),true);
    assert.equal(await page.locator('[name=schedule_enabled]').isChecked(),false);
    passed('旧小时响应回退为 03:00，既有来源保留整日替换且自动开关不变');
    await closeDialog();await page.locator('#new-source').click();
    assert.equal(await page.locator('[name=write_mode]').inputValue(),'upsert');
    assert.equal(await page.locator('[name=schedule_enabled]').isChecked(),false);
    assert.match(await page.locator('#write-mode-help').innerText(),/NULL.*覆盖旧值/);
    assert.match(await page.locator('#write-mode-help').innerText(),/未返回的旧记录保留/);
    await page.locator('[name=write_mode]').selectOption('replace_day');
    assert.match(await page.locator('#write-mode-help').innerText(),/未返回的旧记录会删除，空结果会清空/);
    passed('新增来源默认按主键更新，NULL 覆盖及整日替换删除语义明确');
    await closeDialog();await openSource();await page.locator('[name=approve_host]').check();
    await page.locator('#add-schedule-time').focus();await page.keyboard.press('Enter');
    assert.equal(await times().count(),2);
    assert.equal(await times().last().evaluate(control=>control===document.activeElement),true);
    passed('键盘添加时间后焦点进入新时间输入框');
    await page.locator('[form=source-form]').click();
    assert.equal(submitted.length,0);
    assert.match(await page.locator('#schedule-time-error').innerText(),/完整填写/);
    assert.equal(await times().last().evaluate(control=>control.checkValidity()),false);
    passed('空时间阻止保存并就地提示，不发起 API 请求');
    await times().last().fill('03:00');
    assert.match(await page.locator('#schedule-time-error').innerText(),/不能重复/);
    await page.locator('[form=source-form]').click();assert.equal(submitted.length,0);
    passed('重复时间阻止保存且不静默去重');
    await times().last().evaluate(control=>{control.value='24:00';control.dispatchEvent(new Event('input',{bubbles:true}));});
    assert.equal(await times().last().evaluate(control=>control.checkValidity()),false);
    await page.locator('[form=source-form]').click();assert.equal(submitted.length,0);
    passed('非法 24:00 被时间控件和表单校验拦截');
    await times().last().fill('23:59');await page.locator('#add-schedule-time').click();await times().last().fill('12:35');
    assert.equal(await page.locator('#schedule-time-error').innerText(),'');
    await page.locator('[name=write_mode]').selectOption('upsert');
    await page.locator('[name=schedule_enabled]').check();
    await save();
    assert.deepEqual(submitted[0].schedule_times,['03:00','12:35','23:59']);
    assert.equal(submitted[0].write_mode,'upsert');assert.equal(submitted[0].schedule_enabled,true);
    assert.equal('schedule_hour' in submitted[0],false);
    assert.deepEqual(submitted[0].config.field_mapping,{row_id:'row_id',dt:'dt'});
    passed('多时间按升序提交且保留分钟，写入方式和原字段映射正确，不发送旧小时参数');
    const cell=page.locator('td').filter({has:page.locator('.source-schedule-times')});
    assert.equal(await cell.locator('.source-schedule-times>span').count(),3);
    assert.match(await cell.innerText(),/12:35/);
    assert.equal(await cell.locator('.source-schedule-times').evaluate(control=>getComputedStyle(control).flexWrap),'wrap');
    passed('来源列表显示所有执行时间并支持换行');
    await openSource();assert.deepEqual(await times().evaluateAll(controls=>controls.map(control=>control.value)),['03:00','12:35','23:59']);
    assert.equal(await page.locator('[name=write_mode]').inputValue(),'upsert');
    await page.locator('[name=title]').fill('仅改名称');await save();
    assert.deepEqual(submitted[1].schedule_times,['03:00','12:35','23:59']);
    passed('重新打开及只改名称保存不会丢失已有多个时间');
    await openSource();
    await page.locator('[data-remove-time]').last().click();await page.locator('[data-remove-time]').last().click();
    assert.equal(await times().count(),1);assert.equal(await page.locator('[data-remove-time]').isDisabled(),true);
    await times().first().fill('00:00');
    passed('删除时间后至少保留一个，并支持午夜 00:00');
    for(let hour=1;hour<24;hour++){await page.locator('#add-schedule-time').click();await times().last().fill(`${String(hour).padStart(2,'0')}:00`);}
    assert.equal(await times().count(),24);assert.equal(await page.locator('#add-schedule-time').isDisabled(),true);
    assert.match(await page.locator('#schedule-time-count').innerText(),/24 \/ 24/);
    const beforeTooMany=submitted.length;
    await page.locator('#schedule-time-rows').evaluate(container=>container.append(container.lastElementChild.cloneNode(true)));
    await page.locator('#source-form').evaluate(form=>form.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true})));
    await page.getByText('请设置 1 至 24 个执行时间。',{exact:true}).first().waitFor();
    assert.equal(submitted.length,beforeTooMany);
    await page.locator('[data-remove-time]').last().click();
    passed('最多允许 24 个时间，额外构造第 25 项仍无法提交');
    await save();assert.equal(submitted.at(-1).schedule_times.length,24);
    await openSource();assert.equal(await times().count(),24);
    passed('24 个时间可保存并完整读回');
    await page.locator('[name=approve_host]').check();failNextSave=true;
    const failure=page.waitForResponse(reply=>reply.status()===503);await page.locator('[form=source-form]').click();await failure;
    await page.getByText('测试隔离：配置服务暂不可用',{exact:true}).waitFor();
    assert.equal(await times().count(),24);assert.equal(await page.locator('[form=source-form]').isEnabled(),true);
    passed('保存接口失败时保留编辑内容并恢复按钮，可重新提交');
    while(await times().count()>3)await page.locator('[data-remove-time]').last().click();
    await page.setViewportSize({width:390,height:844});await page.locator('#daily-schedule').scrollIntoViewIfNeeded();
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    assert(await page.locator('#editor').evaluate(control=>control.getBoundingClientRect().right<=innerWidth));
    assert(await page.locator('#daily-schedule').evaluate(control=>control.scrollWidth<=control.clientWidth+1));
    passed('390px 窄屏时间编辑区与弹窗无横向溢出');
    if(process.env.UI_EVIDENCE_DIR){fs.mkdirSync(process.env.UI_EVIDENCE_DIR,{recursive:true});await page.screenshot({path:path.join(process.env.UI_EVIDENCE_DIR,'multi-schedule-mobile.png'),fullPage:true});}
    await page.setViewportSize({width:1440,height:1000});await page.locator('#daily-schedule').scrollIntoViewIfNeeded();
    if(process.env.UI_EVIDENCE_DIR)await page.screenshot({path:path.join(process.env.UI_EVIDENCE_DIR,'multi-schedule-desktop.png'),fullPage:true});
    await closeDialog();
    const rowsHtml=await page.evaluate(async()=>{const {jobsTable}=await import('/admin/operations.js');return jobsTable([
      {dataset_slug:'example',business_date:'2026-10-21',status:'succeeded',row_count:2,published_row_count:5,write_mode:'upsert'},
      {dataset_slug:'example',business_date:'2026-10-21',status:'succeeded',row_count:2,published_row_count:2,write_mode:'replace_day'},
      {dataset_slug:'example',business_date:'2026-10-21',status:'failed',row_count:1,published_row_count:null,write_mode:'upsert'},
      {dataset_slug:'example',business_date:'2026-10-21',status:'succeeded',row_count:2},
    ]);});
    assert.equal((rowsHtml.match(/合并后/g)||[]).length,1);assert.match(rowsHtml,/合并后 5/);
    passed('成功增量任务显示接收数和合并后行数，整日替换、失败和旧响应兼容');
    await page.evaluate(async slug=>{const {syncDialog}=await import('/admin/operations.js');syncDialog(slug);},definition.slug);
    assert.match(await page.locator('#editor .dialog-head p').innerText(),/按来源配置的写入方式/);
    assert.match(await page.locator('#editor .notice').innerText(),/按主键更新会保留未返回的旧记录/);
    passed('手动同步说明与来源写入方式一致');
    assert.deepEqual(errors,[]);passed('浏览器无脚本错误或非预期 API 请求');
    const result={passed:checks.length,failed:0,skipped:0,checks,browser:'Microsoft Edge / Playwright',scope:'真实页面脚本与内存 HTTP API 桩；不验证真实数据库和定时执行'};
    if(process.env.UI_EVIDENCE_DIR)fs.writeFileSync(path.join(process.env.UI_EVIDENCE_DIR,'multi-schedule-ui-result.json'),JSON.stringify(result,null,2));
    console.log(JSON.stringify(result));
  }finally{await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(error=>{console.error(error);process.exitCode=1;server.close();});

