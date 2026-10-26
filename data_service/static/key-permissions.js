import { $, $$, state, api, esc, icon, empty, table, field, input, check, select, val, int, dialog, formFooter, busy, showError, toast, badge, dateTime, copyText } from './core.js';
import { createScopeEditor, scopeDescription } from './key-scope-editor.js';

function permissionSummary(key, services) {
  const grantLabels=Object.entries(key.grants || {}).map(([slug,grants])=>esc(slug)+'：'+grants.map(grant=>grant==='read'?'读取':'写入').join(' / '));
  if(!key.policy)return '<div class="key-list-summary">'+badge('完整数据表权限','warn')+'<p>'+grantLabels.join('<br>')+'</p><span class="subtle">整表数据；读取权限包含该表所有开放接口</span></div>';
  const allowed=Object.entries(key.policy.services || {}).map(([slug])=>esc(services.find(service=>service.slug===slug)?.name || slug)+' <code>'+esc(slug)+'</code>');
  const ranges=Object.entries(key.policy.row_scopes || {}).map(([slug,scope])=>{
    const dataset=state.datasets.find(record=>record.slug===slug);
    return '<p><strong>'+esc(dataset?.name || slug)+'</strong>：'+esc(scopeDescription(scope,dataset))+'</p>';
  });
  return '<div class="key-list-summary">'+(allowed.length?'<p>指定接口：'+allowed.join('、')+'</p>':'')+(grantLabels.length?'<p>通用查询：'+grantLabels.join('、')+'</p>':'')+'<details><summary>查看数据范围</summary>'+ranges.join('')+'</details></div>';
}

