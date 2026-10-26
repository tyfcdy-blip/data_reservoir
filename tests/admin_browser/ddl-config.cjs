// 真实管理页面与 Edge；浏览器路由装载本地静态文件和内存 API 桩，无外部网络及数据库访问。
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require(require.resolve('playwright',{paths:[process.env.BROWSER_NODE_MODULES||'C:/Users/JIE/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules']}));
const root=path.resolve(__dirname,'../..'),staticRoot=path.join(root,'data_service/static'),evidence=process.env.UI_EVIDENCE_DIR||path.join(root,'evidence');
const unsafe='中文说明 <img src=x onerror="window.ddlInjected=true">';
const makeField=(name,type='decimal',description=name)=>({name,type,description,required:false,dimension:false,max_length:255});
const previewFields=[makeField('daily_id','string','稳定商品日行 ID'),makeField('dt','datetime','北京时间数据日期'),makeField('net_sales_amount','string','净销售额'),makeField('owner_person','string','负责人'),makeField('listing_time','datetime','上架时间'),makeField('pay_amount','decimal','支付金额'),...Array.from({length:44},(_,index)=>makeField('metric_'+index,'decimal',index===0?unsafe:'经营指标 '+index))];
const existingFields=previewFields.map(field=>field.name==='dt'?{...field,type:'date',required:true,dimension:true}:field.name==='daily_id'?{...field,required:true}:field);
const definition={slug:'ddl_existing',name:'导入验收现有表',date_field:'dt',key_fields:['daily_id'],retention_days:180,fields:existingFields};
const datasets=[{slug:definition.slug,name:definition.name,definition}];
let source={dataset_slug:definition.slug,title:'来源映射验收',origin:'managed',secret_names:[],enabled:false,schedule_enabled:false,schedule_times:['03:00'],refresh_days:1,write_mode:'upsert',revision:1,config:{dataset_slug:definition.slug,protocol:'http_json',url:'https://example.test/query',method:'POST',parameter_location:'json',date_param:'dt',page_param:'pageIndex',page_size_param:'pageSize',page_start:1,page_size:200,rows_path:'data',field_mapping:Object.fromEntries(existingFields.map(field=>[field.name,field.name==='owner_person'?'nested.owner':field.name])),date_fields_from_datetime:['dt'],text_whitespace_fields:['owner_person'],source_timezone:'Asia/Shanghai'}};
let responseStatus=200,hold=false,release,declaredKey=false,missingComment=false;
const calls=[],creates=[],saves=[],checks=[],errors=[];
(async()=>{
 const base='http://ddl-ui.test';
 const browser=await chromium.launch({headless:true,channel:'msedge',args:['--no-proxy-server']}),page=await browser.newPage({viewport:{width:1440,height:1000},timezoneId:'Asia/Shanghai'});page.setDefaultTimeout(8000);
 const passed=name=>{checks.push(name);console.log('PASS '+name);};
 page.on('pageerror',error=>errors.push(error.message));await page.addInitScript(()=>sessionStorage.setItem('data-service-session','ddl-isolated-ui-session'));
 await page.route('**/*',async route=>{
  const request=route.request(),url=new URL(request.url());if(url.origin!==base){errors.push('禁止外部请求');return route.abort();}
  if(!url.pathname.startsWith('/api/')){
   const filename=url.pathname==='/admin/'?'index.html':url.pathname.slice('/admin/'.length);
   if(!url.pathname.startsWith('/admin/') || !/^[a-z-]+\.(html|js|css)$/.test(filename))return route.fulfill({status:404,body:''});
   return route.fulfill({status:200,contentType:filename.endsWith('.js')?'text/javascript':filename.endsWith('.css')?'text/css':'text/html',body:fs.readFileSync(path.join(staticRoot,filename))});
  }
  calls.push([request.method(),url.pathname]);let data;
  if(url.pathname==='/api/v1/datasets')data=datasets;
  else if(url.pathname==='/api/v1/admin/datasets/ddl-preview'){
   assert.equal(request.method(),'POST');assert.deepEqual(Object.keys(request.postDataJSON()),['sql']);
   if(hold){hold=false;await new Promise(resolve=>release=resolve);}
   if(responseStatus!==200){const status=responseStatus;responseStatus=200;return route.fulfill({status,json:{message:status===403?'仅管理员可解析建表语句':'只支持一条 CREATE TABLE 语句',error_code:status===403?'FORBIDDEN':'DDL_INVALID'}});}
   data={table_name:'ddl_created',table_comment:'商品驾驶舱验收表',fields:structuredClone(previewFields).map(field=>missingComment&&field.name==='owner_person'?{...field,description:''}:declaredKey&&field.name==='daily_id'?{...field,required:true}:field),key_fields:declaredKey?['daily_id']:[],warnings:['DOUBLE 映射为 decimal(38,6)，超出精度会拒绝入库。',unsafe]};
  }else if(url.pathname==='/api/v1/admin/datasets' && request.method()==='POST'){
   const body=request.postDataJSON();creates.push(body);data={slug:body.slug,name:body.name,definition:body};datasets.push(data);
  }else if(url.pathname==='/api/v1/admin/sources' && request.method()==='GET')data=[source];
  else if(url.pathname==='/api/v1/admin/sources/'+definition.slug && request.method()==='PUT'){
   const body=request.postDataJSON();saves.push(body);source={...source,...body,revision:source.revision+1};data=source;
  }else{errors.push('未预期 API '+request.method()+' '+url.pathname);return route.fulfill({status:500,json:{message:'未预期请求'}});}
  return route.fulfill({status:200,json:{code:200,data}});
 });
 const close=()=>page.locator('#editor .dialog-head [data-close]').click();
 const open=async()=>{await page.locator('#new-table').click();await page.locator('#ddl-import summary').click();};
 const parse=async()=>{await page.locator('#ddl-preview').click();await page.locator('#ddl-preview:not([aria-busy])').waitFor();};
 const previewCalls=()=>calls.filter(([,url])=>url.endsWith('/ddl-preview')).length;
 const row=name=>page.locator('#ddl-fields [data-row]').filter({has:page.locator(`[name=field_name][value="${name}"]`)});
 let failure;
 try{
  await page.goto(base+'/admin/#tables');await open();
  await page.locator('#dataset-form [name=name]').fill('手工名称保持');await page.locator('#dataset-form [name=slug]').fill('manual_preserved');
  assert.equal(await page.locator('#dataset-fields [data-row]').count(),2);await parse();assert.match(await page.locator('#ddl-error').innerText(),/先粘贴/);assert.equal(previewCalls(),0);passed('空语句本地阻止请求，默认手工字段保留');
  await page.locator('#ddl-sql').fill('中'.repeat(22000));await parse();assert.match(await page.locator('#ddl-error').innerText(),/64 KiB/);assert.equal(previewCalls(),0);passed('按 UTF-8 字节限制 64 KiB，中文超限不请求');
  await page.locator('#ddl-sql').fill('CREATE TABLE test; DROP TABLE test;');responseStatus=422;await parse();assert.match(await page.locator('#ddl-error').innerText(),/只支持一条/);assert.equal(await page.locator('#dataset-form [name=name]').inputValue(),'手工名称保持');passed('解析失败展示后端错误，不覆盖手工数据');
  responseStatus=403;await parse();assert.match(await page.locator('#ddl-error').innerText(),/仅管理员/);assert.equal(await page.locator('#ddl-preview-content').isHidden(),true);passed('权限错误不产生可应用草稿');
  await page.locator('#ddl-sql').fill('CREATE TABLE example (...)');await parse();
  assert.equal(await page.locator('#ddl-fields [data-row]').count(),50);assert.equal(await page.locator('#ddl-fields [name=field_key]:checked').count(),0);assert.equal(await page.locator('#ddl-date-field').inputValue(),'');assert.equal(await page.locator('#dataset-fields [data-row]').count(),2);assert.equal(creates.length,0);passed('50 字段预览保留中文说明，不猜日期和未声明主键，不自动建表');
  assert.equal(await page.locator('#ddl-import img').count(),0);assert.equal(await page.evaluate(()=>window.ddlInjected),undefined);assert.match(await row('metric_0').locator('[name=field_description]').inputValue(),/<img/);passed('表字段和警告中的 HTML 仅按文字显示');
  await page.locator('#ddl-apply').click();assert.match(await page.locator('#ddl-error').innerText(),/1—5/);assert.equal(await page.locator('#dataset-fields [data-row]').count(),2);passed('未选择唯一键时拒绝应用');
  await row('daily_id').locator('[name=field_key]').check();assert.equal(await row('daily_id').locator('[name=field_required]').isChecked(),true);
  await page.locator('#ddl-apply').click();assert.match(await page.locator('#ddl-error').innerText(),/业务日期/);passed('勾选主键自动必填，业务日期必须显式选择');
  await page.locator('#ddl-date-field').selectOption('dt');await row('net_sales_amount').locator('[name=field_type]').selectOption('decimal');await row('owner_person').locator('[name=field_dimension]').check();await row('listing_time').locator('[name=field_type]').selectOption('string');await row('listing_time').locator('[name=field_description]').fill('来源上架时间原文');
  await row('daily_id').locator('[name=field_length]').fill('10001');await page.locator('#ddl-apply').click();assert.match(await page.locator('#ddl-error').innerText(),/1—10000/);await row('daily_id').locator('[name=field_length]').fill('64');passed('预览可修改类型、说明、维度和长度，非法长度阻止应用');
  await row('pay_amount').locator('[name=field_description]').fill(' ');await page.locator('#ddl-apply').click();assert.match(await page.locator('#ddl-error').innerText(),/中文说明/);await row('pay_amount').locator('[name=field_description]').fill('支付金额，单位元');passed('导入字段缺少说明不能应用');
  await page.locator('#dataset-form [name=retention_days]').fill('90');await page.locator('#ddl-apply').click();
  assert.equal(await page.locator('#ddl-import').evaluate(element=>element.open),false);assert.equal(await page.locator('#dataset-fields [data-row]').count(),50);assert.equal(await page.locator('#dataset-form [name=date_field]').inputValue(),'dt');assert.equal(await page.locator('#dataset-form [name=retention_days]').inputValue(),'90');assert.equal(creates.length,0);passed('显式应用替换表名标识和50字段，保留天数，仍未写入');
  await page.locator('[form=dataset-form]').click();await page.locator('[data-schema="ddl_created"]').waitFor();assert.equal(creates.length,1);assert.deepEqual(creates[0].key_fields,['daily_id']);assert.equal(creates[0].fields.find(field=>field.name==='dt').type,'date');assert.equal(creates[0].fields.find(field=>field.name==='dt').required,true);assert.equal(creates[0].fields.find(field=>field.name==='dt').dimension,true);assert.equal(creates[0].fields.find(field=>field.name==='net_sales_amount').type,'decimal');assert.equal(creates[0].fields.find(field=>field.name==='listing_time').type,'string');assert.equal(creates[0].fields.find(field=>field.name==='owner_person').dimension,true);passed('最终创建复用现有接口，提交人工确认的日期主键和类型');
  await open();await page.locator('#ddl-sql').fill('CREATE TABLE first (...)');hold=true;const requested=page.waitForRequest(request=>request.url().endsWith('/ddl-preview'));await page.locator('#ddl-preview').click();await requested;await page.waitForFunction(()=>document.querySelector('#ddl-preview').getAttribute('aria-busy')==='true');const before=previewCalls();await page.locator('#ddl-preview').evaluate(button=>button.click());assert.equal(previewCalls(),before);await page.locator('#ddl-sql').fill('CREATE TABLE changed (...)');while(!release)await new Promise(resolve=>setTimeout(resolve,10));release();release=null;await page.locator('#ddl-preview:not([aria-busy])').waitFor();assert.equal(await page.locator('#ddl-preview-content').isHidden(),true);assert.match(await page.locator('#ddl-status').innerText(),/语句已修改/);passed('请求中重复点击被阻止，改写语句后忽略旧响应');
  hold=true;const second=page.waitForRequest(request=>request.url().endsWith('/ddl-preview'));await page.locator('#ddl-preview').click();await second;await close();await open();await page.locator('#dataset-form [name=name]').fill('新弹窗内容');while(!release)await new Promise(resolve=>setTimeout(resolve,10));release();release=null;await page.waitForTimeout(150);assert.equal(await page.locator('#ddl-preview-content').isHidden(),true);assert.equal(await page.locator('#dataset-form [name=name]').inputValue(),'新弹窗内容');passed('关闭后复用弹窗，旧请求不能覆盖新表单');
  await page.locator('#ddl-sql').fill('CREATE TABLE declared (...)');declaredKey=true;await parse();assert.equal(await row('daily_id').locator('[name=field_key]').isChecked(),true);assert.equal(await page.locator('#ddl-date-field').inputValue(),'');passed('DDL 声明主键可预选，业务日期仍由管理员选择');
  fs.mkdirSync(evidence,{recursive:true});await page.screenshot({path:path.join(evidence,'ddl-frontend-preview.png'),fullPage:true});await page.setViewportSize({width:390,height:844});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));assert(await page.locator('#editor').evaluate(element=>element.getBoundingClientRect().right<=innerWidth));passed('窄屏预览表横向滚动，页面与弹窗无横向溢出');await page.setViewportSize({width:1440,height:1000});await close();
  await page.locator('#new-table').click();await page.locator('#dataset-form [name=name]').fill('纯手工创建');await page.locator('#dataset-form [name=slug]').fill('manual_created');await page.locator('[form=dataset-form]').click();await page.locator('[data-schema="manual_created"]').waitFor();assert.equal(creates.length,2);assert.equal(creates[1].fields.length,2);passed('未使用导入时原手工建表仍正常保存');
  await open();await page.locator('#dataset-form [name=name]').fill('保留手工，不应用草稿');await page.locator('#dataset-form [name=slug]').fill('manual_draft_preserved');await page.locator('#ddl-sql').fill('CREATE TABLE missing_comment (...)');missingComment=true;await parse();missingComment=false;
  assert.equal(await row('owner_person').locator('[name=field_description]').inputValue(),'');assert.equal(await page.locator('#ddl-fields input').evaluateAll(controls=>controls.every(control=>control.form===null)),true);passed('预览控件在主表单外，缺失说明不会加入主表单原生校验');
  await row('pay_amount').locator('[name=field_name]').fill('1_invalid');await page.locator('#ddl-import summary').click();await page.locator('[form=dataset-form]').click();await page.locator('[data-schema=manual_draft_preserved]').waitFor();assert.equal(creates.length,3);assert.equal(creates[2].fields.length,2);assert.deepEqual(creates[2].key_fields,['id']);passed('缺说明和半填非法预览折叠后不应用，原手工配置仍可创建');
  await page.locator('.navigation a[href="#sources"]').click();await page.locator(`[data-edit-source="${definition.slug}"]`).click();assert.equal(await page.locator('[data-mapping]').count(),50);assert.match(await page.locator('#mapping-count').innerText(),/50 \/ 50.*49 项/);assert.equal(await page.locator('[data-mapping=owner_person] [name=normalize_whitespace]').isChecked(),true);assert.equal(await page.locator('[data-mapping=daily_id] [name=normalize_whitespace]').isChecked(),false);passed('来源加载50项映射与计数，恢复显式文本转换配置');
  page.once('dialog',dialog=>dialog.dismiss());await page.locator('#reset-mapping').click();assert.equal(await page.locator('[data-mapping=owner_person] [name=mapping_value]').inputValue(),'nested.owner');passed('取消重置保留已有自定义映射');
  page.once('dialog',dialog=>{assert.match(dialog.message(),/自定义路径会被覆盖/);dialog.accept();});await page.locator('#reset-mapping').click();assert.equal(await page.locator('[data-mapping=owner_person] [name=mapping_value]').inputValue(),'owner_person');assert.equal(await page.locator('[data-mapping=dt] [name=convert_date]').isChecked(),true);assert.equal(await page.locator('[data-mapping=owner_person] [name=normalize_whitespace]').isChecked(),true);assert.match(await page.locator('#mapping-count').innerText(),/50 \/ 50.*50 项/);passed('确认重置为同名映射，仅重置路径，保留转换选项');
  await page.locator('[data-mapping=pay_amount] [name=mapping_value]').fill('');assert.match(await page.locator('#mapping-count').innerText(),/49 \/ 50/);await page.locator('[data-mapping=pay_amount] [name=mapping_value]').fill('pay_amount');await page.locator('[name=approve_host]').check();await page.locator('[form=source-form]').click();await page.locator(`[data-edit-source="${definition.slug}"]`).waitFor();assert.equal(saves.length,1);assert.equal(Object.keys(saves[0].config.field_mapping).length,50);assert.deepEqual(saves[0].config.date_fields_from_datetime,['dt']);assert.deepEqual(saves[0].config.text_whitespace_fields,['owner_person']);passed('映射计数实时更新，来源保存完整映射及显式转换字段');
  await page.locator('#new-source').click();assert.equal(await page.locator('[name=normalize_whitespace]:checked').count(),0);assert.match(await page.locator('#mapping-count').innerText(),/50 \/ 50.*50 项/);passed('新接入默认同名映射，文本转换默认关闭');
  assert.deepEqual(errors,[]);passed('无浏览器脚本错误、外部访问或未定义API请求');
 }catch(error){failure=error;throw error;}
 finally{
  fs.mkdirSync(evidence,{recursive:true});fs.writeFileSync(path.join(evidence,'ddl-frontend-result.json'),JSON.stringify({passed:checks.length,failed:failure?1:0,checks,error:failure?String(failure):null,browser:'Microsoft Edge / Playwright',scope:'真实管理页面；浏览器路由装载本地静态文件和API内存桩，无网络访问。未替代真实解析器、数据库或供应商验证。'},null,2));await browser.close();
 }
 console.log(JSON.stringify({passed:checks.length,failed:0}));
})().catch(error=>{console.error(error);process.exitCode=1;});
