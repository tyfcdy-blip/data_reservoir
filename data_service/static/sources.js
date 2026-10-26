import { $, $$, state, api, esc, icon, empty, table, field, input, select, check, val, int, checked, dialog, formFooter, busy, showError, toast, datasetOptions, day, badge, removeButton } from './core.js';
import { syncDialog } from './operations.js';

export async function sourcesPage() {
  const sources=await api('/api/v1/admin/sources');
  return {sources,html:`<div class="page-heading"><div><h1>接口接入</h1><p>把来源 HTTP 接口映射到数据表，按天获取、校验并保存数据。</p></div><button class="btn primary" id="new-source"${!state.datasets.length?' disabled':''}>${icon('plus')}新增接口接入</button></div><section class="panel"><div class="panel-heading"><h2>来源接口 <span class="table-count">${sources.length} 个</span></h2><span class="subtle">每张数据表对应一个来源</span></div>${sources.length?table(['来源 / 数据表','请求地址','状态','每日同步','最近修改','操作'],sources.map(source=>[`${esc(source.title || source.dataset_slug)}<span class="secondary mono">${esc(source.dataset_slug)}</span>`,`<span title="${esc(source.config.url)}">${esc(source.config.method)} ${esc(shortUrl(source.config.url))}</span><span class="secondary">${source.config.protocol==='vendor_batch'?'供应商批量接口':'通用 JSON 接口'}${source.origin==='file'?' · 来自部署配置':''}</span>`,badge(source.enabled?'已启用':'已停用',source.enabled?'good':''),source.schedule_enabled?`<span class="source-schedule-times">${sourceTimes(source).map(time=>`<span>${esc(time)}</span>`).join('')}</span><span class="secondary">回刷 ${esc(source.refresh_days)} 天</span>`:'未开启',source.updated_at?esc(new Date(source.updated_at).toLocaleString('zh-CN',{hour12:false})):'—',`<div class="actions"><button class="btn link small" data-edit-source="${esc(source.dataset_slug)}">配置</button><button class="btn link small" data-test-source="${esc(source.dataset_slug)}">测试</button><button class="btn link small" data-sync-source="${esc(source.dataset_slug)}"${!source.enabled?' disabled':''}>同步</button></div>`])):empty('还没有接入来源接口',state.datasets.length?'选择一张已建数据表，填写来源地址和返回字段映射。':'请先在“数据表”中建立要接收数据的表。',state.datasets.length?'<button class="btn primary" data-create-source>新增接口接入</button>':'<a class="btn primary" href="#tables">前往创建数据表</a>')}</section>`};
}
function shortUrl(value){try{const url=new URL(value);return `${url.host}${url.pathname}`;}catch{return value;}}
export function bindSources(root, data, reload) {
  $('#new-source',root).onclick=()=>editSource(null,reload);
  $('[data-create-source]',root)?.addEventListener('click',()=>editSource(null,reload));
  $$('[data-edit-source]',root).forEach(button=>button.onclick=()=>editSource(data.sources.find(source=>source.dataset_slug===button.dataset.editSource),reload));
  $$('[data-test-source]',root).forEach(button=>button.onclick=()=>testSource(button.dataset.testSource));
  $$('[data-sync-source]',root).forEach(button=>button.onclick=()=>syncDialog(button.dataset.syncSource));
}
// 旧接口只返回小时；再次保存时升级为完整的分钟时间列表。
function sourceTimes(source) {
  return Array.isArray(source?.schedule_times) && source.schedule_times.length
    ? [...source.schedule_times].sort()
    : [`${String(source?.schedule_hour ?? 3).padStart(2,'0')}:00`];
}
function scheduleRow(time='') {
  return `<div class="schedule-time-row" data-schedule-row><label class="field"><span data-time-label>执行时间</span>${input('schedule_time',time,'type="time" step="60" required aria-describedby="schedule-time-help schedule-time-error"')}</label><button type="button" class="btn icon-only danger" data-remove-time aria-label="删除此执行时间">${icon('close')}</button></div>`;
}
function scheduleValues(form) {
  const controls=$$('[name=schedule_time]',form), values=controls.map(control=>control.value);
  let message='';
  if(values.length<1 || values.length>24)message='请设置 1 至 24 个执行时间。';
  else if(values.some(value=>!/^([01]\d|2[0-3]):[0-5]\d$/.test(value)))message='请完整填写执行时间，格式为 HH:mm（00:00 至 23:59）。';
  else if(new Set(values).size!==values.length)message='执行时间不能重复，请修改或删除重复项。';
  controls.forEach(control=>control.setCustomValidity(message));
  $('#schedule-time-error',form).textContent=message;

  return message?null:[...values].sort();
}
function headerRow(name='',reference='',configured=false) {
  return `<div class="form-row" data-row>${field('请求头名称',input('header_name',name,'placeholder="Authorization" pattern="[A-Za-z][A-Za-z0-9-]{0,100}"'))}${field('凭据引用名',input('header_reference',reference,'placeholder="SOURCE_TOKEN" pattern="[A-Za-z_][A-Za-z0-9_]{0,119}"'))}${field(configured?'凭据值（已保存，留空保留）':'凭据值',input('header_value','','type="password" autocomplete="new-password" placeholder="按接口要求填写完整值"'))}${removeButton()}</div>`;
}
function editSource(source,reload) {
  const config=source?.config || {}, selected=source?.dataset_slug || state.datasets[0]?.slug;
  const savedNames=source?.origin==='file'?[]:(source?.secret_names || []);
  const node=dialog(source?'配置接口接入':'新增接口接入','配置保存后生效；测试和手动同步会向该来源发出请求。',`<form id="source-form"><div class="form-error" role="alert"></div><div class="form-grid">${field('来源名称 *',input('title',source?.title || '','required maxlength="120" placeholder="如：商品日表接口"'))}${field('写入数据表 *',select('dataset_slug',datasetOptions(),selected,source?'disabled':''))}${field('接口协议',select('protocol',[['http_json','通用 HTTP JSON'],['vendor_batch','供应商批量查询（签名）']],config.protocol || 'http_json'))}${field('接口 URL *',input('url',config.url || '','required type="url" maxlength="2048" placeholder="https://api.example.com/query"'),'填写完整端点，日期、分页等参数在下方设置。')}${field('请求方式',select('method',['POST','GET'],config.method || 'POST'))}${field('参数位置',select('parameter_location',[['json','JSON 请求体'],['query','URL 查询参数']],config.parameter_location || 'json'))}</div><div class="checks">${check('enabled','启用此来源',source?.enabled ?? true)}${check('approve_host','我确认服务可以访问此接口主机',false,'required')}</div><p id="source-host" class="subtle"></p><div class="form-section"><div class="form-section-title"><div><h3>日期与分页</h3><p>服务按业务日期逐天拉取，每天自动翻页直到取完。</p></div></div><div class="form-grid cols-3">${field('日期参数名',input('date_param',config.date_param || 'dt','required'))}${field('页码参数名',input('page_param',config.page_param || 'pageIndex','required'))}${field('每页条数参数名',input('page_size_param',config.page_size_param || 'pageSize','required'))}${field('起始页码',select('page_start',[['0','从 0 开始'],['1','从 1 开始']],config.page_start ?? 0))}${field('每页条数',input('page_size',config.page_size || 200,'type="number" min="1" max="1000" required'))}${field('返回记录路径',input('rows_path',config.rows_path || 'data','required placeholder="data.list"'),'例如 data.list；响应本身为数组时填 $。')}</div></div><div class="form-section" id="generic-auth"><div class="form-section-title"><div><h3>请求头与凭据</h3><p>凭据加密保存；已保存的值不会回显。可配置多个请求头。</p></div><button class="btn small" type="button" id="add-header">${icon('plus')}添加请求头</button></div><div id="header-rows">${Object.entries(config.header_env || {}).map(([header,reference])=>headerRow(header,reference,savedNames.includes(reference))).join('')}</div><p class="subtle">如果接口无需认证，可以不添加。引用名用于区分凭据，例如 SOURCE_TOKEN。</p>${field('其他固定参数（JSON 对象，可留空）',`<textarea name="static_params" class="mono" spellcheck="false" placeholder='{"channel":"all"}'>${esc(Object.keys(config.static_params || {}).length?JSON.stringify(config.static_params,null,2):'')}</textarea>`,'不要在固定参数内填写密钥或令牌。',true)}</div><div class="form-section" id="vendor-auth"><div class="form-section-title"><div><h3>供应商签名凭据</h3><p>供应商协议使用 POST JSON，每页最多 200 条；起始页码按来源接口实际规则选择 0 或 1。</p></div></div><div class="form-grid">${field('接口 ID *',input('vendor_api_id',config.vendor_api_id || ''))}${field('App Key 引用名 *',input('vendor_app_key_env',config.vendor_app_key_env || 'SOURCE_APP_KEY'))}${field(savedNames.includes(config.vendor_app_key_env)?'App Key（已保存，留空保留）':'App Key *',input('vendor_app_key','','type="password" autocomplete="new-password"'))}${field('App Secret 引用名 *',input('vendor_app_secret_env',config.vendor_app_secret_env || 'SOURCE_APP_SECRET'))}${field(savedNames.includes(config.vendor_app_secret_env)?'App Secret（已保存，留空保留）':'App Secret *',input('vendor_app_secret','','type="password" autocomplete="new-password"'))}</div></div><div class="form-section"><div class="form-section-title"><div><h3>返回字段 → 数据表字段</h3><p>新建接入默认按同名字段填充；嵌套字段可手工修改路径。日期可从带时间的字段转换。</p></div><button class="btn small" type="button" id="reset-mapping">重置为同名映射</button></div><p id="mapping-count" class="subtle mapping-count" role="status"></p><div id="mapping-rows"></div>${field('来源时区',input('source_timezone',config.source_timezone || 'Asia/Shanghai'),'日期时间转业务日期时使用。')}</div><div class="form-section"><div class="form-section-title"><div><h3>数据写入方式</h3><p>手动同步和每日同步使用同一规则；整次同步成功后，新数据才对外可见。</p></div></div>${field('同一业务日期内如何更新',select('write_mode',[['upsert','按主键更新，保留未返回的旧记录'],['replace_day','整日替换，删除未返回的旧记录']],source?(source.write_mode || 'replace_day'):'upsert'))}<p id="write-mode-help" class="notice write-mode-help"></p></div><div class="form-section" id="daily-schedule"><div class="form-section-title"><div><h3>每日同步</h3><p>每天可设置多个时间，按北京时间执行；每次回刷最近若干天。</p></div>${check('schedule_enabled','开启每日同步',source?.schedule_enabled || false)}</div><div class="form-grid"><div class="schedule-time-editor"><div id="schedule-time-rows">${sourceTimes(source).map(time=>scheduleRow(time)).join('')}</div><div class="schedule-time-toolbar"><button class="btn small" type="button" id="add-schedule-time">${icon('plus')}添加执行时间</button><span id="schedule-time-count" class="subtle"></span></div><p id="schedule-time-help" class="subtle">北京时间，至少 1 个、最多 24 个时间，支持精确到分钟；保存后按时间排序。</p><div id="schedule-time-error" class="form-error" role="alert" aria-live="polite"></div></div>${field('每次回刷天数',input('refresh_days',source?.refresh_days || 7,'type="number" min="1" max="180" required'),'回刷 1 天 = 昨天；7 天 = 从昨天往前共 7 天，不包含今天。')}</div></div><details class="form-section"><summary>高级请求设置</summary><div class="form-grid cols-3" style="margin-top:16px">${field('超时（秒）',input('timeout_seconds',config.timeout_seconds || 30,'type="number" min="1" max="120" required'))}${field('失败重试次数',input('max_retries',config.max_retries ?? 2,'type="number" min="0" max="5" required'))}${field('每秒最大请求数',input('requests_per_second',config.requests_per_second || 5,'type="number" min="0.1" max="20" step="0.1" required'))}${field('最多拉取页数',input('max_pages',config.max_pages || 10000,'type="number" min="1" max="100000" required'))}${field('单页响应上限（字节）',input('max_response_bytes',config.max_response_bytes || 2097152,'type="number" min="1024" max="16777216" required'))}${field('成功标记路径（可选）',input('success_field',config.success_field || '','placeholder="code"'))}${field('成功标记值（JSON 值）',input('success_value',config.success_value == null?'':JSON.stringify(config.success_value),'placeholder="200"'),'例如 200、true 或 "success"。')}</div></details></form>`,formFooter('source-form','保存接入配置','保存后无需重启服务；定时同步可独立开启。'));
  const form=$('#source-form',node);
  const scheduleRows=$('#schedule-time-rows',node);
  const updateSchedule=()=>{
    const rows=$$('[data-schedule-row]',scheduleRows);
    rows.forEach((row,index)=>{
      $('[data-time-label]',row).textContent=`执行时间 ${index+1}（北京时间）`;
      $('[data-remove-time]',row).disabled=rows.length===1;
      $('[data-remove-time]',row).setAttribute('aria-label',`删除执行时间 ${index+1}`);
    });
    $('#add-schedule-time',node).disabled=rows.length>=24;
    $('#schedule-time-count',node).textContent=`已设置 ${rows.length} / 24 个`;
    scheduleValues(form);
  };
  $('#add-schedule-time',node).onclick=()=>{
    if($$('[data-schedule-row]',scheduleRows).length>=24)return;
    scheduleRows.insertAdjacentHTML('beforeend',scheduleRow());updateSchedule();
    $('[name=schedule_time]',scheduleRows.lastElementChild).focus();
  };
  scheduleRows.onclick=event=>{
    const button=event.target.closest('[data-remove-time]');
    if(button && $$('[data-schedule-row]',scheduleRows).length>1){button.closest('[data-schedule-row]').remove();updateSchedule();}
  };
  scheduleRows.oninput=updateSchedule;
  scheduleRows.onchange=updateSchedule;
  const updateWriteMode=()=>{
    $('#write-mode-help',node).textContent=val(form,'write_mode')==='upsert'
      ? '相同业务日期、相同主键的记录整行覆盖（来源返回 NULL 也会覆盖旧值）；新主键新增，本次未返回的旧记录保留。失败时保留上一份已发布数据。'
      : '以本次返回的完整数据替换该业务日期的所有旧记录；本次未返回的旧记录会删除，空结果会清空该日数据。失败时保留上一份已发布数据。';
  };
  form.elements.write_mode.onchange=updateWriteMode;updateWriteMode();updateSchedule();
  if(source?.origin==='file')form.insertAdjacentHTML('afterbegin','<div class="notice warning" style="margin-bottom:16px">此来源目前由部署文件配置。保存后改由页面管理，请重新填写所有凭据值。</div>');
  const mappingRows=$('#mapping-rows',node);
  const updateMappingCount=()=>{
    const rows=$$('[data-mapping]',mappingRows), configured=rows.filter(row=>$('[name=mapping_value]',row).value.trim()).length;
    const sameName=rows.filter(row=>$('[name=mapping_value]',row).value.trim()===row.dataset.mapping).length;
    $('#mapping-count',node).textContent=`已配置 ${configured} / ${rows.length} 项，其中 ${sameName} 项为同名映射。保存后可通过“测试”验证真实返回字段。`;
  };
  mappingRows.addEventListener('input',updateMappingCount);
  $('#reset-mapping',node).onclick=()=>{
    const rows=$$('[data-mapping]',mappingRows);
    const changed=rows.some(row=>$('[name=mapping_value]',row).value.trim()!==row.dataset.mapping);
    if(changed && !window.confirm('将全部来源字段路径改为对应数据表字段的同名路径，已有自定义路径会被覆盖。日期转换设置保持不变。是否继续？'))return;
    rows.forEach(row=>$('[name=mapping_value]',row).value=row.dataset.mapping);updateMappingCount();
  };
  const updateMapping=()=>{
    const dataset=state.datasets.find(item=>item.slug===val(form,'dataset_slug'));
    $('#mapping-rows',node).innerHTML=(dataset?.definition.fields || []).map(item=>`<div class="form-row mapping" data-mapping="${esc(item.name)}"><div class="mapping-name"><strong>${esc(item.name)}</strong><span class="secondary muted"> · ${esc(item.type)}</span><span class="mapping-description muted">${esc(item.description?.trim() || '尚未填写说明')}</span></div><span class="mapping-arrow">←</span>${input('mapping_value',config.field_mapping?.[item.name] || item.name,'required aria-label="来源字段路径"')}${item.type==='date'?check('convert_date','从日期时间转换',config.date_fields_from_datetime?.includes(item.name)):item.type==='string'?check('normalize_whitespace','换行/制表符转空格',config.text_whitespace_fields?.includes(item.name)):''}</div>`).join('');
    updateMappingCount();
  };
  const updateProtocol=()=>{
    const vendor=val(form,'protocol')==='vendor_batch';$('#vendor-auth',node).hidden=!vendor;$('#generic-auth',node).hidden=vendor;
    $$('input,textarea,button', $('#vendor-auth',node)).forEach(control=>control.disabled=!vendor);
    $$('input,textarea,button', $('#generic-auth',node)).forEach(control=>control.disabled=vendor);
    if(vendor){form.elements.method.value='POST';form.elements.parameter_location.value='json';form.elements.page_size.value=Math.min(200,int(form,'page_size'));}
    form.elements.method.disabled=vendor;form.elements.parameter_location.disabled=vendor || val(form,'method')==='GET';form.elements.page_size.max=vendor?'200':'1000';
    if(val(form,'method')==='GET')form.elements.parameter_location.value='query';
  };
  form.elements.dataset_slug.onchange=updateMapping;form.elements.protocol.onchange=updateProtocol;form.elements.method.onchange=updateProtocol;
  form.elements.url.oninput=()=>{let host='';try{host=new URL(val(form,'url')).host;}catch{}$('#source-host',node).textContent=host?`批准访问的主机：${host}`:'';form.elements.approve_host.checked=false;};
  $('#add-header',node).onclick=()=>$('#header-rows',node).insertAdjacentHTML('beforeend',headerRow());
  $('#header-rows',node).onclick=event=>event.target.closest('[data-remove]')?.closest('[data-row]').remove();updateMapping();updateProtocol();form.elements.url.oninput();
  form.onsubmit=event=>{event.preventDefault();busy($('[form=source-form]',node),async()=>{const error=$('.form-error',form);error.textContent='';try{
    const scheduleTimes=scheduleValues(form);if(!scheduleTimes)throw new Error($('#schedule-time-error',form).textContent);
    const slug=val(form,'dataset_slug'), vendor=val(form,'protocol')==='vendor_batch', secrets={}, headers={}, mapping={}, converts=[], whitespaceFields=[];
    $$('[data-mapping]',form).forEach(row=>{mapping[row.dataset.mapping]=$('[name=mapping_value]',row).value.trim();if($('[name=convert_date]',row)?.checked)converts.push(row.dataset.mapping);if($('[name=normalize_whitespace]',row)?.checked)whitespaceFields.push(row.dataset.mapping);});
    if(!vendor)$$('[data-row]',$('#header-rows',node)).forEach(row=>{const name=$('[name=header_name]',row).value.trim(),reference=$('[name=header_reference]',row).value.trim(),value=$('[name=header_value]',row).value;if(!name||!reference)throw new Error('请求头名称和凭据引用名都需要填写。');if(Object.keys(headers).some(header=>header.toLowerCase()===name.toLowerCase()))throw new Error('请求头名称不能重复。');headers[name]=reference;if(value)secrets[reference]=value;else if(!savedNames.includes(reference))throw new Error(`请填写 ${name} 的凭据值。`);});
    const staticParams=vendor?{}:JSON.parse(val(form,'static_params') || '{}');if(!staticParams || typeof staticParams!=='object' || Array.isArray(staticParams))throw new Error('固定参数必须是 JSON 对象。');
    const output={dataset_slug:slug,protocol:val(form,'protocol'),url:val(form,'url'),method:val(form,'method'),parameter_location:val(form,'parameter_location'),date_param:val(form,'date_param'),page_param:val(form,'page_param'),page_size_param:val(form,'page_size_param'),page_start:int(form,'page_start'),page_size:int(form,'page_size'),rows_path:val(form,'rows_path'),static_params:staticParams,field_mapping:mapping,date_fields_from_datetime:converts,text_whitespace_fields:whitespaceFields,source_timezone:val(form,'source_timezone'),header_env:headers,success_field:val(form,'success_field') || null,success_value:val(form,'success_value')?JSON.parse(val(form,'success_value')):null,vendor_api_id:null,vendor_app_key_env:null,vendor_app_secret_env:null,timeout_seconds:int(form,'timeout_seconds'),max_retries:int(form,'max_retries'),max_pages:int(form,'max_pages'),max_response_bytes:int(form,'max_response_bytes'),requests_per_second:int(form,'requests_per_second')};
    if(vendor){output.vendor_api_id=val(form,'vendor_api_id');output.vendor_app_key_env=val(form,'vendor_app_key_env');output.vendor_app_secret_env=val(form,'vendor_app_secret_env');for(const [name,reference] of [['vendor_app_key',output.vendor_app_key_env],['vendor_app_secret',output.vendor_app_secret_env]]){const value=form.elements[name].value;if(!reference)throw new Error('请填写供应商凭据引用名。');if(value)secrets[reference]=value;else if(!savedNames.includes(reference))throw new Error('请填写供应商 App Key 和 App Secret。');}}
    await api(`/api/v1/admin/sources/${encodeURIComponent(slug)}`,{method:'PUT',body:{title:val(form,'title'),config:output,secrets,enabled:checked(form,'enabled'),approve_host:checked(form,'approve_host'),schedule_enabled:checked(form,'schedule_enabled'),schedule_times:scheduleTimes,write_mode:val(form,'write_mode'),refresh_days:int(form,'refresh_days'),revision:source?.revision || 0}});
    $$('input[type=password]',node).forEach(control=>control.value='');node.close();toast('接入配置已保存');reload();
  }catch(exception){showError(error,exception);}});};
}
function testSource(slug) {
  const node=dialog('测试来源接口',`使用已保存的 ${slug} 配置，读取一页并验证字段；不会写入业务数据。`,`<form id="test-source"><div class="inline-form">${field('业务日期',input('business_date',day(-1),'type="date" required'))}<button class="btn primary" type="submit">发起测试请求</button></div><div class="form-error" role="alert" style="margin-top:14px"></div></form><div class="result" id="source-test-result"></div>`);
  const form=$('#test-source',node);form.onsubmit=event=>{event.preventDefault();busy($('[type=submit]',form),async()=>{const error=$('.form-error',form);error.textContent='';$('#source-test-result',node).innerHTML='';try{const result=await api(`/api/v1/admin/sources/${encodeURIComponent(slug)}/test`,{method:'POST',body:{business_date:val(form,'business_date')}});$('#source-test-result',node).innerHTML=`<div class="notice">测试成功，单页读取 ${esc(result.row_count)} 行，字段映射和类型校验通过。</div><pre class="json-panel">${esc(JSON.stringify(result,null,2))}</pre>`;}catch(exception){showError(error,exception);}});};
}