export async function keysPage() {
  const [keys,services]=await Promise.all([api('/api/v1/admin/keys'),api('/api/v1/admin/query-services')]);
  return {keys,services,html:'<div class="page-heading"><div><h1>调用密钥</h1><p>为每个调用方分别授权接口和数据范围，同一个接口可以向不同店铺返回各自的数据。</p></div><button class="btn primary" id="new-key"'+(!state.datasets.length?' disabled':'')+'>'+icon('plus')+'创建调用密钥</button></div><div class="notice key-page-notice">新密钥默认只开放指定接口。数据范围由服务端强制过滤，调用方传入的筛选只能进一步缩小范围。</div><section class="panel"><div class="panel-heading"><h2>已创建密钥 <span class="table-count">'+keys.length+' 个</span></h2><span class="subtle">完整密钥只在创建时显示一次</span></div>'+
    (keys.length?table(['名称 / 尾号','授权接口与数据范围','状态','调用上限','过期时间','操作'],keys.map(key=>[
      esc(key.name)+'<span class="secondary mono">•••• '+esc(key.token_hint)+'</span>',permissionSummary(key,services),
      key.revoked_at?badge('已撤销'):new Date(key.expires_at)<=new Date()?badge('已过期','warn'):badge('有效','good'),
      esc(key.requests_per_minute)+' 次 / 分钟',esc(dateTime(key.expires_at)),
      key.revoked_at?'—':'<button class="btn link small danger" data-revoke-key="'+esc(key.id)+'">撤销</button>'])):
      empty('还没有调用密钥',state.datasets.length?'选择需要开放的接口，再设置允许查看的店铺、渠道或负责人。':'先创建数据表，再为调用方授权。',
        state.datasets.length?'<button class="btn primary" data-create-key>创建调用密钥</button>':'<a class="btn primary" href="#tables">前往创建数据表</a>'))+'</section>'};
}
export function bindKeys(root, data, reload) {
  $('#new-key',root).onclick=()=>newKey(data.services,reload);
  $('[data-create-key]',root)?.addEventListener('click',()=>newKey(data.services,reload));
  $$('[data-revoke-key]',root).forEach(button=>button.onclick=()=>{
    const key=data.keys.find(record=>record.id===button.dataset.revokeKey);
    const node=dialog('撤销调用密钥',key.name,'<p>撤销后，使用此密钥的系统将无法继续读取或写入数据。此操作不能恢复，需要时可以重新创建密钥。</p>',
      '<span></span><div class="actions"><button class="btn" data-close>取消</button><button class="btn danger" id="confirm-revoke">确认撤销</button></div>');
    $('#confirm-revoke',node).onclick=()=>busy($('#confirm-revoke',node),async()=>{
      try{await api('/api/v1/admin/keys/'+encodeURIComponent(key.id),{method:'DELETE'});node.close();toast('调用密钥已撤销');reload();}
      catch(exception){toast(exception.message,true);}
    });
  });
}
function selectionHtml(mode, services) {
  if(mode==='services')return '<p class="subtle">只允许调用勾选的接口，无法绕过接口直接查询数据表或其他接口。</p>'+
    (services.length?'<div class="key-targets">'+services.map(service=>'<label class="key-target"><input type="checkbox" name="service_grant" value="'+esc(service.slug)+'"><span><strong>'+esc(service.name)+'</strong><code>'+esc(service.slug)+'</code><small>数据表：'+esc(state.datasets.find(dataset=>dataset.slug===service.dataset_slug)?.name || service.dataset_slug)+'</small></span></label>').join('')+'</div>':
      '<div class="notice warning">暂无已启用的开放接口。请先在“加工与开放”中配置并启用接口，再返回创建密钥。</div>');
  if(mode==='tables')return '<div class="notice warning">允许通过通用接口查询所选表的全部字段，但每次查询仍受下方数据范围限制；不会自动获得开放接口权限。仅需销售额等指定结果时，请选择“指定开放接口”。</div><div class="key-targets">'+state.datasets.map(dataset=>'<label class="key-target"><input type="checkbox" name="table_grant" value="'+esc(dataset.slug)+'"><span><strong>'+esc(dataset.name)+'</strong><code>'+esc(dataset.slug)+'</code></span></label>').join('')+'</div>';
  return '<div class="notice warning">此高级模式沿用完整数据表权限：读取可访问整表全部字段及该表所有开放接口；写入可导入数据。这里不限制渠道、店铺或负责人，不适合给单店店长使用。</div>'+
    table(['数据表','读取与查询','导入写入'],state.datasets.map(dataset=>[esc(dataset.name)+'<span class="secondary mono">'+esc(dataset.slug)+'</span>',
      check('grant_read','允许读取',false,'value="'+esc(dataset.slug)+'"'),check('grant_write','允许写入',false,'value="'+esc(dataset.slug)+'"')]));
}
function newKey(allServices, reload) {
  const services=allServices.filter(service=>service.enabled);
  const node=dialog('创建调用密钥','先选调用入口，再指定允许访问的记录；每个店铺或调用系统单独创建。',
    '<form id="key-form"><div class="form-error" role="alert"></div><div class="form-grid cols-3">'+
    field('密钥名称 *',input('name','','required maxlength="120" placeholder="如：A店铺经营分析"'))+
    field('有效天数',input('expires_in_days',90,'type="number" min="1" max="365" required'))+
    field('每分钟请求上限',input('requests_per_minute',60,'type="number" min="1" max="600" required'))+
    '</div><section class="form-section"><div class="form-section-title"><div><h3>1. 可调用的接口</h3><p>按调用方需要授予最小范围，其他入口默认不开放。</p></div></div>'+
    field('授权方式',select('key_mode',[['services','指定开放接口（推荐）'],['tables','按数据表查询（只读，高级）'],['legacy','完整数据表读写（高级）']],'services'))+
    '<div id="key-target-selection"></div></section><section class="form-section" id="key-data-ranges"><div class="form-section-title"><div><h3>2. 允许访问的数据范围</h3><p>同名店铺请将“渠道”和“店铺名称”放在同一范围组；不同组合添加另一组。</p></div></div><p class="subtle key-value-help">字段只显示字符串维度；允许值精确匹配，每行一个，不要加引号。首尾空格会保留，空行和重复值不能保存。</p><div id="key-scope-editor"></div></section>'+
    '<section class="form-section key-authorization-summary" aria-live="polite"><h3>授权摘要</h3><div id="key-authorization-summary"></div></section></form>',
    formFooter('key-form','创建密钥','完整密钥仅本次展示，请及时保存。'));
  const form=$('#key-form',node),button=$('[form=key-form]',node),selection=$('#key-target-selection',form),summary=$('#key-authorization-summary',form);
  const scopes=createScopeEditor($('#key-scope-editor',form),updateSummary);
  const readMode=()=>val(form,'key_mode');
  function selectedTargets() {
    const mode=readMode(),grants=Object.create(null),serviceBindings=Object.create(null);
    if(mode==='legacy'){
      $$('input[name^=grant_]:checked',selection).forEach(control=>(grants[control.value] ||= []).push(control.name==='grant_read'?'read':'write'));
      if(!Object.keys(grants).length)throw new Error('请至少授权一张完整数据表。');
      return {grants,policy:null};
    }
    if(mode==='services'){
      $$('input[name=service_grant]:checked',selection).forEach(control=>{
        const service=services.find(record=>record.slug===control.value);
        if(service)serviceBindings[service.slug]=service.dataset_slug;
      });
      if(!Object.keys(serviceBindings).length)throw new Error('请至少选择一个已启用的开放接口。');
    }else{
      $$('input[name=table_grant]:checked',selection).forEach(control=>{grants[control.value]=['read'];});
      if(!Object.keys(grants).length)throw new Error('请至少选择一张可查询的数据表。');
    }
    if(Object.keys(grants).length>100 || Object.keys(serviceBindings).length>100)throw new Error('每个密钥最多授权 100 个接口或数据表。');
    const rowScopes=scopes.read();
    if(Object.keys(rowScopes).length>100)throw new Error('每个密钥最多授权 100 张数据表。');
    const expected=new Set([...Object.keys(grants),...Object.values(serviceBindings)]);
    if(expected.size!==Object.keys(rowScopes).length || [...expected].some(slug=>!rowScopes[slug]))throw new Error('接口关联的数据表配置已变化，请重新加载后授权。');
    return {grants,policy:{services:serviceBindings,row_scopes:rowScopes}};
  }
  function updateSummary() {
    try{
      const authorization=selectedTargets(),policy=authorization.policy;
      if(policy){
        const entry=readMode()==='services'?'只可调用：'+Object.keys(policy.services).map(slug=>services.find(service=>service.slug===slug).name+'（'+slug+'）').join('、'):'可通用查询：'+Object.keys(authorization.grants).join('、')+'（全部字段，只读）';
        summary.innerHTML='<p>'+esc(entry)+'</p>'+Object.entries(policy.row_scopes).map(([slug,scope])=>'<p><strong>'+esc(state.datasets.find(dataset=>dataset.slug===slug)?.name || slug)+'</strong>：'+esc(scopeDescription(scope,state.datasets.find(dataset=>dataset.slug===slug)))+'</p>').join('')+
          '<p class="subtle">先按以上范围过滤，再汇总、计算公式、排序和分页。调用方无法扩大数据范围。</p>';
      }else summary.innerHTML='<p class="danger-text">完整数据表授权，无行范围限制</p>'+Object.entries(authorization.grants).map(([slug,grants])=>'<p>'+esc(slug)+'：'+grants.map(grant=>grant==='read'?'整表读取及全部开放接口':'导入写入').join('、')+'</p>').join('');
      if(!node.dataset.busy)button.disabled=false;
    }catch(exception){summary.innerHTML='<p class="subtle">'+esc(exception.message)+'</p>';if(!node.dataset.busy)button.disabled=true;}
  }
  function updateTargets(reset=false) {
    const mode=readMode();
    if(reset)selection.innerHTML=selectionHtml(mode,services);
    $('#key-data-ranges',form).hidden=mode==='legacy';
    const slugs=mode==='services'?$$('input[name=service_grant]:checked',selection).map(control=>services.find(service=>service.slug===control.value)?.dataset_slug):
      mode==='tables'?$$('input[name=table_grant]:checked',selection).map(control=>control.value):[];
    scopes.setDatasets(state.datasets.filter(dataset=>slugs.includes(dataset.slug)),reset);updateSummary();
  }
  form.elements.key_mode.onchange=()=>{updateTargets(true);$('.form-error',form).textContent='';};
  selection.addEventListener('change',()=>updateTargets());
  updateTargets(true);
  form.onsubmit=event=>{
    event.preventDefault();
    if(button.disabled || node.dataset.busy)return;
    const session=state.token,generation=node.dataset.generation;
    busy(button,async()=>{
      const error=$('.form-error',form);error.textContent='';
      const controls=$$('input,select,textarea',form).map(control=>[control,control.disabled]);
      try{
        const authorization=selectedTargets();
        const payload={name:val(form,'name'),...authorization,expires_in_days:int(form,'expires_in_days'),requests_per_minute:int(form,'requests_per_minute')};
        controls.forEach(([control])=>control.disabled=true);
        const result=await api('/api/v1/admin/keys',{method:'POST',body:payload});
        if(state.token!==session || node.dataset.generation!==generation)return;
        node.close();reload();
        const secret=dialog('调用密钥已创建',result.name,'<div class="stack"><div class="notice warning">完整密钥只显示这一次。关闭后无法再次查看，请保存到调用系统的安全配置中。</div><div class="secret-value" id="created-key">'+esc(result.api_key)+'</div><button class="btn" id="copy-key">'+icon('copy')+'复制调用密钥</button><p class="subtle">调用请求头：<code>X-API-Key: &lt;调用密钥&gt;</code></p></div>');
        $('#copy-key',secret).onclick=()=>copyText(result.api_key);
      }catch(exception){if(node.dataset.generation===generation && state.token===session)showError(error,exception);}
      finally{controls.forEach(([control,disabled])=>control.disabled=disabled);}
    }).finally(()=>{if(node.dataset.generation===generation && state.token===session)updateSummary();});
  };
}

