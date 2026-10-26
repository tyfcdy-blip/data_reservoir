import { $, api, esc, field, input, select, check, val, checked, busy, showError, toast } from './core.js';

export async function llmPage() {
  const config=await api('/api/v1/admin/llm');
  return {config,html:`<div class="page-heading"><div><h1>大模型配置</h1><p>连接你选择的大模型，让自然语言描述生成可检查、可修改的查询配置。</p></div></div><section class="panel llm-panel"><form id="llm-form"><div class="form-error" role="alert"></div><div class="notice">使用兼容 OpenAI Chat Completions 的接口。模型只协助生成草稿，查询仍由数据服务按已确认规则执行。</div><div class="form-grid">${field('API 地址',input('base_url',config.base_url,'type="url" required maxlength="2048" placeholder="https://模型服务地址/v1"'),'可填写 /v1 基础地址，或完整的 /chat/completions 地址。')}${field('模型名称',input('model',config.model,'required maxlength="200" placeholder="填写服务商提供的模型名称"'))}${field('API KEY',input('api_key','','type="password" autocomplete="new-password" maxlength="8192" placeholder="留空保留已保存的 KEY"'),config.has_api_key?'已保存 KEY，不会回显；留空表示保留。':'尚未保存 KEY。')}${field('密钥处理',check('clear_api_key','明确清除已保存的 KEY'), '更换服务地址的域名时，请重新填写 KEY 或勾选清除，防止向新地址发送原密钥。')}</div><div class="checks">${check('enabled','启用自然语言配置',config.enabled)}</div><details class="form-section"><summary>高级设置</summary><div class="form-grid">${field('请求超时（秒）',input('timeout_seconds',config.timeout_seconds,'type="number" min="5" max="90" step="1" required'))}${field('模型输出长度参数',select('token_parameter',[['max_tokens','max_tokens（常用）'],['max_completion_tokens','max_completion_tokens']],config.token_parameter))}</div><p class="subtle">本机运行的模型如供 Docker 容器访问，可使用 host.docker.internal；地址应指向你信任的模型服务。</p></details><div class="assistant-actions"><button class="btn primary" id="save-llm" type="submit">保存配置</button><button class="btn" id="test-llm" type="button">测试当前配置</button><span class="subtle">测试不会保存，可能产生模型调用费用。</span></div><div id="llm-test-result" role="status"></div></form></section>`};
}

export function bindLlm(root,data) {
  const form=$('#llm-form',root),error=$('.form-error',form),test=$('#test-llm',root),save=$('#save-llm',root);let revision=data.config.revision;
  function payload(){
    if(checked(form,'clear_api_key')&&val(form,'api_key'))throw new Error('填写新 KEY 与清除 KEY 不能同时选择。');
    const timeout=Number(val(form,'timeout_seconds'));if(!Number.isInteger(timeout)||timeout<5||timeout>90)throw new Error('请求超时须为 5 至 90 秒的整数。');
    return {base_url:val(form,'base_url'),model:val(form,'model'),...(val(form,'api_key')?{api_key:val(form,'api_key')}:{}),clear_api_key:checked(form,'clear_api_key'),enabled:checked(form,'enabled'),timeout_seconds:timeout,token_parameter:val(form,'token_parameter'),revision};
  }
  async function submit(button,isTest){
    if(!form.reportValidity())return;
    await busy(button,async()=>{const controls=[...form.querySelectorAll('input,select')];controls.forEach(control=>control.disabled=true);error.textContent='';save.disabled=true;test.disabled=true;$('#llm-test-result',root).textContent='';try{
      const submitted=payload();const result=await api('/api/v1/admin/llm'+(isTest?'/test':''),{method:isTest?'POST':'PUT',body:submitted});
      if(!form.isConnected||$('#llm-form',root)!==form)return;
      if(isTest){$('#llm-test-result',root).textContent=result.message||'连接测试成功。';}
      else{revision=result.revision;form.elements.api_key.value='';form.elements.clear_api_key.checked=false;const note=form.elements.api_key.closest('.field').querySelector('small');note.textContent=result.has_api_key?'已保存 KEY，不会回显；留空表示保留。':'尚未保存 KEY。';toast('大模型配置已保存');}
    }catch(exception){if(form.isConnected&&$('#llm-form',root)===form)showError(error,exception);}finally{controls.forEach(control=>control.disabled=false);save.disabled=false;test.disabled=false;}});
  }
  form.onsubmit=event=>{event.preventDefault();submit(save,false);};test.onclick=()=>submit(test,true);
  form.addEventListener('input',()=>$('#llm-test-result',root).textContent='');
}
