import { servicePath, $, $$, state, api, esc, icon, empty, panel, table, busy, showError, toast, setToken, refreshDatasets, badge, dateTime, number } from './core.js';
import { tablesPage, bindTables } from './tables.js';
import { sourcesPage, bindSources } from './sources.js';
import { servicesPage, bindServices } from './services.js';
import { llmPage, bindLlm } from './llm.js';
import { keysPage, bindKeys, syncPage, bindSync, jobsTable, workerBadge } from './operations.js';

const navigation=[['overview','服务概览'],['tables','数据表'],['sources','接口接入'],['services','加工与开放'],['keys','调用密钥'],['sync','同步记录'],['llm','大模型配置']];
let navigationVersion=0, refreshTimer=null, activePage='';
function login(message='') {
  clearInterval(refreshTimer);navigationVersion++;activePage='';
  const editor=$('#editor');if(editor.open)editor.close();editor.innerHTML='';
  $('#app').innerHTML=`<main class="login-screen"><section class="login-story"><div class="login-brand">慕雅居 · 数据服务</div><h1>连接数据<br>让取数更简单</h1><p>集中接入来源数据，配置汇总规则，为业务系统提供统一的查询接口。</p><div class="login-flow"><span>接口接入</span>→<span>存储与汇总</span>→<span>开放查询</span></div><div class="login-caption">DATA SERVICE / 管理控制台</div></section><section class="login-main"><form class="login-card" id="login-form"><h2>管理员登录</h2><p>登录后管理数据接入、加工和开放权限。</p><label class="field"><span>管理员账号</span><input name="username" required autocomplete="username" maxlength="64" autofocus placeholder="请输入管理员账号"></label><label class="field"><span>密码</span><input name="password" required type="password" autocomplete="current-password" placeholder="请输入密码"></label><div class="form-error" role="alert">${esc(message)}</div><button class="btn primary" type="submit">登录控制台 ${icon('arrow')}</button><footer><a href="${esc(servicePath('/docs'))}" target="_blank" rel="noopener">查看接口文档</a></footer></form></section></main>`;
  const form=$('#login-form');form.onsubmit=event=>{event.preventDefault();busy($('[type=submit]',form),async()=>{const error=$('.form-error',form);error.textContent='';try{const result=await api('/api/v1/auth/login',{method:'POST',body:{username:form.elements.username.value.trim(),password:form.elements.password.value}});form.elements.password.value='';setToken(result.token);await showShell();}catch(exception){showError(error,exception);}});};
}
async function showShell() {
  if(!state.token){login();return;}
  $('#app').innerHTML=`<div class="shell"><button class="mobile-overlay" aria-label="关闭导航"></button><aside class="sidebar"><div class="brand"><div class="brand-name">慕雅居</div><div class="brand-subtitle">数据服务控制台</div></div><div class="nav-label">工作空间</div><nav class="navigation" aria-label="管理导航">${navigation.map(([key,label])=>`<a class="nav-link" href="#${key}" data-page="${key}">${icon(key)}<span>${label}</span></a>`).join('')}</nav><div class="sidebar-foot">统一接入 · 按需汇总 · 开放查询<br>数据服务管理后台</div></aside><section class="workspace"><header class="topbar"><div class="inline"><button class="btn icon-only nav-toggle" aria-label="展开导航">${icon('menu')}</button><div class="breadcrumb"><span>工作空间</span><span>/</span><strong id="current-page">服务概览</strong></div></div><div class="account"><a href="${esc(servicePath('/docs'))}" target="_blank" rel="noopener">接口文档</a><span class="avatar">管</span><span class="account-name">管理员</span><button class="btn link small" id="logout">退出</button></div></header><main class="content" id="content" aria-live="polite"></main></section></div>`;
  $('.nav-toggle').onclick=()=>$('.shell').classList.toggle('menu-open');$('.mobile-overlay').onclick=()=>$('.shell').classList.remove('menu-open');
  $('#logout').onclick=()=>busy($('#logout'),async()=>{try{await api('/api/v1/auth/logout',{method:'POST'});setToken('');state.datasets=[];login();}catch(exception){toast(exception.message,true);}});
  await render();
}
async function overviewPage() {
  const [status,sources,services,keys]=await Promise.all([api('/api/v1/admin/status'),api('/api/v1/admin/sources'),api('/api/v1/admin/query-services'),api('/api/v1/admin/keys')]);
  state.status=status;const count=status.coverage.reduce((total,item)=>total+Number(item.row_count),0);
  const activeKeys=keys.filter(key=>!key.revoked_at && new Date(key.expires_at)>new Date()).length;
  return `<div class="page-heading"><div><h1>服务概览</h1><p>从接口接入到开放查询，在这里管理数据的完整流转。</p></div>${workerBadge(status.workers)}</div><div class="stats-grid">${[['数据表',state.datasets.length,'独立存储与权限管理'],['接入来源',sources.length,`${sources.filter(source=>source.enabled).length} 个已启用`],['开放接口',services.filter(service=>service.enabled).length,`${services.length} 个已配置`],['已保存记录',number(count),'当前已发布的数据版本']].map(([label,value,note])=>`<div class="stat"><div class="stat-title">${esc(label)}</div><div class="stat-value">${esc(value)}</div><div class="stat-note">${esc(note)}</div></div>`).join('')}</div><div class="overview-grid">${panel('配置数据服务',`<div class="steps">${[['01','定义数据表','确定保存字段、业务日期和唯一键。','tables','管理数据表'],['02','接入来源','配置请求参数、字段映射和每日同步。','sources','配置接口接入'],['03','加工并开放','配置汇总规则，给调用系统分配密钥。','services','管理开放接口']].map(([index,title,description,page,label])=>`<div class="step"><div class="step-number">${index}</div><h3>${title}</h3><p>${description}</p><a class="btn small" href="#${page}">${label}</a></div>`).join('')}</div>`)}${panel('运行状态',table(['检查项','当前状态'],[['同步服务',workerBadge(status.workers)],['最新心跳',esc(dateTime(status.workers[0]?.heartbeat_at))],['每日同步来源',`${sources.filter(source=>source.enabled&&source.schedule_enabled).length} 个`],['有效调用密钥',`${activeKeys} 个`]]),'<a href="#sync" class="subtle">查看同步记录 →</a>')}</div>${panel('最近同步',status.recent_batches.length?jobsTable(status.recent_batches.slice(0,6)):empty('还没有同步记录','依次创建数据表、配置来源接口，即可开始同步。'),'<a href="#sync" class="subtle">全部记录 →</a>')}`;
}
async function render(quiet=false) {
  if(!state.token){login();return;}
  const requested=location.hash.slice(1),page=navigation.some(([key])=>key===requested)?requested:'overview';
  const version=++navigationVersion;activePage=page;clearInterval(refreshTimer);
  const content=$('#content');if(!content)return;
  if(!quiet)content.innerHTML='<div class="loading" role="status"><span class="spinner"></span>正在加载配置…</div>';
  $('.shell').classList.remove('menu-open');$$('.nav-link').forEach(link=>{link.classList.toggle('active',link.dataset.page===page);if(link.dataset.page===page)link.setAttribute('aria-current','page');else link.removeAttribute('aria-current');});$('#current-page').textContent=navigation.find(([key])=>key===page)[1];
  try{
    await refreshDatasets();let data,html;
    if(page==='overview')html=await overviewPage();
    else if(page==='tables')html=tablesPage();
    else if(page==='sources'){data=await sourcesPage();html=data.html;}
    else if(page==='services'){data=await servicesPage();html=data.html;}
    else if(page==='keys'){data=await keysPage();html=data.html;}
    else if(page==='llm'){data=await llmPage();html=data.html;}
    else{data=await syncPage();html=data.html;}
    if(version!==navigationVersion||!state.token)return;
    content.innerHTML=html;const reload=()=>render(true);
    if(page==='tables')bindTables(content,reload);
    else if(page==='sources')bindSources(content,data,reload);
    else if(page==='services')bindServices(content,data,reload);
    else if(page==='keys')bindKeys(content,data,reload);
    else if(page==='llm')bindLlm(content,data);
    else if(page==='sync'){bindSync(content,data,reload);refreshTimer=setInterval(()=>{if(!document.hidden&&!$('#editor').open)render(true);},15000);}
  }catch(exception){if(version!==navigationVersion||!state.token)return;content.innerHTML=`<div class="panel"><div class="form-error" role="alert">${esc(exception.message)}</div><button class="btn" id="retry-page" style="margin-top:14px">重新加载</button></div>`;$('#retry-page').onclick=()=>render();}
}
window.addEventListener('hashchange',()=>{if(state.token)render();});
window.addEventListener('session-expired',()=>login('登录已失效，请重新登录。'));
window.addEventListener('beforeunload',()=>clearInterval(refreshTimer));
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&activePage==='sync'&&state.token&&!$('#editor').open)render(true);});
if(state.token)showShell();else login();
