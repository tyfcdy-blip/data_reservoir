import { $, $$, state, api, esc, icon, empty, table, field, input, select, check, val, int, checked, dialog, formFooter, busy, showError, toast, datasetOptions, day, badge, removeButton, resultView, copyText } from './core.js';

import { formulaRow, bindFormulaBuilder } from './formula-builder.js';
import { assistantMarkup, bindQueryAssistant } from './query-assistant.js';

const operations=[['sum','求和'],['avg','平均值'],['min','最小值'],['max','最大值'],['count','计数'],['count_distinct','去重计数']];
const filterOps=[['eq','等于'],['in','属于列表'],['gt','大于'],['gte','大于等于'],['lt','小于'],['lte','小于等于'],['is_null','是否为空']];
export async function servicesPage() {
  const services=await api('/api/v1/admin/query-services');
  return {services,html:`<div class="page-heading"><div><h1>加工与开放</h1><p>保存字段、筛选和汇总规则，为其他系统提供可按日期范围调用的接口。</p></div><button class="btn primary" id="new-service"${!state.datasets.length?' disabled':''}>${icon('plus')}新建开放接口</button></div><section class="panel"><div class="panel-heading"><h2>开放查询接口 <span class="table-count">${services.length} 个</span></h2><span class="subtle">明细查询 / 分组聚合 / 周期汇总</span></div>${services.length?table(['接口名称 / 标识','来源数据表','加工规则','状态','单次上限','操作'],services.map(service=>[`${esc(service.name)}<span class="secondary mono">${esc(service.slug)}</span>`,esc(state.datasets.find(dataset=>dataset.slug===service.dataset_slug)?.name || service.dataset_slug),service.definition.aggregates.length?`${service.definition.group_by.length?'按 '+esc(service.definition.group_by.join('、')):'所选周期'}汇总 · ${service.definition.aggregates.length} 个汇总${service.definition.formulas?.length?' · '+service.definition.formulas.length+' 个公式':''}`:`明细 · ${service.definition.fields?.length || '全部'}字段`,badge(service.enabled?'已开放':'未开放',service.enabled?'good':''),`${esc(service.max_limit)} 行`,`<div class="actions"><button class="btn link small" data-edit-service="${esc(service.slug)}">配置</button><button class="btn link small" data-example-service="${esc(service.slug)}">调用说明</button><button class="btn link small${service.enabled?' danger':''}" data-toggle-service="${esc(service.slug)}">${service.enabled?'停用':'开放'}</button></div>`])):empty('还没有开放查询接口',state.datasets.length?'先选数据表，再决定返回明细还是按维度汇总。':'请先建立数据表，接入数据后配置查询规则。',state.datasets.length?'<button class="btn primary" data-create-service>创建第一个接口</button>':'<a class="btn primary" href="#tables">前往创建数据表</a>')}</section>`};
}
export function bindServices(root,data,reload) {
  $('#new-service',root).onclick=()=>editService(null,reload);
  $('[data-create-service]',root)?.addEventListener('click',()=>editService(null,reload));
  $$('[data-edit-service]',root).forEach(button=>button.onclick=()=>editService(data.services.find(service=>service.slug===button.dataset.editService),reload));
  $$('[data-example-service]',root).forEach(button=>button.onclick=()=>showExample(data.services.find(service=>service.slug===button.dataset.exampleService)));
  $$('[data-toggle-service]',root).forEach(button=>button.onclick=()=>{const service=data.services.find(item=>item.slug===button.dataset.toggleService);busy(button,async()=>{try{await api(`/api/v1/admin/query-services/${encodeURIComponent(service.slug)}`,{method:'PUT',body:servicePayload(service,!service.enabled)});toast(service.enabled?'接口已停用':'接口已开放');reload();}catch(exception){toast(exception.message,true);}});});
}
function servicePayload(service,enabled) { return {name:service.name,dataset_slug:service.dataset_slug,definition:service.definition,allowed_filter_fields:service.allowed_filter_fields,max_limit:service.max_limit,enabled,revision:service.revision}; }
function editService(service,reload) {
  const definition={fields:null,group_by:[],aggregates:[],formulas:[],filters:[],result_filters:[],order_by:[],require_complete:true,...service?.definition};
  const selected=service?.dataset_slug || state.datasets[0]?.slug;
  const mode=definition.aggregates.length || definition.group_by.length?'aggregate':'detail';
  const node=dialog(service?'配置开放接口':'新建开放接口','查询时传日期范围，服务按已保存的规则返回结果。',`<form id="service-form"><div class="form-error" role="alert"></div><div class="form-grid">${field('接口名称 *',input('name',service?.name || '','required maxlength="120" placeholder="如：按店铺汇总销售额"'))}${field('接口标识 *',input('slug',service?.slug || '',`required pattern="[a-z][a-z0-9_]{0,47}" maxlength="48" class="mono" placeholder="如：shop_sales"${service?' disabled':''}`))}${field('来源数据表 *',select('dataset_slug',datasetOptions(),selected))}${field('单次返回行数上限',input('max_limit',service?.max_limit || 100,'type="number" min="1" max="500" required'))}</div>${assistantMarkup(!service)}<div class="form-section"><div class="form-section-title"><h3>数据处理方式</h3><div class="mode-switch"><label><input type="radio" name="mode" value="detail"${mode==='detail'?' checked':''}><span>明细查询</span></label><label><input type="radio" name="mode" value="aggregate"${mode==='aggregate'?' checked':''}><span>聚合汇总</span></label></div></div><div id="detail-config"><p class="subtle">选择需要返回的字段。不选择则返回全部字段。</p><div id="detail-fields" class="selection-grid" style="margin-top:10px"></div></div><div id="aggregate-config"><h3>分组维度</h3><p class="subtle" style="margin:6px 0 10px">不选择维度时，汇总整个查询周期；选择业务日期可返回每天的结果。</p><div id="group-fields" class="selection-grid"></div><div class="form-section-title" style="margin-top:18px"><div><h3>汇总指标</h3><p>先按分组汇总原始字段，再用下面的公式计算比率或差额。</p></div><button class="btn small" type="button" id="add-aggregate">${icon('plus')}添加指标</button></div><details class="quick-aggregates"><summary>批量添加求和指标</summary><p class="subtle">勾选需要的金额或数量，自动生成返回列名；添加后仍可修改聚合方式。</p><div id="quick-aggregate-fields" class="selection-grid"></div><button type="button" class="btn small" id="add-selected-aggregates">添加所选指标</button><p id="quick-aggregate-message" class="subtle" aria-live="polite"></p></details><div id="aggregate-rows"></div><div class="form-section-title" style="margin-top:18px"><div><h3>公式指标 <span class="subtle">可选</span></h3><p>选择已汇总的指标组成公式；常用比率只需选择分子、分母。也可继续手工编辑表达式。</p></div><button class="btn small" type="button" id="add-formula">${icon('plus')}添加公式</button></div><div id="formula-rows"></div><p id="formula-alias-help" class="subtle" aria-live="polite"></p><p class="subtle">支持 + - * / 和括号，只能引用数字汇总列，不能引用其他公式。空值或除以 0 返回 null。比率 0.12 表示 12%；默认保留 6 位小数，四舍五入后筛选、排序。</p><details id="result-filter-config" class="form-section"><summary>汇总后筛选 <span class="subtle">可选 · 如支付总额大于 100</span></summary><div class="form-section-title" style="margin-top:12px"><p>按汇总值或公式结果筛选，多个条件同时满足；不改变参与汇总的原始记录。</p><button class="btn small" type="button" id="add-result-filter">${icon('plus')}添加条件</button></div><div id="result-filter-rows"></div><p class="subtle">数值请填写小数或整数；列表中的小数加引号，如 ["0.1","0.2"]，避免精度损失；“是否为空”填写 true 或 false。</p></details></div><p id="query-pipeline" class="subtle query-pipeline">处理顺序：原始筛选 → 分组汇总 → 公式计算 → 汇总后筛选 → 排序 → 分页。</p></div><div class="form-section"><div class="form-section-title"><div><h3>固定筛选条件</h3><p>先筛选原始记录，再计算汇总；多个条件同时满足。</p></div><button class="btn small" type="button" id="add-filter">${icon('plus')}添加筛选</button></div><div id="filter-rows"></div><p class="subtle">“属于列表”填写 JSON 数组，如 ["淘宝","京东"]；“是否为空”填写 true 或 false。</p></div><div class="form-section"><div class="form-section-title"><h3>结果排序</h3><button class="btn small" type="button" id="add-order">${icon('plus')}添加排序</button></div><div id="order-rows"></div></div><div class="form-section"><div class="form-section-title"><div><h3>允许调用方补充的筛选字段</h3><p>调用时只能筛选勾选的原始字段，不能改变已保存的汇总、公式和排序规则。</p></div></div><div id="allowed-fields" class="selection-grid"></div></div><div class="form-section"><div class="checks">${check('require_complete','要求范围内每个日期都有已发布数据',definition.require_complete)}${check('enabled','保存后开放此接口',service?.enabled || false)}</div></div><div class="form-section"><div class="form-section-title"><div><h3>查询预览</h3><p>按当前表单配置查询，最多显示 20 行；不会保存配置。</p></div></div><div class="inline-form">${field('开始日期',input('start_date',day(-1),'type="date" required'))}${field('结束日期',input('end_date',day(-1),'type="date" required'))}<button class="btn" type="button" id="preview-service">预览结果</button></div><div class="form-error" id="preview-error" role="alert" style="margin-top:12px"></div><div class="result" id="service-result"></div></div></form>`,formFooter('service-form','保存接口配置','开放后仍需使用具有该表读取权限的调用密钥。'));
  const form=$('#service-form',node);
  const fields=()=>state.datasets.find(dataset=>dataset.slug===val(form,'dataset_slug'))?.definition.fields || [];
  const fieldLabel=column=>`${column.name} · ${column.description || column.type}`;
  const sourceOptions=()=>fields().map(column=>[column.name,fieldLabel(column)]);
  const aggregateRow=(value={})=>`<div class="form-row" data-row>${field('字段',select('aggregate_field',[['','全部记录（仅计数）'],...sourceOptions()],value.field || ''))}${field('聚合方式',select('aggregate_op',operations,value.op || 'count'))}${field('返回列名 *',input('aggregate_alias',value.alias || '','required maxlength="63" pattern="[A-Za-z][A-Za-z0-9_]{0,62}" placeholder="如 paid_total"'))}${removeButton()}</div>`;
  const filterRow=(value={})=>`<div class="form-row" data-row>${field('字段',select('filter_field',sourceOptions(),value.field || ''))}${field('条件',select('filter_op',filterOps,value.op || 'eq'))}${field('值',input('filter_value',value.value == null?'':typeof value.value==='object'?JSON.stringify(value.value):String(value.value),'required placeholder="筛选值"'))}${removeButton()}</div>`;
  const aggregateValues=()=>$$('[data-row]',$('#aggregate-rows',node)).map(row=>({field:$('[name=aggregate_field]',row).value || null,op:$('[name=aggregate_op]',row).value,alias:$('[name=aggregate_alias]',row).value.trim()}));
  const numericAliases=()=>aggregateValues().filter(metric=>['count','count_distinct'].includes(metric.op)||['integer','decimal'].includes(fields().find(column=>column.name===metric.field)?.type)).map(metric=>metric.alias).filter(Boolean);
  const numericOptions=()=>aggregateValues().filter(metric=>numericAliases().includes(metric.alias)).map(metric=>[metric.alias,`${fields().find(column=>column.name===metric.field)?.description||metric.field||'全部记录'} · ${operations.find(([name])=>name===metric.op)?.[1]} · ${metric.alias}`]);
  const refreshFormulaChoices=bindFormulaBuilder($('#formula-rows',node),numericOptions);
  const formulaAliases=()=>$$('[name=formula_alias]',$('#formula-rows',node)).map(control=>control.value.trim()).filter(Boolean);
  const selectedNames=selector=>$$('input:checked',$(selector,node)).map(control=>control.value);
  const outputColumns=()=>val(form,'mode')==='detail'?(selectedNames('#detail-fields').length?selectedNames('#detail-fields'):fields().map(column=>column.name)):[...selectedNames('#group-fields'),...aggregateValues().map(metric=>metric.alias).filter(Boolean),...formulaAliases()];
  const resultColumns=()=>[...numericAliases(),...formulaAliases()];
  const orderRow=(value={})=>`<div class="form-row" data-row>${field('返回字段',select('order_field',outputColumns(),value.field || ''))}${field('方向',select('order_direction',[['desc','从高到低 / 降序'],['asc','从低到高 / 升序']],value.direction || 'desc'))}<span></span>${removeButton()}</div>`;
  const resultFilterRow=(value={})=>`<div class="form-row" data-row>${field('汇总 / 公式列',select('result_filter_field',resultColumns(),value.field || ''))}${field('条件',select('result_filter_op',filterOps,value.op || 'gt'))}${field('值',input('result_filter_value',value.value == null?'':typeof value.value==='object'?JSON.stringify(value.value):String(value.value),'required placeholder="如 0.1"'))}${removeButton()}</div>`;
  function updateSelect(control,columns) {
    const current=control.value;
    // 删除或重命名指标后保留原选择并提示，避免悄悄改变排序或筛选含义。
    control.innerHTML=[...new Set(columns)].map(column=>`<option value="${esc(column)}"${column===current?' selected':''}>${esc(column)}</option>`).join('');
    if(!columns.includes(current))control.insertAdjacentHTML('afterbegin',`<option value="${esc(current)}" selected>${current?`已失效：${esc(current)}`:'请选择结果列'}</option>`);
  }
  function updateChoices() {
    refreshFormulaChoices();
    $$('[name=order_field]',$('#order-rows',node)).forEach(control=>updateSelect(control,outputColumns()));
    $$('[name=result_filter_field]',$('#result-filter-rows',node)).forEach(control=>updateSelect(control,resultColumns()));
    $('#formula-alias-help',node).textContent=numericAliases().length?`公式可引用的汇总列：${numericAliases().join('、')}`:'先添加数字汇总指标并填写返回列名，即可在公式中引用。';
    $('#add-formula',node).disabled=val(form,'mode')!=='aggregate'||$$('[data-row]',$('#formula-rows',node)).length>=10;
    $('#add-result-filter',node).disabled=val(form,'mode')!=='aggregate'||$$('[data-row]',$('#result-filter-rows',node)).length>=20;
  }
  const updateMode=()=>{
    const aggregate=val(form,'mode')==='aggregate';
    $('#detail-config',node).hidden=aggregate;$('#aggregate-config',node).hidden=!aggregate;$('#query-pipeline',node).hidden=!aggregate;
    $$('input,select,button',$('#aggregate-config',node)).forEach(control=>control.disabled=!aggregate);
    $$('input',$('#detail-config',node)).forEach(control=>control.disabled=aggregate);updateChoices();
  };
  const setFields=(initial=false)=>{
    const names=fields();
    $('#quick-aggregate-fields',node).innerHTML=names.filter(column=>['integer','decimal'].includes(column.type)).map(column=>check('quick_aggregate_field',fieldLabel(column),false,`value="${esc(column.name)}"`)).join('')||'<span class="subtle">此表没有数字字段，仍可手工添加计数指标。</span>';
    $('#quick-aggregate-message',node).textContent='';
    $('#detail-fields',node).innerHTML=names.map(column=>check('selected_field',fieldLabel(column),initial&&definition.fields?.includes(column.name),`value="${esc(column.name)}"`)).join('');
    $('#group-fields',node).innerHTML=names.filter(column=>column.dimension||column.type==='date').map(column=>check('group_field',fieldLabel(column),initial&&definition.group_by.includes(column.name),`value="${esc(column.name)}"`)).join('') || '<span class="subtle">此表没有可分组字段，可对整个周期汇总。</span>';
    $('#allowed-fields',node).innerHTML=names.map(column=>check('allowed_field',fieldLabel(column),initial&&service?.allowed_filter_fields.includes(column.name),`value="${esc(column.name)}"`)).join('');
    $('#aggregate-rows',node).innerHTML=initial?definition.aggregates.map(aggregateRow).join(''):'';
    $('#formula-rows',node).innerHTML=initial?definition.formulas.map(formulaRow).join(''):'';
    $('#filter-rows',node).innerHTML=initial?definition.filters.map(filterRow).join(''):'';
    $('#result-filter-rows',node).innerHTML=initial?definition.result_filters.map(resultFilterRow).join(''):'';
    $('#result-filter-config',node).open=initial&&definition.result_filters.length>0;
    $('#order-rows',node).innerHTML=initial?definition.order_by.map(orderRow).join(''):'';updateMode();
  };
  let previewRevision=0;
  let assistant;
  const invalidatePreview=()=>{previewRevision++;assistant?.invalidate();$('#service-result',node).innerHTML='';};
  form.addEventListener('input',invalidatePreview);form.addEventListener('change',invalidatePreview);
  form.elements.dataset_slug.onchange=()=>setFields(false);
  $$('[name=mode]',node).forEach(control=>control.onchange=updateMode);
  const addRow=(button,container,render,max)=>{$(button,node).onclick=()=>{if(max&&$$('[data-row]',$(container,node)).length>=max)return;$(container,node).insertAdjacentHTML('beforeend',render());updateChoices();invalidatePreview();};};
  addRow('#add-aggregate','#aggregate-rows',aggregateRow,20);
  addRow('#add-formula','#formula-rows',formulaRow,10);
  addRow('#add-filter','#filter-rows',filterRow,20);
  addRow('#add-result-filter','#result-filter-rows',resultFilterRow,20);
  addRow('#add-order','#order-rows',orderRow,20);
  for(const selector of ['#aggregate-rows','#formula-rows','#filter-rows','#result-filter-rows','#order-rows'])$(selector,node).addEventListener('click',event=>{if(event.target.closest('[data-remove]')){event.target.closest('[data-row]').remove();updateChoices();invalidatePreview();}});
  for(const selector of ['#aggregate-rows','#formula-rows']){
    $(selector,node).addEventListener('input',updateChoices);$(selector,node).addEventListener('change',updateChoices);
  }
  function uniqueAlias(fieldName,op) {
    const used=new Set([...aggregateValues().map(metric=>metric.alias),...formulaAliases(),...selectedNames('#group-fields')]);
    const stem=`${fieldName}_${op}`.slice(0,57);let alias=stem,index=2;while(used.has(alias))alias=`${stem}_${index++}`;return alias;
  }
  $('#aggregate-rows',node).addEventListener('change',event=>{
    if(!['aggregate_field','aggregate_op'].includes(event.target.name))return;
    const row=event.target.closest('[data-row]'),control=$('[name=aggregate_alias]',row),fieldName=$('[name=aggregate_field]',row).value,operation=$('[name=aggregate_op]',row);
    // 只为未命名的新指标补默认值；已有名称及引用公式始终保留给管理员确认。
    if(!control.value.trim()){
      if(event.target.name==='aggregate_field'&&['integer','decimal'].includes(fields().find(column=>column.name===fieldName)?.type))operation.value='sum';
      control.value=uniqueAlias(fieldName||'records',operation.value);updateChoices();
    }
  });
  $('#add-selected-aggregates',node).onclick=()=>{
    const chosen=$$('input:checked',$('#quick-aggregate-fields',node)).map(control=>control.value);
    const metrics=aggregateValues(),fresh=chosen.filter(name=>!metrics.some(metric=>metric.field===name&&metric.op==='sum'));
    const message=$('#quick-aggregate-message',node);
    if(!chosen.length){message.textContent='请先勾选需要汇总的字段。';return;}
    if(metrics.length+fresh.length>20){message.textContent='汇总指标最多 20 个，请减少勾选数量后添加。';return;}
    fresh.forEach(name=>$('#aggregate-rows',node).insertAdjacentHTML('beforeend',aggregateRow({field:name,op:'sum',alias:uniqueAlias(name,'sum')})));
    $$('input:checked',$('#quick-aggregate-fields',node)).forEach(control=>control.checked=false);
    message.textContent=`已添加 ${fresh.length} 个求和指标${chosen.length>fresh.length?'，已有相同字段的求和项已保留':''}。`;updateChoices();invalidatePreview();
  };
  $('#detail-fields',node).addEventListener('change',updateChoices);$('#group-fields',node).addEventListener('change',updateChoices);setFields(true);
  assistant=bindQueryAssistant(node,{dataset:()=>val(form,'dataset_slug'),getFields:fields,getRevision:()=>previewRevision,apply:draft=>{
    const next={fields:null,group_by:[],aggregates:[],formulas:[],filters:[],result_filters:[],order_by:[],require_complete:true,...draft.definition};
    Object.assign(definition,next);
    form.elements.name.value=draft.name||form.elements.name.value;
    if(!service)form.elements.slug.value=draft.slug||form.elements.slug.value;
    form.elements.max_limit.value=draft.max_limit||100;
    $(`[name=mode][value=${next.aggregates.length||next.group_by.length?'aggregate':'detail'}]`,form).checked=true;
    form.elements.require_complete.checked=next.require_complete;
    setFields(true);
    $$('[name=allowed_field]',node).forEach(control=>control.checked=(draft.allowed_filter_fields||[]).includes(control.value));
    invalidatePreview();
  }});
  function readFilters(container,prefix,result=false) {
    return $$('[data-row]',$(container,node)).map(row=>{
      const name=$(`[name=${prefix}_field]`,row).value,op=$(`[name=${prefix}_op]`,row).value,raw=$(`[name=${prefix}_value]`,row).value.trim();let value=raw;
      if(!name||(result&&!resultColumns().includes(name)))throw new Error('请选择有效的汇总后筛选列。');
      if(!raw)throw new Error('请填写筛选值。');
      if(op==='in'){value=JSON.parse(raw);if(!Array.isArray(value)||!value.length||value.length>200)throw new Error('属于列表的筛选值必须是含 1 至 200 项的 JSON 数组。');}
      else if(op==='is_null'||(!result&&fields().find(column=>column.name===name)?.type==='boolean')){if(!['true','false'].includes(raw))throw new Error('布尔筛选值请填写 true 或 false。');value=raw==='true';}
      if(result&&op!=='is_null'){
        const values=Array.isArray(value)?value:[value];
        if(values.some(entry=>!['string','number'].includes(typeof entry)||String(entry).length>100||!/^[-+]?(?:\d+(?:\.\d*)?|\.\d+)$/.test(String(entry))))throw new Error('汇总后筛选值必须是 100 字符以内的有限整数或小数；空值请使用“是否为空”。');
        if(values.some(entry=>typeof entry==='number'&&!Number.isSafeInteger(entry)))throw new Error('列表中的小数或大整数请加双引号，如 ["0.1","0.2"]，以保留精度。');
      }
      return {field:name,op,value};
    });
  }
  function readDefinition() {
    const aggregate=val(form,'mode')==='aggregate';
    const selectedFields=selectedNames('#detail-fields');
    const group=aggregate?selectedNames('#group-fields'):[];
    const aggregates=aggregate?aggregateValues():[];
    if(aggregate&&!aggregates.length)throw new Error('聚合汇总至少需要一个汇总指标。');
    if(aggregates.length>20)throw new Error('最多配置 20 个汇总指标。');
    const formulas=aggregate?$$('[data-row]',$('#formula-rows',node)).map(row=>({alias:$('[name=formula_alias]',row).value.trim(),expression:$('[name=formula_expression]',row).value.trim(),decimal_places:$('[name=formula_decimal_places]',row).value===''?NaN:Number($('[name=formula_decimal_places]',row).value)})):[];
    if(formulas.length>10)throw new Error('最多配置 10 个公式指标。');
    const aliases=[...group,...aggregates.map(metric=>metric.alias),...formulas.map(metric=>metric.alias)];
    if(aliases.some(alias=>!/^[A-Za-z][A-Za-z0-9_]{0,62}$/.test(alias)))throw new Error('返回列名须以英文字母开头，只含字母、数字、下划线，最多 63 个字符。');
    if(new Set(aliases).size!==aliases.length)throw new Error('分组字段、汇总列名和公式列名不能重复。');
    for(const formula of formulas){
      if(!formula.expression||formula.expression.length>500||!/^[A-Za-z0-9_+*/(). -]+$/.test(formula.expression))throw new Error('请填写有效公式，仅支持汇总列名、数字、+ - * / 和括号。');
      if(!Number.isInteger(formula.decimal_places)||formula.decimal_places<0||formula.decimal_places>12)throw new Error('保留小数位须为 0 至 12 的整数。');
      const references=formula.expression.match(/[A-Za-z][A-Za-z0-9_]*/g)||[];
      if(references.some(alias=>!numericAliases().includes(alias)))throw new Error('公式只能引用已有的数字汇总列，不能引用原始字段或其他公式。');
    }
    const resultFilters=aggregate?readFilters('#result-filter-rows','result_filter',true):[];
    if(resultFilters.length>20)throw new Error('最多配置 20 个汇总后筛选条件。');
    const orders=$$('[data-row]',$('#order-rows',node)).map(row=>({field:$('[name=order_field]',row).value,direction:$('[name=order_direction]',row).value}));
    if(orders.some(order=>!outputColumns().includes(order.field)))throw new Error('排序字段已失效，请重新选择或删除该排序。');
    return {fields:aggregate?null:(selectedFields.length?selectedFields:null),group_by:group,aggregates,formulas,filters:readFilters('#filter-rows','filter'),result_filters:resultFilters,order_by:orders,require_complete:checked(form,'require_complete')};
  }
  $('#preview-service',node).onclick=()=>busy($('#preview-service',node),async()=>{const error=$('#preview-error',node),revision=previewRevision;error.textContent='';$('#service-result',node).innerHTML='';try{if(!val(form,'start_date')||!val(form,'end_date'))throw new Error('请选择预览日期。');const result=await api('/api/v1/admin/query-services/preview',{method:'POST',body:{dataset_slug:val(form,'dataset_slug'),definition:readDefinition(),start_date:val(form,'start_date'),end_date:val(form,'end_date'),limit:20}});if(revision===previewRevision)$('#service-result',node).innerHTML=resultView(result);}catch(exception){if(revision===previewRevision)showError(error,exception);}});
  form.onsubmit=event=>{event.preventDefault();busy($('[form=service-form]',node),async()=>{const error=$('.form-error',form);error.textContent='';try{await api(`/api/v1/admin/query-services/${encodeURIComponent(val(form,'slug'))}`,{method:'PUT',body:{name:val(form,'name'),dataset_slug:val(form,'dataset_slug'),definition:readDefinition(),allowed_filter_fields:$$('input:checked',$('#allowed-fields',node)).map(control=>control.value),max_limit:int(form,'max_limit'),enabled:checked(form,'enabled'),revision:service?.revision || 0}});node.close();toast('开放接口配置已保存');reload();}catch(exception){showError(error,exception);}});};
}
function showExample(service) {
  const path=`/api/v1/services/${encodeURIComponent(service.slug)}/query`,url=location.origin+path;
  const body={start_date:day(-7),end_date:day(-1),filters:[],limit:Math.min(100,service.max_limit),offset:0};
  const command=`curl --request POST '${url}' \\\n  --header 'Content-Type: application/json' \\\n  --header 'X-API-Key: <你的调用密钥>' \\\n  --data '${JSON.stringify(body,null,2)}'`;
  const node=dialog('调用说明',`${service.name} · ${service.enabled?'已开放':'尚未开放，调用前请先开放'}`,`<div class="stack"><div>${badge('POST','blue')} <code>${esc(url)}</code></div><div class="notice">在“调用密钥”中新建密钥，并授予 ${esc(service.dataset_slug)} 的读取权限。读取权限允许查询该表全部字段及其开放接口。</div><div class="form-grid">${field('日期参数','<div class="subtle">start_date / end_date，格式 YYYY-MM-DD，最多 180 天。</div>')}${field('允许的动态筛选字段',`<div class="subtle">${esc(service.allowed_filter_fields.join('、') || '无，仅使用保存的固定条件')}</div>`)}</div><div class="panel-heading"><h3>请求示例</h3><button class="btn small" id="copy-call">${icon('copy')}复制</button></div><textarea class="copy-area mono" readonly aria-label="调用示例">${esc(command)}</textarea><p class="subtle">每页最多 ${esc(service.max_limit)} 行。通过 offset 翻页，返回 total 为匹配行数。金额和公式结果保留十进制字符串，空值或公式除以 0 返回 null。比率 0.12 表示 12%；公式按配置的小数位四舍五入后筛选、排序，total 为汇总后筛选通过的总组数（明细查询为记录数）。</p><a href="/docs" target="_blank" rel="noopener">查看全部接口说明 →</a></div>`);
  $('#copy-call',node).onclick=()=>copyText(command);
}
