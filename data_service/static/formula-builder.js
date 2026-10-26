import { $, $$, esc, field, input, select, removeButton } from './core.js';

export function formulaRow(value={}) {
  return `<div class="formula-card" data-row><div class="form-row formula-row">${field('返回列名 *',input('formula_alias',value.alias || '','required maxlength="63" pattern="[A-Za-z][A-Za-z0-9_]{0,62}" placeholder="如 refund_rate"'))}${field('计算公式 *',input('formula_expression',value.expression || '','required maxlength="500" class="mono" spellcheck="false" placeholder="选择下方汇总结果，或手工输入"'))}${field('保留小数位',input('formula_decimal_places',value.decimal_places ?? 6,'type="number" min="0" max="12" step="1" required'))}${removeButton()}</div><div class="formula-tools">${field('插入汇总结果',select('formula_insert',[['','选择已汇总的指标…']]))}<div class="formula-operators" aria-label="公式运算符">${['+','-','*','/','(',')'].map(symbol=>`<button type="button" class="btn small" data-formula-token="${symbol}" aria-label="插入 ${symbol}">${symbol==='*'?'×':symbol==='/'?'÷':symbol}</button>`).join('')}</div></div><details class="ratio-builder"${value.expression?'':' open'}><summary>快速设置比率：分子 ÷ 分母</summary><div class="inline-form">${field('分子（已汇总）',select('ratio_numerator',[['','请选择汇总指标']]))}${field('分母（已汇总）',select('ratio_denominator',[['','请选择汇总指标']]))}<button class="btn small" type="button" data-build-ratio>使用比率公式</button></div><p class="subtle">选择两项后应用，将替换本行计算公式；空值或除以 0 返回 null。</p></details></div>`;
}

export function bindFormulaBuilder(container, options) {
  const write=(row,text,replace=false)=>{
    const expression=$('[name=formula_expression]',row);
    const start=replace?0:expression.selectionStart??expression.value.length;
    const end=replace?expression.value.length:expression.selectionEnd??start;
    const next=expression.value.slice(0,start)+text+expression.value.slice(end);
    if(next.length>500){expression.setCustomValidity('公式最多 500 个字符。');expression.reportValidity();return;}
    expression.setCustomValidity('');expression.value=next;expression.focus();expression.setSelectionRange(start+text.length,start+text.length);
    expression.dispatchEvent(new Event('input',{bubbles:true}));
  };
  container.addEventListener('input',event=>{if(event.target.name==='formula_expression')event.target.setCustomValidity('');});
  container.addEventListener('mousedown',event=>{if(event.target.closest('[data-formula-token]'))event.preventDefault();});
  container.addEventListener('change',event=>{
    if(event.target.name==='formula_insert'&&event.target.value){write(event.target.closest('[data-row]'),event.target.value);event.target.value='';}
  });
  container.addEventListener('click',event=>{
    const token=event.target.closest('[data-formula-token]');if(token)write(token.closest('[data-row]'),token.dataset.formulaToken);
    const ratio=event.target.closest('[data-build-ratio]');if(!ratio)return;
    const row=ratio.closest('[data-row]'),numerator=$('[name=ratio_numerator]',row).value,denominator=$('[name=ratio_denominator]',row).value;
    if(!numerator||!denominator){$(`[name=${numerator?'ratio_denominator':'ratio_numerator'}]`,row).focus();return;}
    if(!options().some(([alias])=>alias===numerator)||!options().some(([alias])=>alias===denominator))return;
    write(row,`${numerator} / ${denominator}`,true);
  });
  return ()=>{
    const choices=options();
    $$('[name=formula_insert],[name=ratio_numerator],[name=ratio_denominator]',container).forEach(control=>{
      const previous=control.value;
      control.innerHTML='<option value="">请选择已汇总的指标…</option>'+choices.map(([alias,label])=>`<option value="${esc(alias)}">${esc(label)}</option>`).join('');
      if(previous&&choices.some(([alias])=>alias===previous))control.value=previous;
      else if(previous){control.insertAdjacentHTML('afterbegin',`<option value="${esc(previous)}">已失效：${esc(previous)}</option>`);control.value=previous;}
    });
  };
}
