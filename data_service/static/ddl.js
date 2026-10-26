import { $, $$, api, esc, field, showError, toast } from './core.js';

const maxSqlBytes=65536;
export function ddlImportPanel() {
  return `<details id="ddl-import" class="ddl-import"><summary>从建表语句快速导入</summary><p class="subtle">粘贴一条 CREATE TABLE，读取字段类型和中文说明。只生成配置草稿，不执行原始 SQL；手工建表仍可直接使用。</p>${field('建表语句', '<textarea id="ddl-sql" class="mono" spellcheck="false" maxlength="65536" placeholder="CREATE TABLE ..."></textarea>', '最多 64 KiB。导入后请确认业务日期、唯一键和金额字段类型。')}<div class="ddl-actions"><button type="button" class="btn" id="ddl-preview">解析并预览</button><span class="subtle" id="ddl-status" role="status" aria-live="polite"></span></div><div class="form-error" id="ddl-error" role="alert"></div><div id="ddl-preview-content" hidden></div></details>`;
}

export function bindDdlImport(node, {fieldRow, readFields, onApply}) {
  const section=$('#ddl-import',node), sql=$('#ddl-sql',section), preview=$('#ddl-preview-content',section);
  const button=$('#ddl-preview',section), error=$('#ddl-error',section), status=$('#ddl-status',section);
  const generation=node.dataset.generation;
  let revision=0;
  const isCurrent=()=>node.open && node.dataset.generation===generation && section.isConnected;
  sql.addEventListener('input',()=>{
    revision+=1;preview.hidden=true;preview.replaceChildren();error.textContent='';
    status.textContent='语句已修改，请重新解析。';
  });
  button.onclick=async()=>{
    if(button.disabled)return;
    error.textContent='';preview.hidden=true;preview.replaceChildren();
    const statement=sql.value.trim(), requestRevision=revision;
    if(!statement){error.textContent='请先粘贴建表语句。';sql.focus();return;}
    if(new TextEncoder().encode(statement).length>maxSqlBytes){error.textContent='建表语句不能超过 64 KiB，请只保留本次要导入的一张表。';return;}
    button.disabled=true;button.setAttribute('aria-busy','true');status.textContent='正在解析字段…';
    try {
      const draft=await api('/api/v1/admin/datasets/ddl-preview',{method:'POST',body:{sql:statement}});
      // 弹窗复用、关闭或原语句变化后，旧请求不能覆盖新的编辑内容。
      if(!isCurrent() || requestRevision!==revision)return;
      renderDraft(draft,preview,{fieldRow,readFields,onApply,error,status});
      preview.hidden=false;status.textContent=`已读取 ${draft.fields.length} 个字段，尚未创建数据表。`;
    }catch(exception){
      if(isCurrent() && requestRevision===revision){showError(error,exception);status.textContent='解析未完成，手工配置保持不变。';}
    }finally{
      if(isCurrent()){button.disabled=false;button.removeAttribute('aria-busy');}
    }
  };
}

function renderDraft(draft, preview, {fieldRow,readFields,onApply,error,status}) {
  const keys=new Set(draft.key_fields);
  preview.innerHTML=`<div class="ddl-preview-heading"><strong>${esc(draft.table_name)}</strong><p class="subtle">${esc(draft.table_comment || '未提供表说明')}</p></div>${draft.warnings.length?`<div class="notice warning"><strong>需要检查</strong><ul>${draft.warnings.map(warning=>`<li>${esc(warning)}</li>`).join('')}</ul></div>`:''}<div class="notice">金额或数量以文本保存时，请按数据实际内容改为小数；业务日期会转为必填 date。其他 datetime 字段入库需要带时区，需保留原文时可选 string。不会自动更改金额口径或计算公式。</div>${field('选择业务日期字段 *','<select id="ddl-date-field" aria-label="导入业务日期字段"></select>','仅可选择 date / datetime 字段；应用后自动设为必填、可分组的 date。')}<p class="subtle ddl-key-help">${keys.size?'已读取声明的主键，请核对下方“唯一键”选项。':'语句未声明主键。请在下方勾选 1—5 个唯一键字段，不会自动使用日期或商品 ID。'}</p><div class="table-wrap ddl-fields-wrap"><table><thead><tr><th>字段名称</th><th>中文说明 *</th><th>类型</th><th>可空性</th><th>分组用途</th><th>记录标识</th><th>文本长度</th><th></th></tr></thead><tbody id="ddl-fields">${draft.fields.map(definition=>fieldRow({...definition,key:keys.has(definition.name)})).join('')}</tbody></table></div><p class="row-note">可在此修改字段，也可应用后继续添加或调整。应用会替换下方当前表名、标识及全部字段；保留天数不变。</p><div class="ddl-actions"><button type="button" class="btn primary" id="ddl-apply">应用并替换当前字段</button><span class="subtle">完成检查后，还需点击底部“创建数据表”保存。</span></div>`;
  const rows=$('#ddl-fields',preview), date=$('#ddl-date-field',preview);
  const update=()=>{
    const current=date.value;
    const choices=$$('[data-row]',rows).filter(row=>['date','datetime'].includes($('[name=field_type]',row).value)).map(row=>$('[name=field_name]',row).value.trim()).filter(Boolean);
    date.innerHTML=`<option value="">请选择业务日期字段</option>${choices.map(name=>`<option value="${esc(name)}"${name===current?' selected':''}>${esc(name)}</option>`).join('')}`;
    $$('[data-row]',rows).forEach(row=>{$('[name=field_length]',row).disabled=$('[name=field_type]',row).value!=='string';});
  };
  rows.addEventListener('input',update);
  rows.addEventListener('change',event=>{
    if(event.target.name==='field_key' && event.target.checked)$('[name=field_required]',event.target.closest('[data-row]')).checked=true;
    update();
  });
  rows.addEventListener('click',event=>{
    const remove=event.target.closest('[data-remove]');
    if(remove){remove.closest('[data-row]').remove();update();}
  });
  update();
  $('#ddl-apply',preview).onclick=()=>{
    error.textContent='';
    try {
      const definitions=readFields(rows), businessDate=date.value;
      if(!businessDate || !definitions.some(definition=>definition.name===businessDate && ['date','datetime'].includes(definition.type)))throw new Error('请选择一个 date 或 datetime 字段作为业务日期。');
      const fields=definitions.map(definition=>definition.name===businessDate?{...definition,type:'date',required:true,dimension:true}:definition);
      onApply({tableName:draft.table_name,tableComment:draft.table_comment,dateField:businessDate,fields});
      status.textContent=`已应用 ${fields.length} 个字段，请检查下方配置后创建。`;
      preview.hidden=true;preview.replaceChildren();toast('字段已填入手工表单，尚未创建数据表');
    }catch(exception){showError(error,exception);error.scrollIntoView({block:'nearest'});}
  };
}
