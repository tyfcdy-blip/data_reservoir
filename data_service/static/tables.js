import { ddlImportPanel, bindDdlImport } from './ddl.js';
import { $, $$, state, api, esc, icon, empty, table, field, input, select, check, val, int, checked, dialog, formFooter, busy, showError, toast, refreshDatasets, datasetOptions, day, resultView, removeButton } from './core.js';

const types=[['string','文本 string'],['integer','整数 integer'],['decimal','金额/小数 decimal'],['date','日期 date'],['datetime','日期时间 datetime'],['boolean','布尔 boolean']];
export function tablesPage() {
  return `<div class="page-heading"><div><h1>数据表</h1><p>定义需要保存的字段、业务日期与唯一键，再接入来源数据。</p></div><button class="btn primary" id="new-table">${icon('plus')}新建数据表</button></div><section class="panel"><div class="panel-heading"><h2>全部数据表 <span class="table-count">${state.datasets.length} 张</span></h2><span class="subtle">每张表独立配置来源与开放权限</span></div>${state.datasets.length ? table(['数据表','字段数','业务日期','唯一键','保留天数','操作'],state.datasets.map(dataset=>[`${esc(dataset.name)}<span class="secondary mono">${esc(dataset.slug)}</span>`,esc(dataset.definition.fields.length),`<code>${esc(dataset.definition.date_field)}</code>`,`<code>${esc(dataset.definition.key_fields.join(' + '))}</code>`,`${esc(dataset.definition.retention_days)} 天`,`<div class="actions"><button class="btn link small" data-schema="${esc(dataset.slug)}">表结构</button><button class="btn link small" data-preview="${esc(dataset.slug)}">查看数据</button></div>`])) : empty('还没有数据表','先定义一张表，后续可把 HTTP 接口返回的字段映射进来。','<button class="btn primary" data-create-table>新建第一张表</button>')}</section>`;
}
export function bindTables(root, reload) {
  $('#new-table',root).onclick=()=>newTable(reload);
  $('[data-create-table]',root)?.addEventListener('click',()=>newTable(reload));
  $$('[data-schema]',root).forEach(button=>button.onclick=()=>showSchema(button.dataset.schema));
  $$('[data-preview]',root).forEach(button=>button.onclick=()=>previewTable(button.dataset.preview));
}
function fieldRow(value={}) {
  return `<tr data-row><td>${input('field_name',value.name || '', 'class="field-name mono" required pattern="[A-Za-z][A-Za-z0-9_]{0,62}" maxlength="63" aria-label="字段名称" placeholder="字段名称"')}</td><td>${input('field_description',value.description || '', 'class="field-description-input" required maxlength="500" aria-label="中文说明" placeholder="说明字段含义、单位或统计口径"')}</td><td>${select('field_type',types,value.type || 'string','aria-label="字段类型"')}</td><td>${check('field_required','必填',value.required)}</td><td>${check('field_dimension','维度',value.dimension)}</td><td>${check('field_key','唯一键',value.key)}</td><td>${input('field_length',value.max_length || 255,'class="field-length" type="number" min="1" max="10000" aria-label="文本最大长度"')}</td><td>${removeButton()}</td></tr>`;
}
function readFieldRows(rows) {
  const definitions=$$('[data-row]',rows).map(row=>({name:$('[name=field_name]',row).value.trim(),description:$('[name=field_description]',row).value.trim(),type:$('[name=field_type]',row).value,required:$('[name=field_required]',row).checked,dimension:$('[name=field_dimension]',row).checked,max_length:$('[name=field_type]',row).value==='string'?Number($('[name=field_length]',row).value):255,key:$('[name=field_key]',row).checked}));
  if(!definitions.length || definitions.length>80)throw new Error('请配置 1—80 个字段。');
  if(definitions.some(item=>!item.description))throw new Error('请为每个字段填写中文说明。');
  if(definitions.some(item=>item.description.length>500))throw new Error('字段中文说明不能超过 500 字。');
  if(definitions.some(item=>!/^([A-Za-z][A-Za-z0-9_]{0,62})$/.test(item.name)))throw new Error('字段名称须以字母开头，仅含字母、数字或下划线，最多 63 字。');
  if(new Set(definitions.map(item=>item.name)).size!==definitions.length)throw new Error('字段名称不能重复。');
  if(definitions.some(item=>!types.some(([type])=>type===item.type)))throw new Error('请选择受支持的字段类型。');
  if(definitions.some(item=>item.type==='string' && (!Number.isInteger(item.max_length) || item.max_length<1 || item.max_length>10000)))throw new Error('文本最大长度须为 1—10000 的整数。');
  const keys=definitions.filter(item=>item.key);
  if(!keys.length || keys.length>5)throw new Error('请选择 1—5 个唯一键字段。');
  if(keys.some(item=>!item.required))throw new Error('唯一键字段必须设为必填。');
  return definitions;
}
function newTable(reload) {
  const node=dialog('新建数据表','设置后即可存储数据；创建后表结构保持固定。',`${ddlImportPanel()}<form id="dataset-form"><div class="form-error" role="alert"></div><div class="form-grid">${field('数据表名称 *',input('name','','required maxlength="120" placeholder="如：商品日数据"'))}${field('数据表标识 *',input('slug','','required pattern="[a-z][a-z0-9_]{0,47}" maxlength="48" class="mono" placeholder="如：product_day"'),'小写字母开头，仅使用字母、数字和下划线。')}${field('业务日期字段 *','<select name="date_field" required></select>','用于日期范围查询和按日同步，必须为必填日期字段。')}${field('数据保留天数 *',input('retention_days',180,'type="number" min="1" max="180" required'))}</div><div class="form-section"><div class="form-section-title"><div><h3>字段定义</h3><p>唯一键用于同一天内识别一条记录，可选择 1—5 个必填字段。</p></div><button class="btn small" type="button" id="add-field">${icon('plus')}添加字段</button></div><div class="table-wrap"><table><thead><tr><th>字段名称</th><th>中文说明 *</th><th>类型</th><th>可空性</th><th>分组用途</th><th>记录标识</th><th>文本长度</th><th></th></tr></thead><tbody id="dataset-fields">${fieldRow({name:'dt',description:'北京时间业务自然日，用于日期范围查询和按日同步',type:'date',required:true,dimension:true})}${fieldRow({name:'id',description:'记录唯一标识，同一业务日期内不得重复',type:'string',required:true,key:true})}</tbody></table></div><p class="row-note">金额选 decimal；商品 ID 等需要保留前导零的字段选 string。</p></div></form>`,formFooter('dataset-form','创建数据表'));
  const form=$('#dataset-form',node), rows=$('#dataset-fields',node);
  const updateDates=()=>{
    const current=val(form,'date_field');
    const names=$$('[data-row]',rows).filter(row=>$('[name=field_type]',row).value==='date' && $('[name=field_required]',row).checked).map(row=>$('[name=field_name]',row).value.trim()).filter(Boolean);
    form.elements.date_field.innerHTML=names.map(name=>`<option value="${esc(name)}"${current===name?' selected':''}>${esc(name)}</option>`).join('');
    $$('[data-row]',rows).forEach(row=>{$('[name=field_length]',row).disabled=$('[name=field_type]',row).value!=='string';});
  };
  $('#add-field',node).onclick=()=>{if($$('[data-row]',rows).length>=80){toast('每张表最多 80 个字段。',true);return;}rows.insertAdjacentHTML('beforeend',fieldRow());updateDates();};
  rows.addEventListener('click',event=>{const remove=event.target.closest('[data-remove]');if(remove){remove.closest('[data-row]').remove();updateDates();}});
  rows.addEventListener('input',updateDates);rows.addEventListener('change',event=>{if(event.target.name==='field_key'&&event.target.checked)$('[name=field_required]',event.target.closest('[data-row]')).checked=true;updateDates();});updateDates();
  bindDdlImport(node,{fieldRow,readFields:readFieldRows,onApply:({tableName,tableComment,dateField,fields})=>{
    form.elements.name.value=tableComment && tableComment.length<=120?tableComment:tableName.slice(0,120);
    const slug=tableName.toLowerCase();
    form.elements.slug.value=/^[a-z][a-z0-9_]{0,47}$/.test(slug)?slug:'';
    rows.innerHTML=fields.map(fieldRow).join('');updateDates();form.elements.date_field.value=dateField;
    $('#ddl-import',node).open=false;
    if(!form.elements.slug.value)toast('来源表名不能直接作为本地标识，请填写合法的数据表标识。',true);
    form.elements.name.focus();
  }});
  form.onsubmit=event=>{event.preventDefault();busy($('[form=dataset-form]',node),async()=>{const error=$('.form-error',form);error.textContent='';try{
    const definitions=readFieldRows(rows), keys=definitions.filter(item=>item.key).map(item=>item.name);
    await api('/api/v1/admin/datasets',{method:'POST',body:{slug:val(form,'slug'),name:val(form,'name'),date_field:val(form,'date_field'),key_fields:keys,fields:definitions.map(({key,...definition})=>definition),retention_days:int(form,'retention_days')}});
    node.close();toast('数据表已创建');await refreshDatasets();reload();
  }catch(exception){showError(error,exception);}});};
}
function showSchema(slug) {
  const dataset=state.datasets.find(item=>item.slug===slug), definition=dataset.definition;
  dialog(dataset.name,`${slug} · 保留 ${definition.retention_days} 天`,`${table(['字段','中文说明','类型','必填','分组维度','唯一键','业务日期'],definition.fields.map(item=>[`<code>${esc(item.name)}</code>`,`<span class="schema-description${item.description?.trim()?'':' muted'}">${esc(item.description?.trim() || '尚未填写说明')}</span>`,esc(item.type),item.required?'是':'否',item.dimension?'是':'否',definition.key_fields.includes(item.name)?'是':'—',definition.date_field===item.name?'是':'—']))}<div class="notice" style="margin-top:16px">已有表结构保持固定。如需调整字段，请新建数据表并配置新的接入映射。</div>`);
}
export function previewTable(slug) {
  const dataset=state.datasets.find(item=>item.slug===slug);
  const node=dialog('查看数据',`${dataset.name} · ${slug}`,`<form id="table-preview" class="stack"><div class="inline-form">${field('开始日期',input('start_date',day(-1),'type="date" required'))}${field('结束日期',input('end_date',day(-1),'type="date" required'))}<button class="btn primary" type="submit">查询数据</button></div><div>${check('require_complete','要求日期完整',true)}</div><div class="form-error" role="alert"></div></form><div class="result" id="table-result"></div>`);
  const form=$('#table-preview',node);form.onsubmit=event=>{event.preventDefault();busy($('[type=submit]',form),async()=>{const error=$('.form-error',form);error.textContent='';try{const result=await api(`/api/v1/datasets/${encodeURIComponent(slug)}/query`,{method:'POST',body:{start_date:val(form,'start_date'),end_date:val(form,'end_date'),limit:50,require_complete:checked(form,'require_complete')}});$('#table-result',node).innerHTML=resultView(result);}catch(exception){$('#table-result',node).innerHTML='';showError(error,exception);}});};
}
