import { $, api, esc } from './core.js';

export function assistantMarkup(isNew) {
  return `<details id="query-assistant" class="query-assistant"${isNew?' open':''}><summary>用自然语言快速配置 <span class="subtle">生成草稿，确认后再保存</span></summary><label class="field"><span>描述你想查询的数据</span><textarea name="assistant_prompt" maxlength="4000" placeholder="例如：按店铺、类目、渠道、负责人分组，汇总指定日期范围的支付金额、退款金额，按支付金额从高到低返回。"></textarea></label><div class="assistant-actions"><button type="button" id="generate-draft" class="btn">生成配置草稿</button><a href="#llm" data-model-settings>配置大模型</a><span class="subtle">仅发送字段结构和这段描述，不发送业务数据行。请勿在描述中填写密钥或敏感数据。</span></div><div id="draft-error" class="form-error" role="alert"></div><div id="draft-result" aria-live="polite"></div></details>`;
}

export function bindQueryAssistant(node,{dataset,getFields,getRevision,apply}) {
  const prompt=$('[name=assistant_prompt]',node),generate=$('#generate-draft',node),result=$('#draft-result',node),error=$('#draft-error',node);
  let requestNumber=0,draft=null,draftRevision=-1;
  const stale=()=>{draft=null;result.innerHTML='';};
  $('[data-model-settings]',node).onclick=()=>node.close();
  generate.onclick=async()=>{
    if(generate.disabled)return;
    error.textContent='';stale();
    if(!prompt.value.trim()){error.textContent='请先描述希望分组、汇总或排序的数据。';prompt.focus();return;}
    const revision=getRevision(),slug=dataset(),sequence=++requestNumber,generation=node.dataset.generation;
    generate.disabled=true;generate.setAttribute('aria-busy','true');generate.textContent='正在生成草稿…';
    try{
      const response=await api('/api/v1/admin/query-services/draft',{method:'POST',body:{dataset_slug:slug,prompt:prompt.value.trim()}});
      if(!node.open||node.dataset.generation!==generation||sequence!==requestNumber)return;
      if(revision!==getRevision()||slug!==dataset()){error.textContent='生成期间配置或描述已改变，本次草稿未应用。请重新生成。';return;}
      draft=response;draftRevision=revision;
      const definition=response.definition;
      const labels=Object.fromEntries(getFields().map(column=>[column.name,`${column.description||column.name}（${column.name}）`]));
      const conditions=values=>(values||[]).map(filter=>`${labels[filter.field]||filter.field} ${{eq:'等于',in:'属于列表',gt:'大于',gte:'大于等于',lt:'小于',lte:'小于等于',is_null:'是否为空'}[filter.op]||filter.op} ${JSON.stringify(filter.value)}`).join('；')||'无';
      const summaries=definition?[
        ['分组维度',definition.group_by?.map(name=>labels[name]||name).join('、')||'整个查询日期范围'],
        ['汇总指标',(definition.aggregates||[]).map(metric=>`${labels[metric.field]||metric.field||'全部记录'} · ${{sum:'求和',avg:'平均值',min:'最小值',max:'最大值',count:'计数',count_distinct:'去重计数'}[metric.op]||metric.op} → ${metric.alias}`).join('；')||'明细查询'],
        ['公式指标',(definition.formulas||[]).map(metric=>`${metric.alias} = ${metric.expression}`).join('；')||'无'],
        ['原始筛选',conditions(definition.filters)],
        ['汇总后筛选',conditions(definition.result_filters)],
        ['排序',(definition.order_by||[]).map(order=>`${order.field} ${order.direction==='desc'?'从高到低':'从低到高'}`).join('；')||'默认顺序'],
        ['明细返回列',definition.aggregates?.length?'使用上方汇总与公式结果':definition.fields?.map(name=>labels[name]||name).join('、')||'全部字段'],
        ['调用方筛选',(response.allowed_filter_fields||[]).map(name=>labels[name]||name).join('、')||'不允许补充筛选'],
        ['查询范围',`调用时传开始、结束日期；每次最多 ${response.max_limit||100} 行；${definition.require_complete?'要求每天都有数据':'允许日期缺失'}`]
      ]:[];
      result.innerHTML=`<div class="draft-card"><strong>${response.status==='ready'?'配置草稿已生成':'还需要明确以下信息'}</strong>${response.name?`<p>${esc(response.name)}</p>`:''}${summaries.length?`<dl>${summaries.map(([label,value])=>`<div><dt>${esc(label)}</dt><dd>${esc(value)}</dd></div>`).join('')}</dl>`:''}${response.questions?.length?`<ul>${response.questions.map(question=>`<li>${esc(question)}</li>`).join('')}</ul><p class="subtle">请把补充说明写入上面的描述，再次生成。</p>`:''}${response.notes?.length?`<ul class="subtle">${response.notes.map(note=>`<li>${esc(note)}</li>`).join('')}</ul>`:''}${response.status==='ready'&&definition?'<button type="button" id="apply-draft" class="btn primary">应用到手工表单（替换当前规则）</button><p class="subtle">应用后仍可修改、预览；点击页面底部“保存接口配置”才会保存。</p>':''}</div>`;
      $('#apply-draft',node)?.addEventListener('click',()=>{
        if(draftRevision!==getRevision()){stale();error.textContent='表单已修改，请重新生成草稿后再应用。';return;}
        const ready=draft;apply(ready);stale();error.textContent='';result.innerHTML='<div class="notice">已应用到下方手工表单，请检查后预览、保存。尚未保存接口。</div>';
      });
    }catch(exception){if(node.open&&node.dataset.generation===generation&&sequence===requestNumber){error.textContent=exception.message;if(exception.code==='llm_not_configured')error.textContent+='，请前往“大模型配置”保存连接信息。';}}
    finally{if(node.open&&node.dataset.generation===generation){generate.disabled=false;generate.removeAttribute('aria-busy');generate.textContent='生成配置草稿';}}
  };
  return {invalidate:()=>{if(draft){stale();error.textContent='表单或描述已修改，请重新生成草稿。';}}};
}
