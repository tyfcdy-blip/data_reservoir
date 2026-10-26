// 独立浏览器验收：真实Edge UI→FastAPI TestClient→真实隔离PG；只替换传输层。
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict'),{spawn}=require('node:child_process');
const {chromium}=require(require.resolve('playwright',{paths:['C:/Users/JIE/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules']}));
const root=path.resolve(__dirname,'../..'),checks=[],errors=[],requests=[];
(async()=>{
 const proc=spawn('docker',['run','--rm','-i','--network','muyaju-data-service_default','--mount',`type=bind,source=${root},target=/app`,'--workdir','/app','-e','PYTHONPATH=/app','muyaju-data-service:key-tests','python','tests/permissions_independent/api_bridge.py'],{windowsHide:true});
 let buffer='',readyResolve,period,waiting=[];const ready=new Promise(resolve=>readyResolve=resolve);
 proc.stdout.on('data',chunk=>{buffer+=chunk.toString();let index;while((index=buffer.indexOf('\n'))>=0){const line=buffer.slice(0,index);buffer=buffer.slice(index+1);const response=JSON.parse(line);if(response.ready){period=response.period;readyResolve();}else waiting.shift().resolve(response);}});
 proc.on('error',error=>errors.push(error.message));proc.stderr.on('data',chunk=>process.stderr.write(chunk));
 const exchange=message=>new Promise((resolve,reject)=>{waiting.push({resolve,reject});proc.stdin.write(JSON.stringify(message)+'\n');});
 let browser,failure;const passed=label=>{checks.push(label);console.log('PASS '+label);};
 try{
  await Promise.race([ready,new Promise((_,reject)=>setTimeout(()=>reject(new Error('API桥启动超时')),25000))]);
  browser=await chromium.launch({headless:true,channel:'msedge',args:['--no-proxy-server']});
  const page=await browser.newPage({viewport:{width:1440,height:1000}});page.setDefaultTimeout(15000);page.on('pageerror',error=>errors.push(error.message));
  await page.route('**/*',async route=>{const request=route.request(),url=new URL(request.url());
   if(url.origin!=='http://permissions-review.test'){errors.push('非测试域网络请求');return route.abort();}
   if(url.pathname.startsWith('/api/')){
    requests.push(request.method()+' '+url.pathname);const response=await exchange({path:url.pathname,method:request.method(),headers:request.headers(),body:request.postDataJSON()});
    return route.fulfill({status:response.status,headers:response.headers,body:response.body});
   }
   const name=url.pathname==='/admin/'?'index.html':url.pathname.replace('/admin/','');assert(/^[a-z-]+\.(html|js|css)$/.test(name));
   return route.fulfill({status:200,contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html',body:fs.readFileSync(path.join(root,'data_service/static',name))});
  });
  await page.goto('http://permissions-review.test/admin/#keys');await page.locator('[name=username]').fill('permissions_independent');await page.locator('[name=password]').fill('permissions-independent-test-only-2026!');await page.locator('#login-form button').click();await page.locator('#new-key').waitFor();passed('真实管理员登录与密钥页加载');
  const createStore=async shop=>{
   await page.locator('#new-key').click();assert.equal(await page.locator('[name=key_mode]').inputValue(),'services');assert.equal(await page.locator('[form=key-form]').isDisabled(),true);
   await page.locator('#key-form [name=name]').fill(shop+'店长独立测试');await page.locator('[name=service_grant][value=sales_rows]').check();
   assert.equal(await page.locator('[name=scope_mode]').inputValue(),'filtered');assert.equal(await page.locator('[form=key-form]').isDisabled(),true);
   const group=page.locator('[data-scope=sales] [data-group="0"]');await group.locator('[name=scope_field]').selectOption('channel');await group.locator('[name=scope_values]').fill('C1');
   await group.locator('[data-add-condition]').click();const second=group.locator('[data-condition="1"]');await second.locator('[name=scope_field]').selectOption('shop_name');await second.locator('[name=scope_values]').fill(shop);
   assert.match(await page.locator('#key-authorization-summary').innerText(),/且/);assert.equal(await page.locator('[form=key-form]').isEnabled(),true);
   await page.locator('[form=key-form]').click();await page.locator('#created-key').waitFor();const secret=await page.locator('#created-key').innerText();assert.match(secret,/^ds_[A-Za-z0-9_-]{43}$/);
   await page.locator('#editor .dialog-head [data-close]').click();return secret;
  };
  const a=await createStore('A');passed('默认最小权限；界面显式选择销售接口及渠道C1+店铺A，真实创建密钥');
  const b=await createStore('B');passed('同一接口为B店长独立创建限定数据范围密钥');
  const call=async(token,endpoint,body)=>{const response=await exchange({method:'POST',path:endpoint,headers:{'X-API-Key':token},body});return {status:response.status,body:JSON.parse(response.body)};};
  const ar=await call(a,'/api/v1/services/sales_rows/query',period),br=await call(b,'/api/v1/services/sales_rows/query',period);
  assert.equal(ar.status,200);assert.deepEqual(ar.body.data.rows.map(row=>row.id),['001','002','009','010']);assert.equal(br.status,200);assert.deepEqual(br.body.data.rows.map(row=>row.id),['003','011']);
  assert.deepEqual(ar.body.data.published_versions.map(row=>row.row_count),[3,1]);passed('UI生成的A/B密钥调用真实查询引擎，各自记录和发布计数隔离');
  assert.equal((await call(a,'/api/v1/datasets/sales/query',period)).status,403);assert.equal((await call(a,'/api/v1/services/unselected/query',period)).status,403);
  const attempted=await call(a,'/api/v1/services/sales_rows/query',{...period,filters:[{field:'shop_name',op:'eq',value:'B'}]});assert.equal(attempted.status,200);assert.deepEqual(attempted.body.data.rows,[]);passed('真实拒绝通用查询和未授权接口，请求改店铺只能收窄为零行');
  await page.reload();await page.locator('#new-key').waitFor();assert.equal(await page.locator('#created-key').count(),0);assert.equal((await page.locator('body').innerText()).includes(a),false);assert.equal((await page.locator('body').innerText()).includes(b),false);
  const aRow=page.locator('tr').filter({hasText:'A店长独立测试'});await aRow.locator('summary').click();assert.match(await aRow.innerText(),/渠道.*C1.*店铺.*A/s);await page.screenshot({path:path.join(root,'evidence/permissions-independent-ui.png'),fullPage:true});passed('刷新后授权条件持久化可审阅，完整密钥不再展示');
  await aRow.locator('[data-revoke-key]').click();await page.locator('#confirm-revoke').click();await page.locator('#editor').waitFor({state:'hidden'});
  assert.equal((await call(a,'/api/v1/services/sales_rows/query',period)).status,401);assert.equal((await call(b,'/api/v1/services/sales_rows/query',period)).status,200);passed('界面撤销A密钥立即失效，B密钥不受影响');
  await page.locator('#new-key').click();await page.locator('[name=service_grant][value=sales_rows]').check();const group=page.locator('[data-scope=sales] [data-group="0"]');await group.locator('[name=scope_field]').selectOption('shop_name');await group.locator('[name=scope_values]').fill('<svg/onload=alert(1)>');assert.equal(await page.locator('#key-authorization-summary svg[onload]').count(),0);assert.match(await page.locator('#key-authorization-summary').innerText(),/<svg/);passed('授权值及摘要按文本显示，HTML值不能执行');
  await group.locator('[name=scope_values]').fill('A\nA');assert.equal(await page.locator('[form=key-form]').isDisabled(),true);await group.locator('[name=scope_values]').fill('A\n');assert.equal(await page.locator('[form=key-form]').isDisabled(),true);passed('重复值和空行阻止提交，不静默修改精确授权');
  await page.setViewportSize({width:390,height:844});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true);passed('移动视口无页面横向溢出');
  assert.deepEqual(errors,[]);assert.equal(requests.some(value=>/\/(sync|test)$/.test(value)),false);passed('无浏览器异常，无来源请求或同步任务创建');
 }catch(error){failure=error;console.error(error.stack);}
 finally{
  if(browser)await browser.close();proc.stdin.end();const exited=await Promise.race([new Promise(resolve=>proc.on('exit',code=>resolve(code))),new Promise(resolve=>setTimeout(()=>resolve('timeout'),15000))]);if(exited==='timeout')proc.kill();
  fs.writeFileSync(path.join(root,'evidence/permissions-independent-browser.json'),JSON.stringify({passed:checks.length,failed:failure?1:0,checks,error:failure?failure.message:null,bridge_exit:exited,scope:'真实Edge UI、实际FastAPI、真实隔离PostgreSQL；传输经测试桥，不访问业务库和外部源。'},null,2));
 }
 if(failure)process.exitCode=1;
})().catch(error=>{console.error(error.stack);process.exitCode=1;});
