// 真实静态管理页 + Edge；路由仅使用内存 API 桩，不连接运行服务或创建真实密钥。
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require(require.resolve('playwright',{paths:[process.env.BROWSER_NODE_MODULES||'C:/Users/JIE/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules']}));
const root=path.resolve(__dirname,'../..'),staticRoot=path.join(root,'data_service/static'),evidence=process.env.UI_EVIDENCE_DIR||path.join(root,'evidence');
const hostile='<img src=x onerror="window.keyInjected=true">';
const fields=[{name:'daily_id',type:'string',description:'稳定行ID',dimension:false,max_length:64},{name:'dt',type:'date',description:'业务日期',dimension:true},{name:'shop_name',type:'string',description:'店铺名称',dimension:true,max_length:255},{name:'channel',type:'string',description:'渠道',dimension:true,max_length:50},{name:'owner_person',type:'string',description:'负责人 '+hostile,dimension:true,max_length:255},{name:'pay_amount',type:'decimal',description:'支付金额',dimension:false}];
const datasets=[{slug:'product_day',name:'商品日表',definition:{fields}},{slug:'other_table',name:'另一张表',definition:{fields}}];
const services=[{slug:'sales',name:'店铺销售汇总',dataset_slug:'product_day',enabled:true},{slug:'refunds',name:'退款汇总 '+hostile,dataset_slug:'product_day',enabled:true},{slug:'other_sales',name:'另一张表汇总',dataset_slug:'other_table',enabled:true},{slug:'disabled',name:'未开放接口',dataset_slug:'product_day',enabled:false}];
const expiry='2099-01-01T00:00:00Z';
let keys=[{id:'legacy',name:'旧密钥',token_hint:'last',grants:{product_day:['read','write']},policy:null,requests_per_minute:60,expires_at:expiry},{id:'scoped',name:'单店 '+hostile,token_hint:'tail',grants:{},policy:{services:{sales:'product_day'},row_scopes:{product_day:{mode:'filtered',groups:[{conditions:[{field:'shop_name',values:['A店 '+hostile]}]}]}}},requests_per_minute:60,expires_at:expiry}];
let hold=false,release=null,createStatus=200,listStatus=200,enabled=true;const creates=[],deletes=[],checks=[],errors=[],requests=[];
(async()=>{
 const base='http://key-permissions-ui.test',browser=await chromium.launch({headless:true,channel:'msedge',args:['--no-proxy-server']});
 const page=await browser.newPage({viewport:{width:1440,height:1000},timezoneId:'Asia/Shanghai'});page.setDefaultTimeout(8000);
 const passed=name=>{checks.push(name);console.log('PASS '+name);};
 page.on('pageerror',error=>errors.push(error.message));
 await page.addInitScript(()=>sessionStorage.setItem('data-service-session','isolated-permissions-test'));
 await page.route('**/*',async route=>{
   const request=route.request(),url=new URL(request.url());if(url.origin!==base){errors.push('禁止外部请求');return route.abort();}
   if(!url.pathname.startsWith('/api/')){
     const filename=url.pathname==='/admin/'?'index.html':url.pathname.slice('/admin/'.length);
     if(!url.pathname.startsWith('/admin/') || !/^[a-z-]+\.(html|js|css)$/.test(filename))return route.fulfill({status:404,body:''});
     return route.fulfill({status:200,contentType:filename.endsWith('.js')?'text/javascript':filename.endsWith('.css')?'text/css':'text/html',body:fs.readFileSync(path.join(staticRoot,filename))});
   }
   requests.push([request.method(),url.pathname]);let data;
   if(url.pathname==='/api/v1/datasets')data=datasets;
   else if(url.pathname==='/api/v1/admin/query-services')data=services.map(service=>({...service,enabled:enabled&&service.enabled}));
   else if(url.pathname==='/api/v1/admin/keys' && request.method()==='GET'){
     if(listStatus!==200){const status=listStatus;listStatus=200;return route.fulfill({status,json:{message:'密钥列表暂时不可用'}});}
     data=keys;
   }else if(url.pathname==='/api/v1/admin/keys' && request.method()==='POST'){
     const payload=request.postDataJSON();creates.push(payload);
     if(hold){hold=false;await new Promise(resolve=>release=resolve);}
     if(createStatus!==200){const status=createStatus;createStatus=200;return route.fulfill({status,json:{message:'授权配置发生变化，请重新加载'}});}
     const stored={...payload,id:'new-'+creates.length,token_hint:'once',expires_at:expiry};keys.push(stored);data={...stored,api_key:'ds_isolated_secret_only_once'};
   }else if(url.pathname.startsWith('/api/v1/admin/keys/') && request.method()==='DELETE'){
     const id=url.pathname.split('/').pop();deletes.push(id);keys=keys.map(key=>key.id===id?{...key,revoked_at:'2026-10-24T00:00:00Z'}:key);data={revoked:true};
   }else{errors.push('未预期 API '+request.method()+' '+url.pathname);return route.fulfill({status:500,json:{message:'未预期请求'}});}
   return route.fulfill({status:200,json:{code:200,data}});
 });
 const close=()=>page.locator('#editor .dialog-head [data-close]').click();
 const open=()=>page.locator('#new-key').click();
 const submit=()=>page.locator('[form=key-form]');
 const formError=()=>page.locator('#key-form .form-error');
 const scope=(slug='product_day')=>page.locator('[data-scope="'+slug+'"]');
 const group=(index=0,slug='product_day')=>scope(slug).locator('[data-group]').nth(index);
 const condition=(index=0,groupIndex=0,slug='product_day')=>group(groupIndex,slug).locator('[data-condition]').nth(index);
 const setCondition=async(field,value,index=0,groupIndex=0,slug='product_day')=>{await condition(index,groupIndex,slug).locator('[name=scope_field]').selectOption(field);await condition(index,groupIndex,slug).locator('[name=scope_values]').fill(value);};
 const chooseSales=()=>page.locator('[name=service_grant][value=sales]').check();
 const name=()=>page.locator('#key-form [name=name]').fill('A店铺验收密钥');
 let failure;
 try{
   await page.goto(base+'/admin/#keys');await page.locator('#new-key').waitFor();
   assert.match(await page.locator('#content').innerText(),/完整数据表权限/);await page.locator('.key-list-summary details summary').click();assert.match(await page.locator('#content').innerText(),/A店/);assert.equal(await page.locator('#content img').count(),0);assert.equal(await page.evaluate(()=>window.keyInjected),undefined);passed('旧密钥整表权限标识与新密钥接口范围摘要可读，名称和值按文字安全显示');
   await open();assert.equal(await page.locator('[name=key_mode]').inputValue(),'services');assert.equal(await submit().isDisabled(),true);assert.equal(await page.locator('[name=service_grant]').count(),3);assert.match(await page.locator('#key-authorization-summary').innerText(),/至少选择/);passed('默认指定接口，空授权不能提交，停用接口不可选');
   await chooseSales();assert.equal(await scope().locator('[name=scope_mode]').inputValue(),'filtered');assert.equal(await submit().isDisabled(),true);const options=await condition().locator('[name=scope_field] option').allTextContents();assert(options.some(text=>text.includes('店铺名称')&&text.includes('shop_name')));assert(!options.some(text=>text.includes('pay_amount')||text.includes('daily_id')||text.includes('业务日期')));passed('选择接口后默认空限定范围，仅提供带中文说明的字符串维度');
   await setCondition('shop_name','A店铺');await name();assert.equal(await submit().isEnabled(),true);
   await condition().locator('[name=scope_values]').fill(' ');assert.equal(await submit().isDisabled(),true);
   await condition().locator('[name=scope_values]').fill('A店铺\n');assert.equal(await submit().isDisabled(),true);
   await condition().locator('[name=scope_values]').fill('A店铺\nA店铺');assert.match(await page.locator('#key-authorization-summary').innerText(),/不能重复/);passed('空白、空行和重复精确值不能提交');
   await condition().locator('[name=scope_values]').fill('A店\t铺');assert.equal(await submit().isDisabled(),true);
   await condition().locator('[name=scope_values]').fill('A'.repeat(256));assert.match(await page.locator('#key-authorization-summary').innerText(),/255/);
   await condition().locator('[name=scope_values]').fill(Array.from({length:101},(_,index)=>'店'+index).join('\n'));assert.match(await page.locator('#key-authorization-summary').innerText(),/100/);passed('控制字符、字段长度及单条件超过100值均阻止提交');
   await setCondition('channel','淘宝');await group().locator('[data-add-condition]').click();await setCondition('shop_name',' A店铺 ',1);assert.equal(await submit().isEnabled(),true);
   await condition(1).locator('[name=scope_field]').selectOption('channel');assert.equal(await submit().isDisabled(),true);assert.match(await page.locator('#key-authorization-summary').innerText(),/重复选择/);await condition(1).locator('[name=scope_field]').selectOption('shop_name');passed('同组渠道与店铺同时满足，字段重复会被阻止，精确值首尾空格保留');
   await scope().locator('[data-add-group]').click();assert.equal(await submit().isDisabled(),true);await setCondition('owner_person','负责人乙',0,1);assert.equal(await submit().isEnabled(),true);assert.match(await page.locator('#key-authorization-summary').innerText(),/且.*或者/s);passed('添加另一组按或关系展示，新增空范围阻止保存');
   await group(1).locator('[data-add-condition]').click();await condition(1,1).locator('[data-remove-condition]').click();assert.equal(await submit().isEnabled(),true);await group(1).locator('[data-remove-group]').click();assert.equal(await scope().locator('[data-group]').count(),1);assert.equal(await condition(1).locator('[name=scope_values]').inputValue(),' A店铺 ');passed('删除条件和范围组保留其他输入，不残留授权');
   await scope().locator('[data-add-group]').click();await setCondition('owner_person','负责人乙',0,1);await page.locator('[name=service_grant][value=refunds]').check();assert.equal(await page.locator('[data-scope]').count(),1);assert.equal(await condition(1).locator('[name=scope_values]').inputValue(),' A店铺 ');passed('同表多个接口复用同一数据范围，不重置已填条件');
   await page.locator('[name=service_grant][value=other_sales]').check();assert.equal(await page.locator('[data-scope]').count(),2);assert.equal(await submit().isDisabled(),true);await scope('other_table').locator('[name=scope_mode]').selectOption('all');assert.equal(await submit().isEnabled(),true);assert.match(await scope('other_table').innerText(),/全部记录/);await page.locator('[name=service_grant][value=other_sales]').uncheck();assert.equal(await page.locator('[data-scope]').count(),1);passed('多表范围逐表必选，全部数据需显式选择，取消接口清除其专属范围');
   fs.mkdirSync(evidence,{recursive:true});await page.locator('#editor .dialog-body').evaluate(element=>element.scrollTop=element.scrollHeight);await page.screenshot({path:path.join(evidence,'permissions-key-summary.png'),fullPage:true});await page.locator('#editor .dialog-body').evaluate(element=>element.scrollTop=0);await page.screenshot({path:path.join(evidence,'permissions-key-desktop.png'),fullPage:true});await page.setViewportSize({width:390,height:844});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));assert(await page.locator('#editor').evaluate(element=>element.getBoundingClientRect().right<=innerWidth));await page.screenshot({path:path.join(evidence,'permissions-key-mobile.png'),fullPage:true});await page.setViewportSize({width:1440,height:1000});passed('1440桌面及390窄屏授权表单无页面或弹窗横向溢出');
   const before=creates.length;createStatus=409;await submit().click();await submit().locator('..').waitFor();await formError().getByText('授权配置发生变化，请重新加载').waitFor();assert.equal(creates.length,before+1);assert.equal(await condition(1).locator('[name=scope_values]').inputValue(),' A店铺 ');passed('后端错误显示在当前表单，填写内容保留供修正');
   hold=true;createStatus=409;await submit().click();while(!release)await new Promise(resolve=>setTimeout(resolve,10));const pendingCount=creates.length;assert.equal(await condition(1).locator('[name=scope_values]').isDisabled(),true);assert.equal(await submit().isDisabled(),true);await page.locator('#key-form').evaluate(form=>form.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true})));assert.equal(creates.length,pendingCount);release();release=null;await submit().locator('xpath=self::*[not(@aria-busy)]').waitFor();passed('请求中锁定表单防止授权摘要变化，重复提交不产生第二请求，失败后恢复');
   await submit().click();await page.locator('#created-key').waitFor();const payload=creates.at(-1);assert.deepEqual(payload.grants,{});assert.deepEqual(payload.policy.services,{sales:'product_day',refunds:'product_day'});assert.deepEqual(payload.policy.row_scopes,{product_day:{mode:'filtered',groups:[{conditions:[{field:'channel',values:['淘宝']},{field:'shop_name',values:[' A店铺 ']}]},{conditions:[{field:'owner_person',values:['负责人乙']}]}]}});assert.equal(payload.expires_in_days,90);assert.equal(payload.requests_per_minute,60);passed('实际提交接口白名单及AND/OR精确范围，服务密钥没有通用表grants');
   await page.evaluate(()=>{Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async value=>{window.copiedKey=value;}}});});await page.locator('#copy-key').click();assert.equal(await page.evaluate(()=>window.copiedKey),'ds_isolated_secret_only_once');await close();await page.locator('#created-key').waitFor({state:'detached'});assert.equal(await page.locator('#created-key').count(),0);assert(!await page.locator('#content').innerText().then(text=>text.includes('ds_isolated_secret_only_once')));passed('创建成功仅一次展示完整密钥，复制有效，关闭后列表只有尾号');
   await open();await chooseSales();await setCondition('shop_name','A店铺');await page.locator('[name=key_mode]').selectOption('tables');assert.equal(await page.locator('[name=table_grant]:checked').count(),0);assert.equal(await page.locator('[data-scope]').count(),0);assert.equal(await submit().isDisabled(),true);await page.locator('[name=table_grant][value=product_day]').check();assert.equal(await condition().locator('[name=scope_field]').inputValue(),'');passed('切换到通用只读模式清空先前接口和范围，不携带隐藏权限');
   await scope().locator('[name=scope_mode]').selectOption('all');await name();await submit().click();await page.locator('#created-key').waitFor();assert.deepEqual(creates.at(-1).grants,{product_day:['read']});assert.deepEqual(creates.at(-1).policy,{services:{},row_scopes:{product_day:{mode:'all',groups:[]}}});await close();passed('显式全部范围的通用查询仍是只读且不会自动授权开放接口');
   await open();await chooseSales();await scope().locator('[name=scope_mode]').selectOption('all');await scope().locator('[name=scope_mode]').selectOption('filtered');assert.equal(await submit().isDisabled(),true);assert.equal(await condition().locator('[name=scope_values]').inputValue(),'');await group().locator('[data-remove-group]').click();assert.equal(await submit().isDisabled(),true);await scope().locator('[data-add-group]').click();await setCondition('shop_name','B店铺');assert.equal(await submit().isEnabled(),true);passed('全部切回限定范围必须重新填写；删除最后一组拒绝保存，可重新添加');
   await page.locator('[name=key_mode]').selectOption('legacy');assert.equal(await page.locator('#key-data-ranges').isHidden(),true);assert.equal(await submit().isDisabled(),true);assert.match(await page.locator('#key-target-selection').innerText(),/不适合给单店店长/);await page.locator('[name=grant_read][value=product_day]').check();await page.locator('[name=grant_write][value=product_day]').check();await name();await submit().click();await page.locator('#created-key').waitFor();assert.equal(creates.at(-1).policy,null);assert.deepEqual(creates.at(-1).grants,{product_day:['read','write']});await close();passed('高级完整表读写模式保留旧载荷，显式整表警告且不混入行范围');
   await page.locator('[data-revoke-key=legacy]').click();await page.locator('#confirm-revoke').click();await page.locator('#content').getByText('已撤销',{exact:true}).waitFor();assert.deepEqual(deletes,['legacy']);assert.equal(await page.locator('[data-revoke-key=legacy]').count(),0);passed('撤销需确认，成功更新状态并移除重复撤销入口');
   enabled=false;await page.reload();await open();assert.equal(await page.locator('[name=service_grant]').count(),0);assert.match(await page.locator('#key-target-selection').innerText(),/暂无已启用/);assert.equal(await submit().isDisabled(),true);await close();passed('没有已启用接口时给出配置引导，不能默认退回整表权限');
   listStatus=503;await page.reload();await page.locator('#retry-page').waitFor();assert.match(await page.locator('#content').innerText(),/暂时不可用/);assert.equal(await page.locator('#new-key').count(),0);await page.locator('#retry-page').click();await page.locator('#new-key').waitFor();passed('列表加载失败可重试，不展示不完整权限数据');
   const boundaries=await page.evaluate(async()=>{
     const {readScopes}=await import('/admin/key-scope-editor.js');
     const definition={fields:[{name:'shop_name',type:'string',dimension:true,max_length:255}]};
     const group=()=>({conditions:[{field:'shop_name',valuesText:Array.from({length:100},(_,index)=>'店'+index).join('\n')}]});
     const first={slug:'first',name:'第一张表',definition},second={slug:'second',name:'第二张表',definition};
     const scopes=new Map([['first',{mode:'filtered',groups:Array.from({length:20},group)}]]);
     const result={atLimit:readScopes(scopes,[first]).first.groups.length};
     scopes.set('second',{mode:'filtered',groups:[{conditions:[{field:'shop_name',valuesText:'额外店'}]}]});
     try{readScopes(scopes,[first,second]);}catch(error){result.overLimit=error.message;}
     scopes.delete('second');scopes.get('first').groups.push(group());
     try{readScopes(scopes,[first]);}catch(error){result.groups=error.message;}
     scopes.get('first').groups=[{conditions:Array.from({length:6},()=>({field:'shop_name',valuesText:'A'}))}];
     try{readScopes(scopes,[first]);}catch(error){result.conditions=error.message;}
     return result;
   });
   assert.equal(boundaries.atLimit,20);assert.match(boundaries.overLimit,/2000/);assert.match(boundaries.groups,/20/);assert.match(boundaries.conditions,/5/);passed('真实前端范围校验接受2000值边界，跨表超限、21组和6条件均拒绝');
   assert.deepEqual(errors,[]);passed('无脚本错误、意外外部请求或未定义API调用');
 }catch(error){failure=error;throw error;}
 finally{
   fs.mkdirSync(evidence,{recursive:true});fs.writeFileSync(path.join(evidence,'permissions-frontend.json'),JSON.stringify({passed:checks.length,failed:failure?1:0,skipped:0,checks,error:failure?String(failure):null,browser:'Microsoft Edge / Playwright',command:'node tests/admin_browser/key-permissions.cjs',exit_code:failure?1:0,scope:'真实管理页面，浏览器路由加载本地静态文件与内存API桩；未访问实际数据库、来源接口和运行服务。后端权限隔离另由真实PG/API测试验证。'},null,2));await browser.close();
 }
 console.log(JSON.stringify({passed:checks.length,failed:0}));
})().catch(error=>{console.error(error);process.exitCode=1;});


