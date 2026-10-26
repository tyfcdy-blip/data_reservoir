// D4独立浏览器真实协议验收；静态资源本地加载，API转交容器TestClient及真实隔离PG。
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict'),{spawn}=require('node:child_process');
const {chromium}=require(require.resolve('playwright',{paths:['C:/Users/JIE/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules']}));
const root=path.resolve(__dirname,'../..'),checks=[],errors=[],requests=[];
(async()=>{
 const proc=spawn('docker',['run','--rm','-i','--network','muyaju-data-service_default','--mount',`type=bind,source=${root},target=/app`,'--workdir','/app','-e','PYTHONPATH=/app','muyaju-data-service:key-tests','python','tests/ddl_independent/api_bridge.py'],{windowsHide:true});
 let buffer='',readyResolve,waiting=[];const ready=new Promise(resolve=>readyResolve=resolve);
 proc.stdout.on('data',chunk=>{buffer+=chunk.toString();let index;while((index=buffer.indexOf('\n'))>=0){const line=buffer.slice(0,index);buffer=buffer.slice(index+1);const response=JSON.parse(line);if(response.ready)readyResolve();else waiting.shift().resolve(response);}});
 proc.on('error',error=>{errors.push(error.message);});proc.stderr.on('data',()=>{});
 const exchange=message=>new Promise((resolve,reject)=>{waiting.push({resolve,reject});proc.stdin.write(JSON.stringify(message)+'\n');});
 let browser,failure;const passed=label=>{checks.push(label);console.log('PASS '+label);};
 try{
  await Promise.race([ready,new Promise((_,reject)=>setTimeout(()=>reject(new Error('API桥启动超时')),25000))]);
  browser=await chromium.launch({headless:true,channel:'msedge',args:['--no-proxy-server']});
  const page=await browser.newPage({viewport:{width:1440,height:1000}});page.setDefaultTimeout(15000);page.on('pageerror',error=>errors.push(error.message));
  await page.route('**/*',async route=>{const request=route.request(),url=new URL(request.url());
   if(url.origin!=='http://ddl-review.test'){errors.push('非测试域网络请求');return route.abort();}
   if(url.pathname.startsWith('/api/')){
    requests.push(request.method()+' '+url.pathname);const response=await exchange({path:url.pathname,method:request.method(),headers:request.headers(),body:request.postDataJSON()});
    return route.fulfill({status:response.status,headers:response.headers,body:response.body});
   }
   const name=url.pathname==='/admin/'?'index.html':url.pathname.replace('/admin/','');
   assert(/^[a-z-]+\.(html|js|css)$/.test(name));return route.fulfill({status:200,contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html',body:fs.readFileSync(path.join(root,'data_service/static',name))});
  });
  await page.goto('http://ddl-review.test/admin/#tables');await page.locator('[name=username]').fill('ddl_independent');await page.locator('[name=password]').fill('ddl-independent-local-only-2026!');await page.locator('#login-form button').click();await page.locator('#new-table').waitFor();passed('真实管理员登录与空测试库加载成功');
  await page.locator('#new-table').click();await page.locator('#ddl-import summary').click();await page.locator('#ddl-sql').fill('CREATE TABLE x (a INT); DROP TABLE datasets;');await page.locator('#ddl-preview').click();await page.locator('#ddl-preview:not([aria-busy])').waitFor();assert.match(await page.locator('#ddl-error').innerText(),/缺少end/);assert.equal(await page.locator('#dataset-fields [data-row]').count(),2);passed('真实解析器拒绝第二条SQL，手工配置保留');
  const ddl="CREATE TABLE ddl_browser (daily_id VARCHAR(80) COMMENT '稳定编号',dt DATETIME COMMENT '业务日期',owner_person VARCHAR(80) COMMENT '负责人',net_sales_amount VARCHAR(1048576) COMMENT '净销售额',pay_amount DOUBLE COMMENT '支付金额',notes TEXT COMMENT '<svg/onload=alert(1)>备注') COMMENT '独立浏览器测试表' DISTRIBUTED BY RANDOM BUCKETS 4";
  await page.locator('#ddl-sql').fill(ddl);await page.locator('#ddl-preview').click();await page.locator('#ddl-fields [data-row]').first().waitFor();assert.equal(await page.locator('#ddl-fields [data-row]').count(),6);assert.equal(await page.locator('#ddl-date-field').inputValue(),'');assert.equal(await page.locator('#ddl-fields [name=field_key]:checked').count(),0);assert.equal(await page.locator('#ddl-preview-content svg[onload]').count(),0);passed('真实DDL预览6字段保真，中文与HTML按文本显示，不猜业务日期主键');
  const row=name=>page.locator('#ddl-fields [data-row]').filter({has:page.locator(`[name=field_name][value="${name}"]`)});
  await row('daily_id').locator('[name=field_key]').check();await page.locator('#ddl-date-field').selectOption('dt');await row('net_sales_amount').locator('[name=field_type]').selectOption('decimal');await row('owner_person').locator('[name=field_dimension]').check();await page.locator('#ddl-apply').click();assert.equal(requests.filter(value=>value==='POST /api/v1/admin/datasets').length,0);passed('审核主键日期和金额类型后应用草稿，尚未写库');
  await page.locator('[form=dataset-form]').click();await page.locator('[data-schema=ddl_browser]').waitFor();await page.locator('[data-schema=ddl_browser]').click();assert.match(await page.locator('#editor').innerText(),/净销售额/);assert.match(await page.locator('#editor').innerText(),/daily_id/);await page.screenshot({path:path.join(root,'evidence/ddl-independent-schema.png'),fullPage:true});await page.locator('#editor .dialog-head [data-close]').click();passed('既有建表API将审核元数据写入真实PG，表结构中文说明可查看');
  await page.locator('a[href="#sources"]').click();await page.locator('#new-source').click();assert.equal(await page.locator('[data-mapping]').count(),6);assert.equal(await page.locator('[name=normalize_whitespace]:checked').count(),0);await page.locator('[name=title]').fill('独立合成来源');await page.locator('[name=url]').fill('https://isolated.example/rows');await page.locator('[name=enabled]').uncheck();await page.locator('[name=approve_host]').check();await page.locator('[data-mapping=dt] [name=convert_date]').check();await page.locator('[data-mapping=owner_person] [name=normalize_whitespace]').check();await page.locator('[form=source-form]').click();await page.locator('[data-edit-source=ddl_browser]').waitFor();passed('新来源同名映射默认完整，仅负责人转换显式开启，真实保存成功');
  await page.reload();await page.locator('[data-edit-source=ddl_browser]').click();assert.equal(await page.locator('[data-mapping=owner_person] [name=normalize_whitespace]').isChecked(),true);assert.equal(await page.locator('[data-mapping=daily_id] [name=normalize_whitespace]').isChecked(),false);assert.equal(await page.locator('[name=enabled]').isChecked(),false);assert.equal(await page.locator('[name=schedule_enabled]').isChecked(),false);assert.equal(await page.locator('[name=write_mode]').inputValue(),'upsert');passed('刷新后配置持久化：指定字段转换、停用来源和定时、按主键更新保留旧记录');
  assert.deepEqual(errors,[]);assert.equal(requests.some(value=>/\/(test|sync)$/.test(value)),false);passed('无浏览器异常，未测试或调用真实外部来源');
 }catch(error){failure=error;console.error(error.message);}
 finally{
  if(browser)await browser.close();proc.stdin.end();
  const exited=await Promise.race([new Promise(resolve=>proc.on('exit',code=>resolve(code))),new Promise(resolve=>setTimeout(()=>resolve('timeout'),15000))]);
  if(exited==='timeout')proc.kill();
  fs.writeFileSync(path.join(root,'evidence/ddl-independent-browser.json'),JSON.stringify({passed:checks.length,failed:failure?1:0,checks,error:failure?failure.message:null,bridge_exit:exited,scope:'真实Edge UI→实际FastAPI TestClient→真实隔离PostgreSQL；传输经测试桥，无HTTP网络、真实供应商、生产库或密钥。'},null,2));
 }
 if(failure)process.exitCode=1;
})().catch(error=>{console.error(error.message);process.exitCode=1;});


